# User-API → REST mapping

This guide was built pragmatically on **two** BetterCo surfaces:

| Scope | Prefix | Auth | Documented? |
|---|---|---|---|
| **REST API** | `/restapi/v1/` | `Authorization: Bearer <api-key token>` (login key+secret) | ✅ public OpenAPI (`app.betterco.ai/bcapi/betterco_api.yaml`, 167 ops) |
| **User API** | `/api/` | `Authorization: Bearer <user token>` (login email+password) + `workspaceId` header | ❌ internal `editor.betterco.ai`, not in the spec |
| **Client API** | `/api/client/` | share-link token *or* User token | ❌ internal |

The reason two APIs are in play: a handful of flow steps have **no REST equivalent today**, so the
guide reaches into the internal User API for those. This doc inventories every User-API call the guide
makes, maps each to its REST equivalent, and — where none exists — **defines the REST endpoint that
would be needed** to make the flow REST-only.

**Legend** — ✅ REST equivalent exists · ⚠️ partial (REST covers most, semantics differ) · ❌ no REST
equivalent → proposed below.

---

## 1. Calls that already have a REST equivalent

These User-API calls are used out of convenience/history, not necessity — a supported REST operation
already covers them. Moving them to REST is a straight swap.

| # | User-API call (purpose) | REST equivalent (`operationId`) | |
|---|---|---|:--:|
| 1 | `POST /auth/sign-in` (email+password login) | `POST /restapi/v1/auth/login` (`login`) — key+secret instead | ✅ |
| 2 | `GET /api/registry/search?domain=ENTITY\|PERSON&query=` (NorthData registry lookup, **step 1**) | `GET /restapi/v1/search/customers?query=&type=ENTITY\|INDIVIDUAL` (`companiesSearch`) — returns `ClientSearchResponse[]` with `externalRegistryId`, `registerCountry/City/Id`, `legalType`, `birthDate` | ✅ |
| 3 | `POST /api/customers` w/ `clientActorExternalId` (create client from registry hit) | `POST .../customers/externalSource` (`createCustomerFromExternalSource`) — body `{externalRegistryId, type, relationType}`, `?createDefaultCase=true` | ✅ |
| 4 | `GET /api/tasks/customer/{brId}` → cases/processRows (resolve case + process id) | `GET .../customers/{id}/cases` (`getCasesByCustomerId`) + `GET .../cases/{caseId}/processes` (`getProcesses`) | ✅ |
| 5 | `PATCH /api/cases/{caseId}/process/add?processName=F1600…` (add screening process to a case) | `POST .../customers/{id}/cases/{caseId}/processes` (`createProcess`) | ✅ |
| 6 | `POST .../processes/{pid}/share-link` *(already REST in the guide)* | `POST .../processes/{pid}/shares` (`createProcessShare`) | ✅ |
| 7 | `GET /api/client/onboarding/full-data?businessRelationId=` (read master data / contacts / amlProfile) | `GET .../processes/{pid}/full-data` (`getProcessFullData`) — keyed by process, `?dataScopes=` | ✅ |
| 8 | `PATCH /api/client/onboarding?...&stepId=<GwG step>` (submit a flow step) | `PATCH .../processes/{pid}/full-data` (`updateProcessFullData`) — `?taskId=&roleTypes=&action=` | ✅ |
| 9 | `POST /api/editor/processes/{pid}/tasks/{tid}/complete` (editor task complete) | covered by `updateProcessFullData` (`taskStatuses`/`action`) then `close` — no 1:1 needed | ✅ |
| 10 | `PATCH /api/questionnaire/{pid}` (close process → freeze report PDF) | `POST .../processes/{pid}/close` (`closeProcessById`) | ✅ |
| 11 | `GET /api/customers/{cid}/documents` + `<downloadUrl>` (list + download docs) | `GET .../customers/{id}/documents` (`getCustomerDocuments`) + `.../documents/{docId}/download` | ✅ |
| 12 | `GET /api/documents-zip` equivalent (all case docs as ZIP) | `GET .../customers/{id}/cases/{caseId}/documents/download` (`downloadCaseDocuments`) | ✅ |
| 13 | `GET /api/companies` (workspace customer overview) | `GET .../organizations/{org}/customers` (`getCustomers`) + `.../customers/processes` (`getOrganizationCustomerProcesses`) for the ReKYC/Screening columns | ✅ |
| 14 | `GET /api/members` (workspace team) | `GET .../workspaces/{ws}/team` (`getUsers`) | ✅ |
| 15 | `GET /api/legal-types` (legal-form code→label) | `GET /restapi/v1/types/legalforms` (`getLegalTypes`) | ✅ |
| 16 | `GET /api/relations` · `PUT`/`DELETE` (KYC contacts view) | `GET`/`PUT .../customers/{id}/contacts` (`getCustomerContacts`/`updateCustomerContact`), `DELETE .../contacts/{cid}` + `GET /restapi/v1/types/relations` | ⚠️ |
| 17 | `GET /api/import/queues` (import queue) | `GET .../organizations/{org}/import/queues` (`getImportQueueByOrganizationId`) | ✅ |
| 18 | `PATCH /restapi/.../customers/{cid}` riskSummary *(already REST)* | `PATCH .../customers/{id}` (`patchCustomer`) | ✅ |
| 19 | `GET/PATCH /api/workspaces/{ws}/settings` (workspace settings) | `GET/PATCH .../workspaces/{ws}/features` (`getWorkspaceFeatures`/`updateWorkspaceFeatures`) | ⚠️ |

**Note on ⚠️ rows** — `contacts` (16) models the KYC relationship graph as first-class REST contacts,
which is functionally equivalent for reads/writes but not a byte-identical shape. `features` (19) covers
feature flags, not every internal workspace setting; audit which settings the guide actually reads
before relying on it.

**Note on registry search (2)** — the REST operation is confusingly named "Customer search"
(`companiesSearch`), but its response schema (`ClientSearchResponse`) carries `externalRegistryId` and
register metadata, and the spec example returns `EXT-ENT-33` / `EXT-PER-12` external-registry hits — i.e.
it **is** the external NorthData-style lookup, not a search over already-imported customers. It feeds
`externalRegistryId` straight into row 3. Map internal `domain=PERSON` → REST `type=INDIVIDUAL`.

---

## 2. Calls with NO REST equivalent → proposed endpoints

Four capabilities are genuinely internal-only. Each is defined below in the spec's own conventions
(`/restapi/v1/workspaces/{ws}/organizations/{org}/…`, `Authorization: Bearer <api-key token>`). These
are the reason the guide can't be REST-only today. They all sit in the **screening lifecycle** plus the
**async-enrichment poll**.

### 2.1 Enrichment-completion status ❌

Internal: `GET /api/customers/business-relation?businessRelationId=` polled until `isFullyInitialized:true`
(~17–19 s while HR / Gesellschafterliste / contacts / Verflechtungen import).

`createCustomerFromExternalSource?asynchronous=true` kicks the enrichment off but there is **no REST way
to know when it finished** (the existing `.../workflows/{pid}/status` is process-workflow status, not
customer enrichment).

> **Proposed**
> ```
> GET /restapi/v1/workspaces/{ws}/organizations/{org}/customers/{customer_id}/enrichment-status
> → 200 { isFullyInitialized: bool, pending: [ "SHAREHOLDERS", "CONTACTS", … ] }
> ```
> `operationId: getCustomerEnrichmentStatus`. Alternatively fold an `isFullyInitialized` field into
> `getCustomerById` so callers poll the existing resource.

> **Live finding (2026-07-06):** enrichment is **async**. `createCustomerFromExternalSource` returns
> with the default case created and a *partial* contact set (~2 immediately); the full set (51 contacts
> for a real GmbH) lands ~15s later. An e2e comparison confirmed the REST create reaches the **same 51
> contacts / 3 docs** as the User-API create — but only after waiting. Since REST has no completion
> signal, reaching full enrichment currently still needs the User-API poll (`wait_enrichment`). So this
> gap is real (though minor: a REST-only caller could poll `list_contacts` until it stabilises). The
> harder blockers remain the screening trio (§2.2–2.4).

### 2.2 Trigger an AML/PEP/sanction screening ❌

Internal: `PATCH /api/client/onboarding?…&stepId=P1615_amlScreeningDefinition&roleTypes=PROCESS` (entity +
all in-scope contacts at once), and per-contact `POST /api/customers/{brId}/contacts/{cid}/screening/scan`.

REST screenings are **read-only** (`getOrganizationScreenings`, `…/changes/{since}`) plus a monitor
on/off toggle (`putCustomerOrContactOnOffScreeningMonitor`). There is no *run a scan now*.

> **Proposed**
> ```
> POST /restapi/v1/workspaces/{ws}/organizations/{org}/customers/{customer_id}/screenings
>   { rescreen: true, roleTypes: ["LEGAL_REP","UBO","ACTING_PERSON"] }   # entity + in-scope contacts
> POST …/customers/{customer_id}/contacts/{contact_id}/screenings          # single contact
> → 202 { screeningId }
> ```
> `operationId: runCustomerScreening` / `runContactScreening`. Each in-scope contact still needs a
> `birthDate` (matches the internal 400-on-missing rule).

### 2.3 Read screening match results per customer ⚠️→❌

Internal: `GET /api/customers/{cid}/search-results[?contactId=]` → `data[]` with
`match, name, score, pepTier, datasets, countries, datesOfBirth`.

`getOrganizationScreenings` lists screenings **org-wide**, not the match detail for one customer/contact,
so the flow's poll-until-populated (step 5) and per-actor verdict resolution (steps 6/9) aren't expressible.

> **Proposed**
> ```
> GET /restapi/v1/workspaces/{ws}/organizations/{org}/customers/{customer_id}/screenings/results
>     [?contactId=]
> → 200 { hasMultipleMatch: bool, data: [ { match, name, score, pepTier, datasets, countries, datesOfBirth } ] }
> ```
> `operationId: getScreeningResults`.

### 2.4 Write a screening decision ❌

Internal: `PATCH /api/contacts/{cid}/screening` (`{matchStatus}` / `{riskLevel}`) and
`POST /api/customers/{brId}/screening/details` (mark entity match / no-match).

No REST way to record a match adjudication.

> **Proposed**
> ```
> PATCH …/customers/{customer_id}/screenings/{screening_id}            { matchStatus, riskLevel }
> PATCH …/customers/{customer_id}/contacts/{contact_id}/screenings/{screening_id}  { matchStatus, riskLevel }
> → 200
> ```
> `operationId: updateScreeningDecision`. (The final risk *classification* onto the actor is already REST
> via `patchCustomer` `riskSummary` — this is specifically the per-match verdict.)

---

## 3. Out of integration scope (super-user / ops)

Used by the client for admin/diagnostics, not the onboarding flow — no REST parity expected:
`POST /api/dashboard/.../ci-enrichment` (workspace shareholder-graph backfill),
`GET /api/monitoring/heartbeat/logs|alerts`, `GET /api/monitoring/error-logs`
(REST offers only the write side, `POST .../error-logs` / `createErrorLog`).

---

## 4. REST-only readiness

Of the flow-critical User-API calls, **~15 map cleanly to REST, ~2 map partially, and 4 have no REST
equivalent.** Registry search — previously assumed to be the biggest blocker — **does exist** in REST
(`companiesSearch`); the whole front half of the flow (search → create → resolve → share → full-data
read/submit → close → documents → overview) is REST-expressible today.

The remaining gap is a single cluster: the **AML screening lifecycle**.

1. **Screening (2.2–2.4)** — REST can *read* org screenings and *toggle monitoring*, but can't *run* a
   scan, *read one customer's matches*, or *record a match decision*. These three are what still force
   the User API.
2. **Enrichment-status poll (2.1)** — minor; likely resolvable by a synchronous create or one status
   field rather than a whole endpoint.

If BetterCo added the screening trio (§2.2–2.4) plus an enrichment-status signal (§2.1) — ≈4 operations —
the guide could drop the User API and the email+password credential entirely, running on the key+secret
REST token alone.

> Sources: internal calls from `betterco_client.py` / `HTTP_REFERENCE.md`; REST operations from the public
> spec (`app.betterco.ai/bcapi/betterco_api.yaml`, 167 ops). Verify proposed shapes with BetterCo before
> building against them.

---

## 5. Migration status (implemented in this repo)

A "clean update on REST, wait for others to evolve" migration has been applied and **verified against the
live base**: per-call parity (`tests_rest_parity.py`, 12/12 incl. a guarded create/submit/delete) **and**
a full-flow equivalence run (`tests_e2e_flow.py`, 15/15) that builds the same registry company via the
OLD User-API path and the NEW REST path and asserts identical results (51 contacts / 3 docs / case /
clientType / aml + risk sections), including cross-reads via the opposite API. Both tests write only to
the editor sandbox and delete what they create; `tests_e2e_flow.py` refuses any prod/afileon env.

**Flipped to REST** (User API no longer used for these):

| Path | Client method added | REST op | Verified |
|---|---|---|:--:|
| Registry search (`/api/search`) | `search_registry_rest` | `companiesSearch` | exact id-parity, ENTITY + PERSON |
| Create from registry (`/api/create-matter`) | `create_customer_from_registry_rest` | `createCustomerFromExternalSource` | e2e vs User-API: same 51 contacts / 3 docs / case (after enrichment poll) |
| Risk step submit (`/api/risk-eval-save`) | `submit_step_spec_rest` → `submit_step_rest` | `updateProcessFullData` | 200 + field read-back |
| Risk read-back (`_risk_full_data`) | `get_full_data_rest` | `getProcessFullData` | riskProfile/riskContainer/amlProfile parity |

Two REST-vs-User-API gotchas the live tests caught: REST `updateProcessFullData` **rejects a
`taskStatuses` body** (drives completion via the `action` query param instead), and its `taskId` param is
the **task instance id, not the `taskSpec`** (a spec 404s) — hence the `submit_step_spec_rest` resolver.

The **scripted CLI (`reference_flow.py`)** was flipped to the same twins too (steps 1/3/6/7/8/9:
`search_registry_rest`, `create_customer_from_registry_rest`, `get_full_data_rest`,
`submit_step_spec_rest`) and runs green end-to-end on the editor sandbox (`--no-screen --cleanup`) with
full enrichment parity (51 contacts). One CLI-only wrinkle handled: REST `createDefaultCase` makes an
*empty* case (the User-API create auto-added the onboarding process), so the REST branch creates the
F1800/F1900 process explicitly. `create_customer_from_registry`'s `as_lead` / `purchase_documents`
options have no REST externalSource variant, so those keep the User-API path.

**Still on the User API (PENDING REST — this is the "wait for others to evolve" set):**
- Relations/contacts tab (`list_relations` / `add_contact_relation` / `delete_relation`) — REST
  `getCustomerContacts` exists but the shape differs (⚠️ §1 row 16); a rework, not a swap. (`create_contact`
  in the same tab is already REST.)
- AML screening (`run_screening` / `get_screening_matches` / `auto_screen_customer`, CLI-only) — §2.2–2.4,
  no REST trigger/read-results/decision endpoints.
- Enrichment-completion poll (§2.1) — needed for *full* enrichment (async); optional, no REST signal yet.

> ⚠️ **Vendored-client re-sync:** the `*_rest` methods were added to `betterco_client.py` **in this repo**.
> Its source of truth is the `betterco_claude_api` repo — port them back on the next sync (grep for the
> "User-API→REST migration" comment block).
