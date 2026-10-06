#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# build-book.sh — Generate the VelaFlow Technical Reference as A4 PDF
#
# Prerequisites (install once):
#   sudo apt install pandoc texlive-latex-recommended texlive-latex-extra \
#        texlive-fonts-recommended texlive-fonts-extra lmodern
#   npm install -g @mermaid-js/mermaid-cli          # for diagram rendering
#
# macOS:
#   brew install pandoc mactex
#   npm install -g @mermaid-js/mermaid-cli
#
# Usage:
#   ./scripts/build-book.sh              # Build PDF
#   ./scripts/build-book.sh --no-mermaid # Skip Mermaid rendering (faster)
# ──────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BOOK_MD="$PROJECT_ROOT/docs/velaflow-book.md"
BUILD_DIR="$PROJECT_ROOT/build"
OUT_PDF="$BUILD_DIR/VelaFlow-Technical-Reference.pdf"
TEMP_MD="$BUILD_DIR/_book_rendered.md"

SKIP_MERMAID=false
if [[ "${1:-}" == "--no-mermaid" ]]; then
    SKIP_MERMAID=true
fi

# ── Dependency checks ────────────────────────────────────────────────
check_cmd() {
    if ! command -v "$1" &>/dev/null; then
        echo "ERROR: '$1' not found. Install it first (see header of this script)."
        exit 1
    fi
}

check_cmd pandoc

if [[ "$SKIP_MERMAID" == false ]]; then
    if ! command -v mmdc &>/dev/null; then
        echo "WARNING: 'mmdc' (mermaid-cli) not found. Mermaid diagrams will be"
        echo "         rendered as code blocks. Install with:"
        echo "           npm install -g @mermaid-js/mermaid-cli"
        echo ""
        SKIP_MERMAID=true
    fi
fi

# ── Prepare build directory ──────────────────────────────────────────
mkdir -p "$BUILD_DIR"
cp "$BOOK_MD" "$TEMP_MD"

# ── Render Mermaid diagrams to PNG ───────────────────────────────────
if [[ "$SKIP_MERMAID" == false ]]; then
    echo "Rendering Mermaid diagrams..."
    DIAGRAM_COUNT=0

    # Extract each ```mermaid block, render to PNG, replace in markdown
    # Use a Python helper for reliable multi-line regex replacement
    python3 - "$TEMP_MD" "$BUILD_DIR" <<'PYEOF'
import re, subprocess, sys, os

md_path = sys.argv[1]
build_dir = sys.argv[2]

with open(md_path, "r", encoding="utf-8") as f:
    content = f.read()

pattern = re.compile(r"```mermaid\n(.*?)```", re.DOTALL)
matches = list(pattern.finditer(content))

if not matches:
    print("  No Mermaid blocks found.")
    sys.exit(0)

print(f"  Found {len(matches)} Mermaid diagram(s).")

for i, m in enumerate(reversed(matches)):  # reverse to preserve offsets
    diagram_code = m.group(1).strip()
    mmd_file = os.path.join(build_dir, f"diagram_{len(matches)-1-i}.mmd")
    png_file = os.path.join(build_dir, f"diagram_{len(matches)-1-i}.png")

    with open(mmd_file, "w", encoding="utf-8") as f:
        f.write(diagram_code)

    result = subprocess.run(
        ["mmdc", "-i", mmd_file, "-o", png_file, "-b", "white",
         "-w", "1200", "-H", "800"],
        capture_output=True, text=True
    )

    if result.returncode == 0 and os.path.exists(png_file):
        replacement = f"![Diagram {len(matches)-1-i}]({png_file})"
        content = content[:m.start()] + replacement + content[m.end():]
        print(f"  Rendered diagram {len(matches)-1-i}")
    else:
        print(f"  WARNING: Failed to render diagram {len(matches)-1-i}")
        if result.stderr:
            print(f"    {result.stderr[:200]}")

with open(md_path, "w", encoding="utf-8") as f:
    f.write(content)

print("  Mermaid rendering complete.")
PYEOF
fi

# ── Generate PDF with pandoc ─────────────────────────────────────────
echo "Generating A4 PDF..."
pandoc "$TEMP_MD" \
    -o "$OUT_PDF" \
    --pdf-engine=xelatex \
    --from=markdown+yaml_metadata_block \
    --toc \
    --toc-depth=3 \
    --number-sections \
    -V geometry:"a4paper, margin=2.5cm" \
    -V fontsize:"11pt" \
    -V documentclass:"report" \
    -V mainfont:"Latin Modern Roman" \
    -V monofont:"DejaVu Sans Mono" \
    -V colorlinks:"true" \
    -V linkcolor:"NavyBlue" \
    -V urlcolor:"NavyBlue" \
    -V toccolor:"NavyBlue" \
    -V header-includes:'\usepackage{fancyhdr}\pagestyle{fancy}\fancyhead[L]{VelaFlow Technical Reference}\fancyhead[R]{\thepage}\fancyfoot[C]{}' \
    -V header-includes:'\usepackage{enumitem}\setlist{nosep}' \
    -V header-includes:'\usepackage{listings}\lstset{basicstyle=\ttfamily\small,breaklines=true,frame=single,backgroundcolor=\color[gray]{0.95}}' \
    --highlight-style=tango \
    2>&1

# ── Cleanup ──────────────────────────────────────────────────────────
if [[ -f "$OUT_PDF" ]]; then
    PDF_SIZE=$(du -h "$OUT_PDF" | cut -f1)
    echo ""
    echo "Build complete: $OUT_PDF ($PDF_SIZE)"
    echo ""
    echo "The PDF is formatted for A4 paper with 2.5cm margins."
    echo "Print directly or view on iPad for annotation."
else
    echo "ERROR: PDF generation failed. Check pandoc output above."
    exit 1
fi
