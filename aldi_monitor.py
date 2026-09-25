#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════════════╗
║          ALDI Talk Auto-Refill Monitor  v5  (Termux / Android)          ║
║                                                                          ║
║  • Prüft Datenvolumen und bucht kostenlos +1 GB wenn < 1 GB             ║
║  • Echtzeit-Tracking via /proc/net/dev (kein Portal-Aufruf nötig)       ║
║  • Cookie-Persistenz: nach erstem Login kein erneuter Login              ║
║  • Smartes Intervall: seltener bei viel Guthaben, öfter wenn knapp      ║
╚══════════════════════════════════════════════════════════════════════════╝

INSTALLATION (proot-distro Ubuntu auf Android):
───────────────────────────────────────────────
  apt update && apt install -y python3 python3-pip chromium
  pip3 install playwright --break-system-packages
  export PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=$(which chromium)
  echo 'export PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=$(which chromium)' >> ~/.bashrc

USAGE:
  python3 aldi_monitor.py --user 017612345678 --pass DeinPasswort
  ALDI_USER=017612345678 ALDI_PASS=DeinPasswort python3 aldi_monitor.py

  Optionen:
    --interval N      Basis-Prüfintervall in Sekunden (Standard: smart)
    --threshold N     Buchen wenn < N MB verbleibend (Standard: 1024 = 1 GB)
    --no-tracker      /proc/net/dev Echtzeit-Tracking deaktivieren

Ctrl+C zum Beenden.
"""

from __future__ import annotations
import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

try:
    from playwright.async_api import (
        async_playwright, Page, BrowserContext,
        TimeoutError as PWTimeout,
    )
except ImportError:
    print("❌  Playwright fehlt. Bitte installieren:")
    print("      pip3 install playwright --break-system-packages")
    sys.exit(1)

# ══════════════════════════════════════════════════════════════════════════════
#  KONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

DASHBOARD_URL = "https://www.alditalk-kundenportal.de/portal/auth/uebersicht/"
OVERVIEW_URL  = "https://www.alditalk-kundenportal.de/user/auth/account-overview/"
LOGIN_URL     = "https://login.alditalk-kundenbetreuung.de/signin/XUI/#login/"

COOKIE_FILE   = Path("aldi_cookies.json")
STATE_FILE    = Path("aldi_state.json")

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Mobile Safari/537.36"
)

THRESHOLD_MB  = 1024   # Buche wenn < 1 GB
DEFAULT_USER  = os.environ.get("ALDI_USER", "")
DEFAULT_PASS  = os.environ.get("ALDI_PASS", "")

# ══════════════════════════════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("aldi")

# ══════════════════════════════════════════════════════════════════════════════
#  ECHTZEIT-DATENVERBRAUCH via /proc/net/dev
# ══════════════════════════════════════════════════════════════════════════════

class DataTracker:
    """Verfolgt den lokalen Datenverbrauch über /proc/net/dev.

    So erkennt das Script wie schnell Daten verbraucht werden,
    ohne das Portal aufzurufen – ähnlich wie das Flugzeugmodus-Trick:
    Den aktuellen Zählerstand vor/nach einer Wartezeit lesen und
    daraus die Rate berechnen.
    """

    def __init__(self):
        self._samples: deque = deque(maxlen=20)   # (timestamp, bytes)
        self._mobile_ifaces: list[str] = []
        self._enabled = self._detect_proc()

    def _detect_proc(self) -> bool:
        return Path("/proc/net/dev").exists()

    def _detect_interfaces(self) -> list[str]:
        """Erkennt Mobile-Daten-Interfaces (rmnet, ccmni, wwan, ppp)."""
        ifaces = []
        try:
            with open("/proc/net/dev") as f:
                for line in f:
                    parts = line.strip().split()
                    if not parts:
                        continue
                    iface = parts[0].rstrip(":")
                    prefixes = ("rmnet", "ccmni", "wwan", "ppp", "usb", "mobile")
                    if any(iface.lower().startswith(p) for p in prefixes):
                        ifaces.append(iface)
        except Exception:
            pass
        return ifaces

    def _read_bytes(self) -> int:
        """Liest aktuelle RX+TX Bytes von allen mobilen Interfaces."""
        if not self._enabled:
            return 0
        if not self._mobile_ifaces:
            self._mobile_ifaces = self._detect_interfaces()
            if self._mobile_ifaces:
                log.info("📡 Mobil-Interfaces gefunden: %s", self._mobile_ifaces)
            else:
                log.warning("⚠️  Kein mobiles Interface in /proc/net/dev gefunden")
                # Alle Interfaces außer lo summieren als Fallback
        total = 0
        try:
            with open("/proc/net/dev") as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) < 10:
                        continue
                    iface = parts[0].rstrip(":")
                    if iface == "lo":
                        continue
                    # Nur mobile Interfaces wenn bekannt, sonst alles
                    if self._mobile_ifaces and iface not in self._mobile_ifaces:
                        continue
                    try:
                        total += int(parts[1]) + int(parts[9])  # rx_bytes + tx_bytes
                    except (ValueError, IndexError):
                        pass
        except Exception:
            pass
        return total

    def sample(self):
        """Messwert aufnehmen (regelmäßig aufrufen)."""
        if not self._enabled:
            return
        b = self._read_bytes()
        if b > 0:
            self._samples.append((time.monotonic(), b))

    @property
    def rate_mb_per_sec(self) -> Optional[float]:
        """Durchschnittliche Verbrauchsrate in MB/s (letzte Messungen)."""
        if len(self._samples) < 2:
            return None
        t0, b0 = self._samples[0]
        t1, b1 = self._samples[-1]
        dt = t1 - t0
        if dt < 1.0:
            return None
        delta = b1 - b0
        if delta < 0:   # Zähler-Überlauf oder Interface-Reset
            self._samples.clear()
            return None
        return delta / dt / (1024 * 1024)

    def seconds_until_threshold(self, remaining_mb: float, threshold_mb: float = 1024) -> Optional[float]:
        """Schätzt Sekunden bis remaining_mb auf threshold_mb sinkt."""
        rate = self.rate_mb_per_sec
        if rate is None or rate <= 0:
            return None
        mb_to_go = remaining_mb - threshold_mb
        if mb_to_go <= 0:
            return 0.0
        return mb_to_go / rate

    def status_line(self, remaining_mb: float) -> str:
        rate = self.rate_mb_per_sec
        if rate is None:
            return f"{remaining_mb:.0f} MB verbleibend (Rate: unbekannt)"
        secs = self.seconds_until_threshold(remaining_mb)
        if secs is None:
            eta = "∞"
        elif secs < 3600:
            eta = f"{secs/60:.0f} min"
        else:
            eta = f"{secs/3600:.1f} h"
        return (
            f"{remaining_mb:.0f} MB verbleibend | "
            f"Rate: {rate*1024:.1f} KB/s | "
            f"Buchung in ca. {eta}"
        )


# ══════════════════════════════════════════════════════════════════════════════
#  SMARTES INTERVALL
# ══════════════════════════════════════════════════════════════════════════════

def smart_interval(remaining_mb: float, tracker: DataTracker) -> int:
    """Berechnet nächstes Prüfintervall in Sekunden.

    Nahe am Schwellwert → häufiger prüfen.
    Viel Guthaben → seltener prüfen (spart Akku).
    """
    # Wenn Echtzeit-Tracking aktiv: Zeit bis Schwellwert schätzen
    secs = tracker.seconds_until_threshold(remaining_mb)
    if secs is not None:
        if secs < 120:
            return 30      # Kritisch – alle 30 s
        elif secs < 600:
            return 60      # Knapp – jede Minute
        elif secs < 3600:
            return 120     # Bald – alle 2 Min

    # Fallback nach verbleibender Menge
    if remaining_mb < 1100:
        return 60           # < 1.1 GB → jede Minute
    elif remaining_mb < 2048:
        return 120          # < 2 GB → alle 2 Min
    elif remaining_mb < 5120:
        return 300          # < 5 GB → alle 5 Min
    elif remaining_mb < 10240:
        return 900          # < 10 GB → alle 15 Min
    else:
        return 1800         # > 10 GB → alle 30 Min


# ══════════════════════════════════════════════════════════════════════════════
#  BROWSER HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def wait_quiet(page: Page, timeout: int = 8000) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout)
    except PWTimeout:
        pass


async def handle_cookie_banner(page: Page) -> bool:
    await page.wait_for_timeout(800)
    selectors = [
        'button[data-testid="uc-deny-all-button"]',   # UserCentrics (proj1)
        '#onetrust-accept-btn-handler',
        'button:has-text("Alle akzeptieren")',
        'button:has-text("Nur notwendige Cookies")',
        'button:has-text("Ablehnen")',
        'button:has-text("Necessary only")',
        '[data-testid="cookie-accept"]',
    ]
    for sel in selectors:
        try:
            btn = await page.query_selector(sel)
            if btn and await btn.is_visible():
                await btn.click()
                log.info("🍪 Cookie-Banner geschlossen (%s)", sel)
                await page.wait_for_timeout(600)
                return True
        except Exception:
            pass
    return False


async def is_logged_in(page: Page) -> bool:
    url = page.url
    if "login.alditalk-kundenbetreuung.de" in url or "/login" in url:
        return False
    try:
        body = await page.evaluate("document.body ? document.body.innerText : ''")
        if any(kw in body for kw in ["Abmelden", "Übersicht", "Mein Konto", "Kontoübersicht"]):
            return True
    except Exception:
        pass
    return "/auth/" in url


# ══════════════════════════════════════════════════════════════════════════════
#  LOGIN
# ══════════════════════════════════════════════════════════════════════════════

async def do_login(page: Page, username: str, password: str) -> bool:
    """ForgeRock/OpenAM Login.

    Selektoren laut Community-Analyse der ALDI Talk Seite (proj1):
      #input-5  = Rufnummer-Feld
      #input-6  = Passwort-Feld
    Fallback: type=password-basiert.

    Cookies werden nach erfolgreichem Login gespeichert.
    """
    log.info("🔐 Navigiere zum Dashboard...")
    try:
        await page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=35000)
    except Exception as e:
        log.error("Navigation fehlgeschlagen: %s", e)
        return False

    await wait_quiet(page)
    await handle_cookie_banner(page)

    if await is_logged_in(page):
        log.info("✅ Bereits eingeloggt (Cookie gültig)!")
        return True

    log.info("   → Nicht eingeloggt. Navigiere zu ForgeRock Login-Seite...")
    try:
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        log.error("Login-URL nicht erreichbar: %s", e)
        return False

    await wait_quiet(page)
    await handle_cookie_banner(page)

    # ── Warte bis Formular gerendert (max. 60s – langsame Android-Geräte) ────
    log.info("   Warte auf Formular (max. 60s)...")
    try:
        await page.wait_for_function(
            "() => document.querySelectorAll('input').length > 0",
            timeout=60000,
            polling=1000,
        )
    except PWTimeout:
        body = await page.evaluate(
            "document.body ? document.body.innerText.substring(0, 300) : 'LEER'"
        )
        log.error("❌ Kein Formular nach 60s! URL: %s\nSeite: %s", page.url, body)
        return False

    # ── Debug: welche Inputs sind da? ────────────────────────────────────────
    inputs_info = await page.evaluate("""
        () => Array.from(document.querySelectorAll('input')).map((el, i) => ({
            i, type: el.type || 'text', id: el.id, name: el.name,
            placeholder: el.placeholder, visible: el.offsetParent !== null
        }))
    """)
    log.info("   Inputs gefunden: %s", inputs_info)

    # ── Passwort-Feld (höchste Priorität: type=password) ─────────────────────
    pw_field = None
    for sel in [
        'input[type="password"]',   # Sicherste Methode
        '#input-6',                 # ALDI Talk ForgeRock (bekannt aus Community)
        '#idToken2',                # ForgeRock Standard (2-Feld-Login)
        'input[name="IDToken2"]',
        '#idToken1',                # ForgeRock Passwort-only (einziges Feld = PW)
        'input[name="IDToken1"]',
    ]:
        try:
            f = await page.query_selector(sel)
            if f and await f.is_visible():
                pw_field = f
                log.info("   Passwort-Feld: %s", sel)
                break
        except Exception:
            pass

    if not pw_field:
        # Absoluter Fallback: erstes sichtbares Input
        for inp in await page.query_selector_all("input"):
            if await inp.is_visible():
                pw_field = inp
                log.warning("   Passwort-Feld: Fallback (erstes sichtbares Input)")
                break

    if not pw_field:
        log.error("❌ Kein Passwort-Feld gefunden! URL: %s", page.url)
        return False

    # ── Username-Feld (optional – nur auf vollständiger Login-Seite) ──────────
    user_field = None
    for sel in [
        '#input-5',                 # ALDI Talk ForgeRock Rufnummer-Feld
        'input[type="tel"]',
        '#idToken1',
        'input[name="IDToken1"]',
        'input[name="username"]',
    ]:
        try:
            f = await page.query_selector(sel)
            if f and await f.is_visible() and f != pw_field:
                el_type = (await f.get_attribute("type") or "text").lower()
                if el_type != "password":
                    user_field = f
                    log.info("   Rufnummer-Feld: %s", sel)
                    break
        except Exception:
            pass

    if user_field:
        await user_field.fill(username)
        log.info("   Rufnummer eingegeben: %s****", username[:4])
    else:
        log.info("   Kein Rufnummer-Feld → Passwort-only Modus")

    await pw_field.fill(password)
    log.info("   Passwort eingegeben")

    # ── Absenden ──────────────────────────────────────────────────────────────
    submitted = False
    for sel in [
        '[class*="button--solid"]',     # ALDI Talk Anmelden-Button (proj1)
        '#loginButton_0',
        'input[type="submit"]',
        'button[type="submit"]',
        'button:has-text("Anmelden")',
        'button:has-text("Login")',
        'button:has-text("Einloggen")',
        'button:has-text("Weiter")',
    ]:
        try:
            btn = await page.query_selector(sel)
            if btn and await btn.is_visible():
                await btn.click()
                submitted = True
                log.info("   Abgeschickt (%s)", sel)
                break
        except Exception:
            pass

    if not submitted:
        await pw_field.press("Enter")
        log.info("   Abgeschickt (Enter)")

    # ── Warte auf Weiterleitung ───────────────────────────────────────────────
    try:
        await page.wait_for_url(
            lambda u: "login.alditalk-kundenbetreuung.de" not in u,
            timeout=25000,
        )
    except PWTimeout:
        log.warning("   Weiterleitung dauert zu lange (URL: %s)", page.url)

    await wait_quiet(page)
    await handle_cookie_banner(page)

    ok = await is_logged_in(page)
    if ok:
        log.info("✅ Login erfolgreich! URL: %s", page.url)
    else:
        log.error("❌ Login fehlgeschlagen! URL: %s", page.url)
    return ok


# ══════════════════════════════════════════════════════════════════════════════
#  BALANCE LESEN
# ══════════════════════════════════════════════════════════════════════════════

# CSS-Selektoren aus Community-Analyse (proj1) – direkter als Shadow-DOM-Traversal
_BALANCE_SELECTORS = [
    # Standard-Tarif
    (
        "one-stack.usage-meter:nth-child(1) > one-usage-meter:nth-child(1) "
        "> one-group:nth-child(1) > one-heading:nth-child(2)",
        "one-stack.usage-meter:nth-child(1) > one-usage-meter:nth-child(1) "
        "> one-button:nth-child(3)",
    ),
    # Zweite Variante (Community+ oder alternatives Layout)
    (
        "one-stack.usage-meter:nth-child(2) > one-usage-meter:nth-child(1) "
        "> one-group:nth-child(1) > one-heading:nth-child(2)",
        "one-stack.usage-meter:nth-child(2) > one-usage-meter:nth-child(1) "
        "> one-button:nth-child(3)",
    ),
    # Nested stack-Variante
    (
        "one-stack.usage-meter:nth-child(1) > one-stack:nth-child(1) "
        "> one-usage-meter:nth-child(1) > one-group:nth-child(1) > one-heading:nth-child(2)",
        "one-stack.usage-meter:nth-child(1) > one-stack:nth-child(1) "
        "> one-usage-meter:nth-child(1) > one-button:nth-child(3)",
    ),
]

# Shadow-DOM JS-Fallback (unser bisheriger Ansatz)
_JS_SHADOW_BALANCE = """
(function() {
  function findBtn(root, d) {
    if (d > 15 || !root || !root.querySelectorAll) return null;
    var all = root.querySelectorAll('*');
    for (var i = 0; i < all.length; i++) {
      var el = all[i];
      if (/button/i.test(el.tagName) && /1\\s*GB/i.test(el.textContent || '')) return el;
      if (el.shadowRoot) { var f = findBtn(el.shadowRoot, d+1); if (f) return f; }
    }
    return null;
  }
  var btn = findBtn(document, 0);
  if (!btn) return { error: 'button_not_found' };
  var vm = btn.__vueParentComponent;
  if (!vm) return { error: 'vue_not_found' };
  var bd = vm.proxy && vm.proxy.baseData;
  if (!bd || !bd[0]) return { error: 'baseData_not_found' };
  var alloc_kb = bd[0].allocated || 0;
  var used_kb  = bd[0].used      || 0;
  var rem_mb   = Math.round((alloc_kb - used_kb) / 1024);
  return {
    remaining_mb: rem_mb,
    allocated_mb: Math.round(alloc_kb / 1024),
    used_mb:      Math.round(used_kb  / 1024),
    on_demand_ok: !!(bd[0].isOnDemandRefillApplicable),
    error: null
  };
})()
"""

_JS_SHADOW_CLICK = """
(function() {
  function findBtn(root, d) {
    if (d > 15 || !root || !root.querySelectorAll) return null;
    var all = root.querySelectorAll('*');
    for (var i = 0; i < all.length; i++) {
      var el = all[i];
      if (/button/i.test(el.tagName) && /1\\s*GB/i.test(el.textContent || '')) return el;
      if (el.shadowRoot) { var f = findBtn(el.shadowRoot, d+1); if (f) return f; }
    }
    return null;
  }
  var btn = findBtn(document, 0);
  if (!btn) return { clicked: false, reason: 'not_found' };
  btn.scrollIntoView({ block: 'center' });
  btn.click();
  btn.dispatchEvent(new MouseEvent('click', { bubbles: true, composed: true }));
  return { clicked: true };
})()
"""


async def read_balance(page: Page) -> Optional[dict]:
    """Liest verbleibendes Datenvolumen. Gibt dict oder None zurück."""
    # Sicherstellen dass wir auf der richtigen Seite sind
    if "uebersicht" not in page.url and "account-overview" not in page.url:
        try:
            await page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=30000)
            await wait_quiet(page, 12000)
            await page.wait_for_timeout(2000)
        except Exception as e:
            log.warning("Navigation zur Übersicht fehlgeschlagen: %s", e)

    # Versuch 1: CSS-Selektoren (direkt, schnell)
    for bal_sel, _btn_sel in _BALANCE_SELECTORS:
        try:
            el = await page.query_selector(bal_sel)
            if el:
                text = await el.text_content()
                if text:
                    m = re.search(r"([\d.,]+)\s*(GB|MB)", text)
                    if m:
                        val, unit = m.groups()
                        val = float(val.replace(",", "."))
                        mb = val * 1024 if unit == "GB" else val
                        log.debug("Balance via CSS-Selektor: %s MB", mb)
                        return {"remaining_mb": mb, "selector": bal_sel}
        except Exception:
            pass

    # Versuch 2: Shadow-DOM JS (Vue-State)
    try:
        result = await page.evaluate(_JS_SHADOW_BALANCE)
        if result and not result.get("error"):
            log.debug("Balance via Shadow-DOM JS: %s MB", result.get("remaining_mb"))
            return result
        if result and result.get("error"):
            log.debug("Shadow-DOM Fehler: %s", result["error"])
    except Exception as e:
        log.debug("Shadow-DOM Ausnahme: %s", e)

    # Versuch 3: Text-Suche im gesamten Body
    try:
        body = await page.evaluate("document.body ? document.body.innerText : ''")
        m = re.search(r"([\d.,]+)\s*(GB|MB)\s*(?:verbleibend|übrig|remaining)", body)
        if m:
            val, unit = m.groups()
            val = float(val.replace(",", "."))
            mb = val * 1024 if unit == "GB" else val
            log.debug("Balance via Text-Suche: %s MB", mb)
            return {"remaining_mb": mb}
    except Exception:
        pass

    return None


async def attempt_booking(page: Page) -> bool:
    """Versucht den +1 GB Button zu klicken. Gibt True bei Erfolg zurück."""
    log.info("📦 Versuche +1 GB zu buchen...")

    # CSS-Selektoren (proj1)
    for _bal_sel, btn_sel in _BALANCE_SELECTORS:
        try:
            btn = await page.query_selector(btn_sel)
            if btn:
                text = await btn.text_content() or ""
                if "1 GB" in text:
                    is_disabled = await btn.get_attribute("disabled")
                    if is_disabled is not None:
                        log.warning("   Button ist deaktiviert – Buchung nicht verfügbar")
                        return False
                    await btn.scroll_into_view_if_needed()
                    await btn.click()
                    log.info("   ✅ Button geklickt (%s)", btn_sel)
                    await page.wait_for_timeout(3000)
                    # Bestätigungs-Dialog schließen wenn vorhanden
                    for conf_sel in ['button:has-text("Ok")', 'button:has-text("Bestätigen")',
                                     'button:has-text("Schließen")', '[data-testid="modal-close"]']:
                        try:
                            c = await page.query_selector(conf_sel)
                            if c and await c.is_visible():
                                await c.click()
                                log.info("   Bestätigungs-Dialog geschlossen (%s)", conf_sel)
                                break
                        except Exception:
                            pass
                    return True
        except Exception as e:
            log.debug("CSS-Selektor Fehler (%s): %s", btn_sel, e)

    # Shadow-DOM JS-Fallback
    try:
        result = await page.evaluate(_JS_SHADOW_CLICK)
        if result and result.get("clicked"):
            log.info("   ✅ Button geklickt (Shadow-DOM JS)")
            await page.wait_for_timeout(3000)
            return True
        elif result:
            log.warning("   Button-Klick fehlgeschlagen: %s", result.get("reason"))
    except Exception as e:
        log.warning("   Shadow-DOM Klick Ausnahme: %s", e)

    return False


# ══════════════════════════════════════════════════════════════════════════════
#  HAUPT-MONITOR-SCHLEIFE
# ══════════════════════════════════════════════════════════════════════════════

async def monitor_loop(username: str, password: str, threshold_mb: int,
                       fixed_interval: Optional[int], use_tracker: bool):
    tracker = DataTracker() if use_tracker else DataTracker.__new__(DataTracker)
    if use_tracker and not tracker._enabled:
        log.warning("⚠️  /proc/net/dev nicht verfügbar – Echtzeit-Tracking deaktiviert")
        use_tracker = False

    async with async_playwright() as pw:
        # Chromium-Pfad (Termux/proot Ubuntu)
        exec_path = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH", "")
        chromium = pw.chromium

        launch_kwargs = dict(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                  "--single-process"],
        )
        if exec_path:
            launch_kwargs["executable_path"] = exec_path

        log.info("🚀 Browser wird gestartet (headless)...")
        browser = await chromium.launch(**launch_kwargs)

        # Cookie-Persistenz laden
        ctx_kwargs = dict(user_agent=USER_AGENT)
        if COOKIE_FILE.exists():
            log.info("🍪 Gespeicherte Cookies geladen (%s)", COOKIE_FILE)
            ctx_kwargs["storage_state"] = str(COOKIE_FILE)

        context: BrowserContext = await browser.new_context(**ctx_kwargs)
        page: Page = await context.new_page()

        # ── Erster Login ──────────────────────────────────────────────────────
        logged_in = await do_login(page, username, password)
        if not logged_in:
            log.error("❌ Login fehlgeschlagen – Script beendet.")
            await browser.close()
            return

        # Cookies speichern
        await context.storage_state(path=str(COOKIE_FILE))
        log.info("🍪 Cookies gespeichert → %s", COOKIE_FILE)

        # ── State laden (letzter bekannter Balance-Stand) ─────────────────────
        last_remaining_mb = float("inf")
        try:
            state = json.loads(STATE_FILE.read_text())
            last_remaining_mb = float(state.get("remaining_mb", "inf"))
        except Exception:
            pass

        # Initialer Tracker-Sample
        if use_tracker:
            tracker._mobile_ifaces = tracker._detect_interfaces()
            tracker.sample()

        log.info("═" * 60)
        log.info("📊 Monitor läuft. Schwellwert: %d MB (%.1f GB)", threshold_mb, threshold_mb/1024)
        if use_tracker:
            log.info("📡 Echtzeit-Tracking aktiv via /proc/net/dev")
        log.info("Ctrl+C zum Beenden")
        log.info("═" * 60)

        consecutive_errors = 0

        while True:
            try:
                # ── Echtzeit-Sample nehmen ────────────────────────────────────
                if use_tracker:
                    tracker.sample()

                # ── Prüfen ob wir jetzt den Stand vom Portal holen müssen ─────
                secs_to_threshold = None
                if use_tracker:
                    secs_to_threshold = tracker.seconds_until_threshold(last_remaining_mb, threshold_mb)

                # Portal IMMER abfragen beim ersten Mal, dann smart
                should_check = (
                    last_remaining_mb == float("inf")         # noch kein Stand
                    or last_remaining_mb < threshold_mb * 1.5  # unter 1.5 × Schwellwert
                    or (secs_to_threshold is not None and secs_to_threshold < 300)  # < 5 Min
                )

                if should_check:
                    # ── Seite aktualisieren ───────────────────────────────────
                    try:
                        await page.reload(wait_until="domcontentloaded", timeout=30000)
                        await wait_quiet(page, 10000)
                        await page.wait_for_timeout(2000)
                    except Exception as e:
                        log.warning("Reload fehlgeschlagen: %s", e)
                        # Session evtl. abgelaufen – neu einloggen
                        logged_in = await do_login(page, username, password)
                        if logged_in:
                            await context.storage_state(path=str(COOKIE_FILE))
                        continue

                    # ── Balance lesen ─────────────────────────────────────────
                    result = await read_balance(page)
                    if result is None:
                        log.warning("⚠️  Balance konnte nicht gelesen werden")
                        consecutive_errors += 1
                        if consecutive_errors >= 3:
                            log.warning("3× Fehler – versuche neu einzuloggen...")
                            await do_login(page, username, password)
                            await context.storage_state(path=str(COOKIE_FILE))
                            consecutive_errors = 0
                        await asyncio.sleep(60)
                        continue

                    consecutive_errors = 0
                    remaining_mb = result["remaining_mb"]
                    last_remaining_mb = remaining_mb

                    # State speichern
                    try:
                        STATE_FILE.write_text(json.dumps({
                            "remaining_mb": remaining_mb,
                            "checked_at": datetime.now().isoformat(),
                        }))
                    except Exception:
                        pass

                    # ── Status ausgeben ───────────────────────────────────────
                    if use_tracker:
                        log.info("📊 %s", tracker.status_line(remaining_mb))
                    else:
                        log.info(
                            "📊 %.0f MB (%.2f GB) verbleibend",
                            remaining_mb, remaining_mb / 1024
                        )

                    # ── Buchen wenn unter Schwellwert ─────────────────────────
                    if remaining_mb < threshold_mb:
                        log.warning(
                            "⚡ UNTER SCHWELLWERT! %.0f MB < %d MB → Buche +1 GB...",
                            remaining_mb, threshold_mb
                        )
                        success = await attempt_booking(page)
                        if success:
                            log.info("🎉 +1 GB erfolgreich gebucht!")
                            await page.wait_for_timeout(5000)
                            # Balance sofort neu lesen
                            result2 = await read_balance(page)
                            if result2:
                                last_remaining_mb = result2["remaining_mb"]
                                log.info("📊 Neuer Stand: %.0f MB", last_remaining_mb)
                        else:
                            log.error("❌ Buchung fehlgeschlagen!")
                else:
                    # Kein Portal-Aufruf – nur lokales Tracking-Update
                    if use_tracker:
                        log.info("📡 %s", tracker.status_line(last_remaining_mb))

                # ── Nächstes Intervall berechnen ──────────────────────────────
                if fixed_interval:
                    wait_secs = fixed_interval
                else:
                    wait_secs = smart_interval(last_remaining_mb, tracker)

                next_check = datetime.now() + timedelta(seconds=wait_secs)
                log.info("⏱️  Nächste Prüfung um %s (in %ds)", next_check.strftime("%H:%M:%S"), wait_secs)

                # ── Warten (mit Tracker-Sampling alle 10s) ────────────────────
                elapsed = 0
                while elapsed < wait_secs:
                    chunk = min(10, wait_secs - elapsed)
                    await asyncio.sleep(chunk)
                    elapsed += chunk
                    if use_tracker:
                        tracker.sample()

            except KeyboardInterrupt:
                log.info("\n🛑 Monitor beendet.")
                break
            except Exception as e:
                log.error("Unerwarteter Fehler: %s", e, exc_info=True)
                consecutive_errors += 1
                await asyncio.sleep(30)

        await browser.close()


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    parser = argparse.ArgumentParser(
        description="ALDI Talk Auto-Refill Monitor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--user", default=DEFAULT_USER,
                        help="ALDI Talk Rufnummer (oder ALDI_USER env var)")
    parser.add_argument("--pass", dest="password", default=DEFAULT_PASS,
                        help="ALDI Talk Passwort (oder ALDI_PASS env var)")
    parser.add_argument("--threshold", type=int, default=THRESHOLD_MB,
                        help=f"Buche wenn MB < N (Standard: {THRESHOLD_MB})")
    parser.add_argument("--interval", type=int, default=None,
                        help="Festes Prüfintervall in Sekunden (Standard: smart)")
    parser.add_argument("--no-tracker", action="store_true",
                        help="/proc/net/dev Echtzeit-Tracking deaktivieren")
    args = parser.parse_args()

    if not args.user or not args.password:
        parser.error(
            "Rufnummer und Passwort erforderlich!\n"
            "  --user 017612345678 --pass DeinPasswort\n"
            "  oder: ALDI_USER=... ALDI_PASS=... python3 aldi_monitor.py"
        )

    log.info("╔════════════════════════════════════════╗")
    log.info("║  ALDI Talk Auto-Refill Monitor  v5     ║")
    log.info("╚════════════════════════════════════════╝")
    log.info("User: %s****  Schwellwert: %d MB", args.user[:4], args.threshold)

    await monitor_loop(
        username=args.user,
        password=args.password,
        threshold_mb=args.threshold,
        fixed_interval=args.interval,
        use_tracker=not args.no_tracker,
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Beendet.")
