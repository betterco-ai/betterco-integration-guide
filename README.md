# BetterCo Integration Guide

A small, self-contained reference app that drives the BetterCo KYC onboarding flow
("Mandanten-Neuannahme") end to end — and serves as a **working example** of how to
integrate against the BetterCo REST + User APIs.

Search a company, create the client + matter with the onboarding / ReKYC / screening
flows, run the workflows in an embedded runner, and review customer data, documents
and the risk profile. Plus a workspace customer overview with KYC/risk columns and
filters.

Zero front-end framework — a single HTML page (`index.html`) backed by a stdlib
`http.server` (`app.py`) that holds the authenticated BetterCo client and proxies
every call. **The browser never talks to BetterCo directly** (credentials stay on the
server; auth, CORS and token refresh are handled in one place).

## The flow

1. **Unternehmenssuche** — NorthData registry search → hit list
2. **Stammdaten validieren** — inspect the selected hit
3. **Akte anlegen** — create the enriched client (NorthData + company.info) + matter,
   start the domain flows, mint a client share link:
   - Entity: `F1800_OnboardingEntity_A` · `F1800_OnboardingEntity_E` · `F18000_ReKYC` · `F1600_RiskAMLScreening`
   - Person: `F1900_OnboardingIndividual_A` · `F1900_OnboardingIndividual_E` · `F19000_ReKYC` · `F1600_RiskAMLScreening`
4. **Workflows** — per-flow **initiator-side** runner embedded in an iframe
   (non-shareable flows like screening show a read-only task view)
5. **Daten & Dokumente** — Mandanten-Daten / Mandanten-Dokumente / Prozess-Dokumente
   (each with "Alle als ZIP") / Risiko-Profil / Process Status
6. **Mandanten-Übersicht** — workspace customer table with KYC/risk columns and
   filters (name, Prozess ReKYC/Screening, Status, Branchen-/Länder-/AML-Risiko)

See **`HTTP_REFERENCE.md`** for the raw HTTP behind every step, and **`reference_flow.py`**
for a scripted 9-step reference of the same flow (runnable from the CLI).

## Setup

```bash
pip install -r requirements.txt
```

Create a workspace env file from the template and fill in real credentials:

```bash
cp workspaces/example.env workspaces/editor-betterco-claude.env
# edit it: BETTERCO_API_KEY/SECRET, WORKSPACE_ID, ORG_ID, USER_EMAIL/PASSWORD
```

Or skip the manual copy: start the app and use the **Zugangsdaten** tab to enter the
credentials in the browser — it writes the `.env`, runs `verify_env()`, and (if OK)
swaps in the live client without a restart. The app boots even with no/invalid env so
you can set one up from there. Stored secrets are masked on reload (leave them blank to
keep). REST key+secret now cover the core flow end to end — registry search, "Akte
anlegen", customer/case/process/document reads & writes, and the risk (F1400) step
submit + read-back. User-API email+password are only still needed for the parts with no
clean REST equivalent yet (see **`REST_MAPPING.md`**): the **relations/contacts tab**, the
**AML screening** flow, and the *optional* enrichment-completion wait after create.

> The migration to REST is intentionally partial — "clean update on REST, wait for others
> to evolve". The onboarding front half runs on the REST key+secret token alone; the
> `*_rest` client methods (`search_registry_rest`, `create_customer_from_registry_rest`,
> `get_full_data_rest`, `submit_step_rest`) are verified against the live base by
> `tests_rest_parity.py`. Run `BetterCoClient().verify_env()` (or just start the app) to
> confirm the env — a wrong `BETTERCO_ORG_ID` is silent and lands data under the wrong
> advisor org.

## Run

```bash
python app.py                       # default env: workspaces/editor-betterco-claude.env, :8770
python app.py --env-file workspaces/<other>.env
python app.py --port 8771 --no-browser
```

Windows: double-click `run_widget.bat`. Opens http://localhost:8770.

## Files

| File | What |
|---|---|
| `app.py` | http.server backend — all endpoints (see table below) |
| `index.html` | single-page UI |
| `reference_flow.py` | scripted 9-step flow + the `connect()` / env helpers `app.py` imports |
| `HTTP_REFERENCE.md` | raw HTTP request/response per step |
| `REST_MAPPING.md` | User-API→REST mapping + migration status (what runs on REST vs pending) |
| `tests_rest_parity.py` | live per-call parity tests for the REST twins (`--create` for the guarded write path) |
| `tests_e2e_flow.py` | live e2e: builds the same company via OLD (User-API) and NEW (REST) paths, asserts equivalence, deletes both (editor sandbox only) |
| `tests_app_e2e.py` | live e2e: boots the real server and drives the HTTP endpoints (search→create→processes→risk), deletes the customer (editor sandbox only) |
| `betterco_client.py` | **vendored** BetterCo API client (snapshot — see Development) |
| `workspaces/` | per-workspace `.env` files (git-ignored; `example.env` is the template) |

## HTTP endpoints (backend → BetterCo)

| Endpoint | Purpose |
|---|---|
| `GET /api/env` / `POST /api/env` | read/write workspace credentials (Zugangsdaten editor; secrets masked) |
| `GET /api/search` | registry search (step 1) |
| `POST /api/create-matter` | enriched client + matter + flows + share link (step 3) |
| `POST /api/processes` | flows of a matter (name, status, progress, shareable) |
| `POST /api/process-link` | initiator-side runner share link for one flow |
| `POST /api/process-detail` | read-only task view of one flow |
| `POST /api/customer` / `POST /api/documents` | customer master data / documents |
| `POST /api/process-documents` | documents of one process |
| `GET /api/document` / `GET /api/documents-zip` | download one doc / all docs as ZIP |
| `POST /api/risk-profile` | KYC/risk fields for one customer (REST) |
| `POST /api/customers-list` | workspace overview (risk columns) |
| `POST /api/customer-processes` | ReKYC + Screening status per customer (filter) |

## Development

**This repo is the home of the app** — make UI/backend changes to `app.py` /
`index.html` / `reference_flow.py` **here** and run it from here.

**`betterco_client.py` is vendored** as a snapshot. Its source of truth is the
`betterco_claude_api` repo (a.k.a. the `betterco` skill); when the client changes there,
re-copy `betterco_client.py` into this repo to pick up the update.

The embedded runner loads `editor.betterco.ai` (cross-origin), so it can't be styled
from this page; the app wraps it in accent-colored chrome. To theme the runner itself,
use the BetterCo workspace portal CSS skin.

Workspace env files contain secrets and are git-ignored — never commit a real `.env`.
