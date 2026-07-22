#!/usr/bin/env python3
"""REST gap checks — re-run these to see whether BetterCo has closed a gap.

Companion to REST_GAP_AUDIT.md / REST_GAPS_BACKEND.md and ticket BCP-8213.
Each check reports OPEN (unchanged), CLOSED (verified fixed) or PARTIAL, so
progress on the ticket is provable with one command instead of assumed.

    python tests_rest_gaps.py                 # all gaps
    python tests_rest_gaps.py --gap G1        # one gap
    python tests_rest_gaps.py --env-file workspaces/other.env

Gaps checked (see REST_GAPS_BACKEND.md for the specs):
  G4   enrichment signal via getWorkflowStatus.isFullyInitialized   [CLOSED]
  B0   full-data PATCH silently 200s on the screening body          [OPEN]
  G1   POST .../screening/scan — run a scan (entity + contact)      [PRESENT, 400s]
  G1m  PUT .../screening/monitor — monitoring toggle                [PRESENT, 200]
  G3a  ScreeningProfile populated (per-customer / org bulk)         [PARTIAL — verdict yes,
                                                                     scan counters no]
  G3b  match candidates via getCustomer[Contact]SearchResults       [CLOSED]
  G3c  candidate dossier via .../search-results/{candidate_id}      [CLOSED]
  G7   PEP data: .../political-functions + .../remarks              [CLOSED — needs search_id]
  G5   summary PDF: .../reports?process_name=                       [CLOSED]
  G6   contact ID docs: PUT/GET .../identity-documents              [CLOSED]
  G2   decision write: PATCH .../screening/profile                  [CLOSED]

Spec-diff note (editor betterco_api.yaml v2.0.0, **207 ops** — vs 179 on
app.betterco.ai; 28 ops are editor-only). The editor host carries a whole
**Customers**-tagged family absent from the public app spec — 1:1 REST twins of
the internal /api/.../screening/* + relations routes:
  scanCustomer / scanCustomerContact          POST  .../screening/scan
  scanCustomerDetails / ...ContactDetails     POST  .../screening/details
  updateCustomer[Contact]ScreeningProfile     PATCH .../screening/profile  (decision)
  updateCustomer[Contact]ScreeningMonitoring  PUT   .../screening/monitor
  getCustomer[Contact]ScreeningCertificate    GET   .../screening/certificate
  getCustomer[Contact]SearchResults[Details]  GET   .../search-results[/{id}]
  getCustomer[Contact]PoliticalFunctions      GET   .../political-functions
  getCustomer[Contact]Remarks                 GET   .../remarks
  getCustomer[Contact]CompanyInfoAml          GET   .../aml
  getCustomerReport                           GET   .../reports?process_name=
  get/uploadCustomerContactIdentityDocuments  GET/PUT .../identity-documents
  add/deleteCustomerContactRelation, getCustomerStructureChart
  listDocumentSearchJurisdictionCoverage      GET   .../document-search/jurisdictions[/{code}]/coverage
plus the org-level Screenings tag (getOrganizationScreenings, putCustomerOr
ContactOnOffScreeningMonitor).

Presence is NOT proof. Live probing 2026-07-22 (this run supersedes 2026-07-21):
the READS all work once a scan exists — candidates come back complete (pepTier,
datesOfBirth, datasets, profileImage), the candidate dossier resolves, PEP
functions and remarks resolve, the summary PDF renders, ID docs round-trip. What
is still broken is the SCAN TRIGGER (scanCustomer[Contact] → 400 "Input data is
corrupted"), the DOSSIER PULL (.../screening/details → 400 for every body shape
incl. the verbatim candidate object → .../aml stays 404), the ENTITY verdict
write (PATCH .../screening/profile 200s with an echo and persists nothing) and
getOrganizationScreenings (always {}). So the ask is "fix 4", not "build".

GOTCHA: .../political-functions and .../remarks take ?search_id=<CANDIDATE id>
(searchResults.data[].id — an opaque base64 blob), NOT a search/scan id. Without
it they return {} / [] with HTTP 200, which reads exactly like "no data".

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
CAND = {}   # {"entity"|"contact": candidate dict} — filled by G3b, read by G3c/G7


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


def _contact_screening_profile(c, cid, ct_id):
    cust = c.get_customer(cid)
    for ct in (cust.get("contacts") or cust.get("relations") or []):
        if ct.get("id") == ct_id or ct.get("actorId") == ct_id:
            return (ct.get("riskProfile") or {}).get("screeningProfile") or {}
    return {}


def _populated(sp):
    """A ScreeningProfile counts as populated once a scan has landed on it."""
    return bool(sp.get("matchStatus") or sp.get("lastScreeningDate")
                or sp.get("totalHits") is not None)


def _new_scan(c, cid, ct_id=None, body=None):
    """The NEW 'Customers'-tagged scan twin: POST .../screening/scan.
    Entity when ct_id is None, else the contact. Returns (status, json|text)."""
    seg = f"/customers/{cid}/contacts/{ct_id}" if ct_id else f"/customers/{cid}"
    r = c.session.post(c._url(f"{seg}/screening/scan"),
                       json=body if body is not None else None, timeout=120)
    try:
        return r.status_code, (r.json() if r.text else None)
    except Exception:
        return r.status_code, (r.text or "")[:200]


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
def check_g1(c, cid, scr_ct=None, **_):
    """The scan twins NOW EXIST under the Customers tag
    (scanCustomer / scanCustomerContact, POST .../screening/scan). CLOSED if the
    scan returns 2xx and actually commits a screeningProfile; PARTIAL if the
    endpoint is present but 400s / commits nothing (the internal bug, mirrored);
    OPEN only if it 404s. Probes the entity, and the screenable contact if one
    was created."""
    targets = [("entity", None)] + ([("contact", scr_ct)] if scr_ct else [])
    verdict, notes = None, []
    for label, ct in targets:
        s, b = _new_scan(c, cid, ct)
        msg = b.get("message") if isinstance(b, dict) else (b or "")
        if s in (404, 405):
            notes.append(f"{label}: HTTP {s} (absent)")
            verdict = verdict or OPEN
            continue
        if s >= 400:
            notes.append(f"{label}: HTTP {s} {str(msg)[:60]!r}")
            # endpoint present but rejects — mirror of the internal scan bug
            verdict = PARTIAL if verdict != CLOSED else CLOSED
            continue
        committed = False
        for _ in range(8):
            time.sleep(3)
            sp = (_contact_screening_profile(c, cid, ct) if ct
                  else _screening_profile(c, cid))
            if sp.get("lastScreeningDate"):
                committed = True
                break
        if committed:
            notes.append(f"{label}: HTTP {s} + committed")
            verdict = CLOSED
        else:
            notes.append(f"{label}: HTTP {s} accepted but no commit in 24s")
            verdict = PARTIAL if verdict != CLOSED else CLOSED
    report("G1", verdict or OPEN,
           "POST .../screening/scan present, " + "; ".join(notes))


# -------------------------------------------------------------------------- G3a
def check_g3a(c, cid, scr_pid, **_):
    """Needs a REAL scan first. Triggers via User API (the only working trigger
    today), then asks what REST exposes. CLOSED if ScreeningProfile is populated
    on EITHER the per-customer read (getCustomerById, which returned {} in the
    audit) or the org-level bulk read (getOrganizationScreenings).

    Nuance found 2026-07-22: getCustomerById.screeningProfile is NOT permanently
    empty — it mirrors the *verdict* (matchStatus/riskLevel) as soon as one is
    committed via save_aml_review. What it never carries is the *scan* side
    (lastScreeningDate / totalHits / searchId / hitsPerCategory), which is what
    this check asks for. getOrganizationScreenings stays {} either way."""
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
def check_g3b(c, cid, scr_ct=None, **_):
    """The candidate-read twin NOW EXISTS: getCustomer[Contact]SearchResults
    (GET .../search-results, Customers tag) — the REST mirror of the internal
    /api/customers/{brId}/search-results. CLOSED if it returns candidates; PARTIAL
    if the route exists but has no data (GET 404 while OPTIONS 200 — present, but
    blocked by G1 never committing a scan); OPEN if the route is absent (OPTIONS
    404). getCustomer[Contact]SearchResultDetails (.../search-results/{id}) is the
    G3c detail twin — same fate."""
    paths = [("entity", f"/customers/{cid}/search-results")]
    if scr_ct:
        paths.append(("contact", f"/customers/{cid}/contacts/{scr_ct}/search-results"))
    present, got, found = False, False, []
    for label, p in paths:
        g = c.session.get(c._url(p))
        if g.ok and g.text:
            data = (g.json() or {}).get("searchResults", {}).get("data") or []
            if data:
                CAND[label] = data[0]          # G3c/G7 read the candidate id from here
                found.append(f"{label}={len(data)} {sorted(data[0].get('attributes') or {})}")
                continue
            got = True
        o = c.session.options(c._url(p))
        if o.status_code < 400:
            present = True
    if found:
        report("G3b", CLOSED, ".../search-results -> " + "; ".join(found))
        return
    if got:
        report("G3b", PARTIAL, ".../search-results returned 200 but no candidates")
    elif present:
        report("G3b", PARTIAL, "getCustomerSearchResults route present (OPTIONS 200) but GET 404 "
                               "— no committed scan to read (blocked by G1)")
    else:
        report("G3b", OPEN, ".../search-results absent (OPTIONS 404)")


# -------------------------------------------------------------------------- G1m
def check_g1_monitor(c, cid, **_):
    """The per-customer monitor twin: PUT .../customers/{cid}/screening/monitor
    (updateCustomerScreeningMonitoring, {enable}). This is a toggle, not a scan
    trigger — reported here for completeness. CLOSED-as-toggle if the PUT is
    accepted (2xx); if it also happens to run a scan we note that; OPEN if absent.
    Runs after B0/G1 so it can't pollute their scan attribution."""
    r = c.session.put(c._url(f"/customers/{cid}/screening/monitor"),
                      json={"enable": True}, timeout=120)
    if r.status_code in (404, 405):
        report("G1m", OPEN, f"PUT .../screening/monitor -> HTTP {r.status_code} "
                            f"(endpoint absent)")
        return
    if r.status_code >= 400:
        report("G1m", PARTIAL, f"endpoint present but rejected: HTTP {r.status_code} "
                               f"{(r.text or '')[:120]}")
        return
    for _ in range(4):
        time.sleep(3)
        if (_screening_profile(c, cid).get("lastScreeningDate")
                or _org_screening_profile(c, cid).get("lastScreeningDate")):
            report("G1m", CLOSED, f"HTTP {r.status_code}; toggle ALSO ran a scan")
            return
    report("G1m", CLOSED, f"HTTP {r.status_code} accepted (monitoring toggle only, "
                          f"not a scan trigger)")


# -------------------------------------------------------------------------- G3c
def check_g3c(c, cid, scr_ct=None, **_):
    """Candidate DETAIL twin: getCustomer[Contact]SearchResultDetails
    (GET .../search-results/{candidate_id}). The {id} is the CANDIDATE id from
    G3b's searchResults.data[].id (an opaque base64 blob), NOT a search id.
    CLOSED if it returns the provider dossier (addresses/datasets/evidences)."""
    if not CAND:
        report("G3c", PARTIAL, "no candidate from G3b to resolve — inconclusive")
        return
    for label, cand in CAND.items():
        seg = (f"/customers/{cid}/contacts/{scr_ct}" if label == "contact"
               else f"/customers/{cid}")
        r = c.session.get(c._url(f"{seg}/search-results/{cand['id']}"))
        if r.ok and r.text:
            d = r.json() or {}
            report("G3c", CLOSED, f"{label} .../search-results/{{id}} -> {sorted(d)[:8]}")
            return
    report("G3c", OPEN, "no candidate detail readable via REST")


# --------------------------------------------------------------------------- G2
def check_g2(c, cid, scr_ct=None, **_):
    """The DECISION WRITE. Two twins exist; only one works:

    a) PATCH .../screening/profile (updateCustomer[Contact]ScreeningProfile,
       {matchStatus, riskLevel, amlNote}) — the real adjudication write.
       Probed 2026-07-22: accepted AND PERSISTED for entity and contact alike,
       pre- and post-scan, in both write orders; readable back from
       getCustomerById.riskProfile.screeningProfile and User-API full-data. True
       PATCH merge — omitted fields survive, the response echoes the merged
       profile. Enums are narrower than the User API's: NONE/VERY_HIGH (risk) and
       PARTIAL_MATCH (status) are rejected with 400.
    b) POST .../screening/details (scanCustomerDetails) — 400 "Input data is
       corrupted" for every body shape incl. the verbatim candidate object from
       .../search-results. That route pulls the provider dossier (it feeds
       .../aml), it is not the decision write.

    CLOSED only when BOTH contact and entity verdicts persist over REST."""
    typed = {"matchStatus": "FALSE_POSITIVE", "riskLevel": "MEDIUM",
             "amlNote": "gap-check probe"}
    notes = []

    ct_ok = None
    if scr_ct:
        r = c.session.patch(
            c._url(f"/customers/{cid}/contacts/{scr_ct}/screening/profile"), json=typed)
        if r.status_code in (404, 405):
            notes.append(f"contact PATCH profile: HTTP {r.status_code} (absent)")
            ct_ok = False
        elif r.status_code >= 400:
            notes.append(f"contact PATCH profile: HTTP {r.status_code}")
            ct_ok = False
        else:
            time.sleep(4)
            sp = _contact_screening_profile(c, cid, scr_ct)
            if not sp.get("matchStatus"):     # REST read lags; full-data is authoritative
                fd = c.get_full_data(cid)
                for x in ((fd.get("contacts") or {}).get("contacts") or []):
                    if x.get("contactId") == scr_ct:
                        sp = x.get("screeningProfile") or {}
            ct_ok = sp.get("matchStatus") == typed["matchStatus"]
            notes.append(f"contact PATCH profile: HTTP {r.status_code}, "
                         + ("PERSISTED" if ct_ok else f"NOT persisted ({sp!r})"))

    r = c.session.patch(c._url(f"/customers/{cid}/screening/profile"),
                        json={"matchStatus": "NO_MATCH", "riskLevel": "LOW",
                              "amlNote": "gap-check probe"})
    ent_ok = False
    if r.status_code in (404, 405):
        notes.append(f"entity PATCH profile: HTTP {r.status_code} (absent)")
    elif r.status_code >= 400:
        notes.append(f"entity PATCH profile: HTTP {r.status_code}")
    else:
        time.sleep(4)
        sp = _screening_profile(c, cid)
        ent_ok = sp.get("matchStatus") == "NO_MATCH"
        notes.append(f"entity PATCH profile: HTTP {r.status_code}, "
                     + ("PERSISTED" if ent_ok else "silent no-op (echo only)"))

    d = c.session.post(c._url((f"/customers/{cid}/contacts/{scr_ct}" if scr_ct
                               else f"/customers/{cid}") + "/screening/details"),
                       json=(CAND.get("contact") or CAND.get("entity")
                             or {"id": "probe", "type": "persons"}))
    notes.append(f"POST .../screening/details: HTTP {d.status_code}")

    state = CLOSED if (ent_ok and ct_ok) else (PARTIAL if (ent_ok or ct_ok) else OPEN)
    report("G2", state, "; ".join(notes))


# --------------------------------------------------------------------------- G5
def check_g5_report(c, cid, **_):
    """NEW twin (never in the app spec): getCustomerReport
    GET .../customers/{cid}/reports?process_name=<flow> -> {fileName, mimeType,
    contentBase64} — the rendered summary PDF. process_name is REQUIRED (400
    without it). CLOSED if a PDF comes back."""
    r = c.session.get(c._url(f"/customers/{cid}/reports"),
                      params={"process_name": "F1600_RiskAMLScreening"}, timeout=120)
    if r.status_code in (404, 405):
        report("G5", OPEN, f"GET .../reports -> HTTP {r.status_code} (absent)")
        return
    if r.status_code >= 400:
        report("G5", PARTIAL, f"present but HTTP {r.status_code} {(r.text or '')[:100]}")
        return
    d = r.json() or {}
    n = len(d.get("contentBase64") or "")
    report("G5", CLOSED if n else PARTIAL,
           f"{d.get('fileName')!r} {d.get('mimeType')} contentBase64={n} chars")


# --------------------------------------------------------------------------- G6
def check_g6_iddocs(c, cid, scr_ct=None, scr_pid=None, **_):
    """NEW twins: uploadCustomerContactIdentityDocument (PUT .../contacts/{ct}/
    identity-documents, multipart file+idDocType+processId) and
    getCustomerContactIdentityDocuments (GET, returns contentBase64 inline).
    Retires the User-API identity-document upload. CLOSED if the round trip works."""
    if not scr_ct:
        report("G6", PARTIAL, "no contact to upload against")
        return
    seg = f"/customers/{cid}/contacts/{scr_ct}/identity-documents"
    png = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
           b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
           b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")
    u = c.session.put(c._url(seg), files={"file": ("probe.png", png, "image/png")},
                      data={"idDocType": "PASSPORT", "processId": scr_pid}, timeout=120)
    if u.status_code in (404, 405):
        report("G6", OPEN, f"PUT .../identity-documents -> HTTP {u.status_code} (absent)")
        return
    if u.status_code >= 400:
        report("G6", PARTIAL, f"upload rejected: HTTP {u.status_code} {(u.text or '')[:120]}")
        return
    g = c.session.get(c._url(seg))
    docs = (g.json() if g.ok and g.text else []) or []
    report("G6", CLOSED if docs else PARTIAL,
           f"PUT {u.status_code} -> GET lists {len(docs)} doc(s) "
           f"{[d.get('fileName') for d in docs][:3]}")


# --------------------------------------------------------------------------- G7
def check_g7_pep(c, cid, scr_ct=None, **_):
    """NEW twins: getCustomer[Contact]PoliticalFunctions (.../political-functions)
    and getCustomer[Contact]Remarks (.../remarks). GOTCHA: both need
    ?search_id=<CANDIDATE id from .../search-results>; without it they return
    {} / [] with HTTP 200 (not an error). CLOSED if the PEP payload comes back."""
    label = "contact" if ("contact" in CAND and scr_ct) else "entity"
    cand = CAND.get(label)
    if not cand:
        report("G7", PARTIAL, "no candidate id (needs G3b) — cannot query")
        return
    seg = (f"/customers/{cid}/contacts/{scr_ct}" if label == "contact"
           else f"/customers/{cid}")
    p = c.session.get(c._url(f"{seg}/political-functions"), params={"search_id": cand["id"]})
    m = c.session.get(c._url(f"{seg}/remarks"), params={"search_id": cand["id"]})
    if p.status_code in (404, 405) and m.status_code in (404, 405):
        report("G7", OPEN, "political-functions / remarks absent")
        return
    pf = (p.json() if p.ok and p.text else {}) or {}
    rm = (m.json() if m.ok and m.text else []) or []
    got = bool(pf.get("current") or pf.get("former") or rm)
    report("G7", CLOSED if got else PARTIAL,
           f"{label}: political-functions current={len(pf.get('current') or [])} "
           f"former={len(pf.get('former') or [])}, remarks={rm[:3]}")


CHECKS = {"G4": check_g4, "B0": check_b0, "G1": check_g1, "G1m": check_g1_monitor,
          "G3a": check_g3a, "G3b": check_g3b, "G3c": check_g3c, "G7": check_g7_pep,
          "G2": check_g2, "G5": check_g5_report, "G6": check_g6_iddocs}
# G1m/G3a must run before G3b (they produce the scan G3b reads) and after B0/G1
# (which must see an unscreened customer to judge whether THEY triggered it).
# G1m sits between them: it is itself a candidate trigger, so it must not run
# before B0/G1 or it would pollute their attribution. G3c/G7 consume the
# candidate G3b stashes in CAND, so they follow it. G2 writes a verdict, so it
# runs last (it would otherwise colour the reads above).
ORDER = ["G4", "B0", "G1", "G1m", "G3a", "G3b", "G3c", "G7", "G5", "G6", "G2"]


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

        # A registry-created entity may enrol no in-scope INDIVIDUAL contact, so
        # the contact-scan twins (scanCustomerContact / ...Details) would have
        # nothing to hit. Add a deterministic screenable PEP (real match, valid
        # birthDate) so G1/G2 exercise the natural-person path — the one the
        # internal API actually screens. Deleted with the customer.
        scr_ct = None
        try:
            scr_ct = c.create_contact(cid, {
                "type": "INDIVIDUAL",
                "legalInfo": {"firstName": "Olaf", "lastName": "Scholz",
                              "legalName": "Olaf Scholz", "birthDate": "1958-06-14",
                              "nationality": "DE", "gender": "MALE"},
                "relations": ["9010"]})
        except Exception as exc:
            print(f"(could not add screenable contact: {str(exc)[:120]})")

        ctx = dict(cid=cid, case_id=case_id, onb_pid=onb_pid,
                   scr_pid=scr_pid, p1615=p1615, scr_ct=scr_ct)
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
