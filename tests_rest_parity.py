#!/usr/bin/env python3
"""REST-migration parity tests against the LIVE base.

Verifies each migrated REST twin returns data equivalent to its User-API
predecessor. Read-only by default; the one mutating test (create a customer
from a registry hit) is gated behind --create and cleans up after itself.

    python tests_rest_parity.py                    # read-only parity only
    python tests_rest_parity.py --create           # + guarded create/submit/delete
    python tests_rest_parity.py --env-file workspaces/other.env

The --create path:
  1. search_registry_rest -> pick one small ENTITY hit (externalRegistryId)
  2. create_customer_from_registry_rest(create_case=True)   [WRITE]
  3. inspect: id, auto-created case, whether enrichment was synchronous
  4. submit_step_rest on a harmless master-data field, read back  [WRITE]
  5. DELETE the customer                                          [WRITE / cleanup]
It never leaves data behind on success; on failure it prints the id to delete.
"""
import argparse
import sys

from reference_flow import connect, _as_list

PASS, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"  [{PASS if ok else FAIL}] {name}" + (f" — {detail}" if detail else ""))


def read_only(c):
    print("\n=== read-only parity ===")

    # 1) registry search: REST twin vs User-API
    for dom in ("ENTITY", "PERSON"):
        q = "GmbH" if dom == "ENTITY" else "Müller"
        u = c.search_registry(q, domain=dom)
        r = c.search_registry_rest(q, domain=dom)
        uid = [h.get("externalRegistryId") for h in u]
        rid = [h.get("externalRegistryId") for h in r]
        check(f"search_registry_rest[{dom}] id-parity",
              uid == rid and len(r) > 0,
              f"user={len(u)} rest={len(r)}")
        if r:
            h = r[0]
            check(f"search_registry_rest[{dom}] normalized shape",
                  h.get("legalName") and h.get("domain")
                  and not isinstance(h.get("legalType"), dict),
                  f"legalType={h.get('legalType')!r}")

    # 2) full-data: REST twin (by process) vs User-API (by brId) on a real process
    pid = brid = None
    for cu in _as_list(c.list_customers()):
        cid = cu.get("id")
        for cs in _as_list(c.list_cases(cid)):
            ps = _as_list(c.list_processes(cid, cs.get("id")))
            if ps:
                pid, brid = ps[0].get("id"), cid
                break
        if pid:
            break
    if not pid:
        check("get_full_data_rest", False, "no process found in workspace")
        return
    rest = c.get_full_data_rest(pid)
    user = c.get_full_data(brid)
    shared = set(user) & set(rest)
    check("get_full_data_rest returns FullData", isinstance(rest, dict) and len(rest) > 5,
          f"keys={len(rest)}")
    check("get_full_data_rest superset of User-API core",
          {"amlProfile", "actorRiskSnapshot", "contactData"} <= set(rest),
          f"shared={len(shared)} of user={len(user)}")


def guarded_create(c):
    print("\n=== guarded create/submit/delete (WRITE) ===")
    hits = [h for h in c.search_registry_rest("Founders1", domain="ENTITY")
            if h.get("externalRegistryId")]
    if not hits:
        hits = c.search_registry_rest("GmbH", domain="ENTITY")
    hit = hits[0]
    print(f"  using registry hit: {hit.get('legalName')} (ext={hit.get('externalRegistryId')})")

    created = c.create_customer_from_registry_rest(
        hit["externalRegistryId"], domain="ENTITY", create_case=True)
    cid = created.get("id")
    check("create_customer_from_registry_rest -> id", bool(cid), f"id={cid}")
    if not cid:
        print("  raw response:", created)
        return
    try:
        cases = _as_list(c.list_cases(cid))
        check("createDefaultCase auto-created a case", len(cases) >= 1,
              f"cases={len(cases)}")
        contacts = _as_list(c.list_contacts(cid))
        # enrichment is ASYNC — only a partial set is present this early; the full
        # set lands ~15s later (see tests_e2e_flow.py for the enriched comparison).
        check("enrichment started (partial contacts already present)",
              len(contacts) > 0, f"contacts={len(contacts)} so far (info only)")

        if cases:
            case_id = cases[0]["id"]
            procs = _as_list(c.list_processes(cid, case_id))
            if not procs:
                proc = c.create_process(cid, case_id, "F1800_OnboardingEntity_A")
                procs = [proc]
            pid = procs[0]["id"]
            # resolve a REAL taskSpec from the process (guessing one -> 404)
            det = c.get_process(cid, case_id, pid)
            tasks = det.get("tasks") or []
            task = next((t for t in tasks if "legalData" in (t.get("taskSpec") or "")),
                        next((t for t in tasks if "Step" in (t.get("taskSpec") or "")),
                             tasks[0] if tasks else {}))
            # REST taskId = task INSTANCE id (not taskSpec — that 404s)
            task_id = task.get("id")
            check("resolved a real task instance for submit", bool(task_id),
                  f"spec={task.get('taskSpec')} id={task_id}")
            # harmless save (action=OPEN so we don't complete the step)
            try:
                ack = c.submit_step_rest(
                    pid, task_id,
                    {"additionalActorData": {"restParityProbe": "ok"}}, action="OPEN")
                check("submit_step_rest accepted (200)", isinstance(ack, dict),
                      f"ack={list(ack)[:4]}")
                fd = c.get_full_data_rest(pid)
                got = (fd.get("additionalActorData") or {}).get("restParityProbe")
                # read-back is informational: full-data PATCH may silently drop unknown fields
                print(f"  [info] submit_step_rest read-back: {got!r} "
                      f"(None is OK — unknown probe field may be dropped)")
            except Exception as exc:
                check("submit_step_rest accepted (200)", False, str(exc)[:160])
    finally:
        r = c.session.delete(c._url(f"/customers/{cid}"))
        check("cleanup: customer deleted", r.status_code in (200, 204),
              f"HTTP {r.status_code} — cid={cid}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", default="workspaces/editor-betterco-claude.env")
    ap.add_argument("--create", action="store_true", help="run the guarded WRITE test")
    args = ap.parse_args()

    c = connect(args.env_file)
    read_only(c)
    if args.create:
        guarded_create(c)

    n = len(results)
    ok = sum(results)
    print(f"\n{ok}/{n} checks passed")
    sys.exit(0 if ok == n else 1)


if __name__ == "__main__":
    main()
