#!/usr/bin/env python3
# ============================================================
#   ALDI Talk Auto-Nachbuch Script – Termux / Android
#   Installieren: pip install requests beautifulsoup4
#   Starten:      python alditalk_auto.py
# ============================================================

import requests
import time
import os
import sys
from datetime import datetime
from bs4 import BeautifulSoup

# ============================================================
#   ✏️  KONFIGURATION – hier deine Daten eintragen
# ============================================================
TELEFONNUMMER = "+4915XXXXXXXXX"   # deine ALDI Talk Nummer (mit +49)
PASSWORT      = "DEIN_PASSWORT"   # dein Portal-Passwort

CHECK_INTERVAL = 300               # Sekunden zwischen Checks (300 = 5 Min)
DATEN_SCHWELLE = 200               # Buchen wenn < X MB übrig (0 = nur bei Limit)
# ============================================================

BASE_URL     = "https://www.alditalk-kundenportal.de"
LOGIN_URL    = f"{BASE_URL}/portal/login"
OVERVIEW_URL = f"{BASE_URL}/portal/auth/uebersicht/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 12; Pixel 6) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Mobile Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection":      "keep-alive",
}


# ============================================================
#   🌐  ECHTZEIT-DATENVERBRAUCH  (wie Flugmodus-Reset, aber live)
# ============================================================
class DataMonitor:
    """Liest Echtzeit-Mobilfunkdaten aus /proc/net/dev – kein Root nötig."""

    MOBILE_PREFIXES = ["rmnet", "wwan", "ccmni", "ppp", "lte", "v4-rmnet"]

    def __init__(self):
        self._last = {}

    def _read_proc(self):
        try:
            with open("/proc/net/dev") as f:
                lines = f.readlines()[2:]
            result = {}
            for line in lines:
                parts = line.split()
                if len(parts) < 10:
                    continue
                iface = parts[0].rstrip(":")
                if any(iface.startswith(p) for p in self.MOBILE_PREFIXES):
                    result[iface] = {
                        "rx": int(parts[1]),
                        "tx": int(parts[9]),
                    }
            return result
        except Exception:
            return {}

    def snapshot(self):
        now = self._read_proc()
        lines = []
        for iface, cur in now.items():
            prev = self._last.get(iface, cur)
            delta_rx = (cur["rx"] - prev["rx"]) / 1024 / 1024
            delta_tx = (cur["tx"] - prev["tx"]) / 1024 / 1024
            total_rx = cur["rx"] / 1024 / 1024 / 1024
            lines.append(
                f"  📡 {iface}: Gesamt ↓{total_rx:.2f} GB  "
                f"| Letzte {CHECK_INTERVAL//60}min: ↓{delta_rx:.2f} MB  ↑{delta_tx:.2f} MB"
            )
        self._last = now
        return lines or ["  ℹ️  Kein Mobilfunk-Interface gefunden (ggf. WLAN aktiv?)"]


# ============================================================
#   🔐  LOGIN & SESSION
# ============================================================
def neue_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def login(session: requests.Session) -> bool:
    """Loggt sich ins Portal ein. Gibt True zurück bei Erfolg."""
    try:
        # 1. Login-Seite holen → CSRF-Token lesen
        resp = session.get(LOGIN_URL, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # CSRF-Token suchen (verschiedene mögliche Feldnamen)
        csrf = None
        for name in ["_csrf", "csrf", "token", "authenticity_token"]:
            inp = soup.find("input", {"name": name})
            if inp:
                csrf = inp.get("value")
                break

        # Form-Action lesen
        form = soup.find("form")
        action = LOGIN_URL
        if form and form.get("action"):
            a = form["action"]
            action = a if a.startswith("http") else BASE_URL + a

        # Login-Daten zusammenbauen
        payload = {
            "j_username": TELEFONNUMMER,
            "j_password": PASSWORT,
        }
        if csrf:
            payload["_csrf"] = csrf

        # 2. Login absenden
        resp2 = session.post(action, data=payload, timeout=30, allow_redirects=True)

        # Erfolg prüfen
        if "login" in resp2.url.lower() and "fehler" in resp2.text.lower():
            log("✗ Login fehlgeschlagen – Zugangsdaten prüfen!")
            return False
        if "uebersicht" in resp2.url or "overview" in resp2.url:
            return True
        if "login" not in resp2.url.lower():
            return True

        log("✗ Login unklar – prüfe Ausgabe")
        return False

    except requests.exceptions.ConnectionError:
        log("✗ Keine Verbindung zum Portal (Drosselung aktiv?)")
        return False
    except Exception as e:
        log(f"✗ Login-Fehler: {e}")
        return False


# ============================================================
#   📶  FLUGMODUS-SIMULATION: Portal-Cache umgehen
# ============================================================
def portal_hard_refresh(session: requests.Session):
    """
    Simuliert den Flugmodus-Trick:
    Lädt die Übersichtsseite mit Cache-Buster-Headern neu.
    Das zwingt den Server, aktuellen Datenstand zu senden.
    """
    session.headers.update({
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma":        "no-cache",
        "Expires":       "0",
    })
    try:
        # Timestamp als URL-Parameter verhindert Browser-/Proxy-Cache
        ts = int(time.time())
        resp = session.get(f"{OVERVIEW_URL}?_={ts}", timeout=30)
        return resp
    except Exception:
        return None


# ============================================================
#   📦  1 GB NACHBUCHEN
# ============================================================
def verbleibende_mb(soup: BeautifulSoup) -> int | None:
    """Versucht das verbleibende Datenvolumen aus dem HTML zu lesen."""
    text = soup.get_text(" ")
    import re

    # Suche nach Mustern wie "0,23 GB", "850 MB", "0 MB"
    for pattern in [
        r"(\d+[,.]?\d*)\s*GB?\s*(?:verbleibend|übrig|noch|left|remaining)",
        r"(?:verbleibend|übrig|noch)\s*(\d+[,.]?\d*)\s*GB?",
        r"(\d+[,.]?\d*)\s*MB?\s*(?:verbleibend|übrig|noch|left)",
    ]:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            val = float(m.group(1).replace(",", "."))
            # GB → MB umrechnen wenn nötig
            if "GB" in pattern:
                return int(val * 1024)
            return int(val)
    return None  # konnte nicht lesen


def buch_1gb(session: requests.Session) -> str:
    """
    Lädt Übersichtsseite, sucht +1GB Button/Link/Form und klickt ihn.
    Rückgabe: 'gebucht', 'nicht_verfuegbar', 'fehler'
    """
    try:
        resp = portal_hard_refresh(session)
        if not resp or resp.status_code != 200:
            return "fehler"

        # Session abgelaufen?
        if "login" in resp.url.lower():
            return "session_abgelaufen"

        soup = BeautifulSoup(resp.text, "html.parser")

        # Verbleibende MB anzeigen
        mb = verbleibende_mb(soup)
        if mb is not None:
            log(f"  📊 Verbleibend (laut Portal): ~{mb} MB")
            if mb > DATEN_SCHWELLE:
                return "nicht_noetig"

        # ── Strategie 1: Formular mit Nachbuch-Kontext finden ──
        for form in soup.find_all("form"):
            form_text = form.get_text(" ").lower()
            if any(k in form_text for k in ["nachbuch", "1 gb", "+1", "datenvolumen buchen"]):
                action = form.get("action", "")
                action = action if action.startswith("http") else BASE_URL + action
                payload = {
                    inp["name"]: inp.get("value", "")
                    for inp in form.find_all("input")
                    if inp.get("name")
                }
                r = session.post(action, data=payload, timeout=30)
                if r.status_code in (200, 302):
                    return "gebucht"

        # ── Strategie 2: Button mit passendem Text finden ──
        for btn in soup.find_all(["button", "a", "input"]):
            btn_text = (btn.get_text() + btn.get("value", "") + btn.get("href", "")).lower()
            if any(k in btn_text for k in ["nachbuch", "+1 gb", "1 gb buchen", "datenvolumen"]):
                href = btn.get("href", "")
                if href:
                    url = href if href.startswith("http") else BASE_URL + href
                    r = session.get(url, timeout=30)
                    if r.status_code in (200, 302):
                        return "gebucht"

        # ── Strategie 3: Direkte API-Endpunkte probieren ──
        api_pfade = [
            "/portal/auth/datenvolumen/nachbuchen",
            "/portal/auth/buchung/1gb",
            "/portal/auth/volumen/nachbuchen",
            "/portal/api/v1/booking/data",
        ]
        for pfad in api_pfade:
            try:
                r = session.post(BASE_URL + pfad, timeout=10)
                if r.status_code == 200:
                    return "gebucht"
            except Exception:
                continue

        return "nicht_verfuegbar"

    except requests.exceptions.ConnectionError:
        return "keine_verbindung"
    except Exception as e:
        log(f"  ✗ Buchungs-Fehler: {e}")
        return "fehler"


# ============================================================
#   🖨️  LOGGING
# ============================================================
def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def trennlinie():
    print("─" * 55, flush=True)


# ============================================================
#   🔄  HAUPT-SCHLEIFE
# ============================================================
def main():
    if TELEFONNUMMER == "+4915XXXXXXXXX" or PASSWORT == "DEIN_PASSWORT":
        print("❌ Bitte zuerst TELEFONNUMMER und PASSWORT im Script eintragen!")
        sys.exit(1)

    monitor = DataMonitor()
    session = neue_session()
    eingeloggt = False
    login_fehlversuche = 0
    MAX_FEHLVERSUCHE = 5

    trennlinie()
    log("🚀 ALDI Talk Auto-Nachbuch Script gestartet")
    log(f"   Interval: alle {CHECK_INTERVAL // 60} Minuten")
    log(f"   Buchen wenn < {DATEN_SCHWELLE} MB übrig")
    trennlinie()

    while True:
        trennlinie()
        log("📋 Check läuft...")

        # ── Echtzeit-Datenverbrauch anzeigen ──
        for zeile in monitor.snapshot():
            print(zeile, flush=True)

        # ── Login (falls nötig) ──
        if not eingeloggt:
            log("🔐 Einloggen...")
            eingeloggt = login(session)
            if eingeloggt:
                log("✓ Login erfolgreich")
                login_fehlversuche = 0
            else:
                login_fehlversuche += 1
                if login_fehlversuche >= MAX_FEHLVERSUCHE:
                    log(f"✗ {MAX_FEHLVERSUCHE}× Login fehlgeschlagen – Script gestoppt.")
                    log("  Zugangsdaten prüfen oder Portal manuell aufrufen.")
                    sys.exit(1)
                wartezeit = min(60 * login_fehlversuche, 300)
                log(f"  Warte {wartezeit}s vor nächstem Login-Versuch...")
                time.sleep(wartezeit)
                continue

        # ── 1 GB buchen ──
        log("📶 Versuche 1 GB nachzubuchen...")
        ergebnis = buch_1gb(session)

        if ergebnis == "gebucht":
            log("✅ 1 GB erfolgreich nachgebucht!")
        elif ergebnis == "nicht_noetig":
            log(f"ℹ️  Noch > {DATEN_SCHWELLE} MB übrig – kein Nachbuchen nötig")
        elif ergebnis == "nicht_verfuegbar":
            log("⏳ Button noch nicht verfügbar (Portal-Cache hinkt hinterher)")
        elif ergebnis == "session_abgelaufen":
            log("🔄 Session abgelaufen – beim nächsten Check neu einloggen")
            eingeloggt = False
            session = neue_session()
        elif ergebnis == "keine_verbindung":
            log("📵 Keine Verbindung (Drosselung?) – warte auf nächsten Check")
            eingeloggt = False
            session = neue_session()
        else:
            log("✗ Unbekannter Fehler – prüfe Portal manuell")

        # ── Warten bis zum nächsten Check ──
        log(f"⏱️  Nächster Check in {CHECK_INTERVAL // 60} Minuten...")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n👋 Script gestoppt (Ctrl+C)")
