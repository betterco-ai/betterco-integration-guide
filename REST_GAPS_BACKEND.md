# REST gaps to close — backend work items

**For:** BetterCo backend · **From:** integration-guide audit, 2026-07-17 · **Updated:** 2026-07-21
**Basis:** public spec `betterco_api.yaml` (178 ops, v2.0.0) + editor host spec (**204 ops**) + live probes on `editor.betterco.ai`
**Full audit:** `REST_GAP_AUDIT.md` · **Re-check:** `python tests_rest_gaps.py`

**Goal:** let a partner run the whole KYC onboarding flow on the **REST key+secret token alone** and retire
the internal User API (email+password).

**Everything blocking that is AML screening.** As of **2026-07-21** the screening endpoints are no longer
absent — the editor host now carries 8 screening ops under the **`Customers`** tag (1:1 REST twins of the
internal `/api/…/screening/*` routes; **not yet on `app.betterco.ai`**). **But live-probed they don't work:**
`POST …/screening/scan` returns 400 "Input data is corrupted" (the provider search fires, but nothing
commits); `POST …/screening/details` rejects the decision body (400 "Invalid fields: ['attributes',…]"); and
`getCustomerById.screeningProfile` / `getOrganizationScreenings` stay empty post-scan. **So the ask below
flips from "build" to "fix the shipped endpoints."** The enrichment gap is separately closed (see §0).

> **What changed vs the 2026-07-17 spec.** This memo originally proposed brand-new `POST …/screenings`
> routes. BetterCo instead shipped the internal route shape verbatim (`…/screening/scan`,
> `…/screening/details`, `…/screening/monitor`, `…/screening/certificate`). The proposals below are kept for
> the *contract* (async, scope, acceptance) but the **path shape is now the shipped one**, and each item is
> re-scoped to the observed failure.

All paths below are relative to the existing convention:
`/restapi/v1/workspaces/{workspace_id}/organizations/{org_id}/…`, `Authorization: Bearer <api-key token>`.

---

## Priority summary

| ID | Item | Type | Priority | Effort |
|---|---|---|---|---|
| **B0** | `updateProcessFullData` silently 200s on screening fields | **Bug** | **P0** | XS |
| **G3a** | Populate the existing `ScreeningProfile` on `Actor`/`Contact` | Fix impl. to match spec | **P0** | S |
| **G1** | `POST …/screening/scan` — **commit** instead of 400 "Input data is corrupted" | **Fix shipped endpoint** | **P0** | M |
| **G3b** | Read match candidates — twin `getCustomerSearchResults` shipped; **404 until G1 commits** | Fix depends on G1 | **P1** | — |
| **G2** | `POST …/screening/details` — **accept** the decision body (rejects `attributes` today) | **Fix shipped endpoint** | **P1** | M |
| **G3c** | Candidate detail — twin `getCustomerSearchResultDetails` shipped; same 404-until-G1 | Fix depends on G1 | **P2** | — |
| **S1** | `LEAD` + document-purchase on `createCustomerFromExternalSource` | Param addition | **P2** | S |
| **S2** | Identity-document upload with `contactId` + `idDocType` | New endpoint | **P3** | S |
| **S3** | Contact `relations` read/write — **CLOSED**, shipped & verified | ✅ done | — | — |
| **S4** | Workspace settings beyond feature flags | Investigation | **P3** | S |

**Minimum to unblock a REST-only integrator: fix B0 + G1 + G3a + G2.** (G3b/G3c twins are already shipped —
they return data the moment G1 commits a scan. S3 relations and the enrichment signal are already closed.)

---

## §0 — Not a gap (no work needed)

**Enrichment-completion signal.** `REST_MAPPING.md` §2.1 asks for a new
`…/customers/{id}/enrichment-status`. **Don't build it.** `getWorkflowStatus`
(`GET /restapi/v1/workspaces/{ws}/workflows/{process_id}/status`) already returns
`ProcessState.isFullyInitialized`, and it tracks customer enrichment exactly (probed: flips `false→true` at
~25 s precisely as contacts go 2 → 12 → 51). The guide will switch its poll to this endpoint.

*Only ask:* **document** that `isFullyInitialized` on this endpoint is the post-`externalSource` enrichment
signal. It's currently undiscoverable — the field sits on `ProcessState`, whose other referent
(`MatterProcess`) is returned by no operation at all.

---

## §B0 — BUG: full-data PATCH silently discards screening fields **[P0]**

**Severity:** correctness. An integrator believes they triggered a screening; nothing happened; no error.

**Repro (editor sandbox, verified):**
```http
PATCH /restapi/v1/workspaces/{ws}/organizations/{org}/processes/{F1600_pid}/full-data
      ?taskId={P1615_amlScreeningDefinition_instance_id}&roleTypes=PROCESS&action=COMPLETE
Content-Type: application/json

{ "entityLegalInfo": {...},
  "clientType": {...},
  "amlProfile": { "screeningProfile": { "isRescreeningEnabled": true } },
  "contacts": { "contacts": [ /* each with screeningProfile.isRescreeningEnabled=true */ ] } }
```
**Actual:** `200 OK` → `{processId, businessRelationId, isNewKyc:false, isProcessClosed:false}`. No scan runs.
`riskProfile.screeningProfile` stays `{}` for 30 s+.
**Control:** byte-identical body to the internal `PATCH /api/client/onboarding?…&stepId=P1615_amlScreeningDefinition&roleTypes=PROCESS`
→ scan runs, 1 candidate (`Founders Fund`, score 94). **Same body, same process, same task — only the surface differs.**

**Root cause (likely):** `FullData` declares every branch as `additionalProperties: true`, so `amlProfile`
and `contacts` are accepted by validation and dropped by the handler. Note `isRescreeningEnabled` appears
**0×** in the published spec — it is undocumented, yet silently swallowed.

**Fix — pick one:**
- **(a) Preferred:** reject undocumented/unhandled branches with `400 Invalid fields: [...]` — the endpoint
  already does exactly this for `taskStatuses`. Consistent, and it makes G1 an honest 404-until-shipped.
- **(b)** Honour the screening fields here, making this the REST trigger (then G1 is documentation only).

**Acceptance:** the body above either runs a screening **or** returns 4xx naming the ignored fields. Never
`200` + no-op.

---

## §G1 — Run a screening **[P0 — now: fix the shipped endpoint]**

**Status 2026-07-21:** the scan endpoint **now exists** at `POST …/customers/{customer_id}/screening/scan`
(`scanCustomer`) and `…/contacts/{contact_id}/screening/scan` (`scanCustomerContact`) — but it is a verbatim
mirror of the broken internal route: it returns **`400 "Input data is corrupted"`**. Probed on a clean PEP
contact (Olaf Scholz, valid `birthDate`, relation `9010`): the provider search **does fire** — 3 candidates
are fetched and readable via the internal `search-results` — **but the `screeningProfile` never commits**, so
`getCustomerById.screeningProfile` stays `{}`. Exactly the behaviour this memo asked you **not** to
reproduce (below), now reproduced over REST.

**The reliable internal trigger has no working REST twin.** Today the guide screens via the P1615 step-submit
(`PATCH /api/client/onboarding?…&stepId=P1615_amlScreeningDefinition&roleTypes=PROCESS`), whose REST
equivalent (`updateProcessFullData`) is the **B0 silent no-op**. So neither REST path actually screens.

### Ask

Make **`POST …/screening/scan` commit** a screening (fetch + persist a `ScreeningProfile`) and return `2xx`
instead of `400 "Input data is corrupted"`. The shipped path shape is fine — keep it. Contract:
`operationId`s `scanCustomer` / `scanCustomerContact` (already published).

**Requirements**
- **Async.** Return `202` immediately; the provider scan is slow (the internal path needs a 120 s timeout on
  heavy entities). Do not block.
- **Scope** = entity + contacts whose relation codes match `roleTypes`
  (`LEGAL_REP` → `9010`,`3010`; `UBO` → `9030`,`3010`; `ACTING_PERSON` → `9060`). Pure shareholders
  (`3040`/`3050`) must **not** be screened.
- **`birthDate`** — an INDIVIDUAL contact without one can't be screened. Mirror the internal rule: `400`
  naming the offending contacts (don't silently skip).
- Must work **without** an F1600 process existing. Today the trigger is bolted to a P1615 task instance, so
  the caller must first `createProcess("F1600_RiskAMLScreening")` and resolve a task id. A customer-scoped
  endpoint should not require that dance.
- Progress must be observable → **G3a**.

**Acceptance:** on a fresh REST-created customer, `POST …/screenings` then poll `getCustomerById` until
`riskProfile.screeningProfile.lastScreeningDate` is set — no User-API call anywhere.

---

## §G3 — Read screening results **[P0/P1/P2]**

Currently a REST-only caller learns **nothing** about screening — not the verdict, not the hit count, not
even whether a scan ran.

### §G3a — Populate `ScreeningProfile` (spec-vs-impl mismatch) **[P0 — cheapest win]**

The spec **already** declares `Actor.riskProfile.screeningProfile` and `Contact.riskProfile.screeningProfile`
as `ScreeningProfile` (`matchStatus`, `riskLevel`, `totalHits`, `totalMatches`, `lastScreeningDate`,
`searchId`, `counterOfScreenings`, `certificate`, `isMonitoringEnabled`, `hitsPerCategory`, …), and
`getCustomerById` / `getCustomerContactDetails` return those types. **The implementation returns `{}`.**

**Probed, after a real screening with a live match (score 94):**
```jsonc
GET .../customers/{cid} -> riskProfile:
{ "anyPep": false,
  "aggregatedAmlRisk": "UNKNOWN",      // stays UNKNOWN until a match is adjudicated
  "amlProfile": { "isStockExchange": false },
  "screeningProfile": {} }             // <-- every declared field absent
```
Contact-level is empty too. `aggregatedAmlRisk` is not a usable substitute (it only resolves post-decision).

**Ask:** populate the declared schema on both `Actor` and `Contact`. **No new endpoint, no new schema** —
the type already exists and is already referenced by three operations. This alone makes the flow's
poll-until-populated (step 5) and per-actor verdict resolution (steps 6/9) REST-expressible.

**Acceptance:** post-scan, `getCustomerById` returns non-empty `screeningProfile` with at minimum
`lastScreeningDate`, `matchStatus`, `totalHits`, `totalMatches`, `searchId`.

### §G3b — Match candidates **[P1 — twin shipped, needs data]**

Needed to *adjudicate* — a human must see who matched before recording a decision (G2).

**Status 2026-07-21:** the REST twin **now exists** — `getCustomerSearchResults`
(`GET …/customers/{customer_id}/search-results`) and `getCustomerContactSearchResults`
(`…/contacts/{contact_id}/search-results`), both `Customers`-tagged, 1:1 with the internal route below.
Probed: `OPTIONS 200`, but `GET 404` because **no scan has committed** (blocked entirely by G1). So there is
no new endpoint to build here — **fixing G1 lights this up.** `getCustomer[Contact]SearchResultDetails`
(`…/search-results/{search_id}`) is the shipped **G3c** detail twin, same state.

**Internal today:** `GET /api/customers/{brId}/search-results[?contactId=]`

**Real payload** (probed — note `REST_MAPPING.md` §2.3 documents this shape **wrongly**):
```jsonc
{ "id": "...", "actorId": "...", "createdAt": "...", "createdBy": "...",
  "updatedAt": "...", "updatedBy": "...",
  "searchResults": {
    "data": [
      { "id": "NQZGEVkxHypHMAoNHhMSCFwl…",        // opaque candidate id
        "type": "businesses",                      // businesses | individuals
        "attributes": {
          "match": "Founders Fund",
          "name": "Founders Fund",
          "score": "94",                           // NB: string, not number
          "monitoringID": "NQZGEVkxH3oVYA5aHxc=",
          "version": "1685700166694",
          "countries": ["US"],
          "datasets": ["POI"]                      // POI | PEP_CURRENT | SAN | CORP | …
        } } ] } }
```
There is **no** `pepTier` and **no** `datesOfBirth` at this level (the earlier proposal invented them);
PEP status is conveyed via `datasets`, and DOB only appears in the candidate detail (G3c).

**Proposed — two options, (b) preferred:**
- **(a)** `GET …/customers/{customer_id}/screenings/results[?contactId=]` → `operationId: getScreeningMatchCandidates`
- **(b)** add `candidates[]` to the existing `ScreeningProfile` schema → **zero new routes**, and G3a already
  puts that object on the wire. Typed properly (`score` as number, `datasets` as an enum) rather than
  passing the provider's raw JSON:API through.

### §G3c — Candidate detail **[P2]**

**Internal:** `GET /api/customers/{brId}/details/{searchId}?contactId=` — the full Acuris profile behind one
candidate. Real keys (probed): `name`, `monitoringID`, `version`, `datasets`, `isDeleted`, `deletionReason`,
`addresses[]`, `aliases[]`, `identifiers[]`, `activities[]`, `businessTypes[]`, `businessLinks[]`,
`individualLinks[]`, `contactEntries[]`, `evidences[]` (with `originalURL`, `summary`, `captureDateISO`,
`credibility`, `keywords`), plus dataset-specific `poiEntries` / `sanEntries` / `relEntries` / `insEntries` /
`griEntries` / `rreEntries`, `profileImages[]`, `notes[]`.

**Proposed:** `GET …/customers/{customer_id}/screenings/results/{candidate_id}[?contactId=]`
→ `operationId: getScreeningCandidateDetail`. Lower priority: a decision can be made from G3b for most
cases; this is the drill-down.

---

## §G2 — Record a screening decision **[P1 — now: fix the shipped endpoint]**

**Status 2026-07-21:** the decision-write twin **now exists** — `scanCustomerDetails`
(`POST …/customers/{customer_id}/screening/details`) and `scanCustomerContactDetails`
(`…/contacts/{contact_id}/screening/details`), `Customers`-tagged, mirroring the internal `…/screening/details`
route. **But it rejects the write:** a `null` body → `400 "Input data is corrupted"`; a candidate body →
`400 "Cannot read JSON. Invalid fields: ['attributes','gender']"` — i.e. it **rejects the very
`SearchResponseData.attributes` field the spec declares** as the request schema. So the route is there; the
request contract is broken. **Fix:** accept the published `SearchResponseData` (or document the real accepted
shape) and persist the decision as `matchStatus` visible through G3a. The proposal below still states the
desired contract.

**Why it matters:** without a working decision write, `aggregatedAmlRisk` is stuck at `UNKNOWN` forever
(see G3a) and the KYC file can't be closed. `patchCustomer` is no substitute — it accepts only
`riskSummary` / `amlProfile` (`UpdateActorRequest`), and `updateProcessFullData`'s `FullData.riskProfile`
declares only `kycNote` / `aggregatedAmlRisk` / `amlRiskRelevant` — no `matchStatus` write.

**Internal today:** `PATCH /api/contacts/{cid}/screening?companyId={entity_actorId}` `{matchStatus}` /
`{riskLevel}`; `POST /api/customers/{brId}[/contacts/{cid}]/screening/details` (body literally `null`) to
mark match / no-match. *(Quirk to not reproduce: the `/contacts` route 404s for the entity actor, so the
guide writes the entity verdict through a step-submit instead.)*

### Proposed

```http
PATCH …/customers/{customer_id}/screenings/{screening_id}
PATCH …/customers/{customer_id}/contacts/{contact_id}/screenings/{screening_id}
{ "matchStatus": "NO_MATCH",     // MATCH | PARTIAL_MATCH | NO_MATCH | FALSE_POSITIVE | POTENTIAL_MATCH | UNKNOWN
  "riskLevel":   "LOW",          // LOW | MEDIUM | HIGH | UNKNOWN
  "amlNote":     "reviewed 2026-07-17, different jurisdiction" }
→ 200
```
`operationId: updateScreeningDecision`

**Requirements**
- Enums must reuse the existing `ScreeningProfile.matchStatus` / `riskLevel` — already in the spec.
- **One uniform route shape for entity and contact** (the internal asymmetry above is the single worst
  ergonomic wart in the current screening API).
- Writing a decision must update `aggregatedAmlRisk` / `anyPep` on the customer, and be visible through G3a.
- Optionally accept `candidateId` to pin the decision to one candidate when several matched.

**Acceptance:** trigger (G1) → read candidates (G3b) → `PATCH` a decision → `getCustomerById` reflects
`matchStatus` **and** a resolved `aggregatedAmlRisk`. Then `closeProcessById` succeeds. All REST.

---

## Secondary items

### §S1 — `LEAD` / document purchase on `createCustomerFromExternalSource` **[P2]**
`create_customer_from_registry(as_lead=True)` → `POST /api/leads`, and `purchase_documents=True` buys HR
documents at creation. REST `createCustomerFromExternalSource` has neither, so the CLI
(`--purchase-documents`) still falls back to the User API — the **only** non-screening reason it does.
**Ask:** `relationType: "LEAD"` (the enum already exists: `CLIENT|LEAD|INVESTOR|CONTACT`) and a
`purchaseDocuments: bool` on the existing body. Small, and it removes a whole fallback path.

### §S2 — Identity-document upload **[P3]**
`upload_id_document` → `PATCH /api/client/onboarding/documents/identity` (multipart `file` +
`{processId, contactId, idDocType}`) — the **only** call in the client needing a *process token*. REST has
`POST …/customers/{id}/documents` but no `contactId` + `idDocType` identity binding (`idDocData` exists on
`CreateContactRequest`, but not as an upload).
**Ask:** `POST …/customers/{customer_id}/contacts/{contact_id}/documents/identity` (multipart, `idDocType`).
Not reached by app/CLI today — low priority, but it's the last process-token dependency.

### §S3 — Contact `relations` parity **[CLOSED 2026-07-21 — no backend work]**
The editor host ships a relation-level sub-resource under the `Customers` tag:
`addCustomerContactRelation` (`PUT …/customers/{id}/contacts/{cid}/relations` `{relationIds:[…]}`) and
`deleteCustomerContactRelation` (`DELETE …/customers/{id}/contacts/{cid}/relations/{relation_id}`).
**Live-verified:** `PUT {relationIds:['9060']}` → `201` and the response shows `9060` **added alongside** the
existing `9010` (additive, no clobber); `DELETE …/relations/9060` → `200`. The KYC graph reads via
`getCustomerStructureChart` (`GET …/structure-chart` → `200`). **Guide-side rework only** — this retires the
app's `/api/relations` dependency (the relations tab).

### §S4 — Workspace settings **[P3 — investigation]**
`get/update_workspace_settings` (`/api/workspaces/{ws}/settings`, flat `{key: "true"|"false"}`) maps only to
`getWorkspaceFeatures`/`updateWorkspaceFeatures` — feature flags, not every setting.
**Ask:** confirm which internal settings have no `features` equivalent. Only matters for the in-app
Zugangsdaten/settings editor, not the flow.

---

## Definition of done

A partner can run the **entire** onboarding lifecycle on a key+secret token:

```
companiesSearch → createCustomerFromExternalSource → getWorkflowStatus (until isFullyInitialized)
  → createProcess → getProcessFullData → updateProcessFullData (risk answers)
  → POST …/screenings                       [G1]
  → poll getCustomerById.screeningProfile   [G3a]
  → GET candidates                          [G3b]
  → PATCH decision                          [G2]
  → closeProcessById → getCustomerDocuments
```
…with **zero** `/api/` calls and no email+password credential.

**Regression to add:** the guide's `tests_e2e_flow.py` asserts REST-vs-User-API parity end to end and would
cover G1–G3 on day one.

---

> **Evidence:** all findings probed live on `editor.betterco.ai` (2026-07-17), each run creating and deleting
> one customer. Scripts: `probe_gaps.py` (G4), `probe_g1_g3.py` (G1 + control), `probe_shapes.py` (payloads).
> Spec parsed from `app.betterco.ai/bcapi/betterco_api.yaml`.
