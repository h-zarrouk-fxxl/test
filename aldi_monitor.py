#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════╗
║        ALDI Talk Auto-Refill Monitor (Termux/Android)        ║
║   Prüft alle 2 Min. den Datenstand und bucht +1 GB           ║
║   automatisch wenn Guthaben < 1 GB (Tarif S kostenlos)       ║
╚══════════════════════════════════════════════════════════════╝

INSTALLATION TERMUX (einmalig):
─────────────────────────────────────────────────────────────
  pkg update && pkg upgrade -y
  pkg install -y python chromium
  pip install playwright
  export PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=$(which chromium)
  echo 'export PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=$(which chromium)' >> ~/.bashrc

USAGE:
  python aldi_monitor.py --user 017612345678 --pass DeinPasswort
  python aldi_monitor.py --user 017612345678 --pass DeinPasswort --visible  # Browser sichtbar
  ALDI_USER=017612345678 ALDI_PASS=DeinPasswort python aldi_monitor.py

Mit Ctrl+C beenden.
"""

from __future__ import annotations
import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime
from typing import Optional

# ─── Playwright Import mit Hinweis ───────────────────────────────────────────
try:
    from playwright.async_api import (
        async_playwright, Page, Browser, BrowserContext,
        TimeoutError as PWTimeout
    )
except ImportError:
    print("❌  Playwright fehlt. Bitte installieren:")
    print("      pip install playwright")
    print("      export PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=$(which chromium)")
    sys.exit(1)

# ══════════════════════════════════════════════════════════════════════════════
#  KONFIGURATION  (alles hier anpassen)
# ══════════════════════════════════════════════════════════════════════════════

BASE_URL     = "https://www.alditalk-kundenportal.de"
LOGIN_URL    = f"{BASE_URL}/user/auth/login/"
OVERVIEW_URL = f"{BASE_URL}/user/auth/account-overview/"

THRESHOLD_MB   = 1024   # Buche wenn verbleibende MB < dieser Wert (1 GB = 1024)
CHECK_SEC      = 120    # Prüfintervall in Sekunden (Standard: 2 Min.)
DEFAULT_USER   = os.environ.get("ALDI_USER", "")
DEFAULT_PASS   = os.environ.get("ALDI_PASS", "")

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
#  JAVASCRIPT-SNIPPETS  (Shadow DOM Traversal + Vue State)
# ══════════════════════════════════════════════════════════════════════════════

_JS_FIND_BTN = """
function _findBtn(root, d) {
  d = d || 0;
  if (d > 15) return null;
  try {
    var host = (root && root.querySelectorAll) ? root : (root && root.host) || root;
    if (!host || !host.querySelectorAll) return null;
    var all = host.querySelectorAll('*');
    for (var i = 0; i < all.length; i++) {
      var el = all[i];
      if (/button/i.test(el.tagName) && /1\\s*GB/i.test(el.textContent || '')) return el;
      if (el.shadowRoot) {
        var f = _findBtn(el.shadowRoot, d + 1);
        if (f) return f;
      }
    }
  } catch(e) {}
  return null;
}
"""

JS_GET_BALANCE = _JS_FIND_BTN + """
(function() {
  var btn = _findBtn(document, 0);
  if (!btn) return { error: 'button_not_found' };

  var vm = btn.__vueParentComponent;
  if (!vm) return { error: 'vue_not_found' };

  var proxy = vm.proxy;
  var bd = proxy && proxy.baseData;
  if (!bd || !bd[0]) return { error: 'baseData_not_found' };

  var alloc_kb  = bd[0].allocated || 0;
  var used_kb   = bd[0].used      || 0;
  var rem_kb    = alloc_kb - used_kb;
  var rem_mb    = Math.round(rem_kb / 1024);

  var btn_grey = false;
  try {
    var bg = window.getComputedStyle(btn).backgroundColor;
    btn_grey = /rgb\\(212,\\s*212,\\s*212\\)/.test(bg);
  } catch(e) {}

  return {
    allocated_mb  : Math.round(alloc_kb / 1024),
    used_mb       : Math.round(used_kb  / 1024),
    remaining_mb  : rem_mb,
    remaining_kb  : rem_kb,
    on_demand_ok  : !!(bd[0].isOnDemandRefillApplicable),
    btn_disabled  : btn_grey,
    error         : null
  };
})()
"""

JS_CLICK_BTN = _JS_FIND_BTN + """
(function() {
  var btn = _findBtn(document, 0);
  if (!btn) return { clicked: false, reason: 'not_found' };
  try { btn.scrollIntoView({ block: 'center' }); } catch(e) {}
  btn.click();
  btn.dispatchEvent(new MouseEvent('click', { bubbles: true, composed: true, cancelable: true }));
  return { clicked: true };
})()
"""

JS_CLOSE_MODAL = """
(function() {
  var selectors = [
    '[data-testid="modal-close"]', '.modal__close',
    'button[aria-label="Close"]', 'button[aria-label="Schließen"]',
    'one-button[label="Schließen"]'
  ];
  function find(root, d) {
    d = d || 0; if (d > 12) return null;
    for (var i = 0; i < selectors.length; i++) {
      try { var el = root.querySelector(selectors[i]); if (el) return el; } catch(e) {}
    }
    try {
      var all = root.querySelectorAll ? root.querySelectorAll('*') : [];
      for (var j = 0; j < all.length; j++) {
        if (all[j].shadowRoot) { var r = find(all[j].shadowRoot, d+1); if(r) return r; }
      }
    } catch(e) {}
    return null;
  }
  var c = find(document);
  if (c) { c.click(); return true; }
  return false;
})()
"""

# ══════════════════════════════════════════════════════════════════════════════
#  BROWSER HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def wait_quiet(page: Page, timeout: int = 8000) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout)
    except PWTimeout:
        pass


async def handle_cookie_banner(page: Page) -> bool:
    """Schließt Cookie-Consent-Banner. Gibt True zurück wenn eines gefunden wurde."""
    await page.wait_for_timeout(1200)

    # Häufigste Selektoren (OneTrust, eigene etc.)
    selectors = [
        '#onetrust-accept-btn-handler',
        'button:has-text("Alle akzeptieren")',
        'button:has-text("Alle Cookies akzeptieren")',
        'button:has-text("Nur notwendige Cookies")',
        'button:has-text("Ablehnen")',
        'button:has-text("Necessary only")',
        'button:has-text("Accept all")',
        '[data-testid="cookie-accept"]',
        '.cookie-consent button',
        '#cookie-agree',
        '.js-accept-cookies',
    ]

    for sel in selectors:
        try:
            btn = await page.query_selector(sel)
            if btn and await btn.is_visible():
                await btn.click()
                log.info(f"🍪 Cookie-Banner geschlossen ({sel})")
                await page.wait_for_timeout(700)
                return True
        except Exception:
            pass

    # Fallback: JS über normales DOM
    try:
        found = await page.evaluate("""
          (function() {
            var texts = ['Alle akzeptieren','Ablehnen','Nur notwendige','Necessary only','Accept'];
            var btns = document.querySelectorAll('button');
            for (var i=0;i<btns.length;i++) {
              var t = btns[i].textContent || '';
              if (texts.some(function(x){ return t.indexOf(x) >= 0; })) {
                btns[i].click(); return true;
              }
            }
            return false;
          })()
        """)
        if found:
            log.info("🍪 Cookie-Banner per JS geschlossen")
            await page.wait_for_timeout(700)
            return True
    except Exception:
        pass

    return False


async def is_logged_in(page: Page) -> bool:
    url = page.url
    if '/login' in url or 'login.alditalk-kundenbetreuung.de' in url:
        return False
    try:
        body = await page.evaluate("document.body && document.body.innerText || ''")
        indicators = ['Abmelden', 'Logout', 'Mein Konto', 'Kontoübersicht', 'account-overview']
        if any(kw in body for kw in indicators):
            return True
    except Exception:
        pass
    return '/auth/' in url and '/login' not in url


async def do_login(page: Page, username: str, password: str) -> bool:
    """Führt Login durch. Gibt True bei Erfolg zurück.

    ForgeRock/OpenAM Besonderheit:
    – Wenn der User im Browser bereits bekannt ist, erscheint nur das Passwort-Feld
      (kein Username-Feld). Das ist normal – wir überspringen username dann einfach.
    – Die Formularfelder werden asynchron per JS gerendert → wait_for_selector nötig.
    """
    log.info("🔐 Navigiere zum Portal (prüfe Login-Status)...")
    try:
        # Erst zur Übersicht: wenn schon eingeloggt, kein Login nötig
        await page.goto(OVERVIEW_URL, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        log.error(f"Navigation fehlgeschlagen: {e}")
        return False

    await wait_quiet(page)
    await handle_cookie_banner(page)

    # ── Schon eingeloggt? ────────────────────────────────────────────────────
    if await is_logged_in(page):
        log.info("✅ Bereits eingeloggt!")
        return True

    log.info("   Nicht eingeloggt – ForgeRock Login-Seite: %s", page.url)
    log.info("   Warte auf Formular-Rendering (bis 10s)...")

    # ── Username ──────────────────────────────────────────────────────────────
    # ForgeRock zeigt manchmal NUR das Passwort-Feld (wenn Username bekannt).
    # → kurze Wartezeit pro Selektor, dann weiter wenn nicht vorhanden.
    user_field = None
    for sel in ['#idToken1', 'input[name="IDToken1"]', 'input[type="tel"]',
                'input[name="username"]', 'input[placeholder*="Rufnummer"]']:
        try:
            await page.wait_for_selector(sel, timeout=3000, state="visible")
            f = await page.query_selector(sel)
            if f and await f.is_visible():
                user_field = f
                log.info("   Username-Feld gefunden (%s)", sel)
                break
        except Exception:
            pass  # Feld fehlt → ForgeRock Passwort-only Seite

    if user_field:
        await user_field.fill(username)
        log.info(f"   Rufnummer eingegeben: {username[:4]}****")
    else:
        log.info("   Kein Username-Feld → ForgeRock kennt den User (Passwort-only Seite)")

    # ── Passwort ──────────────────────────────────────────────────────────────
    pw_field = None
    for sel in ['#idToken2', 'input[name="IDToken2"]', 'input[type="password"]',
                'input[name="password"]', 'input[id*="pass"]']:
        try:
            await page.wait_for_selector(sel, timeout=8000, state="visible")
            f = await page.query_selector(sel)
            if f and await f.is_visible():
                pw_field = f
                log.info("   Passwort-Feld gefunden (%s)", sel)
                break
        except Exception:
            pass

    if not pw_field:
        log.error("❌ Kein Passwort-Feld gefunden!")
        log.info(f"   Aktuelle URL: {page.url}")
        # Screenshot-Hinweis
        log.info("   Tipp: Starte mit --visible um den Browser zu sehen")
        return False

    await pw_field.fill(password)
    log.info("   Passwort eingegeben")

    # ── Absenden ──────────────────────────────────────────────────────────────
    submitted = False
    for sel in [
        '#loginButton_0',               # ForgeRock Standard-Submit
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
                log.info("   Formular abgeschickt (Klick: %s)", sel)
                break
        except Exception:
            pass

    if not submitted:
        await pw_field.press("Enter")
        log.info("   Formular abgeschickt (Enter)")

    log.info("   Warte auf Weiterleitung nach Login...")

    # Warte auf URL-Änderung weg von login-Seite
    try:
        await page.wait_for_url(
            lambda u: 'login.alditalk-kundenbetreuung.de' not in u and '/login' not in u,
            timeout=20000
        )
    except PWTimeout:
        log.warning("   Weiterleitung hat zu lange gedauert (URL: %s)", page.url)

    await wait_quiet(page)
    await handle_cookie_banner(page)

    ok = await is_logged_in(page)
    if ok:
        log.info(f"✅ Login erfolgreich! URL: {page.url}")
    else:
        log.error(f"❌ Login fehlgeschlagen! URL: {page.url}")
        log.error("   Tipp: Starte mit --visible um zu sehen was passiert")
    return ok


async def ensure_on_overview(page: Page) -> bool:
    """Navigiert zur Kontoübersicht falls nötig."""
    if 'account-overview' not in page.url:
        log.info("Navigiere zur Kontoübersicht...")
        try:
            await page.goto(OVERVIEW_URL, wait_until="domcontentloaded", timeout=30000)
            await wait_quiet(page, timeout=12000)
            await handle_cookie_banner(page)
            return True
        except Exception as e:
            log.error(f"Navigation zur Übersicht fehlgeschlagen: {e}")
            return False
    return True


async def reload_balance(page: Page) -> None:
    """Seite neu laden um aktuellen Datenstand zu bekommen."""
    log.debug("Seite wird neu geladen...")
    try:
        await page.reload(wait_until="domcontentloaded", timeout=30000)
        await wait_quiet(page, timeout=12000)
        await handle_cookie_banner(page)
        # MFE-Bundles brauchen einen Moment
        await page.wait_for_timeout(2500)
    except Exception as e:
        log.warning(f"Reload fehlgeschlagen: {e}")


async def read_balance(page: Page, max_attempts: int = 4) -> Optional[dict]:
    """Liest Datenvolumen aus Vue-Komponente. Gibt None bei Fehler zurück."""
    for attempt in range(max_attempts):
        try:
            result = await page.evaluate(JS_GET_BALANCE, timeout=10000)
            if result and result.get('error') is None:
                return result
            err = result.get('error', '?') if result else 'null'
            log.debug(f"Balance-Versuch {attempt+1}/{max_attempts}: {err}")
        except Exception as e:
            log.debug(f"Balance JS Fehler {attempt+1}: {e}")

        if attempt < max_attempts - 1:
            await page.wait_for_timeout(2500)   # warte auf MFE-Ladezeit

    return None


async def attempt_booking(page: Page) -> bool:
    """Klickt den +1 GB Button und prüft ob die Buchung erfolgreich war."""
    log.info("🖱️  Klicke +1 GB Button...")
    try:
        res = await page.evaluate(JS_CLICK_BTN, timeout=5000)
        if not res or not res.get('clicked'):
            log.warning("   Button nicht gefunden oder nicht klickbar")
            return False
    except Exception as e:
        log.error(f"   Button-Klick Fehler: {e}")
        return False

    # Warte auf Reaktion der Seite
    await page.wait_for_timeout(3000)
    await wait_quiet(page, timeout=10000)

    # Prüfe Ergebnis
    try:
        body = await page.evaluate("document.body && document.body.innerText || ''")

        success_kw = [
            'erfolgreich', 'gebucht', 'wurde gebucht', 'booked', 'buchung erfolgreich',
            'Das Datenvolumen wurde', 'Du hast erfolgreich', '+1 GB gebucht',
        ]
        block_kw = [
            'Sobald du weniger als 1 GB', 'weniger als 1 GB übrig',
            'nicht verfügbar', 'nicht möglich',
        ]

        body_lower = body.lower()
        if any(kw.lower() in body_lower for kw in success_kw):
            log.info("✅ Buchung ERFOLGREICH!")
            return True

        if any(kw.lower() in body_lower for kw in block_kw):
            log.warning("⚠️  Modal 'Noch nicht buchbar' (Balance > 1 GB oder Server-Check)")
            await page.evaluate(JS_CLOSE_MODAL)
            await page.wait_for_timeout(600)
            return False

    except Exception as e:
        log.debug(f"Buchungs-Check Fehler: {e}")

    # Kein eindeutiges Signal – vorsichtig annehmen es hat funktioniert
    log.info("   Kein eindeutiges Ergebnis – Seite wird neu geladen zur Verifikation")
    return False


# ══════════════════════════════════════════════════════════════════════════════
#  HAUPTSCHLEIFE
# ══════════════════════════════════════════════════════════════════════════════

async def monitor_loop(page: Page, username: str, password: str) -> None:
    errors_in_row = 0
    bookings_done = 0
    reload_every  = 5   # Nach N Checks Seite neu laden (Sicherheit)
    checks_done   = 0

    log.info(f"\n{'═'*60}")
    log.info("  Monitor aktiv – prüfe alle %ds, Schwelle: %d MB", CHECK_SEC, THRESHOLD_MB)
    log.info(f"{'═'*60}\n")

    while True:
        now = datetime.now().strftime("%H:%M")
        checks_done += 1

        try:
            # ── Session prüfen ────────────────────────────────────────────────
            if not await is_logged_in(page):
                log.warning("⚠️  Session abgelaufen – erneuter Login...")
                ok = await do_login(page, username, password)
                if not ok:
                    log.error("   Login fehlgeschlagen! Warte 60s...")
                    await asyncio.sleep(60)
                    continue
                await ensure_on_overview(page)
                await page.wait_for_timeout(3000)

            # ── Periodischer Reload für frischen Balance-Stand ────────────────
            if checks_done % reload_every == 0:
                await reload_balance(page)

            # ── Balance lesen ─────────────────────────────────────────────────
            bal = await read_balance(page)

            if bal is None:
                errors_in_row += 1
                log.warning(f"[{now}] Balance nicht lesbar (#{errors_in_row})")
                if errors_in_row >= 3:
                    log.info("   Seite wird komplett neu geladen...")
                    await reload_balance(page)
                    errors_in_row = 0
                await asyncio.sleep(30)
                continue

            errors_in_row = 0
            rem  = bal['remaining_mb']
            used = bal['used_mb']
            tot  = bal['allocated_mb']

            log.info(
                "[%s] 📊 %4d MB verbleibend  (%d / %d MB verbraucht)%s",
                now, rem, used, tot,
                "  ← BUTTON AKTIV!" if not bal.get('btn_disabled') else ""
            )

            # ── Buchung auslösen ──────────────────────────────────────────────
            if rem < THRESHOLD_MB:
                log.info("💡 %d MB < %d MB Schwelle → starte Buchung!", rem, THRESHOLD_MB)

                success = await attempt_booking(page)
                if success:
                    bookings_done += 1
                    log.info("✅ Buchung #%d abgeschlossen!", bookings_done)
                    await asyncio.sleep(15)
                    await reload_balance(page)  # sofort neu laden
                    checks_done = 0             # reset reload-Timer
                else:
                    log.info("   Erneuter Versuch in 60s...")
                    await asyncio.sleep(60)
                    await reload_balance(page)
                    continue
            else:
                # Nächste Prüfung in CHECK_SEC
                pass

        except asyncio.CancelledError:
            log.info("Monitor durch CancelledError gestoppt.")
            break
        except KeyboardInterrupt:
            log.info("\n⛔ Durch Benutzer gestoppt.")
            break
        except Exception as e:
            log.error(f"Unerwarteter Fehler: {e}", exc_info=True)
            errors_in_row += 1

        await asyncio.sleep(CHECK_SEC)


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

async def main(username: str, password: str, visible: bool = False) -> None:
    if not username or not password:
        log.error("❌ Bitte Rufnummer und Passwort angeben!")
        log.error("   python aldi_monitor.py --user 017612345678 --pass DeinPasswort")
        sys.exit(1)

    log.info("╔══════════════════════════════════════════╗")
    log.info("║  ALDI Talk Auto-Refill Monitor           ║")
    log.info("╚══════════════════════════════════════════╝")
    log.info("  User:      %s****", username[:4])
    log.info("  Schwelle:  %d MB  (= %s GB)", THRESHOLD_MB, round(THRESHOLD_MB/1024,1))
    log.info("  Intervall: %ds", CHECK_SEC)
    log.info("  Browser:   %s", "sichtbar" if visible else "headless")

    chromium_path = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
    if chromium_path:
        log.info("  Chromium:  %s", chromium_path)

    async with async_playwright() as pw:
        launch_opts = dict(
            headless=not visible,
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--disable-extensions',
            ],
        )
        if chromium_path:
            launch_opts['executable_path'] = chromium_path

        browser: Browser = await pw.chromium.launch(**launch_opts)

        ctx: BrowserContext = await browser.new_context(
            viewport={'width': 390, 'height': 844},
            user_agent=(
                'Mozilla/5.0 (Linux; Android 14; Pixel 8) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/124.0.0.0 Mobile Safari/537.36'
            ),
            locale='de-DE',
            timezone_id='Europe/Berlin',
        )
        page: Page = await ctx.new_page()
        page.set_default_timeout(30000)

        try:
            # ── Login / Session-Check ─────────────────────────────────────────
            # do_login navigiert zuerst zur Übersicht; Login nur wenn nötig
            ok = await do_login(page, username, password)
            if not ok:
                log.error("Login fehlgeschlagen – Script beendet.")
                return

            # Sicherstellen auf Übersicht
            await ensure_on_overview(page)

            # MFE-Bundles laden lassen
            log.info("Warte auf MFE-Ladezeit...")
            await page.wait_for_timeout(4000)
            await wait_quiet(page, timeout=12000)

            # ── Monitor starten ───────────────────────────────────────────────
            await monitor_loop(page, username, password)

        except KeyboardInterrupt:
            log.info("\n⛔ Durch Benutzer gestoppt.")
        finally:
            await browser.close()
            log.info("Browser geschlossen. Tschüss!")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="ALDI Talk Auto-Refill Monitor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Beispiele:
  python aldi_monitor.py --user 017612345678 --pass MeinPasswort
  python aldi_monitor.py --user 017612345678 --pass MeinPasswort --interval 60
  python aldi_monitor.py --user 017612345678 --pass MeinPasswort --visible
  ALDI_USER=017612345678 ALDI_PASS=MeinPasswort python aldi_monitor.py
        """
    )
    p.add_argument('--user',     default=DEFAULT_USER,   help='ALDI Talk Rufnummer (mit 0 vorne)')
    p.add_argument('--pass',     default=DEFAULT_PASS,   dest='password', help='ALDI Talk Passwort')
    p.add_argument('--interval', default=CHECK_SEC,      type=int, help=f'Prüfintervall Sekunden (Standard: {CHECK_SEC})')
    p.add_argument('--threshold',default=THRESHOLD_MB,   type=int, help=f'Buche wenn MB < Wert (Standard: {THRESHOLD_MB})')
    p.add_argument('--visible',  action='store_true',              help='Browser sichtbar machen (kein headless)')
    return p.parse_args()


if __name__ == '__main__':
    args = parse_args()
    CHECK_SEC    = args.interval
    THRESHOLD_MB = args.threshold
    try:
        asyncio.run(main(args.user, args.password, visible=args.visible))
    except KeyboardInterrupt:
        print("\n⛔ Gestoppt.")
