#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
command -v python3 >/dev/null || { echo "Install Python 3.10+ from https://www.python.org/downloads/ and run this again."; exit 1; }
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
echo "Installing packages (first run only, 1-3 minutes)..."
python -m pip install -q -r requirements.txt
python -m playwright install chromium
echo
echo "OPTIONAL: paste an Anthropic API key for AI explanations, or press Enter to skip."
read -r -p "API key (Enter to skip): " KEY
AIFLAG="--no-ai"
if [ -n "$KEY" ]; then export ANTHROPIC_API_KEY="$KEY"; AIFLAG=""; fi
echo; echo "===== STEP 1: version with planted bugs ====="
python tester.py site/harborline-homes-buggy.html --out reports/before --headed $AIFLAG
echo; echo "===== STEP 2: fixed version, compared with step 1 ====="
python tester.py site/harborline-homes-fixed.html --out reports/after --compare reports/before/results.json --headed $AIFLAG
OPEN=$(command -v open || command -v xdg-open || true)
[ -n "$OPEN" ] && "$OPEN" reports/before/report.html && "$OPEN" reports/after/report.html
echo "Done."
