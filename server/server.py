#!/usr/bin/env python3
"""
KI-Oasen · lokaler Demo-Server

Liefert die Website aus (index.html, demo.html …), erzeugt Bilder über die
OpenAI-Bild-API und stellt unter /admin eine Admin-Seite bereit.

Start:   python3 server/server.py        (oder Doppelklick auf server/start.command)
Adresse: http://127.0.0.1:8787/demo.html  ·  Admin: http://127.0.0.1:8787/admin

Geheimnisse (API-Schlüssel, Admin-Passwort) stehen nur in server/.env,
nie im Browser und nie in den HTML-Dateien. Nur Python-Standardbibliothek.
"""
import base64
import copy
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

HIER = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.dirname(HIER)                      # Ordner „Aktueller Stand“ (Website)
DATEN = os.path.join(HIER, "daten")
BILDER = os.path.join(DATEN, "bilder")
GRUNDBILDER = os.path.join(WEB, "bilder", "grundbilder")
KONFIG_STANDARD = os.path.join(HIER, "konfig-standard.json")
KONFIG = os.path.join(DATEN, "konfig.json")
NUTZUNG = os.path.join(DATEN, "nutzung.json")
BEITRAEGE = os.path.join(DATEN, "beitraege.json")
SALZ = os.path.join(DATEN, "salz.txt")

HOST = "127.0.0.1"          # nur dieser Rechner, nicht im Netzwerk sichtbar
OPENAI_URL = "https://api.openai.com/v1/images/edits"
MAX_BODY = 256 * 1024
STATISCH_ERLAUBT = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                    ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml",
                    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                    ".webp": "image/webp", ".ico": "image/x-icon", ".woff2": "font/woff2"}
BILD_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
ANTWORTEN = {
    "alter": ["unter 18", "18–29", "30–49", "50–64", "65+"],
    "bezug": ["Ich wohne hier", "Ich arbeite hier", "Aus der Region", "Zu Besuch"],
    "haeufigkeit": ["Täglich", "Wöchentlich", "Monatlich", "Selten"],
}

SPERRE = threading.RLock()          # schützt alle JSON-Dateien
API_PLAETZE = threading.Semaphore(2)  # höchstens zwei gleichzeitige API-Aufrufe
LETZTE_ANFRAGE = {}                 # Besucher -> Zeitpunkt (für die Pause)
ADMIN_SITZUNGEN = {}                # Token -> Ablaufzeit
ANMELDE_FEHLER = {"anzahl": 0, "bis": 0.0}


# ---------------------------------------------------------------- Hilfen
def lade_env():
    pfad = os.path.join(HIER, ".env")
    if not os.path.exists(pfad):
        return
    with open(pfad, encoding="utf-8-sig") as f:
        uebernimm_env(f)


def uebernimm_env(zeilen):
    """Zeilen im Format NAME=wert in die Umgebung übernehmen (bereits gesetzte Werte gewinnen)."""
    ohne_namen = ""
    for zeile in zeilen:
        zeile = zeile.strip()
        if not zeile or zeile.startswith("#"):
            continue
        if "=" not in zeile:
            # Nachsicht: OpenAI-Schlüssel ohne „OPENAI_API_KEY=“ davor trotzdem erkennen.
            if zeile.strip('"\'').startswith("sk-"):
                ohne_namen = zeile.strip('"\'')
            continue
        k, v = zeile.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k != "OPENAI_API_KEY" and v.startswith("sk-"):
            ohne_namen = ohne_namen or v      # Schlüssel unter falschem Namen
            continue
        if k and k not in os.environ:
            os.environ[k] = v
    if ohne_namen and not os.environ.get("OPENAI_API_KEY", "").strip():
        os.environ["OPENAI_API_KEY"] = ohne_namen
        print("  Hinweis: Schlüssel in server/.env ohne „OPENAI_API_KEY=“ gefunden und übernommen.")


def lies_json(pfad, standard):
    try:
        with open(pfad, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return copy.deepcopy(standard)


def schreib_json(pfad, daten):
    tmp = pfad + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(daten, f, ensure_ascii=False, indent=2)
    os.replace(tmp, pfad)


def ergaenze(standard, wert):
    """Fehlende Schlüssel aus der Standard-Konfiguration nachtragen (z. B. nach Updates)."""
    if isinstance(standard, dict) and isinstance(wert, dict):
        aus = dict(wert)
        for k, v in standard.items():
            aus[k] = ergaenze(v, wert[k]) if k in wert else copy.deepcopy(v)
        return aus
    return wert


def jetzt():
    return datetime.now().isoformat(timespec="seconds")


def salz():
    if not os.path.exists(SALZ):
        with open(SALZ, "w") as f:
            f.write(secrets.token_hex(32))
    with open(SALZ) as f:
        return f.read().strip().encode()


def kennung(wert):
    """Besucher-IDs und IP-Adressen nur als Prüfsumme speichern, nie im Klartext."""
    return hmac.new(salz(), wert.encode(), hashlib.sha256).hexdigest()[:20]


def grundbild_liste():
    try:
        return sorted(n for n in os.listdir(GRUNDBILDER)
                      if os.path.splitext(n)[1].lower() in BILD_MIME and not n.startswith("."))
    except OSError:
        return []


# ---------------------------------------------------------------- Konfiguration
def standard_konfig():
    with open(KONFIG_STANDARD, encoding="utf-8") as f:
        return json.load(f)


def konfig():
    with SPERRE:
        return ergaenze(standard_konfig(), lies_json(KONFIG, standard_konfig()))


def _text(wert, name, maxlen, leer_ok=False):
    if not isinstance(wert, str):
        raise ValueError(name + " muss Text sein.")
    wert = wert.replace("\r\n", "\n").strip()
    if not wert and not leer_ok:
        raise ValueError(name + " darf nicht leer sein.")
    if len(wert) > maxlen:
        raise ValueError(name + " ist zu lang (höchstens %d Zeichen)." % maxlen)
    return wert


def _zahl(wert, name, lo, hi, ganz=True):
    try:
        z = int(wert) if ganz else round(float(wert), 4)
    except (TypeError, ValueError):
        raise ValueError(name + " muss eine Zahl sein.")
    if not lo <= z <= hi:
        raise ValueError("%s muss zwischen %s und %s liegen." % (name, lo, hi))
    return z


def _wahl(wert, name, erlaubt):
    if wert not in erlaubt:
        raise ValueError("%s: ungültiger Wert." % name)
    return wert


ID_MUSTER = re.compile(r"^[a-z0-9-]{1,40}$")


def pruefe_konfig(neu):
    """Admin-Eingaben prüfen und eine saubere Konfiguration zurückgeben."""
    if not isinstance(neu, dict):
        raise ValueError("Ungültige Daten.")
    lim, bm = neu.get("limits") or {}, neu.get("bildmodell") or {}
    k = {
        "aktiv": bool(neu.get("aktiv")),
        "modus": _wahl(neu.get("modus"), "Modus", ("simulation", "live")),
        "limits": {
            "proBesucherTag": _zahl(lim.get("proBesucherTag"), "Bilder pro Besucher und Tag", 0, 1000),
            "proIpTag": _zahl(lim.get("proIpTag"), "Bilder pro Anschluss und Tag", 0, 10000),
            "gesamtTag": _zahl(lim.get("gesamtTag"), "Bilder pro Tag gesamt", 0, 100000),
            "gesamtMax": _zahl(lim.get("gesamtMax"), "Bilder insgesamt", 0, 1000000),
            "budgetEuro": _zahl(lim.get("budgetEuro"), "Budget", 0, 100000, ganz=False),
            "kostenProBildEuro": _zahl(lim.get("kostenProBildEuro"), "Kosten pro Bild", 0, 10, ganz=False),
            "pauseSekunden": _zahl(lim.get("pauseSekunden"), "Pause", 0, 3600),
            "maxZeichen": _zahl(lim.get("maxZeichen"), "Wunschlänge", 20, 1000),
        },
        "bildmodell": {
            "modell": bm.get("modell") if re.match(r"^[A-Za-z0-9._-]{1,60}$", str(bm.get("modell", ""))) else None,
            "qualitaet": _wahl(bm.get("qualitaet"), "Qualität", ("low", "medium", "high", "auto")),
            "groesse": _wahl(bm.get("groesse"), "Größe", ("1536x1024", "1024x1024", "1024x1536", "auto")),
            "eingabetreue": _wahl(bm.get("eingabetreue", ""), "Eingabetreue", ("high", "low", "")),
            "format": _wahl(bm.get("format"), "Format", ("jpeg", "png", "webp")),
        },
        "basisprompt": _text(neu.get("basisprompt"), "Basisprompt", 8000),
        "leitplanken": _text(neu.get("leitplanken", ""), "Leitplanken", 3000, leer_ok=True),
        "galerie": {"freigabePflicht": bool((neu.get("galerie") or {}).get("freigabePflicht"))},
    }
    if not k["bildmodell"]["modell"]:
        raise ValueError("Modellname ungültig.")
    if "{wunsch}" not in k["basisprompt"]:
        raise ValueError("Der Basisprompt braucht den Platzhalter {wunsch}.")

    stile = neu.get("stile")
    if not isinstance(stile, list) or not 1 <= len(stile) <= 12:
        raise ValueError("Es braucht 1 bis 12 Stilmittel.")
    k["stile"] = []
    for s in stile:
        sid = str((s or {}).get("id", ""))
        if not ID_MUSTER.match(sid):
            raise ValueError("Stil-Kürzel nur aus a–z, 0–9 und Bindestrich.")
        k["stile"].append({"id": sid, "name": _text(s.get("name"), "Stilname", 80),
                           "text": _text(s.get("text"), "Stiltext", 4000)})
    ids = [s["id"] for s in k["stile"]]
    if len(set(ids)) != len(ids):
        raise ValueError("Stil-Kürzel doppelt.")
    k["stilAktiv"] = _wahl(neu.get("stilAktiv"), "Aktiver Stil", ids)

    worte = neu.get("sperrwoerter") or []
    if not isinstance(worte, list) or len(worte) > 300:
        raise ValueError("Sperrwörter: höchstens 300.")
    k["sperrwoerter"] = sorted({_text(w, "Sperrwort", 40).lower() for w in worte if str(w).strip()})

    orte = neu.get("orte")
    if not isinstance(orte, list) or not 1 <= len(orte) <= 12:
        raise ValueError("Es braucht 1 bis 12 Orte.")
    dateien = grundbild_liste()
    k["orte"] = []
    for o in orte:
        oid = str((o or {}).get("id", ""))
        if not ID_MUSTER.match(oid):
            raise ValueError("Ort-Kürzel nur aus a–z, 0–9 und Bindestrich.")
        k["orte"].append({"id": oid, "name": _text(o.get("name"), "Name des Blickwinkels", 80),
                          "ort": _text(o.get("ort"), "Ortsbezeichnung", 120),
                          "beschreibung": _text(o.get("beschreibung", ""), "Ortsbeschreibung", 2000, leer_ok=True),
                          "grundbild": _wahl(o.get("grundbild"), "Grundbild (" + oid + ")", dateien),
                          "aktiv": bool(o.get("aktiv"))})
    if len({o["id"] for o in k["orte"]}) != len(k["orte"]):
        raise ValueError("Ort-Kürzel doppelt.")
    if not any(o["aktiv"] for o in k["orte"]):
        raise ValueError("Mindestens ein Ort muss aktiv sein.")
    return k


# ---------------------------------------------------------------- Prompt
def saeubere_wunsch(text, maxz):
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(text))
    text = re.sub(r"[{}<>\[\]\"„“”«»]", "", text)
    return re.sub(r"\s+", " ", text).strip()[:maxz]


def baue_prompt(k, ort, wunsch):
    stil = next((s for s in k["stile"] if s["id"] == k["stilAktiv"]), k["stile"][0])
    werte = {"ort": ort["ort"], "ortsbeschreibung": ort.get("beschreibung", ""), "wunsch": wunsch,
             "stil": stil["text"], "leitplanken": k.get("leitplanken", "")}
    # ein Durchgang: Text aus dem Wunsch wird nicht noch einmal als Platzhalter gelesen
    text = re.sub(r"\{(\w+)\}", lambda m: werte.get(m.group(1), m.group(0)), k["basisprompt"])
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def finde_ort(k, oid, nur_aktiv=True):
    for o in k["orte"]:
        if o["id"] == oid and (o["aktiv"] or not nur_aktiv):
            return o
    return None


# ---------------------------------------------------------------- Nutzung & Limits
def leere_nutzung():
    return {"tage": {}, "gesamt": {"bilder": 0, "kosten": 0.0}, "protokoll": []}


def tag_eintrag(n, tag):
    return n["tage"].setdefault(tag, {"bilder": 0, "fehler": 0, "abgelehnt": 0, "besucher": {}, "ip": {}})


def reserviere(k, besucher, ip, admin=False):
    """Prüft alle Limits und reserviert einen Platz. Gibt (ok, meldung) zurück."""
    lim, tag = k["limits"], date.today().isoformat()
    with SPERRE:
        n = lies_json(NUTZUNG, leere_nutzung())
        t = tag_eintrag(n, tag)
        if not k["aktiv"] and not admin:
            return False, "Die Demo ist gerade pausiert. Bitte später noch einmal versuchen."
        if n["gesamt"]["bilder"] >= lim["gesamtMax"]:
            return False, "Das Bildkontingent dieser Demo ist ausgeschöpft."
        if k["modus"] == "live" and n["gesamt"]["kosten"] + lim["kostenProBildEuro"] > lim["budgetEuro"] + 1e-9:
            return False, "Das Budget dieser Demo ist ausgeschöpft."
        if not admin:
            if t["bilder"] >= lim["gesamtTag"]:
                return False, "Für heute sind alle Demo-Bilder vergeben. Schauen Sie gern morgen wieder vorbei."
            if t["besucher"].get(besucher, 0) >= lim["proBesucherTag"]:
                return False, "Sie haben heute schon %d Bilder erzeugt. Morgen geht es weiter." % lim["proBesucherTag"]
            if t["ip"].get(ip, 0) >= lim["proIpTag"]:
                return False, "Von diesem Anschluss wurden heute schon genug Bilder erzeugt. Morgen geht es weiter."
            warten = lim["pauseSekunden"] - (time.time() - LETZTE_ANFRAGE.get(besucher, 0))
            if warten > 0:
                return False, "Bitte warten Sie noch %d Sekunden bis zum nächsten Bild." % (int(warten) + 1)
            LETZTE_ANFRAGE[besucher] = time.time()
        t["bilder"] += 1
        t["besucher"][besucher] = t["besucher"].get(besucher, 0) + 1
        t["ip"][ip] = t["ip"].get(ip, 0) + 1
        n["gesamt"]["bilder"] += 1
        schreib_json(NUTZUNG, n)
        return True, ""


def abschliessen(k, besucher, ip, eintrag, erfolg, kosten):
    """Protokolliert die Anfrage; bei Fehlern wird der reservierte Platz zurückgegeben."""
    tag = date.today().isoformat()
    with SPERRE:
        n = lies_json(NUTZUNG, leere_nutzung())
        t = tag_eintrag(n, tag)
        if not erfolg:
            t["bilder"] = max(0, t["bilder"] - 1)
            t["besucher"][besucher] = max(0, t["besucher"].get(besucher, 1) - 1)
            t["ip"][ip] = max(0, t["ip"].get(ip, 1) - 1)
            n["gesamt"]["bilder"] = max(0, n["gesamt"]["bilder"] - 1)
            t["fehler" if eintrag["ergebnis"] == "fehler" else "abgelehnt"] += 1
        n["gesamt"]["kosten"] = round(n["gesamt"]["kosten"] + kosten, 4)
        n["protokoll"].append(eintrag)
        n["protokoll"] = n["protokoll"][-500:]
        grenze = (date.today() - timedelta(days=60)).isoformat()
        n["tage"] = {d: v for d, v in n["tage"].items() if d >= grenze}
        schreib_json(NUTZUNG, n)


def protokolliere_ablehnung(besucher, ort, wunsch, grund):
    with SPERRE:
        n = lies_json(NUTZUNG, leere_nutzung())
        tag_eintrag(n, date.today().isoformat())["abgelehnt"] += 1
        n["protokoll"].append({"zeit": jetzt(), "ort": ort, "wunsch": wunsch, "modus": "-",
                               "ergebnis": "abgelehnt", "grund": grund, "besucher": besucher})
        n["protokoll"] = n["protokoll"][-500:]
        schreib_json(NUTZUNG, n)


def verbleibend(k, besucher):
    lim = k["limits"]
    with SPERRE:
        n = lies_json(NUTZUNG, leere_nutzung())
    t = n["tage"].get(date.today().isoformat(), {"bilder": 0, "besucher": {}})
    return max(0, min(lim["proBesucherTag"] - t["besucher"].get(besucher, 0),
                      lim["gesamtTag"] - t["bilder"],
                      lim["gesamtMax"] - n["gesamt"]["bilder"]))


# ---------------------------------------------------------------- Bild erzeugen
class BildFehler(Exception):
    def __init__(self, oeffentlich, intern="", abgelehnt=False):
        super().__init__(intern or oeffentlich)
        self.oeffentlich, self.intern, self.abgelehnt = oeffentlich, intern or oeffentlich, abgelehnt


def openai_bild(k, grundbild, prompt):
    schluessel = os.environ.get("OPENAI_API_KEY", "").strip()
    if not schluessel:
        raise BildFehler("Die Bilderzeugung ist gerade nicht eingerichtet.",
                         "Kein API-Schlüssel: OPENAI_API_KEY fehlt in server/.env")
    bm = k["bildmodell"]
    felder = {"model": bm["modell"], "prompt": prompt, "n": "1", "size": bm["groesse"],
              "quality": bm["qualitaet"], "output_format": bm["format"]}
    if bm["format"] in ("jpeg", "webp"):
        felder["output_compression"] = "85"
    if bm.get("eingabetreue"):
        felder["input_fidelity"] = bm["eingabetreue"]
    grenze = "----kioasen" + secrets.token_hex(12)
    teile = []
    for name, wert in felder.items():
        teile.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                      % (grenze, name, wert)).encode("utf-8"))
    pfad = os.path.join(GRUNDBILDER, grundbild)
    with open(pfad, "rb") as f:
        daten = f.read()
    mime = BILD_MIME[os.path.splitext(grundbild)[1].lower()]
    teile.append(('--%s\r\nContent-Disposition: form-data; name="image"; filename="grundbild%s"\r\n'
                  'Content-Type: %s\r\n\r\n' % (grenze, os.path.splitext(grundbild)[1].lower(), mime)).encode()
                 + daten + b"\r\n")
    teile.append(("--%s--\r\n" % grenze).encode())
    anfrage = urllib.request.Request(OPENAI_URL, data=b"".join(teile), method="POST", headers={
        "Authorization": "Bearer " + schluessel,
        "Content-Type": "multipart/form-data; boundary=" + grenze})
    try:
        with urllib.request.urlopen(anfrage, timeout=240) as antwort:
            ergebnis = json.loads(antwort.read())
    except urllib.error.HTTPError as e:
        try:
            info = json.loads(e.read()).get("error", {}) or {}
        except ValueError:
            info = {}
        # OpenAI nennt bei falschem Schlüssel Teile davon: nie ins Protokoll oder Terminal übernehmen.
        meldung = re.sub(r"\b(sk|key)[-_][A-Za-z0-9*_-]+", "[Schlüssel]", str(info.get("message") or ""))[:400]
        if e.code == 401:
            meldung = "API-Schlüssel ungültig (in server/.env prüfen, er beginnt mit sk-). " + meldung
        if info.get("code") == "moderation_blocked" or "safety" in meldung.lower():
            raise BildFehler("Dieser Wunsch lässt sich leider nicht als Bild darstellen. Bitte anders formulieren.",
                             "Von OpenAI abgelehnt: " + meldung, abgelehnt=True)
        raise BildFehler("Das Bild konnte gerade nicht erzeugt werden. Bitte später noch einmal versuchen.",
                         "OpenAI HTTP %s: %s" % (e.code, meldung))
    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        raise BildFehler("Die Bilderzeugung antwortet gerade nicht. Bitte später noch einmal versuchen.",
                         "Verbindung: %s" % e)
    try:
        roh = base64.b64decode(ergebnis["data"][0]["b64_json"])
    except (KeyError, IndexError, TypeError, ValueError):
        raise BildFehler("Das Bild konnte gerade nicht erzeugt werden.", "Unerwartete Antwort der API")
    endung = {"jpeg": ".jpg", "png": ".png", "webp": ".webp"}[bm["format"]]
    name = secrets.token_hex(8) + endung
    with open(os.path.join(BILDER, name), "wb") as f:
        f.write(roh)
    return name


def erzeuge(k, ort, wunsch, besucher, ip, admin=False):
    """Gemeinsamer Ablauf für Besucher und Admin-Testbilder."""
    ok, meldung = reserviere(k, besucher, ip, admin=admin)
    if not ok:
        return 429, {"fehler": meldung}
    prompt = baue_prompt(k, ort, wunsch)
    beginn, eintrag = time.time(), {"zeit": jetzt(), "ort": ort["id"], "wunsch": wunsch, "modus": k["modus"],
                                    "stil": k["stilAktiv"], "besucher": besucher, "admin": admin}
    if k["modus"] == "simulation":
        time.sleep(2.5)
        eintrag.update(ergebnis="ok", dauer=round(time.time() - beginn, 1), bild="")
        abschliessen(k, besucher, ip, eintrag, True, 0)
        return 200, {"bild": "", "simulation": True, "grundbild": "bilder/grundbilder/" + ort["grundbild"],
                     "id": "", "verbleibend": verbleibend(k, besucher)}
    with API_PLAETZE:
        try:
            name = openai_bild(k, ort["grundbild"], prompt)
        except BildFehler as f:
            eintrag.update(ergebnis="abgelehnt" if f.abgelehnt else "fehler", grund=f.intern,
                           dauer=round(time.time() - beginn, 1))
            abschliessen(k, besucher, ip, eintrag, False, 0)
            print("  [Bild] %s" % f.intern)
            return 502 if not f.abgelehnt else 422, {"fehler": f.intern if admin else f.oeffentlich}
    eintrag.update(ergebnis="ok", dauer=round(time.time() - beginn, 1), bild=name)
    abschliessen(k, besucher, ip, eintrag, True, k["limits"]["kostenProBildEuro"])
    return 200, {"bild": "api/bilder/" + name, "id": name, "simulation": False,
                 "grundbild": "bilder/grundbilder/" + ort["grundbild"], "verbleibend": verbleibend(k, besucher)}


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "KI-Oasen"
    sys_version = ""

    def log_message(self, fmt, *args):
        print("  %s  %s" % (self.log_date_time_string(), fmt % args))

    # --- Cookies & Kennungen
    def cookie(self, name):
        for teil in (self.headers.get("Cookie") or "").split(";"):
            if "=" in teil:
                k, v = teil.strip().split("=", 1)
                if k == name:
                    return v
        return ""

    def besucher(self):
        bid = self.cookie("kio_id")
        if not re.match(r"^[a-f0-9]{32}$", bid):
            bid = secrets.token_hex(16)
            self._set_cookies.append("kio_id=%s; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax" % bid)
        return kennung("b:" + bid)

    def ip(self):
        return kennung("ip:" + self.client_address[0])

    def ist_admin(self):
        token = self.cookie("kio_admin")
        ablauf = ADMIN_SITZUNGEN.get(token)
        if ablauf and ablauf > time.time():
            return True
        ADMIN_SITZUNGEN.pop(token, None)
        return False

    # --- Antworten
    def kopf(self, status, typ, laenge, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", typ)
        self.send_header("Content-Length", str(laenge))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        for c in self._set_cookies:
            self.send_header("Set-Cookie", c)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def json(self, status, obj):
        roh = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.kopf(status, "application/json; charset=utf-8", len(roh), {"Cache-Control": "no-store"})
        self.wfile.write(roh)

    def datei(self, pfad, typ, cache="no-cache"):
        try:
            with open(pfad, "rb") as f:
                roh = f.read()
        except OSError:
            return self.json(404, {"fehler": "Nicht gefunden."})
        self.kopf(200, typ, len(roh), {"Cache-Control": cache})
        if self.command != "HEAD":
            self.wfile.write(roh)

    def koerper(self):
        laenge = int(self.headers.get("Content-Length") or 0)
        if laenge > MAX_BODY:
            raise ValueError("Anfrage zu groß.")
        try:
            daten = json.loads(self.rfile.read(laenge) or b"{}")
        except ValueError:
            raise ValueError("Ungültige Daten.")
        if not isinstance(daten, dict):
            raise ValueError("Ungültige Daten.")
        return daten

    def gleicher_ursprung(self):
        """Schreibende Anfragen nur von dieser Seite (Schutz vor fremden Websites)."""
        if self.headers.get("X-KIO") != "1":
            return False
        herkunft = self.headers.get("Origin")
        return not herkunft or urlparse(herkunft).netloc == self.headers.get("Host")

    # --- Routen
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self._set_cookies = []
        pfad = urlparse(self.path).path
        if pfad == "/api/status":
            return self.api_status()
        if pfad == "/api/galerie":
            return self.api_galerie()
        m = re.match(r"^/api/bilder/([a-f0-9]{16}\.(?:jpg|png|webp))$", pfad)
        if m:
            return self.datei(os.path.join(BILDER, m.group(1)), BILD_MIME["." + m.group(1).rsplit(".", 1)[1]],
                              "public, max-age=31536000, immutable")
        # /server/admin.html ist der Pfad des Login-Buttons; sonst bleibt der Ordner server/ gesperrt.
        if pfad in ("/admin", "/admin/", "/server/admin.html"):
            return self.datei(os.path.join(HIER, "admin.html"), STATISCH_ERLAUBT[".html"])
        if pfad.startswith("/api/admin/"):
            if not self.admin_bereit():
                return
            if pfad == "/api/admin/konfig":
                return self.json(200, {"konfig": konfig(), "grundbilder": grundbild_liste(),
                                       "schluessel": bool(os.environ.get("OPENAI_API_KEY", "").strip())})
            if pfad == "/api/admin/nutzung":
                return self.api_admin_nutzung()
            if pfad == "/api/admin/beitraege":
                with SPERRE:
                    liste = lies_json(BEITRAEGE, [])
                return self.json(200, {"beitraege": [{x: b.get(x) for x in b if x != "besucher"} for b in liste]})
            return self.json(404, {"fehler": "Unbekannt."})
        if pfad.startswith("/api/"):
            return self.json(404, {"fehler": "Unbekannt."})
        return self.statisch(pfad)

    def do_POST(self):
        self._set_cookies = []
        pfad = urlparse(self.path).path
        if not self.gleicher_ursprung():
            return self.json(403, {"fehler": "Nicht erlaubt."})
        try:
            daten = self.koerper()
        except ValueError as e:
            return self.json(400, {"fehler": str(e)})
        if pfad == "/api/bild":
            return self.api_bild(daten)
        if pfad == "/api/einreichen":
            return self.api_einreichen(daten)
        if pfad == "/api/admin/anmelden":
            return self.api_anmelden(daten)
        if pfad == "/api/admin/abmelden":
            ADMIN_SITZUNGEN.pop(self.cookie("kio_admin"), None)
            self._set_cookies.append("kio_admin=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")
            return self.json(200, {"ok": True})
        if pfad.startswith("/api/admin/"):
            if not self.admin_bereit():
                return
            if pfad == "/api/admin/konfig":
                try:
                    neu = pruefe_konfig(daten)
                except ValueError as e:
                    return self.json(400, {"fehler": str(e)})
                with SPERRE:
                    schreib_json(KONFIG, neu)
                return self.json(200, {"ok": True, "konfig": neu})
            if pfad == "/api/admin/standard":
                with SPERRE:
                    if os.path.exists(KONFIG):
                        os.replace(KONFIG, KONFIG + ".vorher")
                return self.json(200, {"ok": True, "konfig": konfig()})
            if pfad == "/api/admin/vorschau":
                k = konfig()
                ort = finde_ort(k, str(daten.get("ort", "")), nur_aktiv=False) or k["orte"][0]
                wunsch = saeubere_wunsch(daten.get("wunsch", ""), k["limits"]["maxZeichen"]) or "…"
                return self.json(200, {"prompt": baue_prompt(k, ort, wunsch)})
            if pfad == "/api/admin/testbild":
                k = konfig()
                ort = finde_ort(k, str(daten.get("ort", "")), nur_aktiv=False) or k["orte"][0]
                wunsch = saeubere_wunsch(daten.get("wunsch", ""), k["limits"]["maxZeichen"])
                if len(wunsch) < 3:
                    return self.json(400, {"fehler": "Bitte einen Testwunsch eingeben."})
                status, obj = erzeuge(k, ort, wunsch, "admin", "admin", admin=True)
                return self.json(status, obj)
            if pfad == "/api/admin/zuruecksetzen":
                bereich = daten.get("bereich")
                with SPERRE:
                    n = lies_json(NUTZUNG, leere_nutzung())
                    if bereich == "heute":
                        n["tage"].pop(date.today().isoformat(), None)
                    elif bereich == "alles":
                        n = leere_nutzung()
                    else:
                        return self.json(400, {"fehler": "Unbekannter Bereich."})
                    schreib_json(NUTZUNG, n)
                LETZTE_ANFRAGE.clear()
                return self.json(200, {"ok": True})
            if pfad == "/api/admin/beitrag":
                bid, status = str(daten.get("id", "")), daten.get("status")
                if status not in ("neu", "frei", "aus"):
                    return self.json(400, {"fehler": "Unbekannter Status."})
                with SPERRE:
                    liste = lies_json(BEITRAEGE, [])
                    for b in liste:
                        if b.get("id") == bid:
                            b["status"] = status
                    schreib_json(BEITRAEGE, liste)
                return self.json(200, {"ok": True})
            return self.json(404, {"fehler": "Unbekannt."})
        return self.json(404, {"fehler": "Unbekannt."})

    do_PUT = do_POST

    # --- Öffentliche API
    def api_status(self):
        k, besucher = konfig(), self.besucher()
        stil = next((s for s in k["stile"] if s["id"] == k["stilAktiv"]), k["stile"][0])
        self.json(200, {
            "aktiv": k["aktiv"], "modus": k["modus"], "stil": stil["name"],
            "maxZeichen": k["limits"]["maxZeichen"], "proTag": k["limits"]["proBesucherTag"],
            "verbleibend": verbleibend(k, besucher),
            "orte": [{"id": o["id"], "name": o["name"], "bild": "bilder/grundbilder/" + o["grundbild"]}
                     for o in k["orte"] if o["aktiv"]],
        })

    def api_bild(self, daten):
        k, besucher, ip = konfig(), self.besucher(), self.ip()
        ort = finde_ort(k, str(daten.get("ort", "")))
        if not ort:
            return self.json(400, {"fehler": "Bitte einen Blickwinkel wählen."})
        wunsch = saeubere_wunsch(daten.get("wunsch", ""), k["limits"]["maxZeichen"])
        if len(wunsch) < 3:
            return self.json(400, {"fehler": "Bitte beschreiben Sie Ihren Wunsch in ein paar Worten."})
        klein = wunsch.lower()
        treffer = next((w for w in k["sperrwoerter"] if w and w in klein), None)
        if treffer:
            protokolliere_ablehnung(besucher, ort["id"], wunsch, "Sperrwort: " + treffer)
            return self.json(422, {"fehler": "Dieser Wunsch lässt sich leider nicht darstellen. Bitte anders formulieren."})
        status, obj = erzeuge(k, ort, wunsch, besucher, ip)
        return self.json(status, obj)

    def api_einreichen(self, daten):
        k, besucher = konfig(), self.besucher()
        bild = str(daten.get("id", ""))
        ort = finde_ort(k, str(daten.get("ort", "")), nur_aktiv=False)
        wunsch = saeubere_wunsch(daten.get("wunsch", ""), k["limits"]["maxZeichen"])
        if not ort or len(wunsch) < 3:
            return self.json(400, {"fehler": "Unvollständige Angaben."})
        with SPERRE:
            n = lies_json(NUTZUNG, leere_nutzung())
            if bild and not any(p.get("bild") == bild and p.get("besucher") == besucher for p in n["protokoll"]):
                return self.json(403, {"fehler": "Dieses Bild gehört nicht zu Ihrer Sitzung."})
            liste = lies_json(BEITRAEGE, [])
            liste.append({
                "id": secrets.token_hex(6), "zeit": jetzt(), "ort": ort["id"], "wunsch": wunsch,
                "bild": ("api/bilder/" + bild) if bild else "", "grundbild": "bilder/grundbilder/" + ort["grundbild"],
                "alter": daten.get("alter") if daten.get("alter") in ANTWORTEN["alter"] else "",
                "bezug": daten.get("bezug") if daten.get("bezug") in ANTWORTEN["bezug"] else "",
                "haeufigkeit": daten.get("haeufigkeit") if daten.get("haeufigkeit") in ANTWORTEN["haeufigkeit"] else "",
                "status": "neu" if k["galerie"]["freigabePflicht"] else "frei", "besucher": besucher,
            })
            schreib_json(BEITRAEGE, liste[-2000:])
        return self.json(200, {"ok": True, "freigabe": k["galerie"]["freigabePflicht"]})

    def api_galerie(self):
        besucher = self.besucher()
        with SPERRE:
            liste = lies_json(BEITRAEGE, [])
        sichtbar = [{"wunsch": b["wunsch"], "bild": b["bild"], "grundbild": b.get("grundbild", ""),
                     "ort": b["ort"], "alter": b.get("alter", ""), "bezug": b.get("bezug", ""),
                     "eigen": b.get("besucher") == besucher, "status": b["status"]}
                    for b in reversed(liste)
                    if b["status"] == "frei" or (b.get("besucher") == besucher and b["status"] != "aus")]
        self.json(200, {"beitraege": sichtbar[:60]})

    # --- Admin
    def admin_bereit(self):
        if not os.environ.get("ADMIN_PASSWORT", "").strip():
            self.json(503, {"fehler": "Admin ist gesperrt: ADMIN_PASSWORT fehlt in server/.env."})
            return False
        if not self.ist_admin():
            self.json(401, {"fehler": "Bitte anmelden."})
            return False
        return True

    def api_anmelden(self, daten):
        soll = os.environ.get("ADMIN_PASSWORT", "").strip()
        if not soll:
            return self.json(503, {"fehler": "Admin ist gesperrt: ADMIN_PASSWORT fehlt in server/.env."})
        if ANMELDE_FEHLER["bis"] > time.time():
            return self.json(429, {"fehler": "Zu viele Versuche. Bitte eine Minute warten."})
        if not hmac.compare_digest(str(daten.get("passwort", "")).encode(), soll.encode()):
            ANMELDE_FEHLER["anzahl"] += 1
            if ANMELDE_FEHLER["anzahl"] >= 5:
                ANMELDE_FEHLER.update(anzahl=0, bis=time.time() + 60)
            return self.json(401, {"fehler": "Passwort falsch."})
        ANMELDE_FEHLER["anzahl"] = 0
        token = secrets.token_urlsafe(32)
        ADMIN_SITZUNGEN[token] = time.time() + 8 * 3600
        self._set_cookies.append("kio_admin=%s; Path=/; Max-Age=28800; HttpOnly; SameSite=Strict" % token)
        return self.json(200, {"ok": True})

    def api_admin_nutzung(self):
        k = konfig()
        with SPERRE:
            n = lies_json(NUTZUNG, leere_nutzung())
        t = n["tage"].get(date.today().isoformat(), {"bilder": 0, "fehler": 0, "abgelehnt": 0, "besucher": {}})
        tage = sorted(n["tage"].items())[-14:]
        self.json(200, {
            "heute": {"bilder": t["bilder"], "fehler": t.get("fehler", 0), "abgelehnt": t.get("abgelehnt", 0),
                      "besucher": len([v for v in t["besucher"].values() if v])},
            "gesamt": n["gesamt"], "limits": k["limits"], "modus": k["modus"],
            "verlauf": [{"tag": d, "bilder": v["bilder"]} for d, v in tage],
            "protokoll": [{x: p.get(x) for x in ("zeit", "ort", "wunsch", "modus", "stil", "ergebnis", "grund",
                                                    "dauer", "bild", "admin")}
                          for p in reversed(n["protokoll"][-200:])],
        })

    # --- Statische Dateien
    def statisch(self, pfad):
        teile = [t for t in unquote(pfad).split("/") if t]
        if not teile:
            teile = ["index.html"]
        if any(t.startswith(".") or t == ".." for t in teile) or teile[0] in ("server", "werkzeug"):
            return self.json(404, {"fehler": "Nicht gefunden."})
        ziel = os.path.realpath(os.path.join(WEB, *teile))
        if os.path.isdir(ziel):
            ziel = os.path.join(ziel, "index.html")
        endung = os.path.splitext(ziel)[1].lower()
        if os.path.commonpath([ziel, os.path.realpath(WEB)]) != os.path.realpath(WEB) or endung not in STATISCH_ERLAUBT:
            return self.json(404, {"fehler": "Nicht gefunden."})
        self.datei(ziel, STATISCH_ERLAUBT[endung])


def main():
    lade_env()
    os.makedirs(BILDER, exist_ok=True)
    port = int(os.environ.get("PORT", "8787"))
    k = konfig()
    print("\n  KI-Oasen Demo-Server")
    print("  Demo:   http://%s:%d/demo.html" % (HOST, port))
    print("  Admin:  http://%s:%d/admin" % (HOST, port))
    print("  Modus:  %s · Stil: %s" % (k["modus"], k["stilAktiv"]))
    print("  API-Schlüssel: %s · Admin-Passwort: %s" % (
        "vorhanden" if os.environ.get("OPENAI_API_KEY", "").strip() else "FEHLT (nur Simulation möglich)",
        "gesetzt" if os.environ.get("ADMIN_PASSWORT", "").strip() else "FEHLT (Admin gesperrt)"))
    print("  Beenden mit ctrl+C\n")
    ThreadingHTTPServer((HOST, port), Handler).serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  Server beendet.")
