#!/data/data/com.termux/files/usr/bin/bash
# ═══════════════════════════════════════════════════════
#  ALDI Talk Monitor – Termux Fix (kein ARM64 Playwright)
# ═══════════════════════════════════════════════════════
#
# WEG 1 (Empfohlen): proot-distro (Ubuntu in Termux)
# WEG 2: requests-only (kein Browser – leichter)
#
# Welchen Weg willst du? Lese unten.

echo "Welcher Weg?"
echo ""
echo "  bash termux_fix.sh proot    → Weg 1: Ubuntu + Playwright"
echo "  bash termux_fix.sh requests → Weg 2: requests (kein Browser)"
echo ""

MODE="${1:-}"

# ─── WEG 1: proot-distro ────────────────────────────────────────────────────
if [ "$MODE" = "proot" ]; then
  echo "=== Weg 1: proot-distro ==="
  pkg install -y proot-distro

  echo "Ubuntu wird installiert (einmalig, ~200 MB)..."
  proot-distro install ubuntu 2>/dev/null || echo "(schon installiert)"

  echo "Chromium + Python in Ubuntu installieren..."
  proot-distro login ubuntu -- bash -c "
    apt-get update -qq
    apt-get install -y python3 python3-pip chromium-browser 2>/dev/null || \
    apt-get install -y python3 python3-pip chromium 2>/dev/null
    pip3 install playwright --quiet
  "

  echo ""
  echo "✅ Fertig! Starten mit:"
  echo ""
  echo "  proot-distro login ubuntu -- bash -c \\"
  echo "    'export PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=\$(which chromium-browser || which chromium)"
  echo "     python3 /pfad/zu/aldi_monitor.py --user 017612345678 --pass DeinPasswort'"
  echo ""
  echo "Oder einmalig in Ubuntu Pfad setzen:"
  echo "  proot-distro login ubuntu"
  echo "  echo 'export PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=\$(which chromium-browser || which chromium)' >> ~/.bashrc"
  echo "  source ~/.bashrc"
  echo "  python3 aldi_monitor.py --user 017612345678 --pass DeinPasswort"

# ─── WEG 2: requests ────────────────────────────────────────────────────────
elif [ "$MODE" = "requests" ]; then
  echo "=== Weg 2: requests (kein Browser) ==="
  pkg install -y python
  pip install requests beautifulsoup4 lxml

  echo ""
  echo "✅ Fertig!"
  echo "  python aldi_requests.py --user 017612345678 --pass DeinPasswort"

else
  echo "Bitte einen Modus angeben: proot oder requests"
  echo "  bash termux_fix.sh proot"
  echo "  bash termux_fix.sh requests"
fi
