#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════════════╗
║          ALDI Talk Auto-Refill Monitor  v7  (Termux / Android)          ║
║                                                                          ║
║  • Prüft Datenvolumen und bucht kostenlos +1 GB wenn < 1 GB             ║
║  • Buchungs-Verifikation via Netzwerk-Interceptor (keine Cache-Probleme) ║
║  • PDP-Refresh via MacroDroid/Automate File-Bridge (kein Root nötig)    ║
║  • Cookie-Persistenz & Smart-Intervall                                   ║
╚══════════════════════════════════════════════════════════════════════════╝

INSTALLATION:
  apt update && apt install -y python3 python3-pip chromium
  pip3 install playwright --break-system-packages
  export PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=$(which chromium)

USAGE:
  python3 aldi_monitor.py --user 017612345678 --pass DeinPasswort

  Optionen:
    --threshold N     Buchen wenn < N MB (Standard: 1024)
    --interval N      Festes Intervall in Sekunden
    --pdp-bridge F    PDP-Refresh Bridge-Datei (Standard: /sdcard/aldi_pdp_trigger.txt)
    --no-pdp          PDP-Refresh deaktivieren
    --force-book      Sofort +1 GB buchen (zum Testen)
    --debug           HTML-Dump bei Fehlern
    --visible         (kein Effekt in proot)

═══════════════════════════════════════════════════════════════
 PDP-REFRESH ohne Root – MacroDroid Setup (einmalig):
═══════════════════════════════════════════════════════════════
 1. MacroDroid installieren (Play Store oder F-Droid)
 2. Neues Makro erstellen:
    TRIGGER:  "File Modified"
              Datei: /sdcard/aldi_pdp_trigger.txt
    AKTIONEN: → "Airplane Mode" = EIN
              → "Wait" 3 Sekunden
              → "Airplane Mode" = AUS
              → "Write to File" → /sdcard/aldi_pdp_done.txt → Inhalt: "done"
 3. Makro aktivieren, App im Hintergrund lassen.

 Das Script schreibt dann automatisch in /sdcard/aldi_pdp_trigger.txt
 und wartet auf /sdcard/aldi_pdp_done.txt.
 Flugzeugmodus-Toggle dauert ~5s statt 20s manuell.
═══════════════════════════════════════════════════════════════
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
        async_playwright, Page, BrowserContext, Response,
        TimeoutError as PWTimeout,
    )
except ImportError:
    print("❌  Playwright fehlt:  pip3 install playwright --break-system-packages")
    sys.exit(1)

# ══════════════════════════════════════════════════════════════════════════════
#  KONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

DASHBOARD_URL = "https://www.alditalk-kundenportal.de/portal/auth/uebersicht/"
LOGIN_URL     = "https://login.alditalk-kundenbetreuung.de/signin/XUI/#login/"

COOKIE_FILE   = Path("aldi_cookies.json")
STATE_FILE    = Path("aldi_state.json")
DEBUG_DIR     = Path("aldi_debug")

PDP_TRIGGER_DEFAULT = Path("/sdcard/aldi_pdp_trigger.txt")
PDP_DONE_DEFAULT    = Path("/sdcard/aldi_pdp_done.txt")
TERMUX_BIN          = Path("/data/data/com.termux/files/usr/bin")

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Mobile Safari/537.36"
)

THRESHOLD_MB = 1024
DEFAULT_USER = os.environ.get("ALDI_USER", "")
DEFAULT_PASS = os.environ.get("ALDI_PASS", "")

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

_debug_enabled = False

def dump_html(page_html: str, label: str = "debug"):
    """Speichert HTML-Dump für Debugging."""
    if not _debug_enabled:
        return
    DEBUG_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%H%M%S")
    p = DEBUG_DIR / f"{ts}_{label}.html"
    p.write_text(page_html, encoding="utf-8")
    log.debug("   HTML-Dump: %s", p)


# ══════════════════════════════════════════════════════════════════════════════
#  PDP-REFRESH  (kein Root nötig via MacroDroid/Automate)
# ══════════════════════════════════════════════════════════════════════════════

_pdp_trigger_file: Path = PDP_TRIGGER_DEFAULT
_pdp_done_file:    Path = PDP_DONE_DEFAULT
_pdp_available:    Optional[bool] = None   # None = noch nicht geprüft


def _check_pdp_bridge() -> bool:
    """Prüft ob MacroDroid/Automate Bridge verfügbar ist."""
    # Bridge verfügbar wenn /sdcard beschreibbar
    try:
        test = _pdp_trigger_file.parent / ".aldi_test"
        test.write_text("test")
        test.unlink()
        return True
    except Exception:
        return False


def force_pdp_refresh(timeout: int = 25) -> bool:
    """Erzwingt Carrier-PDP-Erneuerung (Flugzeugmodus-Alternative).

    Methode 1 (bevorzugt, kein Root): MacroDroid/Automate File-Bridge
    Methode 2 (Root): svc data disable/enable via su/tsu
    """
    global _pdp_available

    # ── Methode 1: MacroDroid/Automate File-Bridge ────────────────────────────
    if _pdp_available is None:
        _pdp_available = _check_pdp_bridge()
        if _pdp_available:
            log.info("🔄 PDP-Bridge: %s → MacroDroid/Automate erwartet", _pdp_trigger_file)
        else:
            log.warning("🔄 PDP-Bridge: /sdcard nicht beschreibbar (proot-Mount?)")

    if _pdp_available:
        try:
            # Alte done-Datei entfernen
            _pdp_done_file.unlink(missing_ok=True)

            # Trigger schreiben (MacroDroid überwacht diese Datei)
            _pdp_trigger_file.write_text(str(time.time()))
            log.info("🔄 PDP-Trigger gesetzt → warte auf MacroDroid (max %ds)...", timeout)

            deadline = time.time() + timeout
            while time.time() < deadline:
                if _pdp_done_file.exists():
                    log.info("✅ PDP-Refresh bestätigt (MacroDroid)")
                    return True
                time.sleep(1)

            log.warning("⚠️  PDP-Refresh Timeout (%ds) – MacroDroid hat nicht geantwortet.", timeout)
            log.warning("   Prüfe: Ist MacroDroid aktiv? Ist das Makro eingerichtet?")
            log.warning("   Makro-Setup: Trigger=FileModified(%s), Aktion=AirplaneModeAN→3s→AUS→WriteFile(%s,'done')",
                        _pdp_trigger_file, _pdp_done_file)
            return False
        except Exception as e:
            log.warning("PDP-Bridge Fehler: %s", e)

    # ── Methode 2: Root (su/tsu) ──────────────────────────────────────────────
    for cmd in [
        ["su", "-c", "svc data disable; sleep 2; svc data enable"],
        ["tsu", "-c", "svc data disable; sleep 2; svc data enable"],
        [str(TERMUX_BIN / "su"), "-c", "svc data disable; sleep 2; svc data enable"],
    ]:
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=12)
            if r.returncode == 0:
                log.info("✅ PDP-Refresh (Root: %s) → warte 4s...", cmd[0])
                time.sleep(4)
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        except Exception as e:
            log.debug("Root PDP '%s': %s", cmd[0], e)

    log.warning("⚠️  PDP-Refresh: Weder MacroDroid noch Root verfügbar.")
    log.warning("   Richte MacroDroid-Makro ein (Anleitung oben im Script, Zeile 30-45).")
    return False


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
    for sel in [
        'button[data-testid="uc-deny-all-button"]',
        '#onetrust-accept-btn-handler',
        'button:has-text("Alle akzeptieren")',
        'button:has-text("Nur notwendige Cookies")',
        'button:has-text("Ablehnen")',
    ]:
        try:
            btn = await page.query_selector(sel)
            if btn and await btn.is_visible():
                await btn.click()
                log.info("🍪 Cookie-Banner: %s", sel)
                await page.wait_for_timeout(600)
                return True
        except Exception:
            pass
    return False


async def is_logged_in(page: Page) -> bool:
    url = page.url
    if "login.alditalk-kundenbetreuung.de" in url:
        return False
    try:
        body = await page.evaluate("document.body ? document.body.innerText : ''")
        if any(kw in body for kw in ["Abmelden", "Übersicht", "Mein Konto"]):
            return True
    except Exception:
        pass
    return "/auth/" in url


# ══════════════════════════════════════════════════════════════════════════════
#  LOGIN
# ══════════════════════════════════════════════════════════════════════════════

async def do_login(page: Page, username: str, password: str) -> bool:
    log.info("🔐 Login-Check...")
    try:
        await page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=35000)
    except Exception as e:
        log.error("Navigation fehlgeschlagen: %s", e)
        return False

    await wait_quiet(page)
    await handle_cookie_banner(page)

    if await is_logged_in(page):
        log.info("✅ Cookie-Session aktiv")
        return True

    log.info("   → Navigiere zu ForgeRock...")
    try:
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        log.error("Login-URL nicht erreichbar: %s", e)
        return False

    await wait_quiet(page)
    await handle_cookie_banner(page)

    log.info("   Warte auf Login-Formular (max 60s)...")
    try:
        await page.wait_for_function(
            "() => document.querySelectorAll('input').length > 0",
            timeout=60000, polling=1000,
        )
    except PWTimeout:
        log.error("❌ Formular nach 60s nicht erschienen (URL: %s)", page.url)
        return False

    inputs_info = await page.evaluate("""
        () => Array.from(document.querySelectorAll('input')).map((el, i) => ({
            i, type: el.type, id: el.id, name: el.name,
            placeholder: el.placeholder, visible: el.offsetParent !== null
        }))
    """)
    log.info("   Inputs: %s", inputs_info)

    # Passwort-Feld
    pw_field = None
    for sel in ['input[type="password"]', '#input-6', '#idToken4', '#idToken2',
                'input[name="IDToken2"]', '#idToken1']:
        try:
            f = await page.query_selector(sel)
            if f and await f.is_visible():
                pw_field = f
                log.info("   PW-Feld: %s", sel)
                break
        except Exception:
            pass

    if not pw_field:
        for inp in await page.query_selector_all("input"):
            if await inp.is_visible():
                pw_field = inp
                log.warning("   PW-Feld: Fallback (erstes Input)")
                break

    if not pw_field:
        log.error("❌ Kein Passwort-Feld!")
        return False

    # Username-Feld
    user_field = None
    for sel in ['#input-5', '#idToken3', 'input[type="tel"]', '#idToken1',
                'input[name="IDToken1"]']:
        try:
            f = await page.query_selector(sel)
            if f and await f.is_visible() and f != pw_field:
                t = (await f.get_attribute("type") or "text").lower()
                if t != "password":
                    user_field = f
                    log.info("   User-Feld: %s", sel)
                    break
        except Exception:
            pass

    if user_field:
        await user_field.fill(username)
        log.info("   Rufnummer: %s****", username[:4])

    await pw_field.fill(password)
    log.info("   Passwort eingegeben")

    submitted = False
    for sel in ['[class*="button--solid"]', '#loginButton_0', 'button[type="submit"]',
                'input[type="submit"]', 'button:has-text("Anmelden")',
                'button:has-text("Weiter")']:
        try:
            btn = await page.query_selector(sel)
            if btn and await btn.is_visible():
                await btn.click()
                submitted = True
                log.info("   Submit: %s", sel)
                break
        except Exception:
            pass

    if not submitted:
        await pw_field.press("Enter")
        log.info("   Submit: Enter")

    try:
        await page.wait_for_url(
            lambda u: "login.alditalk-kundenbetreuung.de" not in u,
            timeout=25000,
        )
    except PWTimeout:
        log.warning("   Weiterleitung langsam... (URL: %s)", page.url)

    await wait_quiet(page)
    await handle_cookie_banner(page)

    ok = await is_logged_in(page)
    log.info("✅ Login OK" if ok else "❌ Login fehlgeschlagen (URL: %s)" % page.url)
    return ok


# ══════════════════════════════════════════════════════════════════════════════
#  BALANCE LESEN
# ══════════════════════════════════════════════════════════════════════════════

_BALANCE_CSS = [
    ("one-stack.usage-meter:nth-child(1) > one-usage-meter:nth-child(1) "
     "> one-group:nth-child(1) > one-heading:nth-child(2)",
     "one-stack.usage-meter:nth-child(1) > one-usage-meter:nth-child(1) "
     "> one-button:nth-child(3)"),
    ("one-stack.usage-meter:nth-child(2) > one-usage-meter:nth-child(1) "
     "> one-group:nth-child(1) > one-heading:nth-child(2)",
     "one-stack.usage-meter:nth-child(2) > one-usage-meter:nth-child(1) "
     "> one-button:nth-child(3)"),
    ("one-stack.usage-meter:nth-child(1) > one-stack:nth-child(1) "
     "> one-usage-meter:nth-child(1) > one-group:nth-child(1) > one-heading:nth-child(2)",
     "one-stack.usage-meter:nth-child(1) > one-stack:nth-child(1) "
     "> one-usage-meter:nth-child(1) > one-button:nth-child(3)"),
]

_JS_READ_BALANCE = r"""
(function() {
  // Versucht Balance aus Vue-State zu lesen
  function findBtn(root, depth) {
    if (depth > 15 || !root) return null;
    var els = root.querySelectorAll ? root.querySelectorAll('*') : [];
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      if (/button/i.test(el.tagName) && /1\s*GB/i.test(el.textContent||'')) return el;
      if (el.shadowRoot) { var f=findBtn(el.shadowRoot,depth+1); if(f) return f; }
    }
    return null;
  }
  var btn = findBtn(document, 0);
  if (!btn) return {error:'no_button'};
  var vm = btn.__vueParentComponent;
  if (!vm) return {error:'no_vue'};
  var bd = vm.proxy && vm.proxy.baseData;
  if (!bd || !bd[0]) return {error:'no_baseData'};
  var a = bd[0].allocated||0, u = bd[0].used||0;
  return {
    remaining_mb: Math.round((a-u)/1024),
    allocated_mb: Math.round(a/1024),
    used_mb: Math.round(u/1024),
    on_demand_ok: !!(bd[0].isOnDemandRefillApplicable),
    error: null
  };
})()
"""


def _parse_mb(text: str) -> Optional[float]:
    m = re.search(r"([\d.,]+)\s*(GB|MB)", text, re.I)
    if not m:
        return None
    val = float(m.group(1).replace(",", "."))
    return val * 1024 if m.group(2).upper() == "GB" else val


async def ensure_dashboard(page: Page):
    if "uebersicht" not in page.url and "account-overview" not in page.url:
        try:
            await page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=30000)
            await wait_quiet(page, 12000)
            await page.wait_for_timeout(2000)
        except Exception as e:
            log.warning("Dashboard-Navigation: %s", e)


async def read_balance(page: Page) -> Optional[dict]:
    await ensure_dashboard(page)

    # Versuch 1: CSS-Selektoren
    for bal_sel, _ in _BALANCE_CSS:
        try:
            el = await page.query_selector(bal_sel)
            if el:
                text = await el.text_content() or ""
                mb = _parse_mb(text)
                if mb is not None:
                    log.debug("Balance (CSS): %.0f MB", mb)
                    return {"remaining_mb": mb}
        except Exception:
            pass

    # Versuch 2: Vue-State JS
    try:
        r = await page.evaluate(_JS_READ_BALANCE)
        if r and not r.get("error"):
            log.debug("Balance (Vue): %.0f MB", r["remaining_mb"])
            return r
        if r:
            log.debug("Vue-Fehler: %s", r.get("error"))
    except Exception as e:
        log.debug("Vue-JS Fehler: %s", e)

    # Versuch 3: Body-Text-Suche
    try:
        body = await page.evaluate("document.body ? document.body.innerText : ''")
        mb = _parse_mb(body)
        if mb is not None:
            log.debug("Balance (Text): %.0f MB", mb)
            return {"remaining_mb": mb}
        if _debug_enabled:
            dump_html(await page.content(), "balance_fail")
            log.debug("   Kein Balance im Body. Body-Anfang: %s", body[:300])
    except Exception:
        pass

    # Debug: alle one-heading / one-stack Texte loggen
    try:
        texts = await page.evaluate("""
            () => {
              var r = [];
              ['one-heading','one-usage-meter','one-stack'].forEach(function(tag) {
                var els = document.querySelectorAll(tag);
                for (var i=0; i<Math.min(els.length,5); i++) {
                  r.push(tag + '['+i+']: ' + (els[i].textContent||'').trim().substring(0,60));
                }
              });
              return r;
            }
        """)
        if texts:
            log.warning("   Web-Komponenten auf Seite: %s", texts)
    except Exception:
        pass

    return None


# ══════════════════════════════════════════════════════════════════════════════
#  BUCHUNG MIT NETZWERK-VERIFIKATION
# ══════════════════════════════════════════════════════════════════════════════

# Verbessertes JS: klickt auch den inneren Shadow-Button
_JS_CLICK_BUTTON = r"""
(function() {
  var found = [];

  function collect(root, depth) {
    if (depth > 15 || !root) return;
    // Alle one-button Elemente sammeln
    var obs = root.querySelectorAll ? root.querySelectorAll('one-button, button') : [];
    for (var i = 0; i < obs.length; i++) {
      var t = (obs[i].textContent||'').trim();
      if (/1\s*GB/i.test(t)) {
        found.push({el: obs[i], text: t.substring(0,50), depth: depth, tag: obs[i].tagName});
      }
    }
    // Recurse in shadow roots
    var all = root.querySelectorAll ? root.querySelectorAll('*') : [];
    for (var i = 0; i < all.length; i++) {
      if (all[i].shadowRoot) collect(all[i].shadowRoot, depth+1);
    }
  }
  collect(document, 0);

  if (found.length === 0) {
    // Alle Buttons loggen zur Diagnose
    var allBtns = [];
    function collectAll(root, d) {
      if (d > 10 || !root) return;
      var bs = root.querySelectorAll ? root.querySelectorAll('one-button, button') : [];
      for (var i=0; i<bs.length && allBtns.length<10; i++) {
        allBtns.push((bs[i].textContent||'').trim().substring(0,30) + ' ['+bs[i].tagName+']');
      }
      var all = root.querySelectorAll ? root.querySelectorAll('*') : [];
      for (var i=0; i<all.length; i++) { if(all[i].shadowRoot) collectAll(all[i].shadowRoot,d+1); }
    }
    collectAll(document, 0);
    return {clicked: false, reason: 'not_found', all_buttons: allBtns};
  }

  var target = found[0].el;
  var method = 'outer';
  var disabled = target.hasAttribute ? target.hasAttribute('disabled') : false;
  if (disabled) return {clicked: false, reason: 'disabled', text: found[0].text};

  // Versuche inneren Shadow-Button zu klicken (one-button hat innen <button>)
  if (target.shadowRoot) {
    var inner = target.shadowRoot.querySelector('button, [role="button"], a[href]');
    if (inner) {
      var innerDisabled = inner.hasAttribute('disabled') || inner.getAttribute('aria-disabled') === 'true';
      if (innerDisabled) return {clicked: false, reason: 'inner_disabled', text: found[0].text};
      inner.scrollIntoView({block:'center'});
      inner.focus();
      inner.click();
      inner.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,composed:true}));
      method = 'inner-shadow';
    }
  }

  // Auch äußeres Element klicken (belt and suspenders)
  target.scrollIntoView({block:'center'});
  target.click();
  target.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,composed:true}));

  return {
    clicked: true,
    method: method,
    text: found[0].text,
    total_found: found.length,
  };
})()
"""

# Prüft nach dem Klick ob ein Erfolgs-Signal sichtbar ist
_JS_CHECK_SUCCESS = r"""
(function() {
  var body = document.body ? document.body.innerText : '';
  var signals = ['erfolgreich', 'gebucht', 'success', 'bestätigt', 'activated', 'aktiviert'];
  var found = signals.filter(function(s) { return body.toLowerCase().indexOf(s) >= 0; });

  // Prüfe ob der Button jetzt disabled ist
  var btnDisabled = false;
  function checkDisabled(root, d) {
    if (d > 15 || !root) return;
    var obs = root.querySelectorAll ? root.querySelectorAll('one-button, button') : [];
    for (var i = 0; i < obs.length; i++) {
      var t = (obs[i].textContent||'').trim();
      if (/1\s*GB/i.test(t)) {
        var dis = obs[i].hasAttribute('disabled') || obs[i].getAttribute('aria-disabled')==='true';
        if (dis) btnDisabled = true;
        // Inner button
        if (obs[i].shadowRoot) {
          var ib = obs[i].shadowRoot.querySelector('button');
          if (ib && (ib.hasAttribute('disabled') || ib.getAttribute('aria-disabled')==='true')) {
            btnDisabled = true;
          }
        }
      }
    }
    var all = root.querySelectorAll ? root.querySelectorAll('*') : [];
    for (var i = 0; i < all.length; i++) { if(all[i].shadowRoot) checkDisabled(all[i].shadowRoot, d+1); }
  }
  checkDisabled(document, 0);

  // Toast / Alert suchen
  var alertText = '';
  var alerts = document.querySelectorAll('[role="alert"],[role="status"],.toast,.notification,one-notification');
  for (var i=0; i<alerts.length; i++) {
    alertText += ' ' + (alerts[i].textContent||'').trim();
  }

  return {
    success_words: found,
    btn_disabled: btnDisabled,
    alert_text: alertText.trim().substring(0, 200),
    body_snippet: body.substring(0, 300),
  };
})()
"""


async def attempt_booking(
    page: Page,
    old_balance_mb: float,
    use_pdp: bool = True,
) -> Tuple[bool, Optional[float]]:
    """Bucht +1 GB und verifiziert via Netzwerk-Interceptor + UI-Signale.

    Gibt (success, new_balance_mb) zurück.
    success = True wenn Buchung definitiv oder sehr wahrscheinlich erfolgreich.
    """
    log.info("📦 Buchungsversuch (+1 GB)...")

    # ── Netzwerk-Interceptor: alle POST-Antworten nach Klick erfassen ─────────
    booking_api_hits: list[dict] = []
    booking_success_api = asyncio.Event()

    def on_response(resp: Response):
        if resp.request.method in ("POST", "PUT", "PATCH"):
            url = resp.url
            status = resp.status
            # Booking-relevante URLs erkennen
            if any(kw in url.lower() for kw in [
                "refill", "book", "ondemand", "tariff", "option",
                "datenvolumen", "volumen", "data", "addon", "top",
            ]):
                booking_api_hits.append({"url": url, "status": status})
                log.info("   📡 API: [%d] %s", status, url[:80])
                if status < 400:
                    booking_success_api.set()

    page.on("response", on_response)

    try:
        # ── Schritt 1: Button klicken (JS, shadow-DOM-bewusst) ────────────────
        clicked = False

        # Methode A: Playwright native (pierces shadow DOM automatisch)
        for loc_spec in [
            ("role", None),   # get_by_role
            ("text", None),   # get_by_text
        ]:
            if clicked:
                break
            try:
                if loc_spec[0] == "role":
                    loc = page.get_by_role("button", name=re.compile(r"1\s*GB", re.I))
                else:
                    loc = page.get_by_text(re.compile(r"^\+?1\s*GB$", re.I))

                cnt = await loc.count()
                log.debug("   Locator '%s': %d Treffer", loc_spec[0], cnt)
                if cnt > 0:
                    # Prüfe ob disabled
                    disabled = await loc.first.get_attribute("disabled")
                    aria_dis = await loc.first.get_attribute("aria-disabled")
                    if disabled is not None or aria_dis == "true":
                        log.warning("   ⚠️  Button disabled! Buchung nicht verfügbar.")
                        page.remove_listener("response", on_response)
                        return False, None
                    await loc.first.scroll_into_view_if_needed()
                    await loc.first.click(timeout=5000)
                    clicked = True
                    log.info("   🖱️  Geklickt (Playwright %s)", loc_spec[0])
            except Exception as e:
                log.debug("   Playwright %s: %s", loc_spec[0], e)

        # Methode B: JS Shadow-DOM
        if not clicked:
            result = await page.evaluate(_JS_CLICK_BUTTON)
            if result and result.get("clicked"):
                clicked = True
                log.info("   🖱️  Geklickt (JS %s, Text: '%s')",
                         result.get("method"), result.get("text"))
            elif result:
                reason = result.get("reason", "?")
                log.error("   ❌ Kein Button: %s", reason)
                if reason == "disabled":
                    log.warning("   Button gefunden aber disabled. Buchung aktuell nicht möglich.")
                else:
                    log.warning("   Alle Buttons auf Seite: %s", result.get("all_buttons", []))
                    dump_html(await page.content(), "booking_no_button")
                page.remove_listener("response", on_response)
                return False, None

        if not clicked:
            log.error("   ❌ Buchungsbutton nicht gefunden!")
            dump_html(await page.content(), "booking_not_found")
            page.remove_listener("response", on_response)
            return False, None

        # ── Schritt 2: Warte auf API-Response oder UI-Signal (max 12s) ────────
        log.info("   ⏳ Warte auf Buchungsbestätigung...")
        try:
            await asyncio.wait_for(booking_success_api.wait(), timeout=12)
            log.info("   ✅ Erfolgreiche API-Antwort empfangen!")
        except asyncio.TimeoutError:
            log.info("   (Keine Booking-API in 12s erkannt — prüfe UI...)")

        # Kurz warten, dann Dialoge schließen
        await page.wait_for_timeout(2000)
        for sel in ['button:has-text("Ok")', 'button:has-text("OK")',
                    'button:has-text("Bestätigen")', 'button:has-text("Schließen")',
                    'button:has-text("Fertig")', 'button:has-text("Weiter")',
                    '[data-testid="modal-close"]', '[aria-label="Schließen"]',
                    '.modal-close', 'button.close']:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    log.info("   Dialog geschlossen: %s", sel)
                    await page.wait_for_timeout(500)
            except Exception:
                pass

        await page.wait_for_timeout(2000)

        # ── Schritt 3: UI-Signale prüfen (OHNE Reload → keine Cache-Probleme) ─
        sig = await page.evaluate(_JS_CHECK_SUCCESS)
        log.info("   UI-Check: Erfolg-Wörter=%s, Button-disabled=%s, Alert='%s'",
                 sig.get("success_words"), sig.get("btn_disabled"), sig.get("alert_text")[:50])

        api_success = bool(booking_api_hits and all(h["status"] < 400 for h in booking_api_hits))
        ui_success = (
            bool(sig.get("success_words")) or
            sig.get("btn_disabled") or
            any(kw in (sig.get("alert_text") or "").lower()
                for kw in ["erfolgreich", "gebucht", "success"])
        )

        if api_success or ui_success:
            log.info("   ✅ Buchung ERFOLGREICH! (API=%s, UI=%s)", api_success, ui_success)
            # PDP-Refresh damit Carrier neuen Stand sendet
            if use_pdp:
                log.info("   🔄 PDP-Refresh nach Buchung...")
                force_pdp_refresh()

            # Nach PDP-Refresh: Seite neu laden und Balance lesen
            log.info("   🔍 Lese neuen Stand nach Buchung...")
            await asyncio.sleep(3)
            try:
                await page.reload(wait_until="domcontentloaded", timeout=30000)
                await wait_quiet(page, 10000)
                await page.wait_for_timeout(3000)
            except Exception:
                pass
            result2 = await read_balance(page)
            new_bal = result2["remaining_mb"] if result2 else None
            if new_bal:
                log.info("   📊 Neuer Stand: %.0f MB (%.2f GB)", new_bal, new_bal/1024)
            return True, new_bal

        # ── Schritt 4: Fallback — Reload und Balance-Vergleich ─────────────────
        log.info("   Kein klares Signal → Reload und Balance-Vergleich...")
        if use_pdp:
            force_pdp_refresh()
        await asyncio.sleep(5)
        try:
            await page.reload(wait_until="domcontentloaded", timeout=30000)
            await wait_quiet(page, 10000)
            await page.wait_for_timeout(3000)
        except Exception:
            pass

        result2 = await read_balance(page)
        if result2 is None:
            log.warning("   Balance nach Buchung nicht lesbar. Button wurde geklickt.")
            log.warning("   Bitte manuell prüfen: %s", DASHBOARD_URL)
            return True, None   # Geklickt, Erfolg unklar

        new_bal = result2["remaining_mb"]
        diff = new_bal - old_balance_mb
        log.info("   Vorher: %.0f MB | Nachher: %.0f MB | Diff: %+.0f MB",
                 old_balance_mb, new_bal, diff)

        if diff >= 500:
            log.info("   ✅ Buchung VERIFIZIERT (+%.0f MB)", diff)
            return True, new_bal
        elif diff > 0:
            log.warning("   ⚠️  Kleiner Anstieg (+%.0f MB) — könnte Cache sein", diff)
            return True, new_bal
        else:
            log.error("   ❌ Balance nicht gestiegen (Cache-Problem oder Buchung fehlgeschlagen)")
            log.error("   Tipp: Aktiviere MacroDroid PDP-Refresh für frische Carrier-Daten.")
            # Balance blieb gleich oder sank — Button war da aber hat nicht gebucht
            # Nächste Schleife: gleich wieder versuchen (lasse last_remaining_mb tief)
            return False, new_bal

    finally:
        page.remove_listener("response", on_response)


# ══════════════════════════════════════════════════════════════════════════════
#  SMART-INTERVALL
# ══════════════════════════════════════════════════════════════════════════════

def smart_interval(remaining_mb: float) -> int:
    """Prüfintervall basierend auf verbleibendem Volumen."""
    if remaining_mb < THRESHOLD_MB:
        return 30           # Unter Schwelle → sofort erneut versuchen
    elif remaining_mb < THRESHOLD_MB * 1.1:
        return 60           # Knapp drüber → jede Minute
    elif remaining_mb < 2048:
        return 120
    elif remaining_mb < 5120:
        return 300
    elif remaining_mb < 10240:
        return 900
    else:
        return 1800


# ══════════════════════════════════════════════════════════════════════════════
#  HAUPT-SCHLEIFE
# ══════════════════════════════════════════════════════════════════════════════

async def monitor_loop(
    username: str,
    password: str,
    threshold_mb: int,
    fixed_interval: Optional[int],
    use_pdp: bool,
    force_book_once: bool,
):
    global THRESHOLD_MB
    THRESHOLD_MB = threshold_mb

    async with async_playwright() as pw:
        exec_path = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH", "")
        launch_kwargs = dict(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--single-process"],
        )
        if exec_path:
            launch_kwargs["executable_path"] = exec_path

        log.info("🚀 Starte Browser (headless)...")
        browser = await pw.chromium.launch(**launch_kwargs)

        ctx_kwargs = dict(user_agent=USER_AGENT)
        if COOKIE_FILE.exists():
            log.info("🍪 Cookies geladen: %s", COOKIE_FILE)
            ctx_kwargs["storage_state"] = str(COOKIE_FILE)

        context: BrowserContext = await browser.new_context(**ctx_kwargs)
        page: Page = await context.new_page()

        ok = await do_login(page, username, password)
        if not ok:
            log.error("❌ Login fehlgeschlagen.")
            await browser.close()
            return

        await context.storage_state(path=str(COOKIE_FILE))
        log.info("🍪 Cookies gespeichert → %s", COOKIE_FILE)

        last_mb = float("inf")
        try:
            state = json.loads(STATE_FILE.read_text())
            last_mb = float(state.get("remaining_mb", "inf"))
        except Exception:
            pass

        log.info("═" * 60)
        log.info("📊 Monitor aktiv | Schwellwert: %d MB | PDP: %s",
                 threshold_mb, "MacroDroid/Root" if use_pdp else "aus")
        log.info("Ctrl+C zum Beenden")
        log.info("═" * 60)

        errors = 0
        booking_attempts = 0

        # --force-book: sofort buchen ohne zu prüfen
        if force_book_once:
            log.info("⚡ --force-book: Versuche sofort zu buchen...")
            suc, new = await attempt_booking(page, last_mb, use_pdp)
            if suc and new:
                last_mb = new
            elif suc:
                last_mb = last_mb + 1024

        while True:
            try:
                # PDP-Refresh VOR Portal-Abfrage (frischer Carrier-Stand)
                if use_pdp:
                    force_pdp_refresh()
                    await asyncio.sleep(2)

                # Seite neu laden
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=30000)
                    await wait_quiet(page, 10000)
                    await page.wait_for_timeout(2000)
                except Exception as e:
                    log.warning("Reload fehlgeschlagen: %s", e)
                    ok = await do_login(page, username, password)
                    if ok:
                        await context.storage_state(path=str(COOKIE_FILE))
                    continue

                # Balance lesen
                result = await read_balance(page)
                if result is None:
                    errors += 1
                    log.warning("⚠️  Balance nicht lesbar (#%d)", errors)
                    if errors >= 3:
                        log.warning("3× Fehler → Re-Login...")
                        await do_login(page, username, password)
                        await context.storage_state(path=str(COOKIE_FILE))
                        errors = 0
                    await asyncio.sleep(60)
                    continue

                errors = 0
                remaining_mb = result["remaining_mb"]
                last_mb = remaining_mb

                try:
                    STATE_FILE.write_text(json.dumps({
                        "remaining_mb": remaining_mb,
                        "checked_at": datetime.now().isoformat(),
                    }))
                except Exception:
                    pass

                log.info("📊 %.0f MB (%.2f GB) verbleibend", remaining_mb, remaining_mb/1024)

                # Buchen wenn unter Schwellwert
                if remaining_mb < threshold_mb:
                    booking_attempts += 1
                    log.warning("⚡ %.0f MB < %d MB → Buchen (Versuch #%d)",
                                remaining_mb, threshold_mb, booking_attempts)

                    suc, new_bal = await attempt_booking(page, remaining_mb, use_pdp)
                    if suc:
                        if new_bal is not None:
                            last_mb = new_bal
                            log.info("🎉 +1 GB gebucht! Neuer Stand: %.0f MB", new_bal)
                        else:
                            log.info("🎉 +1 GB gebucht! (Neuer Stand unbekannt)")
                            last_mb = remaining_mb + 1024  # schätzweise
                        booking_attempts = 0
                    else:
                        log.error("❌ Buchung fehlgeschlagen. Versuche in 30s erneut.")
                        if booking_attempts >= 3:
                            log.error("3× Buchungsfehler in Folge – prüfe Portal manuell!")
                            log.error("URL: %s", DASHBOARD_URL)
                            booking_attempts = 0
                        await asyncio.sleep(30)
                        continue   # sofort erneut versuchen

                # Intervall
                wait_secs = fixed_interval if fixed_interval else smart_interval(last_mb)
                nxt = datetime.now() + timedelta(seconds=wait_secs)
                log.info("⏱️  Nächste Prüfung: %s (in %ds)", nxt.strftime("%H:%M:%S"), wait_secs)

                await asyncio.sleep(wait_secs)

            except KeyboardInterrupt:
                log.info("\n🛑 Beendet.")
                break
            except Exception as e:
                log.error("Fehler: %s", e, exc_info=True)
                errors += 1
                await asyncio.sleep(30)

        await browser.close()


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    global _debug_enabled, _pdp_trigger_file, _pdp_done_file

    parser = argparse.ArgumentParser(
        description="ALDI Talk Auto-Refill Monitor v7",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--pass", dest="password", default=DEFAULT_PASS)
    parser.add_argument("--threshold", type=int, default=THRESHOLD_MB,
                        help=f"Buche wenn < N MB (Standard: {THRESHOLD_MB})")
    parser.add_argument("--interval", type=int, default=None,
                        help="Festes Intervall in Sekunden")
    parser.add_argument("--no-pdp", action="store_true",
                        help="PDP-Refresh deaktivieren")
    parser.add_argument("--pdp-bridge", type=Path, default=PDP_TRIGGER_DEFAULT,
                        metavar="FILE",
                        help=f"PDP-Trigger-Datei (Standard: {PDP_TRIGGER_DEFAULT})")
    parser.add_argument("--pdp-done", type=Path, default=PDP_DONE_DEFAULT,
                        metavar="FILE",
                        help=f"PDP-Done-Datei (Standard: {PDP_DONE_DEFAULT})")
    parser.add_argument("--force-book", action="store_true",
                        help="Sofort +1 GB buchen (zum Testen)")
    parser.add_argument("--debug", action="store_true",
                        help="HTML-Dumps bei Fehlern speichern")
    parser.add_argument("--visible", action="store_true",
                        help="(ignoriert — kein X-Server in proot)")
    args = parser.parse_args()

    if args.visible:
        log.warning("⚠️  --visible ignoriert (proot hat keinen X-Server → headless)")

    _debug_enabled = args.debug
    _pdp_trigger_file = args.pdp_bridge
    _pdp_done_file = args.pdp_done

    if not args.user or not args.password:
        parser.error(
            "Rufnummer und Passwort fehlen!\n"
            "  --user 017612345678 --pass DeinPasswort\n"
            "  oder: ALDI_USER=... ALDI_PASS=... python3 aldi_monitor.py"
        )

    log.info("╔════════════════════════════════════════╗")
    log.info("║  ALDI Talk Auto-Refill Monitor  v7     ║")
    log.info("╚════════════════════════════════════════╝")
    log.info("User: %s****  Schwellwert: %d MB", args.user[:4], args.threshold)
    if not args.no_pdp:
        log.info("PDP-Bridge: %s", _pdp_trigger_file)
        log.info("  (MacroDroid-Makro: FileModified→AirplaneAN→3s→AUS→WriteFile done)")
    if args.debug:
        log.info("Debug-Modus: HTML-Dumps in ./aldi_debug/")

    await monitor_loop(
        username=args.user,
        password=args.password,
        threshold_mb=args.threshold,
        fixed_interval=args.interval,
        use_pdp=not args.no_pdp,
        force_book_once=args.force_book,
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Beendet.")
