#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════════════╗
║          ALDI Talk Auto-Refill Monitor  v6  (Termux / Android)          ║
║                                                                          ║
║  • Prüft Datenvolumen und bucht kostenlos +1 GB wenn < 1 GB             ║
║  • Echtzeit-Tracking via /proc/net/dev (auch via Termux-Bridge)         ║
║  • Cookie-Persistenz: nach erstem Login kein erneuter Login              ║
║  • Smartes Intervall: seltener bei viel Guthaben, öfter wenn knapp      ║
║  • PDP-Refresh: erzwingt Carrier-Datenneuregistrierung (--pdp-refresh)  ║
║  • Buchungs-Verifikation: prüft ob +1 GB wirklich gebucht wurde         ║
╚══════════════════════════════════════════════════════════════════════════╝

INSTALLATION (proot-distro Ubuntu auf Android):
───────────────────────────────────────────────
  apt update && apt install -y python3 python3-pip chromium
  pip3 install playwright --break-system-packages
  export PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=$(which chromium)
  echo 'export PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=$(which chromium)' >> ~/.bashrc

USAGE:
  python3 aldi_monitor.py --user 017612345678 --pass DeinPasswort

  Optionen:
    --interval N      Basis-Prüfintervall in Sekunden (Standard: smart)
    --threshold N     Buchen wenn < N MB verbleibend (Standard: 1024 = 1 GB)
    --no-tracker      /proc/net/dev Echtzeit-Tracking deaktivieren
    --pdp-refresh     PDP-Refresh nach Portal-Abfrage (braucht Root/termux-su)
    --bridge-file F   Bridge-Datei für /proc/net/dev (Standard: /sdcard/aldi_netdev.txt)
    --visible         (kein Effekt auf proot – headless bleibt aktiv)

ECHTZEIT-TRACKING in proot-distro:
────────────────────────────────────
  Das Script versucht automatisch /proc/net/dev über mehrere Wege zu lesen.
  Wenn das nicht funktioniert, starte diesen Befehl in HOST-Termux (nicht proot!):

    while true; do cat /proc/net/dev > /sdcard/aldi_netdev.txt; sleep 5; done &

  Dann läuft das Script mit: --bridge-file /sdcard/aldi_netdev.txt

PDP-REFRESH (Flugzeugmodus-Alternative):
──────────────────────────────────────────
  Statt 20s Flugzeugmodus: nur ~2s offline mit Root-Zugriff.
  Starte mit: --pdp-refresh
  (Benötigt: tsu/su in Termux, oder termux-su)

Ctrl+C zum Beenden.
"""

from __future__ import annotations
import argparse
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple

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

# Standard Bridge-Datei (von Host-Termux geschrieben)
BRIDGE_FILE_DEFAULT = Path("/sdcard/aldi_netdev.txt")

# Termux Host-Binaries (zum Lesen von /proc/net/dev außerhalb proot)
TERMUX_BIN_DIR = Path("/data/data/com.termux/files/usr/bin")

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
#  PDP-REFRESH  (Flugzeugmodus-Alternative – benötigt Root)
# ══════════════════════════════════════════════════════════════════════════════

def force_pdp_refresh() -> bool:
    """Erzwingt Carrier-Datenneuregistrierung ohne Flugzeugmodus.

    Was Flugzeugmodus macht: Radio aus/ein → Gerät re-registriert sich beim Carrier →
    Carrier sendet aktuellen Datenstand (PDP-Kontext-Erneuerung).

    Diese Funktion macht dasselbe in ~2 Sekunden statt 20 Sekunden Flugzeugmodus.
    Benötigt Root-Zugriff (tsu/su in Termux).
    """
    methods = [
        # Methode 1: Android svc (Standard-Android-Tool)
        {
            "cmd": ["su", "-c", "svc data disable; sleep 1; svc data enable"],
            "desc": "svc data toggle",
            "timeout": 8,
        },
        # Methode 2: tsu (Termux Root)
        {
            "cmd": ["tsu", "-c", "svc data disable; sleep 1; svc data enable"],
            "desc": "tsu svc data toggle",
            "timeout": 8,
        },
        # Methode 3: termux-su
        {
            "cmd": [str(TERMUX_BIN_DIR / "termux-su"), "-c",
                    "svc data disable; sleep 1; svc data enable"],
            "desc": "termux-su svc data toggle",
            "timeout": 8,
        },
    ]

    for m in methods:
        try:
            r = subprocess.run(
                m["cmd"],
                capture_output=True, text=True,
                timeout=m["timeout"],
            )
            if r.returncode == 0:
                log.info("🔄 PDP-Refresh (%s) → warte 4s auf Neuregistrierung...", m["desc"])
                time.sleep(4)   # Carrier braucht ~3-4s zum Neuregistrieren
                log.info("🔄 PDP-Refresh abgeschlossen")
                return True
            else:
                log.debug("PDP-Methode '%s' Exit %d: %s", m["desc"], r.returncode, r.stderr.strip())
        except FileNotFoundError:
            log.debug("PDP-Methode '%s': Befehl nicht gefunden", m["desc"])
        except subprocess.TimeoutExpired:
            log.debug("PDP-Methode '%s': Timeout", m["desc"])
        except Exception as e:
            log.debug("PDP-Methode '%s': %s", m["desc"], e)

    log.warning("⚠️  PDP-Refresh: Kein Root-Zugriff verfügbar. "
                "Tipp: 'pkg install tsu' in Host-Termux für Root.")
    return False


# ══════════════════════════════════════════════════════════════════════════════
#  ECHTZEIT-DATENVERBRAUCH via /proc/net/dev
# ══════════════════════════════════════════════════════════════════════════════

class DataTracker:
    """Verfolgt den lokalen Datenverbrauch über /proc/net/dev.

    In proot-distro Ubuntu ist /proc/net/dev oft nicht verfügbar.
    Mehrere Fallback-Methoden werden versucht:

    1. /proc/net/dev direkt
    2. /proc/1/net/dev (PID-1-Namespace)
    3. Host-Termux cat-Binary (/data/data/com.termux/.../cat /proc/net/dev)
    4. Bridge-Datei (von Host-Termux periodisch geschrieben)

    Wenn alles scheitert: Tracker deaktiviert, nur Portal-Balance wird genutzt.
    """

    def __init__(self, bridge_file: Optional[Path] = None):
        self._samples: deque = deque(maxlen=20)   # (timestamp, bytes)
        self._mobile_ifaces: list[str] = []
        self._bridge_file: Optional[Path] = bridge_file
        self._read_method: Optional[str] = None   # welche Methode funktioniert
        self._termux_cat: Optional[str] = None    # Pfad zu Termux-cat wenn nötig
        self._enabled = self._detect_method()

    def _detect_method(self) -> bool:
        """Findet eine funktionierende Methode, /proc/net/dev zu lesen."""

        # Methode 1: direkte /proc/net/dev
        for proc_path in ["/proc/net/dev", "/proc/1/net/dev"]:
            try:
                content = Path(proc_path).read_text()
                if "Inter-|" in content or "Inter-" in content:
                    self._read_method = f"direct:{proc_path}"
                    log.info("📡 /proc/net/dev via: %s", proc_path)
                    return True
            except Exception:
                pass

        # Methode 2: Host-Termux cat-Binary
        cat_bin = TERMUX_BIN_DIR / "cat"
        if cat_bin.exists():
            try:
                r = subprocess.run(
                    [str(cat_bin), "/proc/net/dev"],
                    capture_output=True, text=True, timeout=3
                )
                if r.returncode == 0 and "Inter-" in r.stdout:
                    self._read_method = f"termux-cat"
                    self._termux_cat = str(cat_bin)
                    log.info("📡 /proc/net/dev via Termux-Host-Binary: %s", cat_bin)
                    return True
            except Exception as e:
                log.debug("Termux-cat fehlgeschlagen: %s", e)

        # Methode 3: Bridge-Datei
        if self._bridge_file and self._bridge_file.exists():
            try:
                content = self._bridge_file.read_text()
                if "Inter-" in content:
                    self._read_method = f"bridge:{self._bridge_file}"
                    log.info("📡 /proc/net/dev via Bridge-Datei: %s", self._bridge_file)
                    return True
            except Exception:
                pass

        # Alle Methoden gescheitert
        log.warning(
            "⚠️  /proc/net/dev nicht verfügbar (proot-Einschränkung).\n"
            "   Echtzeit-Tracking deaktiviert. Portal wird als alleinige Quelle genutzt.\n"
            "   Für Echtzeit-Tracking: Starte in HOST-Termux:\n"
            "     while true; do cat /proc/net/dev > /sdcard/aldi_netdev.txt; sleep 5; done &\n"
            "   Dann starte Monitor mit: --bridge-file /sdcard/aldi_netdev.txt"
        )
        return False

    def _read_proc_content(self) -> Optional[str]:
        """Liest /proc/net/dev über die erkannte Methode."""
        if not self._read_method:
            return None
        try:
            if self._read_method.startswith("direct:"):
                path = self._read_method.split(":", 1)[1]
                return Path(path).read_text()
            elif self._read_method == "termux-cat":
                r = subprocess.run(
                    [self._termux_cat, "/proc/net/dev"],
                    capture_output=True, text=True, timeout=3
                )
                return r.stdout if r.returncode == 0 else None
            elif self._read_method.startswith("bridge:"):
                return self._bridge_file.read_text()
        except Exception as e:
            log.debug("_read_proc_content Fehler: %s", e)
        return None

    def _detect_interfaces(self) -> list[str]:
        """Erkennt Mobile-Daten-Interfaces (rmnet, ccmni, wwan, ppp)."""
        content = self._read_proc_content()
        if not content:
            return []
        ifaces = []
        for line in content.splitlines():
            parts = line.strip().split()
            if not parts:
                continue
            iface = parts[0].rstrip(":")
            prefixes = ("rmnet", "ccmni", "wwan", "ppp", "usb", "mobile", "lte")
            if any(iface.lower().startswith(p) for p in prefixes):
                ifaces.append(iface)
        return ifaces

    def _read_bytes(self) -> int:
        """Liest aktuelle RX+TX Bytes von allen mobilen Interfaces."""
        if not self._enabled:
            return 0
        if not self._mobile_ifaces:
            self._mobile_ifaces = self._detect_interfaces()
            if self._mobile_ifaces:
                log.info("📡 Mobil-Interfaces: %s", self._mobile_ifaces)
            else:
                log.debug("Kein mobiles Interface gefunden – nutze alle außer lo")

        content = self._read_proc_content()
        if not content:
            return 0

        total = 0
        for line in content.splitlines():
            parts = line.strip().split()
            if len(parts) < 10:
                continue
            iface = parts[0].rstrip(":")
            if iface == "lo":
                continue
            if self._mobile_ifaces and iface not in self._mobile_ifaces:
                continue
            try:
                total += int(parts[1]) + int(parts[9])  # rx_bytes + tx_bytes
            except (ValueError, IndexError):
                pass
        return total

    def sample(self):
        """Messwert aufnehmen."""
        if not self._enabled:
            return
        b = self._read_bytes()
        if b > 0:
            self._samples.append((time.monotonic(), b))

    @property
    def rate_mb_per_sec(self) -> Optional[float]:
        """Durchschnittliche Verbrauchsrate in MB/s."""
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

    def seconds_until_threshold(self, remaining_mb: float,
                                threshold_mb: float = 1024) -> Optional[float]:
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
    """Berechnet nächstes Prüfintervall in Sekunden."""
    secs = tracker.seconds_until_threshold(remaining_mb)
    if secs is not None:
        if secs < 120:
            return 30
        elif secs < 600:
            return 60
        elif secs < 3600:
            return 120

    if remaining_mb < 1100:
        return 60
    elif remaining_mb < 2048:
        return 120
    elif remaining_mb < 5120:
        return 300
    elif remaining_mb < 10240:
        return 900
    else:
        return 1800


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
        'button[data-testid="uc-deny-all-button"]',
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
    """ForgeRock/OpenAM Login."""
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

    # Warte bis Formular gerendert (max. 60s – langsame Android-Geräte)
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

    # Debug: welche Inputs sind da?
    inputs_info = await page.evaluate("""
        () => Array.from(document.querySelectorAll('input')).map((el, i) => ({
            i, type: el.type || 'text', id: el.id, name: el.name,
            placeholder: el.placeholder, visible: el.offsetParent !== null
        }))
    """)
    log.info("   Inputs gefunden: %s", inputs_info)

    # Passwort-Feld (höchste Priorität: type=password)
    pw_field = None
    for sel in [
        'input[type="password"]',
        '#input-6',                 # ALDI Talk ForgeRock (bekannt)
        '#idToken4',                # Aus Debug-Log bekannt
        '#idToken2',
        'input[name="IDToken2"]',
        '#idToken1',
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
        for inp in await page.query_selector_all("input"):
            if await inp.is_visible():
                pw_field = inp
                log.warning("   Passwort-Feld: Fallback (erstes sichtbares Input)")
                break

    if not pw_field:
        log.error("❌ Kein Passwort-Feld gefunden! URL: %s", page.url)
        return False

    # Username-Feld (optional – nur auf vollständiger Login-Seite)
    user_field = None
    for sel in [
        '#input-5',
        '#idToken3',                # Aus Debug-Log bekannt
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

    submitted = False
    for sel in [
        '[class*="button--solid"]',
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

_BALANCE_SELECTORS = [
    (
        "one-stack.usage-meter:nth-child(1) > one-usage-meter:nth-child(1) "
        "> one-group:nth-child(1) > one-heading:nth-child(2)",
        "one-stack.usage-meter:nth-child(1) > one-usage-meter:nth-child(1) "
        "> one-button:nth-child(3)",
    ),
    (
        "one-stack.usage-meter:nth-child(2) > one-usage-meter:nth-child(1) "
        "> one-group:nth-child(1) > one-heading:nth-child(2)",
        "one-stack.usage-meter:nth-child(2) > one-usage-meter:nth-child(1) "
        "> one-button:nth-child(3)",
    ),
    (
        "one-stack.usage-meter:nth-child(1) > one-stack:nth-child(1) "
        "> one-usage-meter:nth-child(1) > one-group:nth-child(1) > one-heading:nth-child(2)",
        "one-stack.usage-meter:nth-child(1) > one-stack:nth-child(1) "
        "> one-usage-meter:nth-child(1) > one-button:nth-child(3)",
    ),
]

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
    if "uebersicht" not in page.url and "account-overview" not in page.url:
        try:
            await page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=30000)
            await wait_quiet(page, 12000)
            await page.wait_for_timeout(2000)
        except Exception as e:
            log.warning("Navigation zur Übersicht fehlgeschlagen: %s", e)

    # Versuch 1: CSS-Selektoren
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
                        log.debug("Balance via CSS: %s MB", mb)
                        return {"remaining_mb": mb, "selector": bal_sel}
        except Exception:
            pass

    # Versuch 2: Shadow-DOM JS
    try:
        result = await page.evaluate(_JS_SHADOW_BALANCE)
        if result and not result.get("error"):
            log.debug("Balance via Shadow-DOM: %s MB", result.get("remaining_mb"))
            return result
        if result and result.get("error"):
            log.debug("Shadow-DOM Fehler: %s", result["error"])
    except Exception as e:
        log.debug("Shadow-DOM Ausnahme: %s", e)

    # Versuch 3: Text-Suche im Body
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


# ══════════════════════════════════════════════════════════════════════════════
#  BUCHUNG MIT VERIFIKATION
# ══════════════════════════════════════════════════════════════════════════════

async def close_confirmation_dialogs(page: Page) -> bool:
    """Schließt Bestätigungs-Dialoge nach der Buchung."""
    closed = False
    conf_selectors = [
        'button:has-text("Ok")',
        'button:has-text("OK")',
        'button:has-text("Bestätigen")',
        'button:has-text("Schließen")',
        'button:has-text("Fertig")',
        'button:has-text("Weiter")',
        '[data-testid="modal-close"]',
        '[aria-label="Schließen"]',
        '[aria-label="Close"]',
        'button.modal__close',
        '.modal-close',
    ]
    for sel in conf_selectors:
        try:
            btn = await page.query_selector(sel)
            if btn and await btn.is_visible():
                await btn.click()
                log.info("   Dialog geschlossen (%s)", sel)
                await page.wait_for_timeout(500)
                closed = True
        except Exception:
            pass
    return closed


async def attempt_booking(
    page: Page,
    old_balance_mb: float,
    pdp_refresh: bool = False,
) -> Tuple[bool, Optional[float]]:
    """Versucht den +1 GB Button zu klicken und verifiziert die Buchung.

    Returns:
        (success, new_balance_mb)
        success = True wenn Buchung verifiziert (Balance gestiegen)
        new_balance_mb = neuer Stand (oder None wenn nicht lesbar)
    """
    log.info("📦 Versuche +1 GB zu buchen...")

    clicked = False

    # Versuch 1: CSS-Selektoren
    for _bal_sel, btn_sel in _BALANCE_SELECTORS:
        try:
            btn = await page.query_selector(btn_sel)
            if btn:
                text = await btn.text_content() or ""
                if "1 GB" in text or "1GB" in text:
                    is_disabled = await btn.get_attribute("disabled")
                    if is_disabled is not None:
                        log.warning("   Button ist deaktiviert – Buchung nicht verfügbar.")
                        log.warning("   Mögliche Ursachen: Tarif S bereits maximal gebucht "
                                    "oder Zeitlimit noch nicht abgelaufen.")
                        return False, None
                    await btn.scroll_into_view_if_needed()
                    await btn.click()
                    log.info("   🖱️  Button geklickt (%s)", btn_sel)
                    clicked = True
                    break
        except Exception as e:
            log.debug("CSS-Selektor Fehler (%s): %s", btn_sel, e)

    # Versuch 2: Shadow-DOM JS
    if not clicked:
        try:
            result = await page.evaluate(_JS_SHADOW_CLICK)
            if result and result.get("clicked"):
                log.info("   🖱️  Button geklickt (Shadow-DOM JS)")
                clicked = True
            elif result:
                log.warning("   Button-Klick fehlgeschlagen: %s", result.get("reason"))
        except Exception as e:
            log.warning("   Shadow-DOM Klick Ausnahme: %s", e)

    if not clicked:
        log.error("❌ Kein Buchungs-Button gefunden!")
        return False, None

    # Warte auf Bestätigungs-Dialog (bis zu 8s)
    log.info("   Warte auf Bestätigungs-Dialog...")
    await page.wait_for_timeout(3000)
    await close_confirmation_dialogs(page)
    await page.wait_for_timeout(1000)
    # Nochmal prüfen (manchmal erscheint Dialog verzögert)
    await close_confirmation_dialogs(page)

    # PDP-Refresh falls gewünscht (damit Carrier neuen Stand sendet)
    if pdp_refresh:
        log.info("   🔄 PDP-Refresh nach Buchung...")
        force_pdp_refresh()

    # ── VERIFIKATION: Seite neu laden und Balance prüfen ──────────────────────
    log.info("   ⏳ Warte 8s dann Verifikation (Seite neu laden)...")
    await asyncio.sleep(8)

    log.info("   🔍 Verifikation: Lese neuen Kontostand...")
    try:
        await page.reload(wait_until="domcontentloaded", timeout=30000)
        await wait_quiet(page, 12000)
        await page.wait_for_timeout(3000)
    except Exception as e:
        log.warning("   Reload für Verifikation fehlgeschlagen: %s", e)

    result2 = await read_balance(page)
    if result2 is None:
        log.warning("   ⚠️  Buchung: Button wurde geklickt, aber neuer Stand nicht lesbar.")
        log.warning("   Bitte manuell im Portal prüfen: %s", DASHBOARD_URL)
        return True, None   # Geklickt, aber nicht verifiziert

    new_balance_mb = result2["remaining_mb"]
    increase_mb = new_balance_mb - old_balance_mb

    log.info("   Stand VORHER: %.0f MB", old_balance_mb)
    log.info("   Stand NACHHER: %.0f MB", new_balance_mb)
    log.info("   Differenz: %+.0f MB", increase_mb)

    if increase_mb >= 500:
        # Mindestens 500 MB Anstieg = Buchung definitiv erfolgreich
        log.info("   ✅ Buchung VERIFIZIERT! +%.0f MB erfolgreich gebucht.", increase_mb)
        return True, new_balance_mb
    elif 0 < increase_mb < 500:
        # Kleiner Anstieg – könnte zufällig sein
        log.warning("   ⚠️  Balance leicht gestiegen (+%.0f MB), aber < 500 MB.", increase_mb)
        log.warning("   Buchung möglicherweise teilweise, oder Portal zeigt noch alten Stand.")
        return True, new_balance_mb
    else:
        # Kein Anstieg oder sogar gesunken
        log.error("   ❌ Buchung NICHT VERIFIZIERT!")
        log.error("   Balance hat sich nicht erhöht (vorher: %.0f MB, nachher: %.0f MB).",
                  old_balance_mb, new_balance_mb)
        log.error("   Mögliche Ursachen:")
        log.error("   1. Portal zeigt noch veraltete Carrier-Daten (Cache-Problem)")
        log.error("      → Starte mit --pdp-refresh für Carrier-Datenaktualisierung")
        log.error("   2. Buchung wurde vom Carrier abgelehnt (Tageslimit erreicht?)")
        log.error("   3. Button war sichtbar aber Buchung nicht wirklich ausgeführt")
        log.error("   Bitte manuell prüfen: %s", DASHBOARD_URL)
        return False, new_balance_mb


# ══════════════════════════════════════════════════════════════════════════════
#  HAUPT-MONITOR-SCHLEIFE
# ══════════════════════════════════════════════════════════════════════════════

async def monitor_loop(
    username: str,
    password: str,
    threshold_mb: int,
    fixed_interval: Optional[int],
    use_tracker: bool,
    pdp_refresh: bool,
    bridge_file: Optional[Path],
):
    tracker = DataTracker(bridge_file=bridge_file)
    if use_tracker and not tracker._enabled:
        use_tracker = False   # Warnung wurde schon im DataTracker.__init__ ausgegeben

    if pdp_refresh:
        log.info("🔄 PDP-Refresh aktiviert (wird nach jeder Portal-Abfrage ausgeführt)")

    async with async_playwright() as pw:
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

        ctx_kwargs = dict(user_agent=USER_AGENT)
        if COOKIE_FILE.exists():
            log.info("🍪 Gespeicherte Cookies geladen (%s)", COOKIE_FILE)
            ctx_kwargs["storage_state"] = str(COOKIE_FILE)

        context: BrowserContext = await browser.new_context(**ctx_kwargs)
        page: Page = await context.new_page()

        logged_in = await do_login(page, username, password)
        if not logged_in:
            log.error("❌ Login fehlgeschlagen – Script beendet.")
            await browser.close()
            return

        await context.storage_state(path=str(COOKIE_FILE))
        log.info("🍪 Cookies gespeichert → %s", COOKIE_FILE)

        last_remaining_mb = float("inf")
        try:
            state = json.loads(STATE_FILE.read_text())
            last_remaining_mb = float(state.get("remaining_mb", "inf"))
        except Exception:
            pass

        if use_tracker:
            tracker._mobile_ifaces = tracker._detect_interfaces()
            tracker.sample()

        log.info("═" * 60)
        log.info("📊 Monitor läuft. Schwellwert: %d MB (%.1f GB)", threshold_mb, threshold_mb/1024)
        if use_tracker:
            log.info("📡 Echtzeit-Tracking aktiv (%s)", tracker._read_method)
        else:
            log.info("📡 Echtzeit-Tracking: deaktiviert (nur Portal-Abfragen)")
        log.info("Ctrl+C zum Beenden")
        log.info("═" * 60)

        consecutive_errors = 0

        while True:
            try:
                if use_tracker:
                    tracker.sample()

                secs_to_threshold = None
                if use_tracker:
                    secs_to_threshold = tracker.seconds_until_threshold(
                        last_remaining_mb, threshold_mb)

                should_check = (
                    last_remaining_mb == float("inf")
                    or last_remaining_mb < threshold_mb * 1.5
                    or (secs_to_threshold is not None and secs_to_threshold < 300)
                )

                if should_check:
                    # PDP-Refresh VOR Portal-Abfrage (frischer Carrier-Stand)
                    if pdp_refresh:
                        force_pdp_refresh()
                        await asyncio.sleep(2)

                    try:
                        await page.reload(wait_until="domcontentloaded", timeout=30000)
                        await wait_quiet(page, 10000)
                        await page.wait_for_timeout(2000)
                    except Exception as e:
                        log.warning("Reload fehlgeschlagen: %s", e)
                        logged_in = await do_login(page, username, password)
                        if logged_in:
                            await context.storage_state(path=str(COOKIE_FILE))
                        continue

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

                    try:
                        STATE_FILE.write_text(json.dumps({
                            "remaining_mb": remaining_mb,
                            "checked_at": datetime.now().isoformat(),
                        }))
                    except Exception:
                        pass

                    if use_tracker:
                        log.info("📊 %s", tracker.status_line(remaining_mb))
                    else:
                        log.info(
                            "📊 %.0f MB (%.2f GB) verbleibend",
                            remaining_mb, remaining_mb / 1024
                        )

                    # Buchen wenn unter Schwellwert
                    if remaining_mb < threshold_mb:
                        log.warning(
                            "⚡ UNTER SCHWELLWERT! %.0f MB < %d MB → Buche +1 GB...",
                            remaining_mb, threshold_mb
                        )
                        success, new_bal = await attempt_booking(
                            page, remaining_mb, pdp_refresh=pdp_refresh
                        )
                        if success and new_bal is not None:
                            last_remaining_mb = new_bal
                            log.info("🎉 +1 GB erfolgreich gebucht! Neuer Stand: %.0f MB", new_bal)
                        elif success and new_bal is None:
                            log.info("🎉 +1 GB gebucht (Verifizierung nicht möglich)")
                            # Konservativer Schätzwert: +1 GB
                            last_remaining_mb = remaining_mb + 1024
                        else:
                            log.error("❌ Buchung fehlgeschlagen oder nicht verifiziert.")
                            if not pdp_refresh:
                                log.info(
                                    "   Tipp: Starte mit --pdp-refresh um sicherzustellen,\n"
                                    "   dass das Portal den aktuellen Carrier-Stand anzeigt."
                                )
                else:
                    if use_tracker:
                        log.info("📡 %s", tracker.status_line(last_remaining_mb))

                if fixed_interval:
                    wait_secs = fixed_interval
                else:
                    wait_secs = smart_interval(last_remaining_mb, tracker)

                next_check = datetime.now() + timedelta(seconds=wait_secs)
                log.info(
                    "⏱️  Nächste Prüfung um %s (in %ds)",
                    next_check.strftime("%H:%M:%S"), wait_secs
                )

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
        description="ALDI Talk Auto-Refill Monitor v6",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
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
    parser.add_argument("--pdp-refresh", action="store_true",
                        help="PDP-Refresh vor/nach Portal-Abfrage (benötigt Root)")
    parser.add_argument("--bridge-file", type=Path, default=None,
                        help=f"Bridge-Datei für /proc/net/dev "
                             f"(Standard: {BRIDGE_FILE_DEFAULT})\n"
                             "Schreibe in HOST-Termux: "
                             "while true; do cat /proc/net/dev > /sdcard/aldi_netdev.txt; sleep 5; done &")
    parser.add_argument("--visible", action="store_true",
                        help="(ignoriert) Headed-Modus ist in proot ohne X-Server nicht verfügbar")
    args = parser.parse_args()

    if args.visible:
        log.warning(
            "⚠️  --visible ignoriert: Headed-Modus nicht verfügbar in proot-Ubuntu "
            "(kein X-Server). Headless-Modus wird verwendet."
        )

    if not args.user or not args.password:
        parser.error(
            "Rufnummer und Passwort erforderlich!\n"
            "  --user 017612345678 --pass DeinPasswort\n"
            "  oder: ALDI_USER=... ALDI_PASS=... python3 aldi_monitor.py"
        )

    # Bridge-Datei: wenn nicht angegeben, Default testen
    bridge_file = args.bridge_file
    if bridge_file is None and BRIDGE_FILE_DEFAULT.exists():
        bridge_file = BRIDGE_FILE_DEFAULT
        log.info("📡 Bridge-Datei automatisch erkannt: %s", bridge_file)

    log.info("╔════════════════════════════════════════╗")
    log.info("║  ALDI Talk Auto-Refill Monitor  v6     ║")
    log.info("╚════════════════════════════════════════╝")
    log.info("User: %s****  Schwellwert: %d MB", args.user[:4], args.threshold)
    if args.pdp_refresh:
        log.info("🔄 PDP-Refresh: aktiviert")
    if bridge_file:
        log.info("📡 Bridge-Datei: %s", bridge_file)

    await monitor_loop(
        username=args.user,
        password=args.password,
        threshold_mb=args.threshold,
        fixed_interval=args.interval,
        use_tracker=not args.no_tracker,
        pdp_refresh=args.pdp_refresh,
        bridge_file=bridge_file,
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Beendet.")
