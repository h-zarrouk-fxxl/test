#!/data/data/com.termux/files/usr/bin/bash
# ALDI Talk Monitor – Termux Einrichtung
# Einmalig ausführen: bash termux_setup.sh

set -e
echo "═══════════════════════════════════════"
echo "  ALDI Talk Monitor – Termux Setup"
echo "═══════════════════════════════════════"

echo "[1/4] Pakete aktualisieren..."
pkg update -y && pkg upgrade -y

echo "[2/4] Python + Chromium installieren..."
pkg install -y python chromium

echo "[3/4] Playwright installieren..."
pip install playwright

echo "[4/4] Chromium-Pfad setzen..."
CHROMIUM_PATH=$(which chromium)
echo "export PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=$CHROMIUM_PATH" >> ~/.bashrc
export PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=$CHROMIUM_PATH

echo ""
echo "✅ Setup fertig!"
echo ""
echo "STARTEN:"
echo "  python aldi_monitor.py --user 017612345678 --pass DeinPasswort"
echo ""
echo "Tipp: Läuft im Hintergrund mit:"
echo "  nohup python aldi_monitor.py --user 017612345678 --pass DeinPasswort > aldi.log 2>&1 &"
echo "  tail -f aldi.log   # Log beobachten"
echo "  kill %1            # Stoppen"
