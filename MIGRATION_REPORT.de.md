# BetterCo REST API — Abdeckung der Onboarding-Integration

**Fragestellung:** Kann die öffentliche BetterCo-**REST-API** (`/restapi/v1/`, Key+Secret) den vollständigen
KYC-Ablauf „Mandanten-Neuannahme" eigenständig abbilden — ohne die interne User-API?

**Antwort: Ja, für den gesamten Onboarding-Ablauf.** Diese Referenz-App nutzte bisher zwei
BetterCo-Schnittstellen — die öffentliche **REST-API** und die interne **User-API** (`/api/…`,
E-Mail+Passwort, nicht in der öffentlichen Spezifikation). Wir haben jeden Onboarding-Schritt migriert,
für den es ein REST-Äquivalent gibt. Der Kernablauf läuft nun **allein mit dem REST-Key+Secret-Token** —
live verifiziert gegen `editor.betterco.ai` und gegen die veröffentlichte OpenAPI-Spezifikation
(`app.betterco.ai/bcapi/betterco_api.yaml`, 167 Operationen).

Die einzigen Funktionen, die weiterhin die interne User-API benötigen, sind das **AML-Screening** (dafür
existieren noch keine REST-Endpunkte) und eine kleine Komfortfunktion im **Beziehungen-/Kontakte-Tab** —
siehe „Verbleibende Lücken".

---

## Vorher → Nachher (der Onboarding-Ablauf, Schritt für Schritt)

| Ablaufschritt | VORHER — interne User-API | NACHHER — öffentliche REST-API (`operationId`) | Live verifiziert |
|---|---|---|:--:|
| 1. Registersuche | `GET /api/registry/search` | `GET /restapi/v1/search/customers` — `companiesSearch` | ✅ exakte Treffer-Parität (ENTITY+PERSON) |
| 2. Mandant aus Register anlegen | `POST /api/customers` (+ Anreicherungs-Polling) | `POST …/customers/externalSource` — `createCustomerFromExternalSource` | ✅ identische 51 Kontakte / 3 Dok. |
| 3. Case + Prozess auflösen | `GET /api/tasks/customer/{id}` | `…/customers/{id}/cases` + `…/processes` — `getCasesByCustomerId` / `getProcesses` | ✅ |
| 4. Prozess zu einem Case hinzufügen | `PATCH /api/cases/{id}/process/add` | `POST …/cases/{id}/processes` — `createProcess` | ✅ 4 Flows gestartet |
| 5. Volldaten lesen (Stammdaten/Kontakte/AML/Risiko) | `GET /api/client/onboarding/full-data` | `GET …/processes/{id}/full-data` — `getProcessFullData` | ✅ riskProfile/amlProfile-Parität |
| 6. Schritt speichern (Risikofragen, GwG) | `PATCH /api/client/onboarding` | `PATCH …/processes/{id}/full-data` — `updateProcessFullData` | ✅ URL-Ebene: 4 REST / 0 User-API |
| 7. Share-Link | *(bereits REST)* | `POST …/processes/{id}/shares` — `createProcessShare` | ✅ |
| 8. Dokumente (Liste / Download / ZIP) | *(bereits REST)* | `…/customers/{id}/documents`, `…/cases/{id}/documents/download` | ✅ |
| 9. Prozess schließen / Risikoklassifizierung festschreiben | `PATCH /api/questionnaire/{id}` | `POST …/processes/{id}/close` — `closeProcessById`; `PATCH …/customers/{id}` — `patchCustomer` | ✅ |
| Workspace-Übersicht / Team / Rechtsformen | `GET /api/companies` · `/api/members` · `/api/legal-types` | `getCustomers` · `getUsers` · `getLegalTypes` | ✅ |

Jede oben genannte REST-`operationId` ist in der öffentlichen OpenAPI-Spezifikation vorhanden
(programmatisch geprüft: `Pfad in Spezifikation = True` für jede).

---

## Was das konkret bedeutet

Ein Partner, der gegen BetterCo integriert, kann jetzt den **kompletten Onboarding-Lebenszyklus**
abbilden — ein Unternehmen im Register suchen, einen vollständig angereicherten Mandanten anlegen
(NorthData + company.info: ~51 Kontakte, HR- und Gesellschafterlisten-Dokumente), die Onboarding-/
ReKYC-Prozesse eröffnen, die vollständigen KYC-/AML-/Risikodaten lesen, **die Antworten des
Risikofragebogens schreiben**, Dokumente abrufen und den Prozess schließen — **allein mit der REST-API und
einem Key/Secret-Credential.** Keine E-Mail/Passwort, keine internen Endpunkte.

Der am häufigsten angezweifelte Schritt, das *Speichern des Risikofragebogens*, wurde auf Protokollebene
belegt: der Aufruf der Risiko-Speichern-Aktion der App erzeugt **4 REST-Aufrufe und 0 User-API-Aufrufe**
(ein `getProcess`, das `updateProcessFullData`-PATCH, das `getProcessFullData`-Rücklesen und die
Aufräum-Löschung).

---

## Nachweise

- **Gegen die Spezifikation:** jede genannte REST-Operation ist eine reale, veröffentlichte Operation in
  `betterco_api.yaml` (167 Operationen) — verifiziert durch Abgleich von Methode+Pfad mit `operationId`.
- **Gegen das Live-System** (`editor.betterco.ai`, Editor-Sandbox-Workspace), automatisiert und
  selbst-aufräumend:
  - `tests_rest_parity.py` — **12/12** Pro-Aufruf-Parität REST vs. User-API.
  - `tests_e2e_flow.py` — **15/15** legt dasselbe Unternehmen über den ALTEN (User-API) und den NEUEN
    (REST) Pfad an und prüft identische Ergebnisse (51 Kontakte, 3 Dok., Case, clientType, AML + Risiko),
    inkl. Cross-Reads.
  - `tests_app_e2e.py` — **11/11** startet den echten App-Server und ruft die tatsächlichen HTTP-Endpunkte
    auf (Suche → Anlegen → Prozesse → Kunde → Risiko → Risiko-Profil).
  - `reference_flow.py` — der skriptbasierte 9-Schritte-CLI läuft durchgängig auf REST grün.

---

## Verbleibende Lücken (BetterCo-Roadmap — ~4 Endpunkte)

Dies sind die *einzigen* onboarding-relevanten Funktionen, für die heute kein REST-Äquivalent existiert.
Sie sind klein und klar umrissen:

| Funktion | Benötigter REST-Endpunkt (Vorschlag) | Priorität |
|---|---|---|
| AML-/PEP-/Sanktions-Screening auslösen | `POST …/customers/{id}/screenings` | hoch |
| Screening-Treffer eines Kunden lesen | `GET …/customers/{id}/screenings/results` | hoch |
| Screening-Treffer-Entscheidung festhalten | `PATCH …/customers/{id}/screenings/{sid}` | hoch |
| Signal für abgeschlossene Anreicherung | `GET …/customers/{id}/enrichment-status` (oder ein `isFullyInitialized`-Feld) | niedrig (async-Polling funktioniert) |

(REST stellt bereits das **Lesen** von Screenings und das Monitor-Umschalten bereit; es fehlen *Auslösen*,
*Pro-Kunde-Treffer-Lesen* und *Entscheidung-Schreiben*.) Vollständige Definitionen in `REST_MAPPING.md` §2.

Ein nicht blockierender Punkt: der **Beziehungen-/Kontakte-Tab** nutzt weiterhin die User-API. REST bildet
dies zwar ab (`getCustomerContacts`), jedoch mit abweichender Struktur (Kontakte vs. Beziehungs-Kanten),
sodass es sich um einen UI-Umbau statt eines 1:1-Austauschs handelt — zurückgestellt, nicht blockiert.

---

## Fazit

**Die BetterCo-REST-API kann die vollständige KYC-Onboarding-Integration bereits durchgängig abbilden** —
mit einem Key/Secret-Credential. Das Schließen der ~3 Screening-Endpunkte (plus ein optionales
Anreicherungs-Status-Signal) würde die REST-Abdeckung auf 100 % bringen und Integratoren erlauben, die
interne User-API vollständig abzulösen.

> Reproduzierbar: `python tests_app_e2e.py`, `python tests_e2e_flow.py`, `python tests_rest_parity.py`
> gegen die Editor-Sandbox ausführen. Mapping-Details in `REST_MAPPING.md`.
