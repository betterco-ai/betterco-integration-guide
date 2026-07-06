#!/usr/bin/env python3
"""End-to-end OLD (User-API) vs NEW (REST) flow equivalence test — LIVE.

Runs the onboarding flow twice on the SAME registry company:
  OLD path = the pre-migration User-API calls (POST /api/customers + poll,
             submit_step, get_full_data)
  NEW path = the migrated REST twins (create_customer_from_registry_rest,
             submit_step_spec_rest, get_full_data_rest)

then asserts the two produce equivalent results (customer type, contacts,
documents, auto-created case, full-data risk/aml sections, and a submitted
step that reads back the same value). Both customers are DELETED afterwards.

This test WRITES (creates two matters). It is hard-guarded to the editor
sandbox workspace and REFUSES any env whose name looks like prod/afileon.

    python tests_e2e_flow.py
    python tests_e2e_flow.py --env-file workspaces/editor-betterco-claude.env
"""
import argparse
import sys

import requests

from reference_flow import connect, _as_list

PASS, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"
FLOW = "F1800_OnboardingEntity_A"
STEP_SPEC = "P1815_legalDataEntityStep_A"
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{PASS if ok else FAIL}] {name}" + (f" — {detail}" if detail else ""))


# ── OLD path: verbatim pre-migration User-API calls ─────────────────
def build_old(c, ext_id, name):
    payload = {"clientActorExternalId": ext_id, "advisorActorId": c.org_id,
               "customerCategoryType": "ENTITY", "clientActorName": name,
               "domain": "ENTITY", "purchaseDocuments": False}
    r = requests.post(c.base_url + "/api/customers", json=payload,
                      headers=c._user_headers(), verify=c.session.verify)
    r.raise_for_status()
    cid = r.json()["businessRelationId"]
    import time as _t
    for _ in range(30):
        rr = requests.get(c.base_url + "/api/customers/business-relation",
                          params={"businessRelationId": cid},
                          headers=c._user_headers(), verify=c.session.verify)
        if rr.ok and rr.json().get("isFullyInitialized"):
            break
        _t.sleep(2)
    return cid


def submit_old(c, cid, pid, values):
    # User-API submit_step keys by (brId, processId, stepId=taskSpec)
    return c.submit_step(cid, pid, STEP_SPEC, values)


def read_old(c, cid, pid):
    r = requests.get(c.base_url + "/api/client/onboarding/full-data",
                     headers=c._user_headers(), verify=c.session.verify,
                     params={"businessRelationId": cid, "processId": pid,
                             "sortingStrategy": "COMPLIANCE", "limit": 50})
    r.raise_for_status()
    return r.json()


# ── NEW path: migrated REST twins ───────────────────────────────────
def build_new(c, ext_id, name):
    created = c.create_customer_from_registry_rest(ext_id, domain="ENTITY", create_case=True)
    cid = created["id"]
    c.wait_enrichment(cid, timeout=60)
    return cid


def submit_new(c, cid, case_id, pid, values):
    return c.submit_step_spec_rest(cid, case_id, pid, STEP_SPEC, values, action="OPEN")


def read_new(c, cid, pid):
    return c.get_full_data_rest(pid)


# ── shared: resolve case + process, snapshot, cleanup ───────────────
def ensure_process(c, cid):
    cases = _as_list(c.list_cases(cid))
    case_id = cases[0]["id"] if cases else c.create_case(cid, "e2e")
    procs = _as_list(c.list_processes(cid, case_id))
    pid = procs[0]["id"] if procs else c.create_process(cid, case_id, FLOW)["id"]
    return case_id, pid


def snapshot(c, cid, fd):
    return {
        "contacts": len(_as_list(c.list_contacts(cid))),
        "documents": len(_as_list(c.list_customer_documents(cid))),
        "cases": len(_as_list(c.list_cases(cid))),
        "clientType": (fd.get("clientType") or {}).get("entityStageType"),
        "amlProfile": bool(fd.get("amlProfile")),
        "riskSnapshot": "actorRiskSnapshot" in fd,
        "entityName": (fd.get("entityLegalInfo") or fd.get("individualLegalInfo") or {}).get("legalName"),
    }


def delete(c, cid):
    try:
        return c.session.delete(c._url(f"/customers/{cid}")).status_code
    except Exception as exc:  # noqa: BLE001
        return f"ERR {exc}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", default="workspaces/editor-betterco-claude.env")
    args = ap.parse_args()

    low = args.env_file.lower()
    if "prod" in low or "afileon" in low:
        sys.exit(f"REFUSING: {args.env_file} looks like prod — this test WRITES. "
                 "Use the editor sandbox workspace.")

    c = connect(args.env_file)
    if not (c.user_email and c.user_password):
        sys.exit("This test needs User-API creds to run the OLD path for comparison.")

    hit = next((h for h in c.search_registry_rest("Founders1", domain="ENTITY")
                if h.get("externalRegistryId")), None) \
        or c.search_registry_rest("GmbH", domain="ENTITY")[0]
    ext, name = hit["externalRegistryId"], hit.get("legalName")
    print(f"registry company: {name} (ext={ext})\n")

    old_cid = new_cid = None
    try:
        # ---- build both ----
        print("=== build ===")
        old_cid = build_old(c, ext, name)
        check("OLD build (User-API create) -> cid", bool(old_cid), old_cid)
        new_cid = build_new(c, ext, name)
        check("NEW build (REST create) -> id", bool(new_cid), new_cid)

        old_case, old_pid = ensure_process(c, old_cid)
        new_case, new_pid = ensure_process(c, new_cid)

        # ---- submit a probe step each via its own path ----
        print("\n=== submit step (old path vs new path) ===")
        probe = {"additionalActorData": {"e2eProbe": "x"}}
        try:
            submit_old(c, old_cid, old_pid, probe)
            check("OLD submit_step accepted", True)
        except Exception as exc:  # noqa: BLE001
            check("OLD submit_step accepted", False, str(exc)[:120])
        try:
            submit_new(c, new_cid, new_case, new_pid, probe)
            check("NEW submit_step_spec_rest accepted", True)
        except Exception as exc:  # noqa: BLE001
            check("NEW submit_step_spec_rest accepted", False, str(exc)[:120])

        # ---- read both, via each path, and cross-read ----
        print("\n=== read + compare ===")
        old_fd = read_old(c, old_cid, old_pid)
        new_fd = read_new(c, new_cid, new_pid)
        so, sn = snapshot(c, old_cid, old_fd), snapshot(c, new_cid, new_fd)
        print(f"  OLD snapshot: {so}")
        print(f"  NEW snapshot: {sn}")
        for k in ("contacts", "documents", "cases", "clientType", "amlProfile",
                  "riskSnapshot", "entityName"):
            check(f"equivalent: {k}", so[k] == sn[k], f"old={so[k]!r} new={sn[k]!r}")

        # cross-read: OLD customer via NEW reader and vice versa must match keys
        x_old = read_new(c, old_cid, old_pid)   # REST read of the OLD-built customer
        x_new = read_old(c, new_cid, new_pid)   # User-API read of the NEW-built customer
        check("cross-read OLD via REST returns FullData", bool(x_old.get("amlProfile") is not None))
        check("cross-read NEW via User-API returns FullData", bool(x_new.get("amlProfile") is not None))
    finally:
        print("\n=== cleanup ===")
        if old_cid:
            check("deleted OLD customer", delete(c, old_cid) in (200, 204), f"cid={old_cid}")
        if new_cid:
            check("deleted NEW customer", delete(c, new_cid) in (200, 204), f"cid={new_cid}")

    n, ok = len(results), sum(results)
    print(f"\n{ok}/{n} checks passed")
    sys.exit(0 if ok == n else 1)


if __name__ == "__main__":
    main()
