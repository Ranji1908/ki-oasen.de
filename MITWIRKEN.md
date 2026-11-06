# KI-Oasen · Aufbau für Mitwirkende

KI-Oasen hat drei Ebenen. Online (GitHub Pages) laufen nur die ersten beiden als fertige Seiten, die dritte ist dort als **Beispielansicht** zu sehen.

| Ebene | Datei | Online | Lokal mit Server |
|---|---|---|---|
| 1 · Startseite | `index.html` + `buch.css` | vollständig | vollständig |
| 2 · Demo (Bürgeransicht, Galerie, Auswertung) | `demo.html` | Skizzen und Beispieldaten | echte KI-Bilder, echte Beiträge und Auswertung |
| 3 · Admin | `server/admin.html` | Beispielansicht, nichts wird gespeichert | Anmeldung mit Passwort, alles einstellbar |

Die Admin erreicht man über den Button **Login** in der Fußzeile der Startseite.

## Was in der Admin steckt

- **Betrieb:** Demo an/aus, Modus Simulation oder Live, Freigabepflicht für die Galerie
- **Nutzung:** Bilder heute und insgesamt, Kosten gegen Budget, Protokoll jeder Anfrage
- **Limits:** pro Besucher, pro Anschluss, pro Tag, insgesamt, Budget, Pause, Textlänge
- **Basisprompt, Leitplanken, Sperrwörter:** daraus setzt der Server den Prompt für die KI zusammen
- **Stilmittel:** der aktive Stiltext ersetzt `{stil}` im Basisprompt
- **Orte:** Blickwinkel mit Grundbild aus `bilder/grundbilder/` und Beschreibung für die KI
- **Bildmodell:** Modell, Qualität, Größe, Format der OpenAI-Bildbearbeitung
- **Ausprobieren:** Prompt ansehen (geht auch online), Testbild erzeugen (nur lokal)
- **Beiträge:** eingereichte Wünsche freigeben oder ausblenden

Die Einstellungen der Beispielansicht stammen aus `server/konfig-standard.json` (online) bzw. `server/konfig-beispiel.js` (wenn `server/admin.html` per Doppelklick geöffnet wird). Beide erzeugt `werkzeug/baue_upload.py`.

## Lokal starten

1. Python 3 genügt, es muss nichts installiert werden.
2. `server/env-vorlage.txt` kopieren, die Kopie `server/.env` nennen, ein eigenes `ADMIN_PASSWORT` und bei Bedarf einen eigenen `OPENAI_API_KEY` eintragen.
3. `server/start.command` doppelklicken oder `python3 server/server.py` ausführen.
4. Startseite http://127.0.0.1:8787/ · Admin über „Login“

## Regeln

- **Nie** `server/.env`, Passwörter oder API-Schlüssel ins Repo hochladen. Alles hier ist öffentlich.
- `server/daten/` (Nutzung, Beiträge, erzeugte Bilder) bleibt ebenfalls lokal.
