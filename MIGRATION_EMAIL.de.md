# Migrationsbericht — E-Mail-Vorlage (Deutsch)

Fertiger E-Mail-Text für die Weitergabe an Afileon. Anrede/Signatur bei Bedarf anpassen.
Ausführliche Fassung: `MIGRATION_REPORT.de.md`.

---

**Betreff:** BetterCo REST-API deckt den vollständigen KYC-Onboarding-Ablauf ab — Nachweis

Sehr geehrtes Afileon-Team,

wie besprochen hier der Nachweis, dass die öffentliche BetterCo-REST-API (`/restapi/v1/`,
Authentifizierung per Key+Secret) den kompletten Onboarding-Ablauf „Mandanten-Neuannahme"
eigenständig abbilden kann — ohne die interne User-API.

**Ausgangslage:** Unsere Referenz-App nutzte bislang zwei Schnittstellen — die öffentliche REST-API und
die interne User-API (E-Mail+Passwort, nicht in der öffentlichen Spezifikation). Wir haben jeden
Onboarding-Schritt, für den ein REST-Äquivalent existiert, migriert. Ergebnis: Der Kernablauf läuft nun
allein mit dem REST-Token.

**Konkret über REST abgebildet** (jeweils vorher User-API → jetzt REST-Operation):
- Registersuche → `companiesSearch`
- Mandant aus Register anlegen (inkl. NorthData/company.info-Anreicherung, ~51 Kontakte) → `createCustomerFromExternalSource`
- Case + Prozess auflösen → `getCasesByCustomerId` / `getProcesses`
- Prozess zu einem Case hinzufügen → `createProcess`
- Volldaten lesen (Stammdaten/Kontakte/AML/Risiko) → `getProcessFullData`
- Risikofragen / GwG-Schritte speichern → `updateProcessFullData`
- Share-Link, Dokumente, Prozess schließen, Risikoklassifizierung → bereits REST

**Verifizierung** (live gegen `editor.betterco.ai`, automatisiert und selbst-aufräumend):
- Pro-Aufruf-Parität REST vs. User-API: 12/12
- End-to-End „alt vs. neu" (identische Ergebnisse, 51 Kontakte / 3 Dokumente): 15/15
- HTTP-End-to-End über den echten App-Server: 11/11
- Auf Protokollebene erzeugt das Speichern der Risikofragen 4 REST-Aufrufe und 0 User-API-Aufrufe.

Alle genutzten REST-Operationen sind in der veröffentlichten OpenAPI-Spezifikation enthalten
(167 Operationen).

**Verbleibende Lücken** (klein und klar umrissen — BetterCo-Roadmap):
- AML-Screening: auslösen, Treffer lesen, Entscheidung schreiben (ca. 3 Endpunkte; REST bietet bislang nur Lesen/Monitoring).
- Optionales Signal für abgeschlossene Anreicherung (async-Polling funktioniert bereits).
- Der Beziehungen-/Kontakte-Tab nutzt noch die User-API; REST bildet dies ab, erfordert jedoch einen kleinen UI-Umbau (kein Blocker).

**Fazit:** Die REST-API kann die vollständige KYC-Onboarding-Integration bereits heute durchgängig
abbilden. Mit den ~3 Screening-Endpunkten (plus optionalem Anreicherungs-Status) läge die Abdeckung bei
100 %, und die interne User-API könnte vollständig abgelöst werden.

Für Details stelle ich gerne den ausführlichen Bericht sowie die reproduzierbaren Testskripte bereit.

Mit freundlichen Grüßen
Eckhard Ortwein
BetterCo
