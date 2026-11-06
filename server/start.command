#!/bin/zsh
# Doppelklick startet den lokalen KI-Oasen-Server.
# Fenster offen lassen, solange Demo und Admin laufen sollen. ctrl+C beendet.
cd "$(dirname "$0")" || exit 1
PORT_NR=8787

warte() { echo; read -k 1 "?  Eine Taste drücken, um das Fenster zu schließen …"; echo; }

echo
echo "  KI-Oasen · lokaler Server"
echo "  -------------------------"

if ! command -v python3 >/dev/null 2>&1; then
  echo "  Python 3 fehlt. macOS bietet beim ersten Aufruf von python3 die Installation an."
  warte; exit 1
fi

# Beim ersten Start: Geheimnis-Datei aus der Vorlage anlegen und zum Ausfüllen öffnen.
if [ ! -f ".env" ]; then
  cp env-vorlage.txt .env && chmod 600 .env
  echo "  Erster Start: Ich habe die Datei server/.env angelegt und öffne sie in TextEdit."
  echo "  1. Hinter ADMIN_PASSWORT= ein Passwort eintragen (API-Schlüssel kann warten)."
  echo "  2. Speichern (cmd+S) und TextEdit schließen."
  echo "  3. start.command noch einmal doppelklicken."
  open -e .env
  warte; exit 0
fi

# Läuft schon ein Server? Dann nur die Seiten öffnen.
if lsof -nP -iTCP:$PORT_NR -sTCP:LISTEN >/dev/null 2>&1; then
  echo "  Der Server läuft bereits (anderes Fenster). Ich öffne nur Demo und Admin (Admin liegt vorn)."
  open "http://127.0.0.1:$PORT_NR/demo.html"; sleep 0.5; open "http://127.0.0.1:$PORT_NR/admin"
  warte; exit 0
fi

( sleep 1.5; open "http://127.0.0.1:$PORT_NR/demo.html"; sleep 0.5; open "http://127.0.0.1:$PORT_NR/admin" ) &
python3 server.py
echo "  Der Server ist beendet. Demo und Admin funktionieren erst nach einem neuen Start."
warte
