# Mandanten-Neuannahme — BetterCo PoC

A small, self-contained web app that drives the BetterCo KYC onboarding flow
("Mandanten-Neuannahme") end to end: search a company, create the client + matter
with the onboarding/ReKYC/screening flows, run the workflows in an embedded runner,
and review customer data, documents and the risk profile.

Zero front-end framework — a single HTML page backed by a stdlib `http.server`
that holds the authenticated BetterCo client and proxies every call (the browser
never talks to BetterCo directly).

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
   (with "Alle als ZIP") / Risiko-Profil / Process Status
6. **Mandanten-Übersicht** — workspace customer table with KYC/risk columns and
   filters (name, Prozess ReKYC/Screening, Status, Branchen-/Länder-/AML-Risiko)

See `POC_MANDANT_NEUANNAHME_HTTP.md` for the raw HTTP behind every step, and
`poc_mandant_neuannahme.py` for a scripted 9-step reference of the same flow.

## Setup

```bash
pip install -r requirements.txt
```

Create a workspace env file from the template and fill in real credentials:

```bash
cp workspaces/example.env workspaces/editor-betterco-claude.env
# edit it: BETTERCO_API_KEY/SECRET, WORKSPACE_ID, ORG_ID, USER_EMAIL/PASSWORD
```

> The app needs **both** REST (key+secret) and User-API (email+password) credentials:
> REST for customer/case/process/document reads & writes, the User-API registry path
> only for the enriched NorthData/company.info customer creation in step 3.

## Run

```bash
python poc_search_app.py                  # default env: workspaces/editor-betterco-claude.env, :8770
python poc_search_app.py --env-file workspaces/<other>.env
python poc_search_app.py --port 8771 --no-browser
```

Windows: double-click `run_widget.bat`. Opens http://localhost:8770.

## HTTP endpoints (backend → BetterCo)

| Endpoint | Purpose |
|---|---|
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

## Notes

- **`betterco_client.py` is vendored** here as a snapshot. The source of truth is the
  `betterco_claude_api` repo / the `betterco` skill — re-copy it to pick up client updates.
- The embedded runner loads `editor.betterco.ai` (cross-origin), so it can't be styled
  from this page; the app wraps it in Afileon-colored chrome. To theme the runner itself,
  use the BetterCo workspace portal CSS skin.
- Workspace env files contain secrets and are git-ignored.
