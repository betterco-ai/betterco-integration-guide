#!/usr/bin/env python3
"""
BetterCo integration guide — local web app driving the Mandanten-Neuannahme flow.

A single HTML page (index.html) backed by this stdlib http.server, which holds the
authenticated BetterCo client and proxies every call (the browser never talks to
BetterCo directly). See README.md for the full flow and HTTP_REFERENCE.md for the
raw HTTP behind each step.

    python app.py                                  # default env, :8770
    python app.py --env-file workspaces/prod.env
    python app.py --port 8771 --no-browser
"""
from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import os
import signal
import subprocess
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

from betterco_client import BetterCoClient
from reference_flow import connect, _as_list, _parse_env_file

HERE = Path(__file__).parent
HTML_FILE = HERE / "index.html"
WORKSPACES_DIR = HERE / "workspaces"
mimetypes.add_type("image/svg+xml", ".svg")   # not always registered on Windows

# Credential fields captured by the in-app Zugangsdaten editor, in file order:
# (key, label, secret, default). REST (key+secret) drives the customer/case/
# process/document calls; the User-API (email+password) is required for registry
# search (step 1) and the enriched "Akte anlegen" (step 3). secret=True fields are
# masked on read and only overwritten when the user actually changes them.
ENV_FIELDS = [
    ("BETTERCO_BASE_URL",      "Base URL",        False, "https://editor.betterco.ai/bcapi"),
    ("BETTERCO_API_KEY",       "REST API Key",    False, ""),
    ("BETTERCO_API_SECRET",    "REST API Secret", True,  ""),
    ("BETTERCO_WORKSPACE_ID",  "Workspace ID",    False, ""),
    ("BETTERCO_ORG_ID",        "Advisor Org ID",  False, ""),
    ("BETTERCO_USER_EMAIL",    "User E-Mail",      False, ""),
    ("BETTERCO_USER_PASSWORD", "User Passwort",    True,  ""),
]
SECRET_MASK = "••••••••"   # shown for a stored secret

# WZ 2008 (Klassifikation der Wirtschaftszweige) code -> Bezeichnung, keyed by the
# numeric code without the leading section letter (e.g. "70.10.1"). Sourced from the
# Destatis WZ 2008 list. Used to describe a customer's segmentCodes.wz on hover.
try:
    WZ2008 = json.loads((HERE / "wz2008.json").read_text(encoding="utf-8"))
except Exception:  # noqa: BLE001
    WZ2008 = {}


def _wz_lookup(code: str) -> str | None:
    """Resolve one WZ code to its Bezeichnung, falling back to broader levels
    (subclass -> class -> group -> division) when the exact code isn't listed."""
    parts = code.split(".")
    for i in range(len(parts), 0, -1):
        hit = WZ2008.get(".".join(parts[:i]))
        if hit:
            return hit
    return None


def _wz_describe(wz: str) -> str:
    """A multi-line "code — Bezeichnung" description for a (possibly comma-joined)
    WZ string, for the WZ-Code hover. Codes with no match are shown bare."""
    out = []
    for raw in str(wz or "").split(","):
        code = raw.strip()
        if not code:
            continue
        desc = _wz_lookup(code)
        out.append(f"{code} — {desc}" if desc else code)
    return "\n".join(out)
ENV_HEADER = (
    "# BetterCo workspace credentials — written by the in-app Zugangsdaten editor.\n"
    "# REST (key+secret) drives customer/case/process/document calls; the User-API\n"
    "# (email+password) is required for registry search and the enriched 'Akte anlegen'.\n"
    "# Contains secrets and is git-ignored — never commit a real .env.\n\n"
)

# Flow sets added to the matter on "Akte anlegen" (step 3), by domain.
ENTITY_FLOWS = ["F1800_OnboardingEntity_A", "F1800_OnboardingEntity_E",
                "F18000_ReKYC", "F1600_RiskAMLScreening"]
PERSON_FLOWS = ["F1900_OnboardingIndividual_A", "F1900_OnboardingIndividual_E",
                "F19000_ReKYC", "F1600_RiskAMLScreening"]

# ── Risiko-Evaluation (F1400) — full P1444 + P1448 question set (FlowBuilder spec) ──
# The risk pages bind to PROCESS-SCOPED containers on an F1400_RiskEvaluation process
# (NOT the bare actor). Verified contract (2026-06-26):
#   READ  GET /api/client/onboarding/full-data?businessRelationId&processId={F1400}
#         &sortingStrategy=COMPLIANCE — processId MANDATORY; else riskProfile/
#         riskContainer read back null.
#   WRITE submit_step(cid, f1400_pid, stepId, values) — the runner's per-step save.
#         The generic bulk update_full_data 200s but SILENTLY DROPS riskProfile/
#         riskContainer (only additionalActorData.* round-trips there).
# Each answer also derives a secondary "RISK flag" into additionalActorData — computed
# here exactly as the spec's bindingFields do (yes->HIGH, geography/industry maps, …).
RISK_FLOW = "F1400_RiskEvaluation"
RISK_STEP_HIGH = "P1444_riskEval2"        # P1444 — internal/PEP/country/complex risk
RISK_STEP_TAX = "P1448_riskEval3_Tax"     # P1448 — services/activities/industry/geo/anomalies


def _opt(*pairs):
    return [{"v": v, "l": l} for v, l in pairs]


def _hl(b):
    return "HIGH" if b else "LOW"


def _level_max(*levels):
    order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    best = max((order.get(x, 0) for x in levels), default=0)
    return ["LOW", "MEDIUM", "HIGH"][best]


# tax_geography enum -> risk level (P1448 bindingFields)
RISK_GEO_LEVEL = {"DOMESTIC": "LOW", "EU_EEA": "LOW",
                  "AML_THIRD_COUNTRIES": "MEDIUM", "HIGH_RISK_STATES": "HIGH"}
# taxIndustry code -> risk level (P1448 tax_industry* bindingFields; same map both variants)
RISK_INDUSTRY_LEVEL = {
    **{f"TAX_INDUSTRY_{n}": "LOW" for n in ("00", "01", "02", "03", "05", "06")},
    **{f"TAX_INDUSTRY_{n}": "MEDIUM" for n in ("04", "07", "08", "12", "15")},
    **{f"TAX_INDUSTRY_{n}": "HIGH" for n in
       ("09", "10", "11", "13", "14", "16", "17", "18", "19", "20",
        "21", "22", "23", "24")},
    "TAX_INDUSTRY_CASH": "HIGH",
}
# Option sets
GEO_OPTS = _opt(("DOMESTIC", "ausschließlich inländische Kunden"),
                ("EU_EEA", "Mitgliedsstaaten der EU und des EWR"),
                ("AML_THIRD_COUNTRIES", "Drittstaaten mit gut funktionierender Gw-Prävention"),
                ("HIGH_RISK_STATES", "Hochrisiko-Staaten ohne angemessene Standards"))
RESERVATION_OPTS = _opt(
    ("TAX_TASK_RESERVATION_01", "Finanzbuchhaltung"),
    ("TAX_TASK_RESERVATION_02", "Lohnbuchhaltung"),
    ("TAX_TASK_RESERVATION_03", "Jahresabschlussarbeiten"),
    ("TAX_TASK_RESERVATION_04", "Steuererklärungen"),
    ("TAX_TASK_RESERVATION_05", "Testamentsvollstreckung"),
    ("TAX_TASK_RESERVATION_06", "Verfahrensdokumentation"),
    ("TAX_TASK_RESERVATION_99", "Andere Dienstleistungen"))
AGREED_OPTS = _opt(
    ("TAX_TASK_AGREED_01", "Vermögensverwaltende Tätigkeit"),
    ("TAX_TASK_AGREED_02", "Treuhänderische Dienstleistungen"),
    ("TAX_TASK_AGREED_03", "Betriebswirtschaftliche Beratung/Wirtschaftsberatung"),
    ("TAX_TASK_AGREED_04", "IT-Beratung"),
    ("TAX_TASK_AGREED_05", "Finanz- und Immobilientransaktionen im Namen des Kunden"),
    ("TAX_TASK_AGREED_06", "Mitwirkung bei Planung/Durchführung von Geschäften des Kunden"),
    ("TAX_TASK_AGREED_07", "weitere Dienstleistungen"))
AGREED06_OPTS = _opt(
    ("TAX_TASK_AGREED_06.01", "Kauf/Verkauf von Immobilien oder Gewerbebetrieben"),
    ("TAX_TASK_AGREED_06.02", "Verwaltung von Geld, Wertpapieren oder Vermögenswerten"),
    ("TAX_TASK_AGREED_06.03", "Eröffnung/Verwaltung von Bank-, Spar- oder Wertpapierkonten"),
    ("TAX_TASK_AGREED_06.04", "Beschaffung von Mitteln zur Gründung/Verwaltung von Gesellschaften"),
    ("TAX_TASK_AGREED_06.05", "Gründung/Verwaltung von Treuhand-/Gesellschaftsstrukturen"),
    ("TAX_TASK_AGREED_06.99", "Sonstige"))
AGREED07_OPTS = _opt(
    ("TAX_TASK_AGREED_07.01", "Gründung von juristischen Personen/Personengesellschaften"),
    ("TAX_TASK_AGREED_07.02", "Ausübung von Leitungs-/Geschäftsführungsfunktion"),
    ("TAX_TASK_AGREED_07.03", "Bereitstellung Sitz/Geschäfts-/Verwaltungsdienstleistungen"),
    ("TAX_TASK_AGREED_07.04", "Treuhänder für eine Rechtsgestaltung (§ 3 Abs. 3 GwG)"),
    ("TAX_TASK_AGREED_07.05", "Nomineller Anteilseigner für eine andere Person"),
    ("TAX_TASK_AGREED_07.06", "Ermöglichen, dass andere Personen o.g. Funktionen ausüben"),
    ("TAX_TASK_AGREED_07.99", "Sonstige"))
INDUSTRY_INDIVIDUAL_OPTS = _opt(
    ("TAX_INDUSTRY_00", "-"),
    ("TAX_INDUSTRY_01", "Freiberufler (Ärzte, Rechtsanwälte, Architekten)"),
    ("TAX_INDUSTRY_02", "Angestellte, Rentner, Student"))
INDUSTRY_BUSINESS_OPTS = _opt(
    ("TAX_INDUSTRY_00", "-"), ("TAX_INDUSTRY_03", "Handwerker"),
    ("TAX_INDUSTRY_04", "Einzelhandel"), ("TAX_INDUSTRY_05", "Produzierendes Gewerbe"),
    ("TAX_INDUSTRY_06", "Börsennotierte Unternehmen"), ("TAX_INDUSTRY_07", "Import-/Exportbetrieb"),
    ("TAX_INDUSTRY_08", "Baugewerbe, Bauträger"), ("TAX_INDUSTRY_09", "Immobilienhandel, -makler"),
    ("TAX_INDUSTRY_10", "Handel mit teurer Kunst/Antiquitäten"),
    ("TAX_INDUSTRY_11", "Handel mit Edelmetallen/Edelsteinen"), ("TAX_INDUSTRY_12", "KfZ-Händler"),
    ("TAX_INDUSTRY_13", "Boots- und Yachthändler"), ("TAX_INDUSTRY_14", "Juweliere"),
    ("TAX_INDUSTRY_15", "Hotellerie, Gastronomie"), ("TAX_INDUSTRY_16", "Glücksspiel/Online-Gaming"),
    ("TAX_INDUSTRY_17", "Pornografie / Erwachsenenunterhaltung"),
    ("TAX_INDUSTRY_18", "Herstellung/Handel mit Waffen und Munition"),
    ("TAX_INDUSTRY_19", "Abholzung/Verbrennung natürlicher Ökosysteme"),
    ("TAX_INDUSTRY_20", "kontroverse Kreditvereinbarungen / Raubkredite"),
    ("TAX_INDUSTRY_21", "Ponzi-Schemata / betrügerische Anlageprogramme"),
    ("TAX_INDUSTRY_22", "Menschenhandel und Ausbeutung"),
    ("TAX_INDUSTRY_23", "Kryptowährungen (außer Whitelist)"),
    ("TAX_INDUSTRY_24", "Spirituosen, Tabak, E-Zigaretten, Cannabis / illegale Substanzen"),
    ("TAX_INDUSTRY_CASH", "Bargeldintensives Geschäft"))
ANOMALIES_OPTS = _opt(
    ("TAX_ANOMALIES_01", "Vermeidung persönlichen Kontakts ohne guten Grund"),
    ("TAX_ANOMALIES_02", "Forderung nach besonderer Diskretion oder Anonymität"),
    ("TAX_ANOMALIES_03", "Einschalten von Dritten (Strohmanngeschäfte)"),
    ("TAX_ANOMALIES_04", "Verwendung von Briefkastenfirmen"),
    ("TAX_ANOMALIES_05", "wirtschaftlich zweifelhafte/unsinnige Gestaltungen"),
    ("TAX_ANOMALIES_06", "Nummernkonten zur Abwicklung von Transaktionen"),
    ("TAX_ANOMALIES_07", "Verweigerung der Vorlage"), ("TAX_ANOMALIES_99", "Sonstige"))


def _risk_schema(entity_type: str) -> list:
    """The question schema sent to the UI, tailored to the entity type.

    Each question: key, step, section, type (yesno|yesno_manual|select|multi|text),
    label, options?, show? (visibility against current answers/entityType), auto? (the
    read-only auto-derived twin path for PEP/country)."""
    is_indiv = str(entity_type or "").upper() == "INDIVIDUAL"
    return [
        # ── P1444_riskEval2 ──
        {"key": "riskHigh_01", "step": "P1444", "section": "Interne Risiko-Analyse",
         "type": "yesno",
         "label": "Besteht aufgrund der unternehmensinternen Risiko-Analyse bzw. einer "
                  "Einzelfallprüfung ein erhöhtes Risiko?"},
        {"key": "pep", "step": "P1444", "section": "Politisch exponierte Person",
         "type": "yesno_manual", "auto": True,
         "label": "Ist der Vertragspartner oder wirtschaftlich Berechtigte eine politisch "
                  "exponierte Person, ein Familienmitglied oder nahestehende Person?"},
        {"key": "country", "step": "P1444", "section": "Länderrisiko",
         "type": "yesno_manual", "auto": True,
         "label": "Ist der Vertragspartner oder wirtschaftlich Berechtigte in einem "
                  "Drittstaat mit hohem Risiko niedergelassen?"},
        {"key": "riskHigh_04", "step": "P1444", "section": "Komplexe/ungewöhnliche Transaktion",
         "type": "yesno",
         "label": "Ist die Transaktion besonders komplex oder groß, läuft ungewöhnlich ab "
                  "oder hat keinen offensichtlichen wirtschaftlichen/rechtmäßigen Zweck?"},
        # ── P1448_riskEval3_Tax ──
        {"key": "tax_task_reservation", "step": "P1448", "section": "Leistungen",
         "type": "multi", "options": RESERVATION_OPTS,
         "label": "An welchen unserer Leistungen besteht Interesse?"},
        {"key": "tax_task_reservationOther", "step": "P1448", "section": "Leistungen",
         "type": "text", "label": "Bitte beschreibe dein Interesse",
         "show": {"key": "tax_task_reservation", "includes": "TAX_TASK_RESERVATION_99"}},
        {"key": "tax_task_agreed", "step": "P1448", "section": "Weitere Tätigkeiten",
         "type": "multi", "options": AGREED_OPTS,
         "label": "Weitere vereinbarte Tätigkeiten"},
        {"key": "tax_task_agreed_06", "step": "P1448", "section": "Weitere Tätigkeiten",
         "type": "select", "options": AGREED06_OPTS,
         "label": "Mitwirkung bei Geschäften des Mandanten — bitte detaillieren",
         "show": {"key": "tax_task_agreed", "includes": "TAX_TASK_AGREED_06"}},
        {"key": "tax_task_agreed_07", "step": "P1448", "section": "Weitere Tätigkeiten",
         "type": "select", "options": AGREED07_OPTS,
         "label": "Weitere Dienstleistungen — bitte detaillieren",
         "show": {"key": "tax_task_agreed", "includes": "TAX_TASK_AGREED_07"}},
        {"key": "tax_industry", "step": "P1448", "section": "Risiko-Branche",
         "type": "select",
         "options": INDUSTRY_INDIVIDUAL_OPTS if is_indiv else INDUSTRY_BUSINESS_OPTS,
         "label": "Wähle die zutreffende Branche aus"},
        {"key": "tax_geography", "step": "P1448", "section": "Geographische Tätigkeit",
         "type": "select", "options": GEO_OPTS, "show": {"entity": "business"},
         "label": "Geographisches Umfeld der Geschäftstätigkeit"},
        {"key": "has_tax_anomalies", "step": "P1448", "section": "Besondere Auffälligkeiten",
         "type": "yesno", "label": "Bestehen besondere Auffälligkeiten bei dem Mandanten?"},
        {"key": "tax_anomalies", "step": "P1448", "section": "Besondere Auffälligkeiten",
         "type": "multi", "options": ANOMALIES_OPTS, "label": "Auffälligkeiten auswählen",
         "show": {"key": "has_tax_anomalies", "equals": True}},
    ]

_client = None          # set in main()
_org_id = None          # advisor org id (from the workspace env) — set in main()
_env_file = None        # path of the active workspace .env — set in main()/on save
_status = {}            # last verify_env() report (for the Zugangsdaten status line)
_client_lock = threading.Lock()


def _safe_env_path(name: str) -> Path:
    """Resolve a user-supplied env name to workspaces/<base>.env (no traversal)."""
    base = Path(name or "").name           # strip any directory part
    if not base.endswith(".env"):
        base += ".env"
    return WORKSPACES_DIR / base


def _build_client(values: dict) -> BetterCoClient:
    """Construct a client from explicit env values (same wiring as connect())."""
    return BetterCoClient(
        base_url=values.get("BETTERCO_BASE_URL"),
        api_key=values.get("BETTERCO_API_KEY"),
        api_secret=values.get("BETTERCO_API_SECRET"),
        workspace_id=values.get("BETTERCO_WORKSPACE_ID"),
        org_id=values.get("BETTERCO_ORG_ID"),
        user_email=values.get("BETTERCO_USER_EMAIL"),
        user_password=values.get("BETTERCO_USER_PASSWORD"),
    )


def _write_env_file(path: Path, values: dict) -> None:
    body = ENV_HEADER + "".join(f"{k}={values.get(k, '')}\n" for k, *_ in ENV_FIELDS)
    path.write_text(body, encoding="utf-8")


def _first(*vals):
    for v in vals:
        if v not in (None, ""):
            return v
    return None


def _relation_codes(body: dict) -> list:
    """Normalize a contact's role codes from a request body, de-duped in order.

    Accepts the multi-select `relationCodes` (list) and falls back to the legacy
    single `relationCode` so older callers keep working.
    """
    raw = body.get("relationCodes")
    if not isinstance(raw, list):
        raw = [body.get("relationCode")]
    out = []
    for c in raw:
        c = str(c or "").strip()
        if c and c not in out:
            out.append(c)
    return out


def _risk_fields_rest(cu: dict) -> dict:
    """Extract KYC/risk fields from a REST getCustomerById object.

    Final bindings (all under amlProfile.riskProfile): kycNote, kycStatus,
    taxIndustry, taxIndustryRisk, riskCountry, aggregatedAmlRisk. wz from
    segmentCodes.wz. Top-level riskProfile (+ riskProfile.amlProfile) are
    fallbacks in case a profiled REST object nests them differently.
    """
    arp = (cu.get("amlProfile") or {}).get("riskProfile") or {}   # amlProfile.riskProfile
    rp = cu.get("riskProfile") or {}                              # top-level fallback
    rpa = rp.get("amlProfile") or {}                              # riskProfile.amlProfile
    seg = cu.get("segmentCodes") or {}
    wz = seg.get("wz")
    wz = ", ".join(wz) if isinstance(wz, list) else (wz or "")

    def f(key):
        return _first(arp.get(key), rp.get(key), rpa.get(key))
    return {
        "pruefnotiz": f("kycNote"),
        "kycStatus": f("kycStatus"),
        "wzCode": wz,
        "wzDescription": _wz_describe(wz),
        "taxIndustry": f("taxIndustry"),
        "industryRisk": f("taxIndustryRisk"),
        "countryRisk": f("riskCountry"),
        "amlRisk": f("aggregatedAmlRisk"),
    }


def _datev_id(cu: dict) -> str:
    """DATEV external ID(s) of a REST customer — externalIdentifiers where system=DATEV.

    Shape per entry: {"externalId": "<connector-uuid>-<datevNumber>", "system": "DATEV"},
    e.g. "692e6898-…-647d4ed6c87f-29646". The DATEV consultant/client number is the
    suffix after the last dash (29646); the overview's DATEV filter substring-matches the
    whole string, so typing just those trailing digits finds the client.
    """
    ids = [e.get("externalId") for e in (cu.get("externalIdentifiers") or [])
           if str(e.get("system", "")).upper() == "DATEV" and e.get("externalId")]
    return ", ".join(ids)


def _find_risk_process(cid: str):
    """Locate a non-CLOSED F1400_RiskEvaluation process for a customer.

    Returns (case_id, pid) — pid is None if no open F1400 exists yet (case_id is
    still returned so the caller can create one). Call inside _client_lock.
    """
    cases = _as_list(_client.list_cases(cid))
    case_id = cases[0].get("id") if cases else None
    for cs in cases:
        cs_id = cs.get("id")
        for p in _as_list(_client.list_processes(cid, cs_id)):
            try:
                d = _client.get_process(cid, cs_id, p.get("id"))
            except Exception:  # noqa: BLE001
                continue
            if d.get("name") == RISK_FLOW and str(d.get("status", "")).upper() != "CLOSED":
                return cs_id, p.get("id")
    return case_id, None


def _risk_full_data(cid: str, pid: str) -> dict:
    """Read full-data WITH the F1400 processId — required for riskProfile/riskContainer
    to be populated (they are process-scoped). Mirrors the flow's getUrl."""
    r = requests.get(
        _client.base_url + "/api/client/onboarding/full-data",
        headers=_client._user_headers(), verify=_client.session.verify,
        params={"businessRelationId": cid, "processId": pid,
                "sortingStrategy": "COMPLIANCE",
                "roleTypes": ["UBO_CURRENT", "LEGAL_REP_CURRENT", "ACTING_PERSON"],
                "limit": 50})
    r.raise_for_status()
    return r.json()


def _risk_read_answers(fd: dict) -> dict:
    """Read all P1444+P1448 answers + auto twins + derived flags from F1400 full-data."""
    rc = fd.get("riskContainer") or {}
    ra = (rc.get("riskAnswers") or {}).get("HIGH") or {}
    rm = (rc.get("manualRiskAnswers") or {}).get("HIGH") or {}
    rp = fd.get("riskProfile") or {}
    aad = fd.get("additionalActorData") or {}
    return {
        "answers": {
            "riskHigh_01": ra.get("riskHigh_01"),
            "pep": rm.get("isAnyPep"),
            "country": rm.get("highRiskThirdCountry"),
            "riskHigh_04": ra.get("riskHigh_04"),
            "tax_task_reservation": rp.get("taskReservation") or [],
            "tax_task_reservationOther": aad.get("tax_task_reservationOther"),
            "tax_task_agreed": rp.get("agreedActivities") or [],
            "tax_task_agreed_06": rp.get("clientTransactions"),
            "tax_task_agreed_07": rp.get("otherServices"),
            "tax_industry": rp.get("taxIndustry"),
            "tax_geography": rp.get("taxGeography"),
            "has_tax_anomalies": aad.get("has_tax_anomalies"),
            "tax_anomalies": rp.get("taxAnomaliesList") or [],
        },
        "auto": {  # read-only auto-derived twins shown next to the PEP/country answers
            "pep": ra.get("isAnyPep"),
            "country": ra.get("highRiskThirdCountry"),
        },
        "flags": {k: aad.get(k) for k in (
            "riskFlag_High_01", "riskFlag_High_02", "riskFlag_High_03", "riskFlag_High_04",
            "risk_task_reservation", "risk_task_agreed", "risk_tax_industry",
            "risk_tax_geography", "risk_tax_anomalies", "risk_tax")},
    }


def _risk_agreed_level(agreed, t06, t07) -> str:
    """risk_task_agreed: LOW unless a MEDIUM-weighted activity/detail is selected
    (spec: 01/02/03/05 or any 06.xx or 07.02..07.99 -> MEDIUM; 04 alone -> LOW)."""
    agreed = agreed or []
    if any(a in agreed for a in ("TAX_TASK_AGREED_01", "TAX_TASK_AGREED_02",
                                 "TAX_TASK_AGREED_03", "TAX_TASK_AGREED_05")):
        return "MEDIUM"
    if t06 and t06 != "TAX_TASK_AGREED_06.00":
        return "MEDIUM"
    if t07 and t07 not in ("TAX_TASK_AGREED_07.00", "TAX_TASK_AGREED_07.01"):
        return "MEDIUM"
    return "LOW"


def _risk_build(ans: dict):
    """Build the two submit_step bodies (P1444, P1448) from a UI answers dict, computing
    every derived RISK flag as the spec's bindingFields do. Returns (p1444, p1448, flags).
    Only answered fields are written (None = leave untouched)."""
    g = ans.get
    answers_high, manual_high, aad44 = {}, {}, {}
    if g("riskHigh_01") is not None:
        answers_high["riskHigh_01"] = bool(g("riskHigh_01")); aad44["riskFlag_High_01"] = _hl(g("riskHigh_01"))
    if g("pep") is not None:
        manual_high["isAnyPep"] = bool(g("pep")); aad44["riskFlag_High_02"] = _hl(g("pep"))
    if g("country") is not None:
        manual_high["highRiskThirdCountry"] = bool(g("country")); aad44["riskFlag_High_03"] = _hl(g("country"))
    if g("riskHigh_04") is not None:
        answers_high["riskHigh_04"] = bool(g("riskHigh_04")); aad44["riskFlag_High_04"] = _hl(g("riskHigh_04"))
    rc = {}
    if answers_high:
        rc["riskAnswers"] = {"HIGH": answers_high}
    if manual_high:
        rc["manualRiskAnswers"] = {"HIGH": manual_high}
    p1444 = {}
    if rc:
        p1444["riskContainer"] = rc
    if aad44:
        p1444["additionalActorData"] = aad44

    rp, aad48 = {}, {}
    if g("tax_task_reservation") is not None:
        rp["taskReservation"] = list(g("tax_task_reservation") or [])
        aad48["risk_task_reservation"] = "LOW"
    if g("tax_task_reservationOther") is not None:
        aad48["tax_task_reservationOther"] = g("tax_task_reservationOther")
    agreed, t06, t07 = g("tax_task_agreed"), g("tax_task_agreed_06"), g("tax_task_agreed_07")
    if agreed is not None:
        rp["agreedActivities"] = list(agreed or [])
    if t06:
        rp["clientTransactions"] = t06
    if t07:
        rp["otherServices"] = t07
    if agreed is not None or t06 or t07:
        aad48["risk_task_agreed"] = _risk_agreed_level(agreed, t06, t07)
    if g("tax_industry"):
        rp["taxIndustry"] = g("tax_industry")
        aad48["risk_tax_industry"] = RISK_INDUSTRY_LEVEL.get(g("tax_industry"), "LOW")
    if g("tax_geography"):
        rp["taxGeography"] = g("tax_geography")
        aad48["risk_tax_geography"] = RISK_GEO_LEVEL.get(g("tax_geography"), "LOW")
    if g("has_tax_anomalies") is not None:
        aad48["has_tax_anomalies"] = bool(g("has_tax_anomalies"))
        aad48["risk_tax_anomalies"] = _hl(g("has_tax_anomalies"))
    if g("tax_anomalies") is not None:
        rp["taxAnomaliesList"] = list(g("tax_anomalies") or [])
    comp = [aad48[k] for k in ("risk_task_reservation", "risk_task_agreed", "risk_tax_industry",
            "risk_tax_geography", "risk_tax_anomalies") if k in aad48]
    if comp:
        aad48["risk_tax"] = _level_max(*comp)
    p1448 = {}
    if rp:
        p1448["riskProfile"] = rp
    if aad48:
        p1448["additionalActorData"] = aad48
    return p1444, p1448, {**aad44, **aad48}


def _doc_meta(d: dict) -> dict:
    """List doc {id,displayName,url} -> add documentType + uploadDate from its detail.

    getCustomer/ProcessDocuments lists don't carry the type (null); the per-doc
    detail (the list `url`) has `type` and `uploadDate`. Call inside _client_lock.
    """
    out = {"id": d.get("id"), "displayName": d.get("displayName") or d.get("name"),
           "documentType": None, "uploadDate": None}
    url = d.get("url")
    if url:
        try:
            det = _client.session.get(url).json()
            out["documentType"] = det.get("type")
            out["uploadDate"] = det.get("uploadDate")
        except Exception:  # noqa: BLE001
            pass
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # quieter console
        pass

    def _send_json(self, obj, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self):
        body = HTML_FILE.read_text(encoding="utf-8").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, url_path: str):
        """Serve a file from the app's assets/ folder (e.g. the header logo).

        Confined to HERE/assets — any path that resolves outside it is refused.
        """
        target = (HERE / url_path.lstrip("/")).resolve()
        assets = (HERE / "assets").resolve()
        if assets != target and assets not in target.parents:
            return self._send_json({"error": "forbidden"}, 403)
        if not target.is_file():
            return self._send_json({"error": "not found"}, 404)
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            return self._send_html()
        if parsed.path.startswith("/assets/"):
            return self._serve_static(parsed.path)
        if parsed.path == "/api/env":
            return self._handle_env_get(parse_qs(parsed.query))
        if parsed.path == "/api/search":
            return self._handle_search(parse_qs(parsed.query))
        if parsed.path == "/api/document":
            return self._handle_document_download(parse_qs(parsed.query))
        if parsed.path == "/api/documents-zip":
            return self._handle_documents_zip(parse_qs(parsed.query))
        self._send_json({"error": "not found"}, 404)

    def _handle_documents_zip(self, qs: dict):
        """Download ALL docs of a customer (cid) or a process (cid+case_id+pid) as one ZIP.

        No bulk endpoint exists — fetch each doc (detail→downloadURI→bytes) and zip them.
        """
        import io
        import zipfile
        cid = (qs.get("cid") or [""])[0].strip()
        case_id = (qs.get("case_id") or [""])[0].strip()
        pid = (qs.get("pid") or [""])[0].strip()
        if not cid:
            return self._send_json({"error": "cid required"}, 400)
        try:
            with _client_lock:
                if case_id and pid:
                    docs = _as_list(_client.list_process_documents(cid, case_id, pid))
                    zipname = f"prozess_dokumente_{pid}.zip"
                else:
                    docs = _as_list(_client.list_customer_documents(cid))
                    zipname = f"mandanten_dokumente_{cid}.zip"
                _client._ensure_auth()
                buf = io.BytesIO()
                added = 0
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    seen = {}
                    for d in docs:
                        url = d.get("url")
                        if not url:
                            continue
                        try:
                            det = _client.session.get(url).json()
                            dl = det.get("downloadURI")
                            fname = det.get("fileName") or d.get("displayName") or f"{d.get('id')}.pdf"
                            if not dl:
                                continue
                            r = _client.session.get(dl)
                            if not r.ok or "json" in r.headers.get("Content-Type", ""):
                                continue
                            # avoid duplicate names inside the zip
                            n = seen.get(fname, 0)
                            seen[fname] = n + 1
                            if n:
                                stem, _, ext = fname.rpartition(".")
                                fname = f"{stem}_{n}.{ext}" if stem else f"{fname}_{n}"
                            zf.writestr(fname, r.content)
                            added += 1
                        except Exception:  # noqa: BLE001
                            continue
                data = buf.getvalue()
            if not added:
                return self._send_json({"error": "keine Dokumente"}, 404)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{zipname}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": str(exc)}, 500)

    def _handle_document_download(self, qs: dict):
        """Proxy a document download. Customer docs (cid+doc_id) or process docs
        (cid+case_id+pid+doc_id). The list `url` is the detail JSON whose
        `downloadURI` is the actual binary — fetch detail, then stream the binary.
        """
        cid = (qs.get("cid") or [""])[0].strip()
        doc_id = (qs.get("doc_id") or [""])[0].strip()
        case_id = (qs.get("case_id") or [""])[0].strip()
        pid = (qs.get("pid") or [""])[0].strip()
        if not (cid and doc_id):
            return self._send_json({"error": "cid, doc_id required"}, 400)
        try:
            with _client_lock:
                if case_id and pid:
                    docs = _as_list(_client.list_process_documents(cid, case_id, pid))
                else:
                    docs = _as_list(_client.list_customer_documents(cid))
                doc = next((d for d in docs if d.get("id") == doc_id), None)
                if not doc or not doc.get("url"):
                    return self._send_json({"error": "document not found"}, 404)
                _client._ensure_auth()
                detail = _client.session.get(doc["url"])
                detail.raise_for_status()
                dj = detail.json()
                dl = dj.get("downloadURI")
                fname = dj.get("fileName") or doc.get("displayName") or f"{doc_id}.pdf"
                if not dl:
                    return self._send_json({"error": "no downloadURI"}, 404)
                r = _client.session.get(dl, stream=True)
            if not r.ok:
                return self._send_json({"error": f"download failed ({r.status_code})"}, 502)
            ctype = r.headers.get("Content-Type", "application/pdf")
            if "json" in ctype:   # malware-scan / error body, not a file
                return self._send_json({"error": r.text[:200]}, 502)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
            self.end_headers()
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    self.wfile.write(chunk)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": str(exc)}, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._send_json({"error": "invalid JSON body"}, 400)
        if parsed.path == "/api/env":
            return self._handle_env_save(body)
        if parsed.path == "/api/create-matter":
            return self._handle_create_matter(body)
        if parsed.path == "/api/process-link":
            return self._handle_process_link(body)
        if parsed.path == "/api/processes":
            return self._handle_processes(body)
        if parsed.path == "/api/process-detail":
            return self._handle_process_detail(body)
        if parsed.path == "/api/customer":
            return self._handle_customer(body)
        if parsed.path == "/api/documents":
            return self._handle_documents(body)
        if parsed.path == "/api/process-documents":
            return self._handle_process_documents(body)
        if parsed.path == "/api/customers-list":
            return self._handle_customers_list(body)
        if parsed.path == "/api/risk-eval":
            return self._handle_risk_eval(body)
        if parsed.path == "/api/risk-eval-save":
            return self._handle_risk_eval_save(body)
        if parsed.path == "/api/risk-profile":
            return self._handle_risk_profile(body)
        if parsed.path == "/api/customer-processes":
            return self._handle_customer_processes(body)
        if parsed.path == "/api/contacts":
            return self._handle_contacts(body)
        if parsed.path == "/api/contact-create":
            return self._handle_contact_create(body)
        if parsed.path == "/api/contact-add-relation":
            return self._handle_contact_add_relation(body)
        if parsed.path == "/api/contact-delete-relation":
            return self._handle_contact_delete_relation(body)
        self._send_json({"error": "not found"}, 404)

    # ── Zugangsdaten (workspace .env credential editor) ──────────────
    def _handle_env_get(self, qs: dict):
        """List workspace .env files and return one file's values (secrets masked).

        Secret fields (API_SECRET, USER_PASSWORD) never leave the server in clear —
        a stored secret reads back as SECRET_MASK; an unset one as "". For a brand-new
        (non-existent) file the non-secret defaults (e.g. Base URL) are prefilled.
        """
        active = Path(_env_file).name if _env_file else None
        requested = (qs.get("file") or [""])[0].strip()
        target = Path(requested).name if requested else active
        files = sorted(p.name for p in WORKSPACES_DIR.glob("*.env"))
        parsed = {}
        if target:
            path = WORKSPACES_DIR / target
            if path.exists():
                parsed = _parse_env_file(str(path))
        is_new = bool(target) and not parsed
        values, is_set = {}, {}
        for key, _label, secret, default in ENV_FIELDS:
            stored = parsed.get(key, "")
            is_set[key] = bool(stored)
            if secret and stored:
                values[key] = SECRET_MASK
            elif stored:
                values[key] = stored
            elif is_new and default:        # new file: prefill sensible defaults
                values[key] = default
            else:
                values[key] = ""
        return self._send_json({
            "files": files, "active": active, "file": target,
            "fields": [{"key": k, "label": l, "secret": s} for k, l, s, _ in ENV_FIELDS],
            "values": values, "isSet": is_set, "mask": SECRET_MASK, "status": _status,
        })

    def _handle_env_save(self, body: dict):
        """Write a workspace .env and, by default, activate it as the live client.

        An unchanged masked secret (== SECRET_MASK) keeps the value already on disk.
        Activation runs verify_env() and only swaps the global client if it reports ok;
        otherwise the file is still written but the running client is left untouched.
        """
        global _client, _org_id, _env_file, _status
        fname = (body.get("file") or "").strip()
        if not fname:
            return self._send_json({"error": "Dateiname erforderlich"}, 400)
        values_in = body.get("values") or {}
        activate = body.get("activate", True)
        path = _safe_env_path(fname)
        existing = _parse_env_file(str(path)) if path.exists() else {}
        out = {}
        for key, _label, secret, _default in ENV_FIELDS:
            v = (values_in.get(key) or "").strip()
            if secret and v == SECRET_MASK:        # unchanged masked secret -> keep stored
                v = existing.get(key, "")
            out[key] = v
        try:
            _write_env_file(path, out)
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": f"Schreiben fehlgeschlagen: {exc}"}, 500)
        resp = {"saved": path.name, "activated": False}
        if activate:
            rep, client = {}, None
            try:
                client = _build_client(out)
                rep = client.verify_env()
            except Exception as exc:  # noqa: BLE001
                resp["error"] = f"Verbindung fehlgeschlagen: {exc}"
            resp["status"] = rep
            if rep.get("ok"):
                with _client_lock:
                    _client = client
                    _org_id = out.get("BETTERCO_ORG_ID")
                    _env_file = str(path)
                    _status = rep
                resp["activated"] = True
            elif "error" not in resp:
                resp["error"] = "Gespeichert, aber Verbindung nicht ok — siehe Status."
        return self._send_json(resp)

    def _handle_customer_processes(self, body: dict):
        """Process map {cid: [{flow,label,status}]} for the overview process filter.

        Two-phase + concurrent (collect refs, then fetch details in one big pool) —
        much faster than per-customer sequential fetching.
        """
        limit = body.get("limit")
        try:
            custs = _as_list(_client.list_customers())
            if limit:
                custs = custs[: int(limit)]
            cids = [c.get("id") for c in custs if c.get("id")]

            # Phase 1: collect refs for ONLY the ReKYC + Screening processes (identified
            # from the list displayName, no detail call) — the only two the filter offers.
            def refs_for(cid):
                refs = []
                try:
                    for case in _as_list(_client.list_cases(cid)):
                        for p in _as_list(_client.list_processes(cid, case.get("id"))):
                            di = p.get("displayInformation") or {}
                            label = ((di.get("processName") or {}).get("deValue")) or p.get("displayName")
                            low = (p.get("displayName") or "").lower().replace(" ", "")
                            if "rekyc" not in low and "screening" not in low:
                                continue
                            refs.append({"cid": cid, "url": p.get("url"), "label": label})
                except Exception:  # noqa: BLE001
                    pass
                return refs
            with ThreadPoolExecutor(max_workers=16) as ex:
                all_refs = [r for lst in ex.map(refs_for, cids) for r in lst]

            # Phase 2: fetch each process detail (status + flow code) in one big pool
            def detail(ref):
                try:
                    det = _client.session.get(ref["url"]).json() if ref.get("url") else {}
                    ref["flow"] = det.get("name")
                    ref["status"] = det.get("status")
                except Exception:  # noqa: BLE001
                    ref["flow"] = ref["status"] = None
                return ref
            with ThreadPoolExecutor(max_workers=16) as ex:
                all_refs = list(ex.map(detail, all_refs))

            pmap = {}
            for r in all_refs:
                pmap.setdefault(r["cid"], []).append(
                    {"flow": r.get("flow"), "label": r.get("label"), "status": r.get("status")})
            return self._send_json({"processes": pmap})
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": str(exc)}, 500)

    def _handle_risk_profile(self, body: dict):
        """Risiko-Profil for one customer via REST getCustomerById.

        Bindings (amlProfile.riskProfile.*): kycNote, kycStatus, taxIndustry,
        taxIndustryRisk, riskCountry, aggregatedAmlRisk; wz from segmentCodes.wz.
        """
        cid = (body.get("cid") or "").strip()
        if not cid:
            return self._send_json({"error": "cid required"}, 400)
        try:
            with _client_lock:
                cu = _client.get_customer(cid)
            f = _risk_fields_rest(cu)
            return self._send_json({
                "pruefnotiz": f["pruefnotiz"],
                "kycStatus": f["kycStatus"],
                "wzCode": f["wzCode"],
                "taxIndustry": f["taxIndustry"],
                "derivedIndustryRisk": f["industryRisk"],
                "derivedCountryRisk": f["countryRisk"],
                "amlRisk": f["amlRisk"],
            })
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": str(exc)}, 500)

    def _handle_customers_list(self, body: dict):
        """Workspace customer overview with KYC/risk fields — REST getCustomerById.

        All bindings come from the REST customer object (see _risk_fields_rest):
        kycStatus=additionalData[import].kycStatus, wzCode=segmentCodes.wz,
        taxIndustry/industryRisk/countryRisk=riskProfile.{taxIndustry,taxIndustryRisk,
        riskCountry}. amlRisk binding TBD.
        """
        limit = body.get("limit")
        try:
            custs = _as_list(_client.list_customers())
            if limit:
                custs = custs[: int(limit)]
            items = [(c.get("id"), c.get("displayName")) for c in custs if c.get("id")]

            def build(item):
                cid, dn = item
                try:
                    cu = _client.get_customer(cid)       # REST
                except Exception:  # noqa: BLE001
                    return {"id": cid, "name": dn, "error": True, "processes": []}
                f = _risk_fields_rest(cu)
                return {
                    "id": cid,
                    "name": (cu.get("legalInfo") or {}).get("legalName") or dn,
                    "type": cu.get("type"),   # INDIVIDUAL = Person, else Unternehmen
                    "datevId": _datev_id(cu),  # externalIdentifiers system=DATEV
                    "kycStatus": f["kycStatus"], "wzCode": f["wzCode"],
                    "wzDescription": f["wzDescription"],
                    "taxIndustry": f["taxIndustry"], "industryRisk": f["industryRisk"],
                    "countryRisk": f["countryRisk"], "amlRisk": f["amlRisk"],
                    "pruefnotiz": f["pruefnotiz"],
                }

            # REST get_customer per customer — concurrent (process data is loaded
            # separately/lazily via /api/customer-processes to keep this fast)
            with ThreadPoolExecutor(max_workers=16) as ex:
                rows = list(ex.map(build, items))
            rows.sort(key=lambda r: (r.get("name") or "").lower())
            return self._send_json({"total": len(rows), "customers": rows})
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": str(exc)}, 500)

    def _handle_risk_eval(self, body: dict):
        """Read the full P1444+P1448 answer set for one customer (the Risiko-Fragen tab).

        Finds a non-CLOSED F1400_RiskEvaluation process and reads full-data WITH its
        processId. Returns the question schema (tailored to the entity type) + current
        answers + auto twins + derived flags. No F1400 yet -> hasProcess=false and the
        process is created lazily on first save."""
        cid = (body.get("cid") or "").strip()
        if not cid:
            return self._send_json({"error": "cid required"}, 400)
        try:
            with _client_lock:
                _, pid = _find_risk_process(cid)
                fd = _risk_full_data(cid, pid) if pid else {}
            et = (fd.get("clientType") or {}).get("entityStageType")
            data = _risk_read_answers(fd) if pid else {"answers": {}, "auto": {}, "flags": {}}
            return self._send_json({"cid": cid, "hasProcess": bool(pid), "pid": pid,
                                    "entityType": et, "schema": _risk_schema(et), **data})
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": str(exc)}, 500)

    def _handle_risk_eval_save(self, body: dict):
        """Persist the P1444+P1448 answers via submit_step (the runner's save).

        Ensures an F1400_RiskEvaluation process (creates one if missing), writes each
        page's primary bindings + derived RISK flags, then reads back with the processId
        and returns the fresh schema/answers/flags."""
        cid = (body.get("cid") or "").strip()
        if not cid:
            return self._send_json({"error": "cid required"}, 400)
        ans = body.get("answers") or {}
        try:
            p1444, p1448, _ = _risk_build(ans)
            with _client_lock:
                case_id, pid = _find_risk_process(cid)
                if not pid:
                    if not case_id:
                        return self._send_json({"error": "Kein Case zum Anlegen des F1400-Prozesses"}, 400)
                    pid = _client.create_process(cid, case_id, RISK_FLOW)["id"]
                if p1444:
                    _client.submit_step(cid, pid, RISK_STEP_HIGH, p1444)
                if p1448:
                    _client.submit_step(cid, pid, RISK_STEP_TAX, p1448)
                fd = _risk_full_data(cid, pid)
            et = (fd.get("clientType") or {}).get("entityStageType")
            return self._send_json({"cid": cid, "pid": pid, "saved": True,
                                    "entityType": et, "schema": _risk_schema(et),
                                    **_risk_read_answers(fd)})
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": str(exc)}, 500)

    def _handle_process_documents(self, body: dict):
        """getProcessDocuments — process-level docs (Sonstige Dokumente)."""
        cid = (body.get("cid") or "").strip()
        case_id = (body.get("case_id") or "").strip()
        pid = (body.get("pid") or "").strip()
        if not (cid and case_id and pid):
            return self._send_json({"error": "cid, case_id, pid required"}, 400)
        try:
            with _client_lock:
                docs = _as_list(_client.list_process_documents(cid, case_id, pid))
                out = [_doc_meta(d) for d in docs]
            return self._send_json({"pid": pid, "documents": out})
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": str(exc)}, 500)

    def _handle_customer(self, body: dict):
        """getCustomerById — full customer master data for the Kundendaten view."""
        cid = (body.get("cid") or "").strip()
        if not cid:
            return self._send_json({"error": "cid required"}, 400)
        try:
            with _client_lock:
                cust = _client.get_customer(cid)
            return self._send_json({"customer": cust})
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": str(exc)}, 500)

    def _handle_documents(self, body: dict):
        """getCustomerDocuments — list (id/displayName); download via /api/document proxy."""
        cid = (body.get("cid") or "").strip()
        if not cid:
            return self._send_json({"error": "cid required"}, 400)
        try:
            with _client_lock:
                docs = _as_list(_client.list_customer_documents(cid))
                out = [_doc_meta(d) for d in docs]
            return self._send_json({"cid": cid, "documents": out})
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": str(exc)}, 500)

    def _handle_process_detail(self, body: dict):
        """Read-only detail of one process (for non-shareable/internal flows)."""
        cid = (body.get("cid") or "").strip()
        case_id = (body.get("case_id") or "").strip()
        pid = (body.get("pid") or "").strip()
        if not (cid and case_id and pid):
            return self._send_json({"error": "cid, case_id, pid required"}, 400)
        try:
            with _client_lock:
                d = _client.get_process(cid, case_id, pid)
            di = d.get("displayInformation") or {}
            label = (((di.get("processName") or {}).get("deValue"))
                     or ((di.get("displayName") or {}).get("deValue")) or d.get("name"))
            tasks = [{"taskSpec": t.get("taskSpec"), "status": t.get("status")}
                     for t in (d.get("tasks") or [])]
            return self._send_json({
                "label": label, "flow": d.get("name"), "status": d.get("status"),
                "total": d.get("totalTasks"), "completed": d.get("numberOfCompletedTasks"),
                "shareable": d.get("shareable"), "tasks": tasks,
            })
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": str(exc)}, 500)

    def _handle_processes(self, body: dict):
        """Enriched process list for the picker (getProcesses + per-process getProcess).

        Returns, per process: flow code, German label, status, task progress, and the
        `shareable` flag (true = has a client runner that can be iframed).
        """
        cid = (body.get("cid") or "").strip()
        case_id = (body.get("case_id") or "").strip()
        if not (cid and case_id):
            return self._send_json({"error": "cid, case_id required"}, 400)
        try:
            with _client_lock:
                rows = _as_list(_client.list_processes(cid, case_id))
                out = []
                for p in rows:
                    pid = p.get("id")
                    try:
                        d = _client.get_process(cid, case_id, pid)
                    except Exception:  # noqa: BLE001
                        d = {}
                    di = d.get("displayInformation") or p.get("displayInformation") or {}
                    label = (((di.get("processName") or {}).get("deValue"))
                             or ((di.get("displayName") or {}).get("deValue"))
                             or p.get("displayName") or d.get("name"))
                    out.append({
                        "id": pid,
                        "flow": d.get("name"),
                        "label": label,
                        "status": d.get("status"),
                        "total": d.get("totalTasks"),
                        "completed": d.get("numberOfCompletedTasks"),
                        "open": d.get("numberOfOpenTasks"),
                        "shareable": d.get("shareable"),
                    })
            return self._send_json({"cid": cid, "case_id": case_id, "processes": out})
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": str(exc)}, 500)

    def _handle_process_link(self, body: dict):
        """Mint a client-runner share link for one process (for the iframe picker)."""
        cid = (body.get("cid") or "").strip()
        case_id = (body.get("case_id") or "").strip()
        pid = (body.get("pid") or "").strip()
        if not (cid and case_id and pid):
            return self._send_json({"error": "cid, case_id, pid required"}, 400)
        try:
            with _client_lock:
                # initiator side = the client/advisor runner (not the target/customer-fill view)
                url = _client.create_share_link(cid, case_id, pid, is_initiator=True)
            return self._send_json({"share_url": url})
        except Exception as exc:  # noqa: BLE001 — e.g. screening has no client runner
            return self._send_json({"share_url": None, "error": str(exc)})

    def _handle_create_matter(self, body: dict):
        """Step 3: enriched client (NorthData) -> matter (auto-case) -> 3 domain flows.

        Enrichment requires creating with the NorthData externalRegistryId via the
        registry path (POST /api/customers) + the correct advisor org — that pulls
        ND + company.info (contacts/Verflechtungen/HR). A bare REST create would NOT
        enrich. Case/process creation then go via REST (customer is REST-visible).
        """
        name = (body.get("name") or "").strip()
        domain = (body.get("domain") or "ENTITY").strip().upper()
        ext_id = (body.get("externalRegistryId") or "").strip()
        if not name:
            return self._send_json({"error": "name required"}, 400)
        if not ext_id:
            return self._send_json({"error": "externalRegistryId required (für Anreicherung)"}, 400)
        is_person = domain == "PERSON"
        flows = PERSON_FLOWS if is_person else ENTITY_FLOWS
        category = "INDIVIDUAL" if is_person else "ENTITY"
        try:
            with _client_lock:
                # 1) enriched client via NorthData registry path
                payload = {
                    "clientActorExternalId": ext_id,
                    "advisorActorId": _org_id,
                    "customerCategoryType": category,
                    "clientActorName": name,
                    "domain": domain,
                    "purchaseDocuments": False,
                }
                r = requests.post(_client.base_url + "/api/customers", json=payload,
                                  headers=_client._user_headers(), verify=_client.session.verify)
                r.raise_for_status()
                cid = r.json()["businessRelationId"]
                # poll until NorthData/company.info enrichment finished
                for _ in range(30):
                    rr = requests.get(_client.base_url + "/api/customers/business-relation",
                                      params={"businessRelationId": cid},
                                      headers=_client._user_headers(), verify=_client.session.verify)
                    if rr.ok and rr.json().get("isFullyInitialized"):
                        break
                    time.sleep(2)
                # 2) matter = the auto-created case
                cases = _as_list(_client.list_cases(cid))
                case_id = cases[0]["id"] if cases else _client.create_case(cid, name)
                # 3) processes (flows) via REST
                processes = []
                for flow in flows:
                    proc = _client.create_process(cid, case_id, flow)
                    processes.append({"flow": flow, "id": proc.get("id")})
                contact_count = len(_as_list(_client.list_contacts(cid)))
                try:
                    document_count = len(_as_list(_client.list_customer_documents(cid)))
                except Exception:  # noqa: BLE001 — REST docs endpoint may 404 when empty
                    document_count = 0
                share_url = share_error = None
                try:
                    share_url = _client.create_share_link(cid, case_id, processes[0]["id"])
                except Exception as exc:  # noqa: BLE001
                    share_error = str(exc)
            return self._send_json({
                "businessRelationId": cid, "case_id": case_id, "domain": domain,
                "contact_count": contact_count, "document_count": document_count,
                "processes": processes,
                "share_url": share_url, "share_error": share_error,
            })
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": str(exc)}, 500)

    def _handle_contacts(self, body: dict):
        """List relations for a customer via GET /api/relations.

        Each item maps to: contactId, contactName, relationId (id), relation (relationType).
        """
        cid = (body.get("cid") or "").strip()
        if not cid:
            return self._send_json({"error": "cid required"}, 400)

        role_map = {
            "9010": "Legal Rep", "9030": "UBO", "9060": "Acting Person",
            "3010": "Owner", "1520": "Managing Director",
            "3040": "Shareholder", "3050": "Shareholder",
        }

        try:
            with _client_lock:
                raw = _client.list_relations(cid)

            logging.getLogger("betterco").info(
                "list_relations raw (%d items): %s", len(raw),
                [{k: item.get(k) for k in ("id", "contactId", "contactName", "relationType")} for item in raw]
            )

            grouped = {}
            for item in raw:
                rel_code = str(item.get("relationType") or "")
                contact_id = item.get("contactId") or ""
                contact_name = (item.get("contactName") or item.get("name") or "").strip()
                key = contact_name or contact_id
                if key not in grouped:
                    grouped[key] = {
                        "contactId": contact_id,
                        "contactName": contact_name,
                        "relations": [],
                    }
                grouped[key]["relations"].append({
                    "relationId": item.get("id"),
                    "relation": role_map.get(rel_code, rel_code),
                    "relationCode": rel_code,
                })

            result = list(grouped.values())
            logging.getLogger("betterco").info(
                "grouped contacts (%d): %s",
                len(result),
                [{"contactName": c["contactName"], "relationCount": len(c["relations"])} for c in result]
            )
            return self._send_json({"cid": cid, "contacts": result})
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": str(exc)}, 500)

    def _handle_contact_create(self, body: dict):
        """Create a new contact on a customer via REST PUT /customers/{cid}/contacts.

        The create-body type binding is `type` (INDIVIDUAL | ENTITY), NOT
        entityType — the read side uses contactType, but the PUT body uses `type`
        (same as create_customer). ENTITY captures companyName -> legalInfo.legalName;
        INDIVIDUAL captures firstName + lastName. `relations` is REQUIRED: without a
        role the PUT returns 201 but the contact is silently discarded, so at least
        one relationCode must be supplied.
        """
        cid = (body.get("cid") or "").strip()
        ctype = (body.get("type") or "").strip().upper()
        codes = _relation_codes(body)
        if not cid:
            return self._send_json({"error": "cid required"}, 400)
        if ctype not in ("INDIVIDUAL", "ENTITY"):
            return self._send_json({"error": "type must be INDIVIDUAL or ENTITY"}, 400)
        if not codes:
            return self._send_json(
                {"error": "mind. eine Rolle erforderlich (sonst verwirft die API den Kontakt)"}, 400)
        if ctype == "ENTITY":
            name = (body.get("companyName") or "").strip()
            if not name:
                return self._send_json({"error": "companyName erforderlich"}, 400)
            contact_body = {
                "type": "ENTITY",
                "legalInfo": {"legalName": name},
                "relations": codes,
            }
        else:
            first = (body.get("firstName") or "").strip()
            last = (body.get("lastName") or "").strip()
            if not (first and last):
                return self._send_json({"error": "firstName und lastName erforderlich"}, 400)
            full = f"{first} {last}".strip()
            contact_body = {
                "type": "INDIVIDUAL",
                "legalInfo": {"legalName": full, "firstName": first, "lastName": last},
                "firstName": first,
                "lastName": last,
                "relations": codes,
            }
        try:
            with _client_lock:
                contact_id = _client.create_contact(cid, contact_body)
            return self._send_json({"ok": True, "contactId": contact_id})
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": str(exc)}, 500)

    def _handle_contact_add_relation(self, body: dict):
        """RelationController.addRelation — add one or more roles to one contact.

        addRelation is single-role, so multiple codes are added in a loop; the
        first failure is reported (any roles added before it stay added).
        """
        cid = (body.get("cid") or "").strip()
        contact_id = (body.get("contactId") or "").strip()
        codes = _relation_codes(body)
        if not (cid and contact_id and codes):
            return self._send_json({"error": "cid, contactId und mind. eine Rolle erforderlich"}, 400)
        try:
            with _client_lock:
                results = [_client.add_contact_relation(cid, contact_id, c) for c in codes]
            return self._send_json({"ok": True, "added": len(results), "results": results})
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": str(exc)}, 500)

    def _handle_contact_delete_relation(self, body: dict):
        """RelationController — delete a relation by ID via DELETE /api/relations/{id}."""
        relation_id = (body.get("relationId") or "").strip()
        cid = (body.get("cid") or "").strip()
        if not relation_id or not cid:
            return self._send_json({"error": "relationId and cid required"}, 400)
        try:
            with _client_lock:
                _client.delete_relation(relation_id, cid)
            return self._send_json({"ok": True})
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": str(exc)}, 500)

    def _handle_search(self, qs: dict):
        query = (qs.get("q") or [""])[0].strip()
        domain = (qs.get("domain") or ["ENTITY"])[0].strip() or "ENTITY"
        if not query:
            return self._send_json({"error": "query required", "hits": []}, 400)
        try:
            with _client_lock:
                hits = _client.search_registry(query, domain=domain)
            # normalize a display subset, but keep the full raw hit for the Stammdaten panel
            results = []
            for h in hits:
                # ENTITY hits carry legalName; PERSON hits carry firstName/lastName
                name = (h.get("legalName") or h.get("name")
                        or " ".join(p for p in (h.get("firstName"), h.get("lastName")) if p).strip())
                # register line for entities (registerId + court); birthDate for persons
                if h.get("registerId"):
                    sub = " · ".join(p for p in (h.get("legalType"), h.get("registerId"),
                                                 h.get("registerCity")) if p)
                elif h.get("birthDate"):
                    sub = f"geb. {h.get('birthDate')}"
                else:
                    sub = ""
                results.append({
                    "externalRegistryId": h.get("externalRegistryId"),
                    "name": name,
                    "address": h.get("address") or h.get("registerInfo") or "",
                    "subline": sub,
                    "raw": h,
                })
            return self._send_json({"query": query, "domain": domain,
                                    "count": len(results), "hits": results})
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": str(exc), "hits": []}, 500)


def _free_port(port: int) -> None:
    """Kill any process already LISTENING on `port` before we bind.

    A stale app instance left on the port is otherwise silently kept alive by
    SO_REUSEADDR (both processes bind, the OS keeps routing to the old code) —
    so a fresh start would serve outdated routes. Best-effort; never our own PID.
    """
    me = os.getpid()
    pids = set()
    try:
        if os.name == "nt":
            # Match the listener by its wildcard foreign address, NOT the State
            # column — that column is localized (e.g. "ABHÖREN" on German Windows),
            # so a string match on "LISTENING" silently finds nothing.
            out = subprocess.run(["netstat", "-ano", "-p", "tcp"],
                                 capture_output=True, text=True).stdout
            for line in out.splitlines():
                parts = line.split()
                if (len(parts) >= 5 and parts[1].endswith(f":{port}")
                        and parts[2] in ("0.0.0.0:0", "[::]:0", "*:*")
                        and parts[4].isdigit()):
                    pids.add(int(parts[4]))
        else:
            out = subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                                 capture_output=True, text=True).stdout
            pids = {int(x) for x in out.split()}
    except Exception as exc:  # noqa: BLE001
        print(f"!! could not scan port {port}: {exc}", flush=True)
        return
    for pid in pids - {me, 0}:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
            else:
                os.kill(pid, signal.SIGKILL)
            print(f"** freed port {port}: killed stale process PID {pid}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"!! could not kill PID {pid} on port {port}: {exc}", flush=True)


def main() -> None:
    global _client, _org_id, _env_file, _status
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    ap = argparse.ArgumentParser(description="BetterCo PoC search widget server")
    ap.add_argument("--env-file", default="workspaces/editor-betterco-claude.env",
                    help="Workspace .env to authenticate with")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    _env_file = args.env_file
    # Don't hard-fail on a missing/invalid env: boot anyway so the Zugangsdaten
    # editor can capture credentials for a fresh workspace. API calls return errors
    # until a valid env is saved (which swaps in the live client).
    try:
        _org_id = _parse_env_file(args.env_file).get("BETTERCO_ORG_ID")
        _client = connect(args.env_file)
        _status = _client.verify_env()
    except (SystemExit, Exception) as exc:  # noqa: BLE001
        print(f"!! Env '{args.env_file}' not ready ({exc}). "
              f"Open the app and set credentials under 'Zugangsdaten'.")
    url = f"http://localhost:{args.port}"
    print(f"BetterCo PoC search widget -> {url}  (env: {args.env_file})")
    _free_port(args.port)   # kill any stale instance squatting the port
    if not args.no_browser:
        webbrowser.open(url)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
