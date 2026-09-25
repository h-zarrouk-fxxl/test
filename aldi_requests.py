#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ALDI Talk Monitor (requests-Version, kein Browser nötig)
=========================================================
Funktioniert direkt in Termux ohne Playwright/Chromium.

INSTALLATION:
  pip install requests

USAGE:
  python aldi_requests.py --user 017612345678 --pass DeinPasswort

WIE ES FUNKTIONIERT:
  1. Loggt sich per HTTP-Session ein (wie ein Browser)
  2. Lädt die Account-Übersichtsseite und extrahiert den Balance-Wert
  3. Falls < 1 GB → bucht +1 GB über den API-Endpunkt
  4. Alle 2 Minuten wiederholen

HINWEIS:
  Wenn der Balance-Wert nicht gefunden wird, musst du einmalig
  mit --sniff die Network-Requests inspizieren (siehe unten).
"""

from __future__ import annotations
import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from typing import Optional

try:
    import requests
    from requests import Session
except ImportError:
    print("❌  requests fehlt. Bitte:\n  pip install requests")
    sys.exit(1)

# ══════════════════════════════════════════════════════════════════════════════
#  KONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

BASE_URL     = "https://www.alditalk-kundenportal.de"
LOGIN_URL    = f"{BASE_URL}/user/auth/login/"
OVERVIEW_URL = f"{BASE_URL}/user/auth/account-overview/"

# BFF Endpoints (aus den MFE-Bundles extrahiert)
BFF_BASE_209 = "https://www.alditalk-kundenportal.de/scs/bff/scs-209"  # Account Overview
BFF_BASE_215 = "https://www.alditalk-kundenportal.de/scs/bff/scs-215"  # Manage Topup

THRESHOLD_MB   = 1024    # Buche wenn < 1024 MB verbleibend
CHECK_SEC      = 120     # Prüfintervall (Sekunden)
DEFAULT_USER   = os.environ.get("ALDI_USER", "")
DEFAULT_PASS   = os.environ.get("ALDI_PASS", "")

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Mobile Safari/537.36"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("aldi")

# ══════════════════════════════════════════════════════════════════════════════
#  SESSION & LOGIN
# ══════════════════════════════════════════════════════════════════════════════

def create_session() -> Session:
    s = Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "de-DE,de;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    return s


def get_csrf_token(s: Session, url: str) -> Optional[str]:
    """Holt CSRF-Token von der Login-Seite."""
    try:
        r = s.get(url, timeout=15)
        # Suche nach CSRF-Token in HTML
        for pattern in [
            r'<input[^>]+name=["\']_token["\'][^>]+value=["\']([^"\']+)["\']',
            r'<input[^>]+name=["\']csrf[_-]?token["\'][^>]+value=["\']([^"\']+)["\']',
            r'"csrf_token"\s*:\s*"([^"]+)"',
            r'"_token"\s*:\s*"([^"]+)"',
            r'csrfToken["\s:=]+["\']([^"\']+)["\']',
        ]:
            m = re.search(pattern, r.text, re.IGNORECASE)
            if m:
                return m.group(1)
    except Exception as e:
        log.debug(f"CSRF-Fetch-Fehler: {e}")
    return None


def do_login(s: Session, username: str, password: str) -> bool:
    """Loggt ein. Gibt True bei Erfolg zurück."""
    log.info("🔐 Login wird durchgeführt...")

    # Login-Seite laden (Session + ggf. CSRF)
    try:
        r = s.get(LOGIN_URL, timeout=15)
    except Exception as e:
        log.error(f"Login-Seite nicht erreichbar: {e}")
        return False

    csrf = get_csrf_token(s, LOGIN_URL)
    if csrf:
        log.debug(f"CSRF-Token: {csrf[:10]}...")

    # Login POST
    payload: dict = {
        "username": username,
        "password": password,
    }
    if csrf:
        payload["_token"] = csrf
        payload["csrf_token"] = csrf

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": LOGIN_URL,
        "Origin": BASE_URL,
    }

    try:
        r = s.post(LOGIN_URL, data=payload, headers=headers, timeout=20, allow_redirects=True)
    except Exception as e:
        log.error(f"Login POST fehlgeschlagen: {e}")
        return False

    # Erfolgs-Check: kein /login mehr in URL, oder Schlüsselwörter im Body
    final_url = r.url
    body = r.text

    if '/login' not in final_url:
        log.info(f"✅ Login erfolgreich! URL: {final_url[:60]}")
        return True

    if any(kw in body for kw in ['Abmelden', 'Mein Konto', 'account-overview', 'Kontoübersicht']):
        log.info("✅ Login erfolgreich (Inhalt)!")
        return True

    # Fehlermeldungen im Body suchen
    for err_kw in ['Falsches Passwort', 'Ungültige', 'Fehler', 'invalid', 'incorrect']:
        if err_kw.lower() in body.lower():
            log.error(f"❌ Login fehlgeschlagen – Fehlermeldung: '{err_kw}' gefunden")
            return False

    log.error(f"❌ Login fehlgeschlagen – URL: {final_url[:80]}")
    return False


def is_still_logged_in(s: Session) -> bool:
    """Prüft ob Session noch gültig ist."""
    try:
        r = s.get(OVERVIEW_URL, timeout=10, allow_redirects=True)
        return '/login' not in r.url
    except Exception:
        return False

# ══════════════════════════════════════════════════════════════════════════════
#  BALANCE LESEN
# ══════════════════════════════════════════════════════════════════════════════

def parse_balance_from_html(html: str) -> Optional[int]:
    """
    Versucht den Datenstand aus der HTML-Seite zu extrahieren.
    Manche SPAs betten den initialen State als JSON ein.
    """
    # Suche nach eingebettetem JSON-State (Vue/Nuxt Initial State)
    patterns = [
        r'window\.__nuxt__\s*=\s*({.+?})\s*</script>',
        r'window\.__INITIAL_STATE__\s*=\s*({.+?})\s*</script>',
        r'window\.__pinia\s*=\s*({.+?})\s*</script>',
        r'"allocated"\s*:\s*(\d+)',    # direkt nach "allocated" in KB
    ]

    for pattern in patterns:
        m = re.search(pattern, html, re.DOTALL)
        if m:
            try:
                if pattern.endswith(r'(\d+)'):
                    # Direkte Zahl (allocated in KB)
                    alloc_kb = int(m.group(1))
                    # Suche "used"
                    used_m = re.search(r'"used"\s*:\s*(\d+)', html)
                    if used_m:
                        used_kb = int(used_m.group(1))
                        rem_mb = (alloc_kb - used_kb) // 1024
                        return rem_mb
                else:
                    data = json.loads(m.group(1))
                    # Durchsuche JSON nach balance-Feldern
                    rem = _find_balance_in_json(data)
                    if rem is not None:
                        return rem
            except Exception as e:
                log.debug(f"JSON-Parse-Fehler: {e}")

    return None


def _find_balance_in_json(obj, depth=0) -> Optional[int]:
    """Rekursiv JSON nach Datenvolumen-Feldern durchsuchen."""
    if depth > 10:
        return None
    if isinstance(obj, dict):
        # Typisches ALDI Talk Format: {allocated: X, used: Y, type: "data"}
        if obj.get('type') == 'data' and 'allocated' in obj and 'used' in obj:
            alloc = obj['allocated']
            used  = obj['used']
            if isinstance(alloc, (int, float)) and alloc > 0:
                unit = obj.get('unit', '')
                if 'kilo' in unit.lower() or alloc > 100000:
                    return int((alloc - used) // 1024)  # KB → MB
                return int(alloc - used)  # schon in MB
        for v in obj.values():
            result = _find_balance_in_json(v, depth + 1)
            if result is not None:
                return result
    elif isinstance(obj, list):
        for item in obj:
            result = _find_balance_in_json(item, depth + 1)
            if result is not None:
                return result
    return None


def get_balance_from_bff(s: Session) -> Optional[int]:
    """
    Ruft BFF-Endpunkte auf um den Datenstand zu bekommen.
    Probiert verschiedene bekannte Endpunkte.
    """
    # Diese Endpunkte wurden aus den MFE-Netzwerkrequests extrahiert
    # Kundennummer wird aus dem Session-State ermittelt
    endpoints_to_try = [
        # scs-209 Account Overview (könnte Datenstände enthalten)
        f"{BFF_BASE_209}/account-overview/v1/overview",
        f"{BFF_BASE_209}/account-overview/v1/packages",
        f"{BFF_BASE_209}/account-overview/v1/usage",
        f"{BFF_BASE_209}/account-overview/v1/balance",
        # scs-215 Manage Topup
        f"{BFF_BASE_215}/manage-topup/v1/balance",
        f"{BFF_BASE_215}/manage-topup/v1/dataBalance",
        f"{BFF_BASE_215}/manage-topup/v1/usage",
    ]

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Referer": OVERVIEW_URL,
        "X-Requested-With": "XMLHttpRequest",
    }

    for url in endpoints_to_try:
        try:
            r = s.get(url, headers=headers, timeout=10)
            if r.status_code == 200:
                try:
                    data = r.json()
                    log.debug(f"✓ Endpoint {url.split('/')[-1]}: {json.dumps(data)[:200]}")
                    rem = _find_balance_in_json(data)
                    if rem is not None and rem > 0:
                        log.info(f"  Balance-Endpoint gefunden: {url}")
                        return rem
                except Exception:
                    pass
            elif r.status_code not in (404, 401, 403):
                log.debug(f"  {r.status_code} {url.split('/')[-1]}")
        except Exception as e:
            log.debug(f"  Fehler {url.split('/')[-1]}: {e}")

    return None


def get_balance(s: Session) -> Optional[int]:
    """Haupt-Balance-Abfrage: HTML → BFF → None."""
    # 1. Versuche BFF-Endpoints (schneller)
    bal = get_balance_from_bff(s)
    if bal is not None:
        return bal

    # 2. Fallback: HTML der Übersichtsseite parsen
    try:
        r = s.get(OVERVIEW_URL, timeout=15)
        if r.status_code == 200:
            bal = parse_balance_from_html(r.text)
            if bal is not None:
                return bal
            # Debug: HTML-Snippet ausgeben
            log.debug("HTML-Snippet (für manuelle Analyse):")
            log.debug(r.text[:500])
    except Exception as e:
        log.debug(f"HTML-Abruf-Fehler: {e}")

    return None

# ══════════════════════════════════════════════════════════════════════════════
#  BUCHUNG
# ══════════════════════════════════════════════════════════════════════════════

# TODO: Endpunkt wird durch einmaliges Ausführen von aldi_bypass.js ermittelt.
# Sobald bekannt, hier eintragen und den Kommentar entfernen.
BOOKING_ENDPOINT: Optional[str] = None   # z.B. "https://...scs-215.../onDemandRefill"
BOOKING_METHOD   = "POST"
BOOKING_PAYLOAD  = {}   # JSON-Body (wenn nötig)


def attempt_booking(s: Session) -> bool:
    """Löst die +1 GB Buchung aus."""

    if not BOOKING_ENDPOINT:
        log.warning("⚠️  BOOKING_ENDPOINT noch nicht bekannt!")
        log.warning("    Führe einmalig aldi_bypass.js in der Browser-Console aus,")
        log.warning("    um den Endpunkt zu ermitteln, dann hier eintragen.")
        return False

    log.info(f"🔥 Buchung wird ausgelöst: {BOOKING_ENDPOINT}")

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Referer": OVERVIEW_URL,
        "Origin": BASE_URL,
    }

    try:
        if BOOKING_METHOD == "POST":
            r = s.post(BOOKING_ENDPOINT, json=BOOKING_PAYLOAD, headers=headers, timeout=15)
        else:
            r = s.get(BOOKING_ENDPOINT, headers=headers, timeout=15)

        log.info(f"  Response: HTTP {r.status_code}")
        try:
            data = r.json()
            log.info(f"  Body: {json.dumps(data)[:300]}")
        except Exception:
            log.info(f"  Body: {r.text[:200]}")

        if r.status_code in (200, 201, 204):
            log.info("✅ Buchung ERFOLGREICH!")
            return True
        else:
            log.warning(f"❌ Buchung fehlgeschlagen (HTTP {r.status_code})")
            return False

    except Exception as e:
        log.error(f"Buchungs-Fehler: {e}")
        return False

# ══════════════════════════════════════════════════════════════════════════════
#  SNIFF-MODUS: Endpunkt automatisch finden
# ══════════════════════════════════════════════════════════════════════════════

def sniff_endpoints(s: Session) -> None:
    """
    Ruft alle bekannten BFF-Endpoints auf und zeigt die Antworten.
    Hilft beim Finden des Buchungs-Endpunkts.
    Ausführen mit: python aldi_requests.py --user ... --pass ... --sniff
    """
    log.info("=== SNIFF-MODUS: Alle BFF-Endpoints testen ===")

    # Alle plausiblen Endpunkte
    candidates = []
    for bff in [BFF_BASE_209, BFF_BASE_215]:
        for path in [
            '/v1/overview', '/v1/usage', '/v1/balance', '/v1/packages',
            '/v1/dataBalance', '/v1/onDemandRefill', '/v1/refill',
            '/v1/topup', '/v1/booking', '/v1/book',
        ]:
            base_name = bff.split('scs-')[-1].split('-')[0]
            candidates.append((f"{bff}{path}", f"scs-{base_name}"))

    headers = {
        "Accept": "application/json",
        "Referer": OVERVIEW_URL,
    }

    print("\nGET-Requests:")
    for url, name in candidates:
        try:
            r = s.get(url, headers=headers, timeout=8)
            if r.status_code != 404:
                print(f"  [{r.status_code}] {url}")
                if r.status_code == 200:
                    try:
                        print(f"       → {json.dumps(r.json())[:150]}")
                    except Exception:
                        print(f"       → {r.text[:100]}")
        except Exception:
            pass

    print("\n✅ Sniff fertig. Trage den Buchungs-Endpoint in BOOKING_ENDPOINT ein.")

# ══════════════════════════════════════════════════════════════════════════════
#  HAUPTSCHLEIFE
# ══════════════════════════════════════════════════════════════════════════════

def monitor_loop(s: Session, username: str, password: str) -> None:
    errors = 0
    bookings = 0

    log.info("\n%s", "═" * 55)
    log.info("  Monitor aktiv – alle %ds, Schwelle: %d MB", CHECK_SEC, THRESHOLD_MB)
    log.info("%s\n", "═" * 55)

    while True:
        now = datetime.now().strftime("%H:%M")

        try:
            # Session prüfen
            if not is_still_logged_in(s):
                log.warning("Session abgelaufen – erneuter Login...")
                if not do_login(s, username, password):
                    log.error("Login fehlgeschlagen! Warte 60s...")
                    time.sleep(60)
                    continue

            # Balance lesen
            remaining = get_balance(s)

            if remaining is None:
                errors += 1
                log.warning("[%s] Balance nicht lesbar (#%d)", now, errors)
                if errors >= 5:
                    log.error("Zu viele Fehler – Script beendet.")
                    break
                time.sleep(30)
                continue

            errors = 0
            log.info("[%s] 📊 %d MB verbleibend", now, remaining)

            if remaining < THRESHOLD_MB:
                log.info("💡 %d MB < %d MB → BUCHUNG!", remaining, THRESHOLD_MB)
                if attempt_booking(s):
                    bookings += 1
                    log.info("✅ Buchung #%d", bookings)
                    time.sleep(15)
                else:
                    time.sleep(60)
                    continue

        except KeyboardInterrupt:
            log.info("\n⛔ Gestoppt.")
            break
        except Exception as e:
            log.error(f"Fehler: {e}")

        time.sleep(CHECK_SEC)

# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    p = argparse.ArgumentParser(description="ALDI Talk Monitor (requests)")
    p.add_argument('--user',     default=DEFAULT_USER, help='Rufnummer')
    p.add_argument('--pass',     default=DEFAULT_PASS, dest='password', help='Passwort')
    p.add_argument('--interval', default=CHECK_SEC,    type=int, help=f'Prüfintervall Sek. (Standard: {CHECK_SEC})')
    p.add_argument('--threshold',default=THRESHOLD_MB, type=int, help=f'Schwelle MB (Standard: {THRESHOLD_MB})')
    p.add_argument('--sniff',    action='store_true',             help='Alle Endpunkte durchprobieren')
    args = p.parse_args()

    global CHECK_SEC, THRESHOLD_MB
    CHECK_SEC    = args.interval
    THRESHOLD_MB = args.threshold

    if not args.user or not args.password:
        print("❌  Rufnummer und Passwort angeben:")
        print("    python aldi_requests.py --user 017612345678 --pass DeinPasswort")
        sys.exit(1)

    s = create_session()

    if not do_login(s, args.user, args.password):
        sys.exit(1)

    if args.sniff:
        sniff_endpoints(s)
        return

    monitor_loop(s, args.user, args.password)


if __name__ == '__main__':
    main()
