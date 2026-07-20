#!/usr/bin/env python3
"""REST gap checks — re-run these to see whether BetterCo has closed a gap.

Companion to REST_GAP_AUDIT.md / REST_GAPS_BACKEND.md and ticket BCP-8213.
Each check reports OPEN (unchanged), CLOSED (verified fixed) or PARTIAL, so
progress on the ticket is provable with one command instead of assumed.

    python tests_rest_gaps.py                 # all gaps
    python tests_rest_gaps.py --gap G1        # one gap
    python tests_rest_gaps.py --env-file workspaces/other.env

Gaps checked (see REST_GAPS_BACKEND.md for the specs):
  G4   enrichment signal via getWorkflowStatus.isFullyInitialized   [expected CLOSED]
  B0   full-data PATCH silently 200s on the screening body          [expected OPEN]
  G1   POST .../screenings — run a scan                             [expected OPEN]
  G1m  PATCH .../screenings/monitor — does enabling monitoring scan? [spec v2.0.0 NEW]
  G3a  ScreeningProfile populated on Actor/Contact after a scan     [spec v2.0.0 NEW read]
  G3b  match candidates readable over REST                          [expected OPEN]
  G2   PATCH .../screenings/{id} — record a decision                [expected OPEN]

Spec-diff note (betterco_api.yaml v2.0.0, 188 ops): a whole org-level
`Screenings` tag appeared that the original audit never probed —
getOrganizationScreenings (GET .../organizations/{org}/screenings) exposes a
CustomerScreeningProfile.screeningData (matchStatus/riskLevel/totalHits/...),
and putCustomerOrContactOnOffScreeningMonitor (PATCH .../screenings/monitor)
is a candidate scan trigger. G3a/G3b now also read this org-level surface, and
G1m probes the monitor toggle. Presence in the spec is NOT proof (this API has
declared ScreeningProfile fields that returned {} live) — hence the live probe.

Writes ONE throwaway customer and deletes it in a finally block. Editor
sandbox only — refuses any prod/afileon env.
"""
import argparse
import sys
import time

from reference_flow import connect, _as_list

OPEN, CLOSED, PARTIAL = "\033[31mOPEN\033[0m", "\033[32mCLOSED\033[0m", "\033[33mPARTIAL\033[0m"
ROLES = ("LEGAL_REP", "UBO", "ACTING_PERSON")
verdicts = {}


def report(gap, state, detail=""):
    verdicts[gap] = state
    print(f"  [{state}] {gap}" + (f" — {detail}" if detail else ""))


def _guard(c, env_file):
    """Never let these writes touch a real workspace."""
    bad = [t for t in ("prod", "afileon") if t in env_file.lower()]
    if bad or "editor" not in (c.base_url or ""):
        sys.exit(f"REFUSING: gap checks are editor-sandbox only "
                 f"(env={env_file!r}, base_url={c.base_url!r})")


def _workflow_status(c, pid):
    """getWorkflowStatus — GET /restapi/v1/workspaces/{ws}/workflows/{pid}/status.
    Not wrapped by the client; this is the G4 signal."""
    r = c.session.get(
        f"{c.base_url}/restapi/v1/workspaces/{c.workspace_id}/workflows/{pid}/status")
    return (r.json() if r.ok and r.text else {})


def _screening_profile(c, cid):
    return ((c.get_customer(cid).get("riskProfile") or {}).get("screeningProfile") or {})


def _org_screening_profile(c, cid):
    """Read the customer's ScreeningProfile from the ORG-level bulk endpoint
    getOrganizationScreenings (GET .../organizations/{org}/screenings) — the
    surface the audit never probed. Returns the ScreeningProfile (screeningData)
    for `cid`, or {} if absent. Follows pagination, bounded."""
    url, params = c._url("/screenings"), {"entity_type": "ALL", "size": 200, "page": 0}
    for _ in range(10):
        r = c.session.get(url, params=params)
        if not (r.ok and r.text):
            return {}
        data = r.json()
        for row in (data.get("results") or []):
            if row.get("customerId") == cid:
                return row.get("screeningData") or {}
        url, params = data.get("next"), None  # next carries page/size itself
        if not url:
            break
    return {}


def _populated(sp):
    """A ScreeningProfile counts as populated once a scan has landed on it."""
    return bool(sp.get("matchStatus") or sp.get("lastScreeningDate")
                or sp.get("totalHits") is not None)


# --------------------------------------------------------------------------- G4
def check_g4(c, cid, onb_pid, **_):
    """CLOSED if isFullyInitialized flips true as enrichment lands."""
    seen, contacts = None, 0
    for i in range(20):
        st = _workflow_status(c, onb_pid)
        if not st:
            report("G4", OPEN, "getWorkflowStatus returned nothing")
            return
        contacts = len(_as_list(c.list_contacts(cid)))
        if st.get("isFullyInitialized"):
            seen = i * 2
            break
        time.sleep(2)
    if seen is None:
        report("G4", OPEN, f"isFullyInitialized never true (contacts={contacts})")
    else:
        report("G4", CLOSED, f"isFullyInitialized true at ~{seen}s, contacts={contacts} "
                             f"— wait_enrichment (User API) is replaceable")


# --------------------------------------------------------------------------- B0
def check_b0(c, cid, scr_pid, p1615, **_):
    """OPEN while REST returns 2xx for the screening body without running a scan.
    CLOSED if it either rejects the body (4xx) or actually screens."""
    fd = c.get_full_data_rest(scr_pid)
    contacts = c._inscope_contacts((fd.get("contacts") or {}).get("contacts") or [], ROLES)
    for ct in contacts:
        ct.setdefault("screeningProfile", {})["isRescreeningEnabled"] = True
    body = {
        "entityLegalInfo": fd.get("entityLegalInfo"),
        "clientType": fd.get("clientType"),
        "amlProfile": {"screeningProfile": {"isRescreeningEnabled": True}},
        "contacts": {"contacts": contacts},
    }
    r = c.session.patch(
        c._url(f"/processes/{scr_pid}/full-data"),
        params={"taskId": p1615, "roleTypes": "PROCESS", "action": "COMPLETE"},
        json=body, timeout=120)
    if r.status_code >= 400:
        report("B0", CLOSED, f"REST now rejects the screening body ({r.status_code}) "
                             f"instead of silently accepting it")
        return
    # accepted — did anything actually happen?
    for _ in range(8):
        time.sleep(3)
        if _screening_profile(c, cid).get("lastScreeningDate"):
            report("B0", CLOSED, "REST honoured the screening body and ran a scan")
            return
    report("B0", OPEN, f"HTTP {r.status_code} + no scan after 24s — silent no-op")


# --------------------------------------------------------------------------- G1
def check_g1(c, cid, **_):
    """CLOSED if POST .../customers/{cid}/screenings exists and triggers a scan."""
    r = c.session.post(c._url(f"/customers/{cid}/screenings"),
                       json={"rescreen": True, "roleTypes": list(ROLES)}, timeout=120)
    if r.status_code in (404, 405):
        report("G1", OPEN, f"POST .../customers/{{id}}/screenings -> HTTP {r.status_code} "
                           f"(endpoint does not exist)")
        return
    if r.status_code >= 400:
        report("G1", PARTIAL, f"endpoint exists but rejected: HTTP {r.status_code} "
                              f"{(r.text or '')[:120]}")
        return
    for _ in range(10):
        time.sleep(3)
        if _screening_profile(c, cid).get("lastScreeningDate"):
            report("G1", CLOSED, f"HTTP {r.status_code} and a scan ran")
            return
    report("G1", PARTIAL, f"HTTP {r.status_code} but no scan observed within 30s")


# -------------------------------------------------------------------------- G3a
def check_g3a(c, cid, scr_pid, **_):
    """Needs a REAL scan first. Triggers via User API (the only working trigger
    today), then asks what REST exposes. CLOSED if ScreeningProfile is populated
    on EITHER the per-customer read (getCustomerById, which returned {} in the
    audit) or the NEW org-level bulk read (getOrganizationScreenings)."""
    try:
        c.run_screening(cid, scr_pid)
    except Exception as exc:
        report("G3a", OPEN, f"could not trigger a scan to test against: {str(exc)[:120]}")
        return
    rows = []
    for _ in range(12):
        time.sleep(3)
        rows = c._match_rows(c.get_screening_matches(cid))
        if rows:
            break
    if not rows:
        report("G3a", OPEN, "no scan result even via User API — inconclusive")
        return

    per_customer = _screening_profile(c, cid)
    org = {}
    for _ in range(6):  # give the org-level projection time to catch up
        org = _org_screening_profile(c, cid)
        if _populated(org):
            break
        time.sleep(3)

    if _populated(per_customer):
        report("G3a", CLOSED, f"per-customer screeningProfile populated: {sorted(per_customer)[:6]}")
    elif _populated(org):
        report("G3a", CLOSED, f"NEW org-level getOrganizationScreenings exposes it "
                              f"(getCustomerById still empty): {sorted(org)[:6]}")
    else:
        report("G3a", OPEN, f"both reads empty despite {len(rows)} candidate(s) "
                            f"— per-customer={per_customer!r} org={org!r}")


# -------------------------------------------------------------------------- G3b
def check_g3b(c, cid, **_):
    """CLOSED if candidates are readable over REST — as ScreeningProfile.candidates[]
    on EITHER the per-customer or the org-level read (the v2.0.0 ScreeningProfile
    schema still declares only counters + searchId, no candidates[]), or a results
    endpoint."""
    for label, sp in (("per-customer", _screening_profile(c, cid)),
                      ("org-level", _org_screening_profile(c, cid))):
        if sp.get("candidates"):
            report("G3b", CLOSED, f"{label} ScreeningProfile.candidates[] present "
                                  f"({len(sp['candidates'])} rows)")
            return
    r = c.session.get(c._url(f"/customers/{cid}/screenings/results"))
    if r.status_code in (404, 405):
        report("G3b", OPEN, "no candidates[] on ScreeningProfile (per-customer or org) "
                            f"and .../screenings/results -> HTTP {r.status_code}")
    elif r.ok:
        report("G3b", CLOSED, f".../screenings/results -> HTTP 200")
    else:
        report("G3b", PARTIAL, f".../screenings/results -> HTTP {r.status_code}")


# -------------------------------------------------------------------------- G1m
def check_g1_monitor(c, cid, **_):
    """v2.0.0 NEW surface the audit missed: PATCH .../screenings/monitor
    (putCustomerOrContactOnOffScreeningMonitor). Enabling monitoring may enrol the
    customer AND kick off an initial scan. CLOSED if the PATCH is accepted AND a
    scan lands; PARTIAL if accepted but no scan (toggle only); OPEN if absent.
    Runs after B0/G1 (which need an unscreened customer to judge attribution)."""
    r = c.session.patch(c._url("/screenings/monitor"),
                        json={"customerId": cid, "enable": True}, timeout=120)
    if r.status_code in (404, 405):
        report("G1m", OPEN, f"PATCH .../screenings/monitor -> HTTP {r.status_code} "
                            f"(endpoint absent)")
        return
    if r.status_code >= 400:
        report("G1m", PARTIAL, f"endpoint exists but rejected: HTTP {r.status_code} "
                               f"{(r.text or '')[:120]}")
        return
    for _ in range(10):
        time.sleep(3)
        if (_screening_profile(c, cid).get("lastScreeningDate")
                or _org_screening_profile(c, cid).get("lastScreeningDate")):
            report("G1m", CLOSED, f"HTTP {r.status_code}; enabling monitoring ran a scan")
            return
    report("G1m", PARTIAL, f"HTTP {r.status_code} accepted but no scan within 30s "
                           f"(monitoring toggled, not a trigger)")


# --------------------------------------------------------------------------- G2
def check_g2(c, cid, **_):
    """The one gap with nothing to call. Probe for the proposed route's existence
    only — a 404 confirms it's still absent. Deliberately does not write."""
    r = c.session.patch(c._url(f"/customers/{cid}/screenings/probe-nonexistent-id"),
                        json={"matchStatus": "NO_MATCH"})
    if r.status_code in (404, 405):
        report("G2", OPEN, f"PATCH .../screenings/{{id}} -> HTTP {r.status_code} "
                           f"(no decision-write endpoint)")
    elif r.status_code == 400:
        report("G2", PARTIAL, "route appears to exist (400 on a bogus id) — verify by hand")
    else:
        report("G2", PARTIAL, f"unexpected HTTP {r.status_code} — verify by hand")


CHECKS = {"G4": check_g4, "B0": check_b0, "G1": check_g1, "G1m": check_g1_monitor,
          "G3a": check_g3a, "G3b": check_g3b, "G2": check_g2}
# G1m/G3a must run before G3b (they produce the scan G3b reads) and after B0/G1
# (which must see an unscreened customer to judge whether THEY triggered it).
# G1m sits between them: it is itself a candidate trigger, so it must not run
# before B0/G1 or it would pollute their attribution.
ORDER = ["G4", "B0", "G1", "G1m", "G3a", "G3b", "G2"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", default="workspaces/editor-betterco-claude.env")
    ap.add_argument("--gap", choices=ORDER, help="run a single gap check")
    args = ap.parse_args()

    c = connect(args.env_file)
    _guard(c, args.env_file)
    print(f"base={c.base_url} ws={c.workspace_id}")

    hits = [h for h in c.search_registry_rest("Founders1", domain="ENTITY")
            if h.get("externalRegistryId")]
    if not hits:
        hits = [h for h in c.search_registry_rest("GmbH", domain="ENTITY")
                if h.get("externalRegistryId")]
    hit = hits[0]
    cid = c.create_customer_from_registry_rest(
        hit["externalRegistryId"], domain="ENTITY", create_case=True).get("id")
    if not cid:
        sys.exit("could not create a probe customer")
    print(f"probe customer: {hit.get('legalName')} cid={cid}\n")

    try:
        case_id = _as_list(c.list_cases(cid))[0]["id"]
        procs = _as_list(c.list_processes(cid, case_id))
        if not procs:
            procs = [c.create_process(cid, case_id, "F1800_OnboardingEntity_A")]
        onb_pid = procs[0]["id"]

        scr_pid = c.create_process(cid, case_id, "F1600_RiskAMLScreening")["id"]
        tasks = c.get_process(cid, case_id, scr_pid).get("tasks") or []
        p1615 = next((t["id"] for t in tasks
                      if "P1615" in (t.get("taskSpec") or "")), None)

        ctx = dict(cid=cid, case_id=case_id, onb_pid=onb_pid,
                   scr_pid=scr_pid, p1615=p1615)
        for gap in ([args.gap] if args.gap else ORDER):
            if gap in ("B0",) and not p1615:
                report(gap, PARTIAL, "no P1615 task instance — cannot probe")
                continue
            try:
                CHECKS[gap](c, **ctx)
            except Exception as exc:
                report(gap, PARTIAL, f"check errored: {str(exc)[:140]}")
    finally:
        r = c.session.delete(c._url(f"/customers/{cid}"))
        print(f"\ncleanup: customer deleted -> HTTP {r.status_code} (cid={cid})")

    closed = sum(1 for v in verdicts.values() if v == CLOSED)
    print(f"\n{closed}/{len(verdicts)} gaps closed — see BCP-8213")
    # exit 0 always: an OPEN gap is a finding, not a test failure.


if __name__ == "__main__":
    main()
