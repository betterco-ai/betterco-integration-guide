# Mandanten-Neuannahme PoC — BetterCo HTTP reference

Raw HTTP for every step of the onboarding flow, so a non-Python PoC frontend can
replicate `poc_mandant_neuannahme.py` directly. Python helper names in parentheses.

## Conventions

- **Base URL:** `https://editor.betterco.ai/bcapi`
- **Three auth scopes:**
  | Scope | Path prefix | Auth header |
  |---|---|---|
  | REST API | `/restapi/v1/` | `Authorization: Bearer <api-key token>` (login with key+secret) |
  | User API | `/api/` | `Authorization: Bearer <user token>` (login email+password) + `workspaceId: <ws>` header |
  | Client API | `/api/client/` | share-link token **or** User token (full-data accepts both) |
- Steps below use **User API** unless noted. `{ws}` = workspace id, `{org}` = advisor org id.
- **Login (User API):** `POST /auth/sign-in` (form-encoded `email`, `password`) → `{ "token": "..." }`.
- **Login (REST):** `POST /restapi/v1/auth/login` (JSON `{publicKey, privateKey}`) → bearer token.

---

## Step 1 — Unternehmenssuche (`search_registry`)

```
GET /api/registry/search?domain=ENTITY&query=Founders1%20GmbH
Authorization: Bearer <user token>
workspaceId: {ws}
```
**Response** (array): each hit carries `externalRegistryId` (NorthData id), `legalName`,
`address`, register info. → feeds step 2/3.

For a person: `domain=PERSON`.

---

## Step 2 — Stammdaten zur Validierung (no call)

The chosen hit from step 1 **is** the master-data preview (NorthData name / Rechtsform /
Registereintrag / Adresse). The PoC shows it for the user to confirm the right company.
No additional request — just keep the selected hit's `externalRegistryId`.

---

## Step 3 — Auswahl bestätigen → Akte anlegen (`create_customer_from_registry`)

### 3.1 Create customer (CLIENT, full NorthData + company.info enrichment)
```
POST /api/customers
Authorization: Bearer <user token>
workspaceId: {ws}
Content-Type: application/json

{
  "clientActorExternalId": "<externalRegistryId>",
  "advisorActorId": "{org}",
  "customerCategoryType": "ENTITY",
  "clientActorName": "Founders1 GmbH",
  "domain": "ENTITY"
}
```
Returns the new customer with `businessRelationId`. (`as_lead` → use `POST /api/leads`
instead: LEAD, NorthData only, ~13 contacts; `/api/customers` = CLIENT, ND+company.info, ~52.)

### 3.2 Poll until enrichment finished (HR/Gesellschafterliste/Kontakte/Verflechtungen)
```
GET /api/customers/business-relation?businessRelationId=<cid>
```
Poll every ~2s until `isFullyInitialized: true` (~17–19s). HR + Gesellschafterliste PDFs,
contacts and the ownership structure (Verflechtungen) are populated by this point.

### 3.3 Resolve case + process id
```
GET /api/customers/{cid}/cases            → first case id  (case auto-created)
GET .../cases/{caseId}  (processRows)     → the F1800_OnboardingEntity_A process id
```
(Python resolves this via `list_cases` + `list_processes`; on tenants where
`GET /api/tasks/customer/{cid}` works, `list_customer_processes` is the shortcut.)

---

## Step 3b — Share-Link erzeugen (`create_share_link`)

After the matter exists, mint an anonymous client share link:
```
POST /restapi/v1/workspaces/{ws}/organizations/{org}/customers/{cid}/cases/{caseId}/processes/{pid}/share-link
Authorization: Bearer <api-key token>
Content-Type: application/json

{ "expirationPeriod": "P3650D", "language": "DEF" }
```
**Response:** the full share URL (token `aud: ANONYMOUS_USER`). It stays alive while the
process is OPEN/COMPLETED and is **revoked the moment the process is CLOSED** (→ 401).

---

## Step 4 — Prüfung anstoßen / Anreicherungsbestellungen

Registry enrichment (Handelsregister, Gesellschafterliste, Kontakte, **Verflechtungen**,
wirtschaftliche Berechtigte) already ran in step 3.2. What is triggered here is the
**AML/PEP/Sanktion screening**, which lives in a **separate process**.

### 4.1 Create the screening process (`create_process` F1600)
```
PATCH /api/cases/{caseId}/process/add?processName=F1600_RiskAMLScreening&workspaceId={ws}
Authorization: Bearer <user token>
```
→ returns the screening process id (`screening_pid`). Wrong process id later → 404.

### 4.2 Run screening headlessly (`run_screening`)
```
PATCH /api/client/onboarding?processId={screening_pid}&businessRelationId={cid}&workspaceId={ws}&stepId=P1615_amlScreeningDefinition&roleTypes=PROCESS
Authorization: Bearer <user token>
Content-Type: application/json

<full-data echo with screeningProfile.isRescreeningEnabled=true on the entity + every in-scope contact>
```
Runs the Acuris provider search for the entity + all in-scope contacts (Legal Rep / UBO /
Acting Person) at once. **Each in-scope contact needs a `birthDate`** or its scan 400s.

> Optional — shareholder-graph (Verflechtungen) backfill for entities missing a graph:
> `POST /api/dashboard/workspaces/{ws}/ci-enrichment?recalculateKyc=false` (super-user auth,
> workspace-wide). Normally unnecessary because step 3 already imported the structure.

---

## Step 5 — Status-Abfrage (asynchron) (`get_screening_matches`)

Poll the screening result until populated:
```
GET /api/customers/{cid}/search-results                 # entity matches
GET /api/customers/{cid}/search-results?contactId={cid} # one contact's matches
Authorization: Bearer <user token>
workspaceId: {ws}
```
**Response:** `searchResults.data[]` with match attributes (`match, name, score, pepTier,
datasets, countries, datesOfBirth`). `screeningProfile.hasMultipleMatch=true` = decision
still pending. Master-data enrichment was already awaited via `isFullyInitialized` (3.2).

---

## Step 6 — Status: vollständig / unvollständig (derived)

No dedicated endpoint — assemble locally:
```
GET /api/client/onboarding/full-data?businessRelationId={cid}&...   # master data + contacts
GET /api/customers/{cid}/documents                                  # documents present?
```
Then resolve every in-scope actor's screening verdict (`auto_screen_customer` with
`screen=false, dry_run=true`, which reads `search-results` per actor):
- **vollständig** = master data present **and** ≥1 document **and** no in-scope actor `skipped`
  (every actor returned a screening result).
- **unvollständig** = anything still `skipped` / missing.

---

## Step 7 — Abholung des Informationsobjekts (`get_full_data` + `download_document`)

```
GET /api/client/onboarding/full-data?businessRelationId={cid}&sortingStrategy=SHARES&roleTypes=LEGAL_REP,SHAREHOLDER,UBO,ACTING_PERSON,MAIN_CONTACT&limit=50
Authorization: Bearer <user token>
```
→ complete entity: `entityLegalInfo`, `contacts` (with roles), `amlProfile`,
`actorRiskSnapshot`, `matter`.

List + download the documents (HR-Auszug, Gesellschafterliste, Strukturchart):
```
GET /api/customers/{cid}/documents
GET <document downloadUrl>     (streamed bytes → write to file)
```

---

## Step 8 — GwG-Fragen → Prozessschritte einarbeiten (`submit_step`)

For each GwG step of the F1800 flow:
```
PATCH /api/client/onboarding?processId={pid}&businessRelationId={cid}&workspaceId={ws}&stepId={GWG_STEP_ID}
Authorization: Bearer <user token>
Content-Type: application/json

{
  "additionalProcessData": { "<topic>": { "<field>": "<answer>" } },
  "additionalActorData":   { "<field>": "<answer>" },
  "taskStatuses": [ { "taskSpec": "{GWG_STEP_ID}", "status": "COMPLETE" } ]
}
```
- `{GWG_STEP_ID}` must be a **valid taskSpec** of the process (discover via
  `GET .../processes/{pid}` → `tasks[].taskSpec`), not the task-instance id (→ 404).
- Container routing (where each answer reads back from):
  | submit container | reads back at |
  |---|---|
  | `additionalProcessData.<topic>.<field>` | `get_process().data.additionalData.<topic>.<field>` |
  | `additionalActorData.<field>` | full-data `additionalActorData.<field>` |
  | `entityLegalInfo` / `contactData` | full-data master data |
- ⚠️ Returns `200` even when it **silently drops** unknown fields — always read back.

---

## Step 9 — Risikobericht abholen (`get_full_data` + screening summary)

```
GET /api/client/onboarding/full-data?businessRelationId={cid}&...
```
Risk classification: `amlProfile.screeningProfile.riskLevel` (AML),
`amlProfile.riskProfile` (PEP / Industrie-/Länderrisiko), `actorRiskSnapshot`.

Screening verdicts per actor: resolve via `auto_screen_customer(screen=false, dry_run=true)`
→ `{auto_cleared[], needs_review[], skipped[]}` (each reads `GET .../search-results`).

To **commit** a GwG risk classification onto the actor (REST, not approval-gated):
```
PATCH /restapi/v1/.../customers/{cid}
{ "riskSummary": { "kycStatus": "COMPLETED", "riskSummaryManualClassificationType": "LOW",
                   "identificationDate": "2026-06-23", ... } }
```
(`set_actor_risk_summary`.) A formal frozen **process report** PDF is generated only when the
process is **closed** (`PATCH /api/questionnaire/{pid}` — irreversible; all tasks must be done).

---

## Sequence at a glance

```
1  GET  /api/registry/search                              search_registry
2  (no call — validate the chosen hit)
3  POST /api/customers  → poll business-relation          create_customer_from_registry
   GET  /cases ; GET /cases/{id}                          resolve case_id + process_id
3b POST /restapi/.../processes/{pid}/share-link           create_share_link
4  PATCH /api/cases/{caseId}/process/add (F1600)          create_process
   PATCH /api/client/onboarding (P1615 ...roleTypes=PROCESS)  run_screening
5  GET  /api/customers/{cid}/search-results  (poll)       get_screening_matches
6  GET  full-data + /documents + per-actor search-results completeness (derived)
7  GET  full-data ; GET /documents ; GET <downloadUrl>    get_full_data + download_document
8  PATCH /api/client/onboarding?...&stepId=<GwG step>     submit_step (per GwG step)
9  GET  full-data (risk) + per-actor screening summary    risk_report
```
