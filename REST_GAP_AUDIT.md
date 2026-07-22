# REST Gap Audit — every API call in the integration guide

**Date:** 2026-07-17 · **Updated:** 2026-07-22 (12 more editor-only ops probed — see §0a) · **Scope:** `betterco_client.py`, `app.py`, `reference_flow.py`
**Checked against:** live public OpenAPI spec `app.betterco.ai/bcapi/betterco_api.yaml` (parsed, **179 operations**, `info.version: 2.0.0`) — **and** the editor host spec `editor.betterco.ai/bcapi/betterco_api.yaml` (**207 operations**, identical to `dev.betterco.ai`), which carries **28 ops the public spec does not**
**Verified against:** live `editor.betterco.ai` sandbox (each probe run creates and deletes one customer)

> ## §0a — UPDATE 2026-07-22: the reads all work, and the decision write is CLOSED
>
> The editor spec grew again (204 → **207 ops**; app: 178 → 179). Twelve editor-only ops had never been
> probed. Probing them flips the picture: **everything that reads screening data works**, and the missing
> **decision write turned up as `PATCH …/screening/profile`**. Harness verdict: **8/11 closed**
> (`python tests_rest_gaps.py`).
>
> | Op (editor-only) | Path | Live behaviour (probed 2026-07-22) |
> |---|---|---|
> | `updateCustomer[Contact]ScreeningProfile` | `PATCH …/screening/profile` | **200 — WORKS AND PERSISTS.** Body `{matchStatus, riskLevel, amlNote}`. True PATCH merge (omitted fields untouched), response echoes the *merged* profile. Verified entity + contact, pre- and post-scan, both write orders, read back via `getCustomerById` **and** User-API full-data. **This is the G2 decision write — `…/screening/details` never was.** |
> | `getCustomer[Contact]SearchResults` | `GET …/search-results` | **200 + full candidates** once a scan exists (1 entity / 3 contact). Attributes: `match, name, score, monitoringID, version, countries, datasets, gender, pepTier, profileImage, datesOfBirth`. The 404 seen on 2026-07-21 was "no scan yet", not a defect. **G3b CLOSED.** |
> | `getCustomer[Contact]SearchResultDetails` | `GET …/search-results/{id}` | **200 + the provider dossier** (addresses, aliases, businessLinks, evidences, datasets…). ⚠️ `{id}` is the **candidate id** from `searchResults.data[].id` (opaque base64), *not* a search/scan id. **G3c CLOSED.** |
> | `getCustomer[Contact]PoliticalFunctions` | `GET …/political-functions?search_id=` | **200 + PEP offices** `{current[], former[]}` (Scholz: 2 current / 10 former). |
> | `getCustomer[Contact]Remarks` | `GET …/remarks?search_id=` | **200 + risk remarks** — e.g. `["PEP Tier 1", "Financial Crime and Fraud - Tax Offences"]`. |
> | `getCustomerReport` | `GET …/reports?process_name=` | **200 — the rendered summary PDF** `{fileName, mimeType, contentBase64}` (~45 kB). `process_name` is **required** (400 without it); works for `F1600_RiskAMLScreening` and `F1800_OnboardingEntity_A`. |
> | `uploadCustomerContactIdentityDocument` | `PUT …/contacts/{ct}/identity-documents` | **201** — multipart `file` + `idDocType` + `processId`. |
> | `getCustomerContactIdentityDocuments` | `GET …/contacts/{ct}/identity-documents` | **200** — lists docs with `contentBase64` inline. Round trip verified. |
> | `listDocumentSearchJurisdictionCoverage` | `GET …/document-search/jurisdictions/coverage` | **200** — coverage per jurisdiction (registries, SLA, data fields). |
> | `getDocumentSearchJurisdictionCoverage` | `GET …/document-search/jurisdictions/{code}/coverage` | **200** — e.g. `DE`: SLA 25 min, Hybrid registry. |
> | `getCustomer[Contact]CompanyInfoAml` | `GET …/aml` | **404 "No AML info found"** — still fed only by `…/screening/details`, which still 400s. |
>
> **⚠️ `search_id` is a misnomer.** `…/political-functions` and `…/remarks` expect the **candidate id**
> (`searchResults.data[].id`). Without the parameter they return `{}` / `[]` with **HTTP 200** — which reads
> exactly like "this customer has no PEP data" and is the easiest way to wrongly declare the gap open.
>
> **Still broken (the whole remaining vendor ask, 4 items):**
> 1. `scanCustomer` / `scanCustomerContact` → **400 "Input data is corrupted"** (entity *and* a clean PEP
>    individual with valid `birthDate`). No working REST scan trigger; `run_screening` (User API, step
>    P1615) remains the only one.
> 2. `scanCustomer[Contact]Details` → **400 for every body shape**, including the **verbatim candidate
>    object** returned by `…/search-results`, its slimmed variant, and `{id, type}` alone.
> 3. `getCustomerById.riskProfile.screeningProfile` carries the **verdict** (`matchStatus`/`riskLevel`) but
>    never the **scan** side (`lastScreeningDate`, `totalHits`, `searchId`, `hitsPerCategory`) — G3a.
> 4. `getOrganizationScreenings` returns **`{}` for every customer**, scanned or adjudicated.
>
> **Net:** with `PATCH …/screening/profile` wired up (`save_aml_review_rest`), the only thing REST still
> cannot do is **start a scan** and **pull a candidate dossier into `…/aml`**.

> ## §0 — UPDATE 2026-07-21: the screening endpoints now EXIST in REST (but don't work yet)
>
> BetterCo shipped, **on the editor host only**, a family of 8 screening ops under the **`Customers`** tag —
> 1:1 REST twins of the internal `/api/…/screening/*` routes. They are **absent from `app.betterco.ai`**
> (still 178 ops / 4 screening ops). This flips the screening surface from *"entirely absent from REST"* to
> *"present in REST but non-functional as probed."*
>
> | New op (`Customers` tag) | Path | Live behaviour (probed 2026-07-21) |
> |---|---|---|
> | `scanCustomer` / `scanCustomerContact` | `POST …/screening/scan` | **400 "Input data is corrupted"** — the provider search fires (candidates get fetched) but the `screeningProfile` never commits. Faithful mirror of the internal scan bug (§G1). |
> | `scanCustomerDetails` / `…ContactDetails` | `POST …/screening/details` | **400** — null body → "Input data is corrupted"; candidate body → **"Invalid fields: ['attributes','gender']"** (rejects the very `SearchResponseData.attributes` the spec declares). |
> | `updateCustomer[Contact]ScreeningMonitoring` | `PUT …/screening/monitor` | **200** — accepted (toggle only, not a scan trigger). |
> | `getCustomer[Contact]ScreeningCertificate` | `GET …/screening/certificate` | **404** with a "no certificate found" message — routes correctly, returns a cert once one exists. |
>
> After a **real** scan (User-API), `getCustomerById.screeningProfile` is **still `{}`** and
> `getOrganizationScreenings` returns **0 rows** for the freshly-scanned customer. **So the vendor ask flips
> from "build these endpoints" to "fix the ones just shipped"** — scan must commit instead of 400, details
> must accept a decision write, and the reads must populate. Re-check: `python tests_rest_gaps.py`.
> The gap table in §2 is updated to `PARTIAL` accordingly.
>
> **The `Customers` tag also gained the READ twins that a committed scan would feed** — all present on
> editor, all documented, none on `app.betterco.ai`:
>
> | New read op | Path | Live (probed 2026-07-21) |
> |---|---|---|
> | `getCustomer[Contact]SearchResults` | `GET …/search-results` | **G3b candidate-read twin.** `OPTIONS 200`, `GET 404` (no data — blocked only by G1 never committing). |
> | `getCustomer[Contact]SearchResultDetails` | `GET …/search-results/{search_id}` | **G3c detail twin.** Same: present, no data yet. |
> | `getCustomerStructureChart` | `GET …/structure-chart` | **`GET 200`** — the KYC ownership graph (RawGraph, 52 entities / 51 rels) reads over REST **today**. |
> | `getCustomer[Contact]CompanyInfoAml` | `GET …/aml` | AML company info. `OPTIONS 200`, `GET 404` until data. |
>
> **And relations (S3) are now CLOSED** — `addCustomerContactRelation` (`PUT …/contacts/{id}/relations`
> `{relationIds}`) and `deleteCustomerContactRelation` (`DELETE …/contacts/{id}/relations/{code}`), both
> `Customers`-tagged. **Live-verified:** `PUT {relationIds:['9060']}` → `201` (adds `9060`, keeps existing
> `9010` — additive, no clobber); `DELETE …/relations/9060` → `200`. This removes the app's **second**
> remaining User-API dependency (the relations tab, §4).
>
> **Net effect on a REST-only integration:** the blocker collapses from "the whole screening lifecycle" to
> **one functional defect — the scan does not commit (G1)** — plus its decision-write sibling (G2). Every
> dependent read (profile, candidates, candidate detail), the ownership graph, and relations are all in place
> in REST on the editor host.

**Question:** for every outbound BetterCo API call the guide makes — does a REST equivalent exist, and where is one still required?

**Method:** every call site was enumerated from source (not from the prior docs), resolved to its full path
and auth surface, and matched against the spec by method + path + `operationId` **and schema**. Every claim
that could be tested was then **probed live** — because the spec declaring a field turned out **not** to mean
the API populates it (see G3).

> ⚠️ Supersedes parts of `REST_MAPPING.md` / `MIGRATION_REPORT.md`, written against a **167-op** snapshot
> (now 178). Their headline "≈4 endpoints needed" is **not** what the live system shows — see §5.

---

## 1. Headline

| | Count |
|---|---|
| Call sites in `betterco_client.py` | **83** (41 REST / 42 non-REST) |
| Client methods reached from the app or CLI ("in scope") | **27** (8 still User-API) |
| Genuine REST gaps blocking a REST-only integration | **3** — all AML screening (now *present but broken*, see §0) |
| Previously-reported gaps now **closed** | **1** (enrichment signal — no work needed) |

**Verdict:** the onboarding flow is REST-complete **except for the AML screening lifecycle**. As of 2026-07-21
the trigger/decision endpoints **exist** in REST on the editor host (§0) but **return 400 and commit
nothing**, and the results reads stay empty — so screening is still not REST-usable end to end. The
enrichment gap is closed today.

---

## 2. The gap list — probed

| # | Capability | REST today | Verdict |
|---|---|---|:--:|
| **G1** | **Trigger an AML scan** | Endpoint NOW EXISTS (`scanCustomer`/`scanCustomerContact`, `POST …/screening/scan`) but returns **400 "Input data is corrupted"** and commits nothing — see §0/§G1 | 🟡 **PARTIAL** (was ❌) |
| **G2** | **Write a match decision** | Endpoint NOW EXISTS (`scanCustomerDetails`, `POST …/screening/details`) but **400**s, rejecting the documented body (§0) | 🟡 **PARTIAL** (was ❌) |
| **G3** | **Read screening results** | `ScreeningProfile` still returns **`{}`** post-scan; `getOrganizationScreenings` returns 0 rows; scan-response would carry it but no 2xx is reachable | ❌ **STILL OPEN** |
| **G4** | **Enrichment-completion signal** | `getWorkflowStatus.isFullyInitialized` **works** | ✅ **CLOSED — no work needed** |
| **Mon** | **Screening monitoring toggle** | `updateCustomer[Contact]ScreeningMonitoring` (`PUT …/screening/monitor`) → **200** (toggle only, not a trigger) | ✅ works |

### G4 — closed. Evidence

`GET /restapi/v1/workspaces/{ws}/workflows/{process_id}/status` → `ProcessState.isFullyInitialized`
tracks customer enrichment exactly:

```
 t(s)  HTTP  isFullyInit  contacts
  5.7   200        False         2
 18.3   200        False         2
 21.0   200        False        12
 25.4   200         True        51      <-- flips exactly when enrichment lands
 51.1   200         True        51
```

`REST_MAPPING.md` §2.1 dismissed this endpoint as "process-workflow status, not customer enrichment"
without checking the schema. **`wait_enrichment` (User API) can be replaced with this poll today** — no new
endpoint, no vendor work. This removes one of the app's two remaining User-API dependencies.

### G1 — open. Evidence (with control)

The internal trigger is a full-data step submit, so it *ought* to have worked through the REST twin. It
doesn't. Replaying **the identical body** on the same customer, same F1600 process, same P1615 task instance:

| Path | Call | Result |
|---|---|---|
| **REST** | `PATCH .../processes/{pid}/full-data?taskId=<P1615>&roleTypes=PROCESS&action=COMPLETE` | **HTTP 200** — `{processId, businessRelationId, isNewKyc:false, isProcessClosed:false}` … **no scan ran** (30 s poll, `lastScreeningDate` stayed `null`) |
| **User API** (control) | `PATCH /api/client/onboarding?…&stepId=P1615_amlScreeningDefinition&roleTypes=PROCESS` | scan ran — **1 candidate** (`Founders Fund`, score 94) |

Same body, same target, only the surface differs → **the gap is the endpoint, not the payload.**

> ⚠️ **REST returns 200 and silently no-ops.** It does not reject the screening fields — it accepts and
> discards them. An integrator following the spec would reasonably believe they had triggered a screening.
> This is a correctness bug independent of the missing feature.

### G3 — open, and worse than documented. Evidence

The spec declares `Actor.riskProfile.screeningProfile` → `ScreeningProfile` (`matchStatus`, `riskLevel`,
`totalHits`, `searchId`, `certificate`, …), and `getCustomerById` returns `Actor`. **On paper the verdict is
readable.** It isn't. After a **real** screening (User-API-confirmed, 1 candidate at score 94):

```
GET .../customers/{cid}   ->   riskProfile:
{ "anyPep": false,
  "aggregatedAmlRisk": "UNKNOWN",     <-- stays UNKNOWN even post-scan
  "amlProfile": { "isStockExchange": false },
  "screeningProfile": {} }            <-- EMPTY. every field absent
```

Contact-level `screeningProfile` is empty too. So a REST-only caller learns **nothing** about screening —
not the verdict, not the hit count, not even whether a scan ever ran. `aggregatedAmlRisk` remains `UNKNOWN`
(it only resolves once a match is adjudicated), so it is **not** usable as a poll signal either.

**This vindicates the original `REST_MAPPING.md` §2.3 claim** and invalidates the schema-based reading in an
earlier draft of this audit: the declared type is not populated by the implementation.

---

## 3. Full inventory by capability

Legend: ✅ REST twin exists & in use · 🟡 twin exists, not used · ⚠️ partial · ❌ no REST · ⬜ out of scope

### 3.1 Flow-critical — already REST ✅

| Capability | Client method | REST `operationId` |
|---|---|---|
| Auth | `_ensure_auth` | `login` `POST /restapi/v1/auth/login` `{key, secret}` |
| Registry search (step 1) | `search_registry_rest` | `companiesSearch` |
| Create from registry (step 3) | `create_customer_from_registry_rest` | `createCustomerFromExternalSource` |
| **Enrichment wait (step 3)** | *(replace `wait_enrichment` with this)* | **`getWorkflowStatus`** — see G4 |
| Cases / processes (3–4) | `list_cases`, `list_processes`, `create_case`, `create_process` | `getCasesByCustomerId`, `getProcesses`, `createProcess` |
| Read full data (6/7/9) | `get_full_data_rest` | `getProcessFullData` |
| Submit step (8/9) | `submit_step_spec_rest` | `updateProcessFullData` (`?taskId=&action=`) |
| Share link (3b) | `create_share_link` | `createProcessShare` |
| Documents | `list_customer_documents`, `download_document` | `getCustomerDocuments`, `downloadCaseDocuments` |
| Close process | `force_close_process` | `closeProcessById` |
| Risk classification write | `update_customer` (`riskSummary`) | `patchCustomer` |
| Overview / team / types | `list_customers`, `get_customer` | `getCustomers`, `getUsers`, `getLegalTypes` |
| Contact create | `create_contact` | `addCustomerContact` |
| Import queue | `get_import_queue_rest` | `getImportQueueByOrganizationId` |

### 3.2 Still User-API — twins exist 🟡

| Client method | Why still used |
|---|---|
| `create_customer_from_registry` | REST `externalSource` has **no `as_lead` / `purchase_documents` variant** (CLI `--purchase-documents`). → gap candidate **S1** |
| `search_registry`, `get_full_data`, `submit_step`, `update_full_data` | dead paths kept for parity tests |
| `wait_enrichment` | **now replaceable** — G4 |

### 3.3 Still User-API — no clean REST swap

| Capability | Calls | Status |
|---|---|---|
| **Screening trigger** | `run_screening`, `scan_contact`, `scan_entity` | ❌ **G1** |
| **Screening decision** | `set_contact_match_status`, `mark_contact_match`, `mark_no_match`, `mark_entity_no_match`, `save_aml_review` | ❌ **G2** |
| **Screening read** | `get_screening_matches`, `get_aml_match_details`, `_risk_records` | ❌ **G3** |
| **Relations / KYC graph** | `list_relations`, `add_contact_relation`, `delete_relation` | ✅ **CLOSED (2026-07-21)** — `addCustomerContactRelation` (`PUT …/contacts/{id}/relations`) + `deleteCustomerContactRelation` (`DELETE …/contacts/{id}/relations/{code}`) live-verified additive (201/200, no clobber); graph via `getCustomerStructureChart` (`GET …/structure-chart`, 200). See §0. → **S3 done** |
| **ID-document upload** | `upload_id_document` — `PATCH /api/client/onboarding/documents/identity`, **process-token auth** (only such site) | ❌ REST uploads exist but not the `contactId`+`idDocType` identity binding. Unlisted in prior docs; not reached by app/CLI. → **S2** |
| **Process/task lifecycle** | `complete_task`, `close_process`, `list_customer_processes` | ⚠️ covered by `action=COMPLETE` + `closeProcessById`; note `force_close_process` uses `?force=true`, which auto-submits open steps — not identical semantics |
| **Workspace config** | `get_workspace_settings`, `update_workspace_settings`, `get_workspace_members` | ⚠️ `getUsers`/`getLegalTypes` cover members+types; **settings** map only to `getWorkspaceFeatures` (feature flags ≠ all settings) |

### 3.4 Out of scope ⬜

Super-user/ops only: `ci_enrichment`, `heartbeat_logs`, `heartbeat_alerts`, `get_error_logs`. (Error-log
**write** is REST via `post_error_log`; only read-back is super-user.)

---

## 4. What blocks a REST-only integration

The app (`app.py`) touches the User API in exactly **two** places — **both now closable on the editor host
with no vendor work**:

1. `/api/create-matter` → `wait_enrichment` — **closable today via G4** (`getWorkflowStatus`).
2. `/api/contacts`, `/api/contact-add-relation`, `/api/contact-delete-relation` → relations tab —
   **closable today via S3** (`addCustomerContactRelation` / `deleteCustomerContactRelation`, live-verified §0).

The app **never** screens. Screening is **CLI-only** (`reference_flow.py` steps 4/5/6/9).

**So the app itself can go REST-only today.** The remaining vendor ask is screening: **fix G1 (scan commit)
and G2 (decision), and populate G3a** — the reads (G3b candidates, G3c detail) already have REST twins that
light up the moment a scan commits. Detailed specs: **`REST_GAPS_BACKEND.md`**.

---

## 5. Corrections to the existing docs

| Claim | Finding |
|---|---|
| "spec = 167 operations" | Now **178**. Both docs stale. |
| §2.1 "`workflows/{pid}/status` is process-workflow status, not customer enrichment" | **Wrong.** It returns `ProcessState.isFullyInitialized`, which tracks enrichment precisely (probed). **G4 needs no vendor work.** |
| §2.3 "REST can't read one customer's matches" | **Correct — and understated.** Not only is the candidate list missing; the declared `ScreeningProfile` returns `{}` even post-screening. |
| §2.2 proposed `POST …/screenings` | Still right. Additionally: REST **silently 200s** on the screening body today rather than rejecting it. |
| §2.3 proposed response `{hasMultipleMatch, data:[{match,name,score,pepTier,datasets,countries,datesOfBirth}]}` | **Shape is wrong** — the real internal payload is `{actorId, id, searchResults:{data:[{id, type, attributes:{match,name,score,monitoringID,version,countries[],datasets[]}}]}}`. No `pepTier`/`datesOfBirth` at that level. Correct shape in `REST_GAPS_BACKEND.md` §G3. |
| §1 row 16 relations "a rework, not a swap" | **Re-examine** — `CreateContactRequest.relations` exists. |
| — | `ProcessState` is also referenced by `MatterProcess`, which **no operation returns** — dead schema branch. |

---

## 6. Reproducing

Probes (editor sandbox only, self-cleaning) are in the scratchpad: `probe_gaps.py` (G4), `probe_g1_g3.py`
(G1 + control), `probe_shapes.py` (payload shapes). Spec parsed from
`app.betterco.ai/bcapi/betterco_api.yaml`. Call sites from `betterco_client.py` (83), routes from `app.py`,
steps from `reference_flow.py`.
