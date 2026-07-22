# Memo — REST-Lücken-Audit, Stand und nächste Schritte

**Datum:** 2026-07-17 · **Aktualisiert:** 2026-07-22 · **Ticket:** [BCP-8213](https://leanmarks.atlassian.net/browse/BCP-8213)
**Grundlage:** `REST_GAP_AUDIT.md` (Belege), `REST_GAPS_BACKEND.md` (Entwickler-Spezifikation)
**Nachprüfung:** `tests_rest_gaps.py` (ein Befehl, siehe unten)

---

## Update 2026-07-22 — der Entscheidungs-Write ist da, die Lese-Seite funktioniert komplett

Die Editor-Spec ist erneut gewachsen (204 → **207 Operationen**; `app.betterco.ai`: 178 → 179). **28 Ops
sind editor-only**, davon waren **12 nie geprobt**. Das Ergebnis dreht die Bewertung von gestern:

**Neu geschlossen:**

- **G2 (Entscheidung) — GESCHLOSSEN.** Der gesuchte Write heißt nicht `…/screening/details`, sondern
  **`PATCH …/screening/profile`** (`updateCustomerScreeningProfile` /
  `updateCustomerContactScreeningProfile`), Body `{matchStatus, riskLevel, amlNote}`. **200, und der Wert
  bleibt** — für Entity *und* Kontakt, vor und nach einem Scan, in beiden Schreibreihenfolgen; rücklesbar
  über `getCustomerById` **und** User-API-`full-data`. Echte PATCH-Semantik (ausgelassene Felder bleiben
  unverändert), die Antwort spiegelt das **gemergte** Profil. Im Client verdrahtet als
  `update_screening_profile_rest()` / `save_aml_review_rest()` — ersetzt `save_aml_review` (User API).
- **G3b (Kandidaten) — GESCHLOSSEN.** `getCustomer[Contact]SearchResults` liefert nach einem Scan die
  vollständigen Treffer inkl. `pepTier`, `datesOfBirth`, `datasets`, `score`, `profileImage`. Das 404 von
  gestern war schlicht „noch kein Scan", kein Defekt.
- **G3c (Kandidaten-Dossier) — GESCHLOSSEN.** `…/search-results/{id}` liefert den Provider-Datensatz
  (Adressen, Aliase, Beteiligungen, Evidenzen). ⚠️ `{id}` ist die **Kandidaten-ID** aus
  `searchResults.data[].id`, keine Such-ID.
- **PEP-Daten in REST:** `…/political-functions` → `{current[], former[]}` (Scholz: 2 aktuell / 10 früher),
  `…/remarks` → z. B. `["PEP Tier 1", "Financial Crime and Fraud - Tax Offences"]`.
- **Zusammenfassungs-PDF:** `GET …/reports?process_name=F1600_RiskAMLScreening` → `{fileName, mimeType,
  contentBase64}` (~45 kB). `process_name` ist **Pflicht** (sonst 400).
- **Ausweisdokumente (S2) — GESCHLOSSEN.** `PUT …/contacts/{ct}/identity-documents` (multipart `file` +
  `idDocType` + `processId`) → 201; `GET` listet sie inkl. `contentBase64`.
- **Jurisdiktions-Abdeckung:** `…/document-search/jurisdictions[/{code}]/coverage` → 200.

**Weiterhin offen — die gesamte verbleibende Bitte an BetterCo, 4 Punkte:**

1. **G1 Scan-Trigger:** `POST …/screening/scan` → weiterhin **400 „Input data is corrupted"** (Entity *und*
   sauberer PEP-Kontakt mit gültigem `birthDate`). Es gibt **keinen** funktionierenden REST-Trigger;
   `run_screening` (User API, Step P1615) bleibt der einzige Weg.
2. **G2b Dossier-Pull:** `POST …/screening/details` → **400 bei jedem Body**, auch beim **wörtlich
   zurückgegebenen** Kandidaten-Objekt. Dadurch bleibt `GET …/aml` auf 404.
3. **G3a:** `getCustomerById…screeningProfile` trägt zwar das **Urteil**, aber nie die **Scan-Seite**
   (`lastScreeningDate`, `totalHits`, `searchId`, `hitsPerCategory`).
4. **`getOrganizationScreenings`** liefert für jeden Kunden `{}` — gescannt oder beurteilt.

**Zwei Doku-Fehler:** `search_id` erwartet in Wahrheit die Kandidaten-ID (ohne den Parameter kommt
`{}` / `[]` mit **HTTP 200** zurück — sieht aus wie „keine Daten"); und
`UpdateScreeningProfileRequest` lehnt `NONE` / `VERY_HIGH` (Risiko) sowie `PARTIAL_MATCH` (Match-Status)
mit 400 ab, obwohl die User API und `ScreeningProfile` sie kennen.

**Harness-Stand:** `python tests_rest_gaps.py` → **8/11 geschlossen** (neu: G3c, G7, G5, G6; G2 von PARTIAL
auf CLOSED). Offen: B0, G1 (PARTIAL), G3a.

---

## Update 2026-07-21 — Screening-Endpoints sind jetzt in REST vorhanden (auf dem Editor-Host)

BetterCo hat **auf `editor.betterco.ai`** (Spec jetzt **204 Operationen**, vs. 178 auf `app.betterco.ai`)
eine ganze Familie Screening-Operationen unter dem Tag **`Customers`** ausgeliefert — **1:1-Twins der
internen `/api/…/screening/*`-Routen**. Genau „mirrors of existing screening apis". **Aber live geprobt
funktionieren sie noch nicht** — der Spiegel ist inklusive der internen Bugs getreu:

- **G1 Scan** (`POST …/screening/scan`, Entity + Contact): **HTTP 400 „Input data is corrupted"**. Die
  Provider-Suche feuert (Kandidaten werden geholt — 3 Treffer für Olaf Scholz), aber das `screeningProfile`
  wird **nicht committet**. Zuverlässiger Trigger (P1615-Step-Submit) hat weiterhin keinen funktionierenden
  REST-Twin (= B0).
- **G2 Entscheidung** (`POST …/screening/details`): **400** — lehnt den dokumentierten `SearchResponseData`-
  Body ab („Invalid fields: ['attributes','gender']").
- **G3 Lesen**: `getCustomerById.screeningProfile` weiter `{}`; `getOrganizationScreenings` = 0 Zeilen.
- **Monitor** (`PUT …/screening/monitor`): **200** (nur Umschalter). **Zertifikat** (`GET …/screening/certificate`): routet (404 bis vorhanden).

**Zusätzlich sind die abhängigen Read-Twins und die Beziehungen jetzt in REST da** (alle `Customers`-Tag,
editor-only, teils live verifiziert):

- **Kandidaten-Read (G3b)** `getCustomerSearchResults` + **Detail (G3c)** `getCustomerSearchResultDetails` —
  Route vorhanden (`OPTIONS 200`), `GET 404` nur mangels committetem Scan → **wird durch G1-Fix automatisch
  aktiv**.
- **Beziehungen (S3) = GESCHLOSSEN**: `addCustomerContactRelation` (`PUT …/contacts/{id}/relations`) +
  `deleteCustomerContactRelation` (`DELETE …/relations/{code}`) — live verifiziert (201/200, additiv, kein
  Überschreiben). Graph via `getCustomerStructureChart` (`GET …/structure-chart` → 200).

**Neues Gesamtbild:** Der Blocker für eine REST-only-Integration schrumpft von „gesamter Screening-
Lebenszyklus" auf **einen funktionalen Defekt — der Scan committet nicht (G1)** — plus dessen Entscheidungs-
Sibling (G2). Die **App selbst** kann **heute** REST-only laufen (G4 Anreicherung + S3 Beziehungen beide
geschlossen). Der Vendor-Ask wechselt von „diese Endpoints bauen" zu **„die gerade ausgelieferten fixen"**.

---

## Worum es geht

Vollständiges Audit aller ausgehenden BetterCo-API-Aufrufe des Integration Guide gegen die **aktuelle**
öffentliche Spezifikation, anschließend **live geprüft** gegen die Editor-Sandbox. Leitfrage: Kann ein
Partner den kompletten Onboarding-Ablauf allein über REST (Key+Secret) fahren?

**Antwort: ja — mit Ausnahme des AML-Screenings.** Alles andere ist heute REST-vollständig.

Vorgehen: 83 Aufrufstellen im Client aus dem Quelltext erfasst, per Methode + Pfad + **Schema** gegen die
Spezifikation gematcht, danach jede prüfbare Aussage **live verifiziert**. Das Prüfen war entscheidend —
die Spezifikation deklariert Felder, die die Implementierung nicht befüllt (siehe G3a).

---

## Ergebnis in Kürze

| | Ergebnis |
|---|---|
| Aufrufstellen im Client | **83** (41 REST / 42 nicht-REST) |
| Vom App/CLI tatsächlich erreicht | **27** Methoden (davon 8 noch User-API) |
| Echte Lücken, die REST-only blockieren | **3** — alle AML-Screening |
| Zuvor gemeldete Lücken, die **entfallen** | **1** (Anreicherungs-Signal — kein Aufwand nötig) |

**Die offenen Punkte** (Details in BCP-8213):

- **B0** — REST nimmt den Screening-Body an, liefert `200` und **tut nichts**. Kein Fehler, keine Wirkung.
- **G3a** — `ScreeningProfile` ist in der Spezifikation deklariert, wird aber als `{}` zurückgegeben, selbst
  nach einem echten Scan mit Treffer. Nur befüllen — kein neuer Endpunkt. **Billigster Hebel.**
- **G1** — kein REST-Weg, einen Scan auszulösen.
- **G2** — kein REST-Weg, eine Treffer-Entscheidung zu schreiben.

---

## Drei Korrekturen an früheren Aussagen

Wichtig, weil Teile davon **bereits an Afileon gegangen sind** (`MIGRATION_EMAIL.de.md`):

1. **Anreicherungs-Signal ist KEINE Lücke.** `getWorkflowStatus` liefert `ProcessState.isFullyInitialized`
   und bildet die Kunden-Anreicherung exakt ab (live: `false→true` bei ~25 s, genau wenn die Kontakte von
   2 → 12 → 51 laufen). `REST_MAPPING.md` §2.1 hatte diesen Endpunkt verworfen, ohne das Schema zu prüfen.
   → In `MIGRATION_EMAIL.de.md` steht dies noch als offene Lücke. **Sollte gegenüber Afileon korrigiert
   werden.**
2. **Spezifikation umfasst inzwischen 178 Operationen**, nicht 167. Beide Migrationsdokumente und die
   E-Mail nennen den alten Stand.
3. **Die in `REST_MAPPING.md` §2.3 vorgeschlagene Treffer-Struktur ist frei erfunden**
   (`pepTier`, `datesOfBirth` existieren nicht). Die reale Struktur steht in BCP-8213 — wichtig, damit
   niemand gegen einen fiktiven Vertrag baut.

Netto gegenüber Afileon: Der Aufwand für BetterCo **schrumpft** (3 Screening-Punkte statt „~4 Endpunkte
inkl. Anreicherung"), die Aussage „REST deckt den Onboarding-Ablauf ab" wird **stärker**, nicht schwächer.

---

## Nächster Schritt: gegen die Lücken laufen

Sobald das Backend liefert, prüfen wir jede Lücke einzeln nach — kein manuelles Nachstellen nötig:

```bash
python tests_rest_gaps.py                 # alle Lücken prüfen (nur Lesen + 1 Wegwerf-Kunde)
python tests_rest_gaps.py --gap G1        # nur eine Lücke
```

Das Skript legt **einen** Kunden an, prüft, und löscht ihn wieder (`finally`). Es schreibt ausschließlich
in die Editor-Sandbox und **verweigert jede prod-/Afileon-Umgebung**.

Ausgabe pro Lücke: `OPEN` (unverändert), `CLOSED` (geschlossen, verifiziert) oder `PARTIAL`. Damit ist der
Fortschritt an BCP-8213 jederzeit mit einem Befehl belegbar — und wenn eine Lücke schließt, sehen wir es
sofort, statt es anzunehmen.

**Reihenfolge, in der wir migrieren, sobald geschlossen:**

1. **G4 (bereits möglich, ohne Backend-Arbeit)** — `wait_enrichment` (User-API) durch `getWorkflowStatus`
   ersetzen. Damit fällt eine der beiden verbliebenen User-API-Abhängigkeiten der App weg. *Das können wir
   sofort tun.*
2. **B0 + G3a** — sobald geliefert: Screening-Status über REST lesen (Schritt 5/6/9 des Ablaufs).
3. **G1 + G2** — dann `run_screening` / Entscheidungs-Writes im CLI auf REST umstellen.
4. **S3** (Beziehungen-Tab) — unabhängig prüfen; evtl. gar keine Backend-Arbeit nötig.

Danach kann der Guide die E-Mail+Passwort-Zugangsdaten vollständig ablegen.

---

## Belegqualität — bewusst transparent

- **B0, G1, G3a, G3b, G3c und das Anreicherungs-Signal:** live gegen `editor.betterco.ai` geprüft
  (2026-07-17), jeder Lauf legt einen Kunden an und löscht ihn wieder.
- **G2 ist der einzige nicht geprüfte Punkt** — es gibt schlicht keinen Endpunkt zum Aufrufen. Die Aussage
  stützt sich auf Schema-Analyse (`UpdateActorRequest` hat kein `matchStatus`-Feld). Belastbar, aber eine
  andere Belegklasse als der Rest.
- **S3/S4 sind Untersuchungen**, keine bestätigten Lücken.

> Lehre aus diesem Audit: Auf dieser API **Schema-Aussagen nicht glauben, sondern prüfen.** G3a sah in der
> Spezifikation wie eine bereits geschlossene Lücke aus — live liefert der Endpunkt `{}`. Ein früherer
> Entwurf dieses Audits war deshalb an genau dieser Stelle falsch.
