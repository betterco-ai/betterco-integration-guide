# BetterCo REST API — Onboarding Integration Coverage

**Subject:** Can the public BetterCo **REST API** (`/restapi/v1/`, key+secret) drive the full KYC
"Mandanten-Neuannahme" onboarding flow on its own — without the internal User API?

**Answer: yes, for the entire onboarding flow.** This reference app previously used two BetterCo
surfaces — the public **REST API** and the internal **User API** (`/api/…`, email+password, not in the
public spec). We migrated every onboarding step that had a REST equivalent. The core flow now runs on the
**REST key+secret token alone**, verified live against `editor.betterco.ai` and against the published
OpenAPI spec (`app.betterco.ai/bcapi/betterco_api.yaml`, 167 operations).

The only capabilities still requiring the internal User API are **AML screening** (no REST endpoints
exist yet) and a small **relations-tab** convenience — see "Remaining gaps" below.

---

## Before → After (the onboarding flow, step by step)

| Flow step | BEFORE — internal User API | AFTER — public REST API (`operationId`) | Verified live |
|---|---|---|:--:|
| 1. Registry search | `GET /api/registry/search` | `GET /restapi/v1/search/customers` — `companiesSearch` | ✅ exact hit-parity (ENTITY+PERSON) |
| 2. Create client from registry | `POST /api/customers` (+ enrichment poll) | `POST …/customers/externalSource` — `createCustomerFromExternalSource` | ✅ same 51 contacts / 3 docs |
| 3. Resolve case + process | `GET /api/tasks/customer/{id}` | `…/customers/{id}/cases` + `…/processes` — `getCasesByCustomerId` / `getProcesses` | ✅ |
| 4. Add a process to a case | `PATCH /api/cases/{id}/process/add` | `POST …/cases/{id}/processes` — `createProcess` | ✅ 4 flows started |
| 5. Read full data (master/contacts/AML/risk) | `GET /api/client/onboarding/full-data` | `GET …/processes/{id}/full-data` — `getProcessFullData` | ✅ riskProfile/amlProfile parity |
| 6. Submit a step (risk answers, GwG) | `PATCH /api/client/onboarding` | `PATCH …/processes/{id}/full-data` — `updateProcessFullData` | ✅ URL-level: 4 REST / 0 User-API |
| 7. Share link | *(already REST)* | `POST …/processes/{id}/shares` — `createProcessShare` | ✅ |
| 8. Documents (list / download / ZIP) | *(already REST)* | `…/customers/{id}/documents`, `…/cases/{id}/documents/download` | ✅ |
| 9. Close process / commit risk classification | `PATCH /api/questionnaire/{id}` | `POST …/processes/{id}/close` — `closeProcessById`; `PATCH …/customers/{id}` — `patchCustomer` | ✅ |
| Workspace overview / team / legal types | `GET /api/companies` · `/api/members` · `/api/legal-types` | `getCustomers` · `getUsers` · `getLegalTypes` | ✅ |

Every REST `operationId` above is present in the public OpenAPI spec (checked programmatically:
`path in spec = True` for each).

---

## What this means concretely

A partner integrating against BetterCo can now run the **complete onboarding lifecycle** — search a
company in the registry, create a fully-enriched client (NorthData + company.info: ~51 contacts, HR &
shareholder documents), open the onboarding/ReKYC processes, read the full KYC/AML/risk data, **write the
risk questionnaire answers**, pull documents, and close the process — **using only the REST API and a
key/secret credential.** No email/password, no internal endpoints.

The single most doubted step, *saving the risk questionnaire*, was proven at the wire level: hitting the
app's risk-save action emits **4 REST calls and 0 User-API calls** (a `getProcess`, the
`updateProcessFullData` PATCH, the `getProcessFullData` read-back, and cleanup).

---

## Evidence

- **Against the spec:** each REST operation cited is a real, published operation in
  `betterco_api.yaml` (167 ops) — verified by matching method+path to `operationId`.
- **Against the live system** (`editor.betterco.ai`, editor sandbox workspace), automated and self-cleaning:
  - `tests_rest_parity.py` — **12/12** per-call REST-vs-User-API parity.
  - `tests_e2e_flow.py` — **15/15** builds the *same* company via the OLD (User-API) and NEW (REST) paths
    and asserts identical results (51 contacts, 3 docs, case, clientType, AML + risk), incl. cross-reads.
  - `tests_app_e2e.py` — **11/11** boots the real app server and drives the actual HTTP endpoints
    (search → create → processes → customer → risk → risk-profile).
  - `reference_flow.py` — the scripted 9-step CLI runs green end-to-end on REST.

---

## Remaining gaps (BetterCo roadmap — ~4 endpoints)

These are the *only* onboarding-relevant capabilities without a REST equivalent today. They are small and
well-scoped:

| Capability | Needed REST endpoint (proposed) | Priority |
|---|---|---|
| Run an AML/PEP/sanction screening | `POST …/customers/{id}/screenings` | high |
| Read a customer's screening matches | `GET …/customers/{id}/screenings/results` | high |
| Record a screening match decision | `PATCH …/customers/{id}/screenings/{sid}` | high |
| Enrichment-completion signal | `GET …/customers/{id}/enrichment-status` (or an `isFullyInitialized` field) | low (async poll works) |

(REST already exposes screenings **read** and monitor-toggle; what's missing is *trigger*, *per-customer
match read*, and *decision write*.) Full definitions in `REST_MAPPING.md` §2.

One non-blocking item: the **relations/contacts tab** still uses the User API. REST *does* model this
(`getCustomerContacts`), but its shape differs (contacts vs relation-edges), so it's a UI rework rather
than a like-for-like swap — deferred, not blocked.

---

## Conclusion

**The BetterCo REST API can already drive the entire KYC onboarding integration** end to end on a
key/secret credential. Closing the ~3 screening endpoints (plus one optional enrichment-status signal)
would take REST coverage to 100% and let integrators retire the internal User API entirely.

> Reproducible: run `python tests_app_e2e.py`, `python tests_e2e_flow.py`, `python tests_rest_parity.py`
> against the editor sandbox. Mapping detail in `REST_MAPPING.md`.
