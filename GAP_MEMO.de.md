# Memo — REST-Lücken-Audit, Stand und nächste Schritte

**Datum:** 2026-07-17 · **Ticket:** [BCP-8213](https://leanmarks.atlassian.net/browse/BCP-8213)
**Grundlage:** `REST_GAP_AUDIT.md` (Belege), `REST_GAPS_BACKEND.md` (Entwickler-Spezifikation)
**Nachprüfung:** `tests_rest_gaps.py` (ein Befehl, siehe unten)

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
