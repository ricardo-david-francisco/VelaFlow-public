#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# build-pdfs.sh — Generate both VelaFlow A4 PDF documents
#
# Outputs (in docs/):
#   1. VelaFlow-Technical-Reference-A4.pdf  — Full technical reference
#   2. VelaFlow-Source-Appendix-A4.pdf      — Complete source code listing
#
# Prerequisites (install once):
#   # Linux/macOS:
#     sudo apt install pandoc           # or: brew install pandoc
#     cargo install typst-cli           # or: brew install typst
#     npm install -g @mermaid-js/mermaid-cli
#
#   # Windows (via winget + fnm):
#     winget install JohnMacFarlane.Pandoc
#     winget install Typst.Typst
#     fnm install --lts && npm install -g @mermaid-js/mermaid-cli
#
# Usage:
#   ./scripts/build-pdfs.sh                 # Build both PDFs
#   ./scripts/build-pdfs.sh --book-only     # Build only the reference
#   ./scripts/build-pdfs.sh --appendix-only # Build only the appendix
#   ./scripts/build-pdfs.sh --skip-mermaid  # Skip Mermaid rendering
# ──────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Activate Python venv if it exists
if [[ -f "$PROJECT_ROOT/.venv/bin/activate" ]]; then
    source "$PROJECT_ROOT/.venv/bin/activate"
elif [[ -f "$PROJECT_ROOT/.venv/Scripts/activate" ]]; then
    source "$PROJECT_ROOT/.venv/Scripts/activate"
fi

# Run the Python build script, passing all arguments through
python3 "$SCRIPT_DIR/build_pdfs.py" "$@" || python "$SCRIPT_DIR/build_pdfs.py" "$@"
