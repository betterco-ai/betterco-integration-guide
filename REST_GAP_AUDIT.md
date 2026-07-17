# REST Gap Audit — every API call in the integration guide

**Date:** 2026-07-17 · **Scope:** `betterco_client.py`, `app.py`, `reference_flow.py`
**Checked against:** live public OpenAPI spec `app.betterco.ai/bcapi/betterco_api.yaml` (parsed, **178 operations**, `info.version: 2.0.0`)
**Verified against:** live `editor.betterco.ai` sandbox (3 probe runs, each created and deleted one customer)

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
| Genuine REST gaps blocking a REST-only integration | **3** — all AML screening |
| Previously-reported gaps now **closed** | **1** (enrichment signal — no work needed) |

**Verdict:** the onboarding flow is REST-complete **except for the AML screening lifecycle**, which is
*entirely* absent from REST — trigger, results, and decision. The enrichment gap is closed today.

---

## 2. The gap list — probed

| # | Capability | REST today | Verdict |
|---|---|---|:--:|
| **G1** | **Trigger an AML scan** | Silently accepts and does nothing — see evidence | ❌ **REQUIRED** |
| **G2** | **Write a match decision** | No endpoint; `patchCustomer` takes only `riskSummary`/`amlProfile` | ❌ **REQUIRED** |
| **G3** | **Read screening results** | `ScreeningProfile` schema exists but returns **`{}`** post-screening | ❌ **REQUIRED** (worse than documented) |
| **G4** | **Enrichment-completion signal** | `getWorkflowStatus.isFullyInitialized` **works** | ✅ **CLOSED — no work needed** |

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
| **Relations / KYC graph** | `list_relations`, `add_contact_relation`, `delete_relation` | ⚠️ **Re-examine** — `CreateContactRequest` declares a `relations` property and `getRelationTypes`/`getRelationCategories` exist, so REST may model this better than "shape differs" implies. Not a blocker. → **S3** |
| **ID-document upload** | `upload_id_document` — `PATCH /api/client/onboarding/documents/identity`, **process-token auth** (only such site) | ❌ REST uploads exist but not the `contactId`+`idDocType` identity binding. Unlisted in prior docs; not reached by app/CLI. → **S2** |
| **Process/task lifecycle** | `complete_task`, `close_process`, `list_customer_processes` | ⚠️ covered by `action=COMPLETE` + `closeProcessById`; note `force_close_process` uses `?force=true`, which auto-submits open steps — not identical semantics |
| **Workspace config** | `get_workspace_settings`, `update_workspace_settings`, `get_workspace_members` | ⚠️ `getUsers`/`getLegalTypes` cover members+types; **settings** map only to `getWorkspaceFeatures` (feature flags ≠ all settings) |

### 3.4 Out of scope ⬜

Super-user/ops only: `ci_enrichment`, `heartbeat_logs`, `heartbeat_alerts`, `get_error_logs`. (Error-log
**write** is REST via `post_error_log`; only read-back is super-user.)

---

## 4. What blocks a REST-only integration

The app (`app.py`) touches the User API in exactly **two** places:

1. `/api/create-matter` → `wait_enrichment` — **closable today via G4, no vendor work.**
2. `/api/contacts`, `/api/contact-add-relation`, `/api/contact-delete-relation` → relations tab (S3).

The app **never** screens. Screening is **CLI-only** (`reference_flow.py` steps 4/5/6/9).

**So: G1 + G2 + G3 are the entire vendor ask.** Ship those and the guide drops the email+password
credential. Detailed specs: **`REST_GAPS_BACKEND.md`**.

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
