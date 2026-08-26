# BetterCo Integration Guide — Claude Code Guide

## What this repo is

A self-contained reference app for the BetterCo KYC onboarding flow
("Mandanten-Neuannahme") — and the **worked example** of how to integrate against the
BetterCo REST + User APIs. Split out of the `betterco_claude_api` monorepo on 2026-06-23.

Zero front-end framework: one HTML page (`index.html`) backed by a stdlib `http.server`
(`app.py`) that holds the authenticated client and proxies every call. **The browser never
talks to BetterCo directly** — credentials, auth, CORS and token refresh stay on the server.

```bash
python app.py                                  # default env, :8770
python app.py --env-file workspaces/prod.env
python app.py --port 8771 --no-browser
run_widget.bat                                 # Windows launcher
```

Branch is **`rest-migration`** (not `main`).

## The rule that keeps biting: `betterco_client.py` is a vendored SNAPSHOT

This repo carries its own copy of `betterco_client.py`. **The source of truth is
`../betterco_claude_api/betterco_client.py`.** Fix the client *there*, then re-copy it
here — never the other way round, and never fix a client bug only in this repo.

## Files

| File | What it is |
|---|---|
| `app.py` | the whole backend — every `/api/*` endpoint, credential editor (`GET/POST /api/env`, secrets masked) |
| `index.html` | the single-page UI |
| `reference_flow.py` | scripted 9-step CLI reference of the same flow; `app.py` imports `connect()`, `_as_list`, `_parse_env_file` from it |
| `betterco_client.py` | **vendored snapshot** — see above |
| `HTTP_REFERENCE.md` | the raw HTTP behind every step |
| `REST_MAPPING.md`, `REST_GAP_AUDIT.md`, `REST_GAPS_BACKEND.md`, `GAP_MEMO.de.md` | the BCP-8213 REST-parity work: which User-API operations still have no REST twin, and the backend work items that would close them |
| `tests_*.py` | e2e flow, app e2e, REST parity, REST gaps |

Real `workspaces/*.env` are gitignored — only `example.env` is committed. Default workspace
is `editor-betterco-claude`.

## Flow facts this app encodes (verified live)

- **Akte flows by domain.** Entity: `F1800_OnboardingEntity_A` · `F1800_OnboardingEntity_E` ·
  `F18000_ReKYC` · `F1600_RiskAMLScreening`. Person: `F1900_OnboardingIndividual_A` ·
  `F1900_OnboardingIndividual_E` · `F19000_ReKYC` · `F1600_RiskAMLScreening`.
  ReKYC codes are **five-digit**; person onboarding is `…Individual_…`, not `…Entity_…`.
- **Enrichment needs the User-API create.** `POST /api/customers` with
  `clientActorExternalId` + the correct `advisorActorId` gives NorthData + company.info
  (~51 contacts). A bare REST `create_customer` does **not** enrich — and a wrong or empty
  ORG_ID produces a customer that is REST-invisible and undeletable.
- **The workflow iframe uses the INITIATOR-side runner** —
  `create_share_link(..., is_initiator=True)`. The default share link is the TARGET
  (customer-fill) side. `F1600_RiskAMLScreening` is non-shareable → read-only task view.
- **Risk fields all live under `amlProfile.riskProfile.*`** — `kycNote`, `kycStatus`,
  `taxIndustry`, `taxIndustryRisk`, `riskCountry`, and `aggregatedAmlRisk` (= the AML risk).
  Read them via REST `getCustomerById`.

## Sibling repos

Attached via `permissions.additionalDirectories` in `.claude/settings.local.json`
(gitignored globally):

- `../betterco_claude_api` — **the client's source of truth** plus the `betterco` skill and
  every provider client. Go here for any client change.
- `../betterco-backend/betterco-backend` — the Java backend whose REST gaps `REST_GAPS_BACKEND.md`
  specifies. Branch `dev`. Its `CLAUDE.md` is gitignored/private; its `.claude/skills/`
  (`restapi`, `mongodb`, `acl`, …) are committed and are the real documentation.
- `../ubo-agent` — the UBO determination service. Branch varies; has its own tracked `CLAUDE.md`.

**Attached ≠ loaded.** A `CLAUDE.md` or `.claude/skills/` in an additional directory is not
auto-loaded and not registered. Root Claude in the repo you are editing.
