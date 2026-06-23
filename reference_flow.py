#!/usr/bin/env python3
"""
Reference implementation — BetterCo integration for the "Mandanten-Neuannahme" PoC.

Maps the PoC's client-onboarding flow onto concrete BetterCo API calls. Each
numbered step below corresponds to one step in the PoC UX:

    1) Unternehmenssuche          -> search_registry()          (NorthData hit list)
    2) Stammdaten zur Validierung -> pick a hit, inspect master data (no extra call)
    3) Auswahl bestaetigen -> Akte -> create_customer_from_registry()  (customer+case+F1800)
    3b) Share-Link erzeugen        -> create_share_link()         (after matter created)
    4) Pruefung anstossen          -> create_process(F1600) + run_screening()
                                      (registry enrichment HR/Gesellschafterliste runs
                                       automatically inside step 3; PEP/Sanktion/wB here)
    5) Status-Abfrage (async)      -> poll get_screening_matches() / get_full_data()
    6) Status vollstaendig/unvoll. -> completeness_report()        (derived locally)
    7) Abholung Informationsobjekt -> get_full_data() + list/download_document()
    8) GwG-Fragen -> Prozessschritte -> submit_step() per GwG step
    9) Risikobericht abholen        -> get_full_data() risk snapshot + screening summary

Run it (creates a REAL customer in the active workspace):

    python poc_mandant_neuannahme.py "Founders1 GmbH"
    python poc_mandant_neuannahme.py "Founders1 GmbH" --no-screen
    python poc_mandant_neuannahme.py "Founders1 GmbH" --download-dir outputs/poc --cleanup

The raw HTTP request/response for every step is documented in
POC_MANDANT_NEUANNAHME_HTTP.md (so a non-Python PoC frontend can replicate it).
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path
from typing import Any

from betterco_client import BetterCoClient

log = logging.getLogger("poc")

# In-scope roles for AML screening (PEP / Sanktion / wirtschaftlich Berechtigte).
# Only Legal Rep / UBO / Acting Person are screened (see SKILL "_inscope_contacts").
SCREEN_ROLES = ("LEGAL_REP", "UBO", "ACTING_PERSON")

ONBOARDING_FLOW = "F1800_OnboardingEntity_A"     # entity onboarding
SCREENING_FLOW = "F1600_RiskAMLScreening"        # separate AML screening process


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _parse_env_file(path: str) -> dict[str, str]:
    """Minimal KEY=VALUE .env parser (avoids dotenv load-order surprises)."""
    out: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


def connect(env_file: str | None = None) -> BetterCoClient:
    """Build the client and fail fast if the environment is misconfigured.

    If env_file is given, its values are passed explicitly to the constructor so
    the chosen workspace wins regardless of what a cwd .env already loaded.
    """
    if env_file:
        e = _parse_env_file(env_file)
        client = BetterCoClient(
            base_url=e.get("BETTERCO_BASE_URL"),
            api_key=e.get("BETTERCO_API_KEY"),
            api_secret=e.get("BETTERCO_API_SECRET"),
            workspace_id=e.get("BETTERCO_WORKSPACE_ID"),
            org_id=e.get("BETTERCO_ORG_ID"),
            user_email=e.get("BETTERCO_USER_EMAIL"),
            user_password=e.get("BETTERCO_USER_PASSWORD"),
        )
    else:
        client = BetterCoClient()
    rep = client.verify_env()
    if not rep.get("ok"):
        raise SystemExit(f"Environment not OK — refusing to write: {rep}")
    log.info("Connected to workspace %s (org %s / %s)",
             rep.get("workspace"), rep.get("orgId"), rep.get("orgName"))
    return client


def _as_list(payload: Any) -> list:
    """list_* helpers sometimes wrap rows in a 'results' key — normalize."""
    if isinstance(payload, dict):
        return payload.get("results") or payload.get("content") or []
    return payload or []


def _resolve_onboarding_process(client: BetterCoClient, cid: str) -> tuple[str, str]:
    """Return (case_id, process_id) of the auto-created F1800 onboarding process."""
    cases = _as_list(client.list_cases(cid))
    if not cases:
        raise RuntimeError(f"No case found for customer {cid}")
    case_id = cases[0]["id"]
    for proc in _as_list(client.list_processes(cid, case_id)):
        name = str(proc.get("processNameCode") or proc.get("name") or proc.get("processName") or "")
        if ONBOARDING_FLOW in name:
            return case_id, proc["id"]
    # fall back to the first process on the case
    procs = _as_list(client.list_processes(cid, case_id))
    if procs:
        return case_id, procs[0]["id"]
    raise RuntimeError(f"No process found on case {case_id}")


def _doc_download_uri(doc: dict) -> str | None:
    """Documents expose their download target under one of several keys."""
    for key in ("downloadUrl", "downloadUri", "url", "uri", "href", "link"):
        if doc.get(key):
            return doc[key]
    return None


# --------------------------------------------------------------------------- #
# Step 1 — Unternehmenssuche
# --------------------------------------------------------------------------- #
def step1_search(client: BetterCoClient, query: str, domain: str = "ENTITY") -> list[dict]:
    """PoC company search -> BetterCo returns a NorthData hit list."""
    hits = client.search_registry(query, domain=domain)
    log.info("Step 1: '%s' -> %d Treffer", query, len(hits))
    for i, h in enumerate(hits[:10]):
        log.info("  [%d] %s | %s | id=%s",
                 i,
                 h.get("legalName") or h.get("name"),
                 h.get("address") or h.get("registerInfo") or "",
                 h.get("externalRegistryId"))
    return hits


# --------------------------------------------------------------------------- #
# Step 2 — Stammdaten zur Validierung
# --------------------------------------------------------------------------- #
def step2_validate(hits: list[dict], index: int = 0) -> dict:
    """PoC selects a hit; its NorthData master data is the validation payload.

    No extra API call — the data already arrived with the search result. The PoC
    shows name / Rechtsform / Registereintrag / Adresse so the user can confirm
    it is the right company before an Akte is created.
    """
    if not hits:
        raise RuntimeError("Keine Treffer zum Validieren")
    chosen = hits[index]
    log.info("Step 2: Auswahl -> %s (externalRegistryId=%s)",
             chosen.get("legalName") or chosen.get("name"),
             chosen.get("externalRegistryId"))
    return chosen


# --------------------------------------------------------------------------- #
# Step 3 — Auswahl bestaetigen -> Akte anlegen (+ 3b Share-Link)
# --------------------------------------------------------------------------- #
def step3_create_matter(client: BetterCoClient, hit: dict, *, as_lead: bool = False,
                        purchase_documents: bool = False) -> dict:
    """Confirm selection -> create customer + case + F1800 onboarding process.

    create_customer_from_registry() polls until isFullyInitialized, i.e. NorthData
    + company.info enrichment (HR-/Gesellschafterliste-PDFs, Kontakte/Verflechtungen)
    has already run when this returns. We then resolve the case/process ids and
    create a client share link (step 3b).

    purchase_documents=True additionally ORDERS company.info documents (billed) —
    off by default; the auto-enrichment already attaches HR + Gesellschafterliste.
    """
    name = hit.get("legalName") or hit.get("name")
    result = client.create_customer_from_registry(
        external_registry_id=hit["externalRegistryId"],
        name=name,
        as_lead=as_lead,
        purchase_documents=purchase_documents,
    )
    cid = result["businessRelationId"]
    case_id, pid = _resolve_onboarding_process(client, cid)
    log.info("Step 3: Akte angelegt — customer=%s case=%s process=%s (%.1fs, %d Kontakte, %d Dok.)",
             cid, case_id, pid, result.get("elapsed_s", 0),
             len(result.get("contacts") or []), len(result.get("documents") or []))

    # --- 3b) Share-Link nach Aktenanlage ---------------------------------- #
    share_url = client.create_share_link(cid, case_id, pid)
    log.info("Step 3b: Share-Link erstellt -> %s", share_url)

    return {
        "businessRelationId": cid,
        "case_id": case_id,
        "process_id": pid,
        "share_url": share_url,
        "create_result": result,
    }


# --------------------------------------------------------------------------- #
# Step 4 — Pruefung anstossen (Anreicherungsbestellungen)
# --------------------------------------------------------------------------- #
def step4_trigger_checks(client: BetterCoClient, cid: str, case_id: str,
                         *, roles=SCREEN_ROLES, screen: bool = True) -> dict:
    """Trigger the compliance checks.

    Registry enrichment (Handelsregister, Gesellschafterliste, Kontakte/
    Verflechtungen, wirtschaftliche Berechtigte) already ran during step 3.
    What we trigger here is the AML/PEP/Sanktion screening, which lives in a
    SEPARATE process (F1600_RiskAMLScreening) — create it, then run_screening().
    """
    screening_pid = client.create_process(cid, case_id, SCREENING_FLOW)["id"]
    log.info("Step 4: Screening-Prozess %s angelegt", screening_pid)
    if screen:
        client.run_screening(cid, screening_pid, roles=roles)
        log.info("Step 4: Screening (PEP/Sanktion/wB) ausgeloest fuer Rollen %s", roles)
    return {"screening_process_id": screening_pid, "screened": screen}


# --------------------------------------------------------------------------- #
# Step 5 — Status-Abfrage (asynchron)
# --------------------------------------------------------------------------- #
def step5_poll_status(client: BetterCoClient, cid: str, *,
                      timeout: float = 90, interval: float = 4) -> dict:
    """Poll until screening results are available (or timeout).

    Step 3 already polled isFullyInitialized for the master-data enrichment; the
    asynchronous part left to wait on is the provider screening search. We poll
    the entity match payload until it carries searchResults.
    """
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = client.get_screening_matches(cid) or {}
        if last.get("searchResults", {}).get("data") is not None or last.get("id"):
            log.info("Step 5: Screening-Ergebnis verfuegbar nach Polling")
            return {"ready": True, "entity_matches": last}
        time.sleep(interval)
    log.warning("Step 5: Timeout (%.0fs) — Screening noch nicht abgeschlossen", timeout)
    return {"ready": False, "entity_matches": last}


# --------------------------------------------------------------------------- #
# Step 6 — Status der automatischen Abfrage (vollstaendig / unvollstaendig)
# --------------------------------------------------------------------------- #
def step6_completeness(client: BetterCoClient, cid: str, *,
                       roles=SCREEN_ROLES) -> dict:
    """Derive a vollstaendig/unvollstaendig status from full-data + screening.

    There is no single 'completeness' endpoint — we assemble it: master data
    present, documents present, and a screening verdict for every in-scope actor.
    """
    fd = client.get_full_data(cid)
    docs = _as_list(client.list_customer_documents(cid))
    # auto_screen_customer(dry_run, screen=False) resolves the already-run results
    # into auto_cleared / needs_review / skipped without committing anything.
    case_id, _ = _resolve_onboarding_process(client, cid)
    screening_pid = _find_screening_process(client, cid, case_id)
    review = client.auto_screen_customer(
        cid, screening_pid, roles=roles, screen=False, dry_run=True
    ) if screening_pid else {}

    report = {
        "has_master_data": bool(fd.get("entityLegalInfo")),
        "contact_count": len(_as_list(fd.get("contacts", {}).get("contacts")
                                      if isinstance(fd.get("contacts"), dict) else fd.get("contacts"))),
        "document_count": len(docs),
        "screening_resolved": len(review.get("auto_cleared", [])) + len(review.get("needs_review", [])),
        "screening_open": len(review.get("needs_review", [])),
        "screening_skipped": len(review.get("skipped", [])),
    }
    report["complete"] = (
        report["has_master_data"]
        and report["document_count"] > 0
        and report["screening_skipped"] == 0
    )
    log.info("Step 6: Status = %s | %s",
             "VOLLSTAENDIG" if report["complete"] else "UNVOLLSTAENDIG", report)
    return report


def _find_screening_process(client: BetterCoClient, cid: str, case_id: str) -> str | None:
    for proc in _as_list(client.list_processes(cid, case_id)):
        name = str(proc.get("processNameCode") or proc.get("name") or proc.get("processName") or "")
        if SCREENING_FLOW in name:
            return proc["id"]
    return None


# --------------------------------------------------------------------------- #
# Step 7 — Abholung des Informationsobjekts
# --------------------------------------------------------------------------- #
def step7_fetch_information_object(client: BetterCoClient, cid: str, *,
                                   download_dir: str | None = None) -> dict:
    """Retrieve the complete information object: master data + contacts + documents."""
    fd = client.get_full_data(cid)
    docs = _as_list(client.list_customer_documents(cid))
    log.info("Step 7: Informationsobjekt — %d Kontakte, %d Dokumente",
             len(_as_list(fd.get("contacts"))) if not isinstance(fd.get("contacts"), dict)
             else len(fd.get("contacts", {}).get("contacts", [])),
             len(docs))

    downloaded = []
    if download_dir:
        out = Path(download_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "full_data.json").write_text(json.dumps(fd, indent=2, ensure_ascii=False),
                                            encoding="utf-8")
        for doc in docs:
            uri = _doc_download_uri(doc)
            if not uri:
                continue
            fname = (doc.get("name") or doc.get("displayName") or doc.get("id") or "document")
            if not str(fname).lower().endswith(".pdf"):
                fname = f"{fname}.pdf"
            dest = out / fname
            try:
                client.download_document(uri, str(dest))
                downloaded.append(str(dest))
            except Exception as exc:  # noqa: BLE001 — best-effort download
                log.warning("  Download fehlgeschlagen (%s): %s", fname, exc)
        log.info("Step 7: %d Dateien nach %s geschrieben", len(downloaded), download_dir)

    return {"full_data": fd, "documents": docs, "downloaded": downloaded}


# --------------------------------------------------------------------------- #
# Step 8 — GwG-Fragen beantworten -> Prozessschritte einarbeiten
# --------------------------------------------------------------------------- #
def step8_answer_gwg(client: BetterCoClient, cid: str, process_id: str,
                     gwg_answers: dict[str, dict]) -> list[dict]:
    """Write GwG questionnaire answers into the flow's process steps.

    gwg_answers maps a flow stepId (a valid taskSpec, e.g. "P18xx_...Step") to a
    dict of field values. Step ids and field names are FLOW-SPECIFIC — discover
    them via list_tasks() (each task carries its taskSpec) for the F1800 flow.

    Values go into the right container depending on the field (see SKILL submit_step
    table): process-domain answers -> additionalProcessData.<topic>.<field>;
    actor answers -> additionalActorData.<field>; master data -> entityLegalInfo.
    Always read back — submit_step returns 200 even when it drops unknown fields.
    """
    acks = []
    for step_id, values in gwg_answers.items():
        ack = client.submit_step(cid, process_id, step_id, values)
        log.info("Step 8: GwG-Antworten in %s eingearbeitet -> %s", step_id, ack)
        acks.append({"step_id": step_id, "ack": ack})
    return acks


# --------------------------------------------------------------------------- #
# Step 9 — Risikobericht abholen
# --------------------------------------------------------------------------- #
def step9_risk_report(client: BetterCoClient, cid: str, *,
                      roles=SCREEN_ROLES) -> dict:
    """Assemble the Risikobericht from the risk snapshot + screening verdicts."""
    fd = client.get_full_data(cid)
    aml = fd.get("amlProfile") or {}
    snapshot = fd.get("actorRiskSnapshot") or {}

    case_id, _ = _resolve_onboarding_process(client, cid)
    screening_pid = _find_screening_process(client, cid, case_id)
    screening = client.auto_screen_customer(
        cid, screening_pid, roles=roles, screen=False, dry_run=True
    ) if screening_pid else {}

    report = {
        "businessRelationId": cid,
        "risk_level": (aml.get("screeningProfile") or {}).get("riskLevel")
                      or (aml.get("riskProfile") or {}).get("taxIndustryRisk"),
        "risk_snapshot": snapshot,
        "screening": {
            "auto_cleared": screening.get("auto_cleared", []),
            "needs_review": screening.get("needs_review", []),
            "skipped": screening.get("skipped", []),
        },
    }
    log.info("Step 9: Risikobericht — Risk=%s | %d cleared, %d review, %d skipped",
             report["risk_level"],
             len(report["screening"]["auto_cleared"]),
             len(report["screening"]["needs_review"]),
             len(report["screening"]["skipped"]))
    return report


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #
def run_flow(query: str, *, index: int = 0, screen: bool = True,
             download_dir: str | None = None, gwg_answers: dict | None = None,
             cleanup: bool = False, purchase_documents: bool = False,
             env_file: str | None = None) -> dict:
    """Run the full 9-step Mandanten-Neuannahme flow end-to-end."""
    client = connect(env_file)

    hits = step1_search(client, query)
    hit = step2_validate(hits, index)
    matter = step3_create_matter(client, hit, purchase_documents=purchase_documents)
    cid, case_id, pid = matter["businessRelationId"], matter["case_id"], matter["process_id"]

    try:
        checks = step4_trigger_checks(client, cid, case_id, screen=screen)
        status = step5_poll_status(client, cid) if screen else {"ready": False}
        completeness = step6_completeness(client, cid)
        info = step7_fetch_information_object(client, cid, download_dir=download_dir)
        gwg = step8_answer_gwg(client, cid, pid, gwg_answers) if gwg_answers else []
        risk = step9_risk_report(client, cid)

        return {
            "query": query,
            "businessRelationId": cid,
            "case_id": case_id,
            "process_id": pid,
            "screening_process_id": checks.get("screening_process_id"),
            "share_url": matter["share_url"],
            "status": status,
            "completeness": completeness,
            "documents": [d.get("name") or d.get("displayName") for d in info["documents"]],
            "downloaded": info["downloaded"],
            "gwg": gwg,
            "risk_report": risk,
        }
    finally:
        if cleanup:
            log.info("Cleanup: loesche Test-Kunde %s", cid)
            try:
                client.delete_customer(cid)
            except Exception as exc:  # noqa: BLE001
                log.warning("Cleanup fehlgeschlagen: %s", exc)


def main() -> None:
    ap = argparse.ArgumentParser(description="BetterCo Mandanten-Neuannahme reference flow")
    ap.add_argument("query", help="Firmenname fuer die Unternehmenssuche")
    ap.add_argument("--index", type=int, default=0, help="Index des zu waehlenden Treffers")
    ap.add_argument("--no-screen", action="store_true", help="AML-Screening ueberspringen")
    ap.add_argument("--download-dir", help="Dokumente + full_data.json hierhin speichern")
    ap.add_argument("--cleanup", action="store_true", help="Test-Kunde am Ende loeschen")
    ap.add_argument("--purchase-documents", action="store_true",
                    help="company.info-Dokumente kostenpflichtig bestellen (Standard: aus)")
    ap.add_argument("--env-file",
                    help="Workspace-.env (z.B. workspaces/editor-betterco-claude.env), "
                         "wird mit override geladen")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = run_flow(
        args.query,
        index=args.index,
        screen=not args.no_screen,
        download_dir=args.download_dir,
        cleanup=args.cleanup,
        purchase_documents=args.purchase_documents,
        env_file=args.env_file,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
