#!/usr/bin/env python3
"""
VelaFlow PDF Documentation Builder.

Generates two A4 PDFs:
  1. VelaFlow-Technical-Reference-A4.pdf  â€” Full technical reference book
  2. VelaFlow-Source-Appendix-A4.pdf      â€” Complete source code listing

Prerequisites:
  pandoc   (winget install JohnMacFarlane.Pandoc)
  typst    (winget install Typst.Typst)
  mmdc     (npm install -g @mermaid-js/mermaid-cli)

Usage:
  python scripts/build_pdfs.py                # Build both PDFs
  python scripts/build_pdfs.py --book-only    # Build only the reference
  python scripts/build_pdfs.py --appendix-only  # Build only the appendix
  python scripts/build_pdfs.py --skip-mermaid   # Skip Mermaid rendering
"""
from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import textwrap
from datetime import date
from pathlib import Path

# â”€â”€ Paths â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
PROJECT_ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = PROJECT_ROOT / "build"
DOCS_DIR = PROJECT_ROOT / "docs"
BOOK_MD = DOCS_DIR / "velaflow-book.md"
BOOK_PDF = DOCS_DIR / "VelaFlow-Technical-Reference-A4.pdf"
APPENDIX_PDF = DOCS_DIR / "VelaFlow-Source-Appendix-A4.pdf"
ROOT_README = PROJECT_ROOT / "README.md"
TECH_README = DOCS_DIR / "README-technical.md"

# Files and directories to EXCLUDE from the appendix listing
APPENDIX_EXCLUDE_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "build", ".pytest_cache",
    ".mypy_cache", "node_modules", ".egg-info",
}
APPENDIX_EXCLUDE_FILES = {
    "config/.env",          # Real credentials â€” never include
    "scripts/_live_test_notion.py",
    "scripts/_live_test.py",
    "scripts/_test_notebooklm_extraction.py",
}
APPENDIX_EXCLUDE_EXTENSIONS = {
    ".pdf", ".pyc", ".pyo", ".egg", ".tar.gz", ".whl",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
}

# Credential patterns â€” fail the build if any match appears in output
_CREDENTIAL_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{20,}"),                # OpenAI/proxy keys
    re.compile(r"ghp_[A-Za-z0-9]{36}"),                 # GitHub PATs
    re.compile(r"gho_[A-Za-z0-9]{36}"),                 # GitHub OAuth
    re.compile(r"AIzaSy[A-Za-z0-9_-]{33}"),             # Google AI keys (old format)
    re.compile(r"AQ\.[A-Za-z0-9_-]{30,}"),              # Google AI API keys (new format)
    re.compile(r"xoxb-[0-9]{10,}"),                     # Slack tokens
    re.compile(r"Bearer\s+[A-Za-z0-9._-]{20,}"),        # Bearer tokens
    re.compile(r"ntn_[A-Za-z0-9]{30,}"),                # Notion integration tokens
    re.compile(r"[0-9a-f]{40}"),                         # 40-char hex tokens (Todoist, etc.)
    re.compile(r"GOOGLE_OAUTH_CLIENT_SECRET=[^\s]{10,}"),  # OAuth secrets in env
    re.compile(r"N8N_ENCRYPTION_KEY=[^\s]{10,}"),        # n8n encryption key
    re.compile(r"REDIS_PASSWORD=[^\s]{10,}"),            # Redis password in env
    re.compile(r"JWT_SECRET=[^\s]{10,}"),                # JWT secret in env
    re.compile(r"VELAFLOW_MASTER_KEY=[^\s]{10,}"),       # Master key in env
]


# â”€â”€ Tool Discovery â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def _find_tool(name: str, extra_paths: list[str] | None = None) -> str | None:
    """Locate a CLI tool on PATH or at known locations."""
    found = shutil.which(name)
    if found:
        return found
    for p in extra_paths or []:
        expanded = os.path.expandvars(p)
        if os.path.isfile(expanded):
            return expanded
    return None


def _resolve_tools() -> dict[str, str]:
    """Resolve pandoc, typst, mmdc paths.  Abort if pandoc/typst missing."""
    home = Path.home()
    tools: dict[str, str] = {}

    pandoc_extra = [
        str(home / "AppData/Local/Pandoc/pandoc.exe"),
        "/usr/bin/pandoc",
        "/usr/local/bin/pandoc",
    ]
    typst_extra = []
    # Scan winget packages for typst
    winget_pkgs = home / "AppData/Local/Microsoft/WinGet/Packages"
    if winget_pkgs.is_dir():
        for d in winget_pkgs.iterdir():
            if "Typst" in d.name:
                for f in d.rglob("typst.exe"):
                    typst_extra.append(str(f))
    typst_extra += ["/usr/bin/typst", "/usr/local/bin/typst"]

    # mmdc: check fnm global installs, npm global, PATH
    mmdc_extra = []
    fnm_dir = home / "AppData/Roaming/fnm/node-versions"
    if fnm_dir.is_dir():
        for v in fnm_dir.iterdir():
            candidate = v / "installation" / "mmdc.cmd"
            if candidate.exists():
                mmdc_extra.append(str(candidate))
            ps_candidate = v / "installation" / "mmdc.ps1"
            if ps_candidate.exists():
                # On Windows, the .cmd wrapper is preferred; fall back to node call
                mmdc_extra.append(str(ps_candidate))
    npm_global = home / "AppData/Roaming/npm"
    if npm_global.is_dir():
        for ext in ("mmdc.cmd", "mmdc"):
            p = npm_global / ext
            if p.exists():
                mmdc_extra.append(str(p))
    mmdc_extra += ["/usr/bin/mmdc", "/usr/local/bin/mmdc"]

    tools["pandoc"] = _find_tool("pandoc", pandoc_extra)
    tools["typst"] = _find_tool("typst", typst_extra)
    tools["mmdc"] = _find_tool("mmdc", mmdc_extra)

    if not tools["pandoc"]:
        _die("pandoc not found.  Install: winget install JohnMacFarlane.Pandoc")
    if not tools["typst"]:
        _die("typst not found.  Install: winget install Typst.Typst")
    return tools


def _die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a subprocess, printing the command and aborting on failure."""
    print(f"  $ {' '.join(cmd[:4])}{'...' if len(cmd) > 4 else ''}")
    result = subprocess.run(
        cmd, capture_output=True, text=True, check=False, **kwargs
    )
    if result.returncode != 0:
        print(f"  STDERR: {result.stderr[:500]}", file=sys.stderr)
    return result


# â”€â”€ Mermaid Rendering â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
_MERMAID_RE = re.compile(r"```mermaid\n(.*?)```", re.DOTALL)


def _render_mermaid(content: str, mmdc_path: str | None) -> str:
    """Replace ```mermaid blocks with rendered PNG images.

    PNGs are written to ``docs/diagrams/`` (a tracked location) at high
    resolution suitable for 4K video reuse. A shared theme config under
    ``scripts/mermaid-theme.json`` applies the VelaFlow colour palette
    (deep-navy / teal accent) so diagrams match the cover and book style.
    """
    if not mmdc_path:
        print("  WARNING: mmdc not found - Mermaid diagrams will remain as code blocks.")
        return content

    diagrams_dir = DOCS_DIR / "diagrams"
    diagrams_dir.mkdir(parents=True, exist_ok=True)
    theme_file = PROJECT_ROOT / "scripts" / "mermaid-theme.json"

    matches = list(_MERMAID_RE.finditer(content))
    if not matches:
        return content

    print(f"  Rendering {len(matches)} Mermaid diagram(s) -> {diagrams_dir}")

    # High-resolution for 4K video reuse (5120 x 3200 effective)
    render_args = [
        "-b", "white",
        "-w", "2560",
        "-H", "1600",
        "-s", "2",
    ]
    if theme_file.exists():
        render_args += ["-c", str(theme_file)]

    # Process in reverse to preserve string offsets
    for i, m in enumerate(reversed(matches)):
        idx = len(matches) - 1 - i
        mmd_code = m.group(1).strip()
        mmd_file = diagrams_dir / f"diagram_{idx}.mmd"
        png_file = diagrams_dir / f"diagram_{idx}.png"

        mmd_file.write_text(mmd_code, encoding="utf-8")

        if mmdc_path.endswith(".ps1"):
            cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                   "-File", mmdc_path,
                   "-i", str(mmd_file), "-o", str(png_file)] + render_args
        else:
            cmd = [mmdc_path, "-i", str(mmd_file), "-o", str(png_file)] + render_args

        result = _run(cmd)
        if result.returncode == 0 and png_file.exists():
            # Reference the PNG from within BUILD_DIR; typst resolves
            # relative paths from the .typ file location.
            try:
                rel_png = os.path.relpath(png_file, BUILD_DIR).replace("\\", "/")
            except ValueError:
                rel_png = png_file.as_posix()
            replacement = f"![Diagram {idx}]({rel_png})"
            content = content[:m.start()] + replacement + content[m.end():]
            print(f"    Diagram {idx}: OK ({png_file.stat().st_size // 1024} KB)")
        else:
            print(f"    Diagram {idx}: FAILED (keeping as code block)")

    return content


# â”€â”€ Markdown Preprocessing â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def _read_readmes_for_pdf() -> str:
    """Return concatenated root + technical README markdown, scrubbed of
    shields.io image badges which pandoc->typst cannot resolve offline."""
    parts: list[str] = []
    for label, path in (
        ("Root README.md", ROOT_README),
        ("Technical README (docs/README-technical.md)", TECH_README),
    ):
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        # Drop shields.io / badge-style image lines â€” they 404 in offline build
        text = re.sub(r"^\[!\[.*?\]\(.*?\)\]\(.*?\)\s*$", "", text, flags=re.M)
        text = re.sub(r"^!\[.*?\]\(https?://img\.shields\.io.*?\)\s*$", "", text, flags=re.M)
        # Replace internal TOC anchor links with plain text â€” the slug labels
        # pandoc emits for heading do not match the root README's "1. Vision"
        # slugs, so keep the prose but drop the link.
        text = re.sub(r"\[([^\]]+)\]\(#[^)]+\)", r"\1", text)
        parts.append(f"\n\\newpage\n\n# {label}\n\n{text}\n")
    return "\n".join(parts)


def _strip_yaml_frontmatter(content: str) -> str:
    """Remove YAML frontmatter (---...---) from the beginning."""
    if content.startswith("---"):
        end = content.find("\n---", 3)
        if end != -1:
            return content[end + 4:].lstrip("\n")
    return content


def _strip_manual_numbering(content: str) -> str:
    """Remove manual chapter/section numbers from headers.

    Transforms:
      # Chapter 1: The Problem          ->  # The Problem
      ## 1.1 Context Switching           ->  ## Context Switching
      # Appendix A: Complete File...     ->  # Complete File Inventory
      # Appendix B: Glossary             ->  # Glossary

    This prevents double-numbering when Pandoc adds --number-sections.
    """
    lines = content.split("\n")
    result = []
    for line in lines:
        if line.startswith("#"):
            # Remove "Chapter N: " prefix
            line = re.sub(r"^(#+\s+)Chapter\s+\d+:\s*", r"\1", line)
            # Remove "Appendix X: " prefix
            line = re.sub(r"^(#+\s+)Appendix\s+[A-Z]:\s*", r"\1", line)
            # Remove "N.N[.N] " numbering from sub-headers
            line = re.sub(r"^(#+\s+)\d+\.\d+(?:\.\d+)?\s+", r"\1", line)
        result.append(line)
    return "\n".join(result)


def _strip_latex_commands(content: str) -> str:
    """Remove LaTeX-specific commands that don't work with Typst."""
    content = content.replace("\\newpage", "")
    return content


# â”€â”€ Cover Page (Typst) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def _typst_cover_page(title: str, subtitle: str, edition: str) -> str:
    """Generate a modern Typst cover page block.

    Uses ``#set page(margin: 0pt, ...)`` so the cover occupies the very
    first physical page of the PDF (no blank page before it). A trailing
    ``#pagebreak()`` hands control back to the body page setup that the
    caller applies after the cover.
    """
    return textwrap.dedent(f"""\
    #set page(
      paper: "a4",
      margin: 0pt,
      numbering: none,
      header: none,
      footer: none,
    )
    #rect(width: 100%, height: 100%, fill: gradient.linear(
      rgb("#070d1a"), rgb("#0f2744"), rgb("#14365d"),
      angle: 135deg,
    ))[
      #align(center + horizon)[
        #block(width: 80%)[
          #v(-4em)
          #line(length: 35%, stroke: 2.5pt + rgb("#38b2ac"))
          #v(2.5em)
          #text(size: 52pt, weight: "bold", fill: white,
                tracking: 0.18em, font: "Arial")[
            VELAFLOW
          ]
          #v(0.8em)
          #text(size: 15pt, fill: rgb("#a0aec0"),
                tracking: 0.1em, font: "Arial")[
            {subtitle.upper()}
          ]
          #v(1.5em)
          #line(length: 15%, stroke: 1pt + rgb("#38b2ac"))
          #v(1.8em)
          #text(size: 11pt, fill: rgb("#718096"), font: "Arial")[
            Self-Hosted AI Productivity Automation System
          ]
          #v(5em)
          #rect(width: 60%, height: 1pt, fill: rgb("#1a365d"))
          #v(1.5em)
          #text(size: 9pt, fill: rgb("#4a5568"), font: "Arial")[
            {edition}
          ]
        ]
      ]
    ]
    #pagebreak()
    """)


# â”€â”€ Build: Technical Reference PDF â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def build_book(tools: dict[str, str], skip_mermaid: bool = False) -> None:
    """Build the main VelaFlow Technical Reference A4 PDF."""
    print("\n=== Building Technical Reference PDF ===")

    BUILD_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Read source markdown
    content = BOOK_MD.read_text(encoding="utf-8")

    # 1a. Prepend both READMEs (root + technical) so every PDF carries them.
    readmes = _read_readmes_for_pdf()
    if readmes:
        content = readmes + "\n\\newpage\n\n" + content

    # 2. Strip frontmatter and LaTeX commands
    content = _strip_yaml_frontmatter(content)
    content = _strip_latex_commands(content)

    # 3. Strip manual numbering to avoid double-numbering with --number-sections
    content = _strip_manual_numbering(content)

    # 4. Render Mermaid diagrams to PNG
    if not skip_mermaid:
        content = _render_mermaid(content, tools.get("mmdc"))
    else:
        print("  Skipping Mermaid rendering (--skip-mermaid)")

    # 5. Write processed markdown
    processed_md = BUILD_DIR / "velaflow-book-processed.md"
    processed_md.write_text(content, encoding="utf-8")

    # 6. Generate cover page as separate Typst file
    cover_typ = BUILD_DIR / "cover.typ"
    today_str = date.today().strftime("%d %B %Y")
    cover_typ.write_text(
        _typst_cover_page(
            "VELAFLOW",
            "COMPLETE TECHNICAL REFERENCE",
            f"{today_str}  -  v1.0",
        ),
        encoding="utf-8",
    )

    # 7. First pass: convert markdown to Typst via Pandoc
    intermediate_typ = BUILD_DIR / "book-content.typ"
    pandoc_cmd = [
        tools["pandoc"],
        str(processed_md),
        "--from", "markdown-citations",
        "--to", "typst",
        "--toc", "--toc-depth=3",
        "--number-sections",
        "-o", str(intermediate_typ),
    ]
    result = _run(pandoc_cmd)
    if result.returncode != 0:
        _die(f"Pandoc markdown->typst failed: {result.stderr[:300]}")

    # 8. Build final Typst document: cover + page setup + content
    final_typ = BUILD_DIR / "velaflow-final.typ"
    typ_content = intermediate_typ.read_text(encoding="utf-8")

    # Assemble full document
    cover_code = cover_typ.read_text(encoding="utf-8")
    final_doc = _assemble_typst_document(cover_code, typ_content)
    final_typ.write_text(final_doc, encoding="utf-8")

    # 9. Compile Typst to PDF
    typst_cmd = [tools["typst"], "compile", "--root", str(PROJECT_ROOT), str(final_typ), str(BOOK_PDF)]
    result = _run(typst_cmd)
    if result.returncode != 0:
        _die(f"Typst compilation failed: {result.stderr[:500]}")

    size_kb = BOOK_PDF.stat().st_size // 1024
    print(f"\n  Technical Reference: {BOOK_PDF} ({size_kb} KB)")


def _assemble_typst_document(cover_code: str, body_code: str) -> str:
    """Assemble the final Typst document with cover, page setup, and content.

    The cover block starts with its own ``#set page(margin: 0pt, ...)`` so it
    occupies the FIRST physical page. After the cover's trailing
    ``#pagebreak()`` we re-apply the body page setup so headers, margins,
    and page numbering take effect for the rest of the document.
    """
    body_setup = textwrap.dedent(r"""\
    // -- Body Page Setup -----------------------------------------------
    #set page(
      paper: "a4",
      margin: (top: 2.2cm, bottom: 2.2cm, left: 2.2cm, right: 2.2cm),
      header: context {
        if counter(page).get().first() > 1 [
          #set text(size: 9pt, fill: rgb("#666666"))
          VelaFlow Technical Reference
          #h(1fr)
          #counter(page).display()
        ]
      },
      footer: none,
      numbering: none,
    )
    #set text(size: 11pt, font: "New Computer Modern")
    #set par(justify: true, leading: 0.65em)
    #set heading(numbering: none)
    #let horizontalrule = line(length: 100%, stroke: 0.5pt + rgb("#cccccc"))
    #show heading.where(level: 1): it => {
      pagebreak(weak: true)
      v(1em)
      text(size: 18pt, weight: "bold", fill: rgb("#14365d"))[#it.body]
      v(0.5em)
    }
    #show heading.where(level: 2): it => {
      v(0.8em)
      text(size: 14pt, weight: "bold", fill: rgb("#1a365d"))[#it.body]
      v(0.3em)
    }
    #show heading.where(level: 3): it => {
      v(0.5em)
      text(size: 12pt, weight: "bold", fill: rgb("#2d3748"))[#it.body]
      v(0.2em)
    }
    // Clamp images (Mermaid PNGs) to page width, preserve aspect ratio.
    #show image: it => box(width: 100%, it)
    // Tables wrapped in `#figure(kind: table)` by pandoc must be breakable
    // so rows that overflow a page continue on the next page instead of
    // painting on top of the following block.
    #show figure.where(kind: table): it => {
      set block(breakable: true)
      it.body
    }
    #set table(
      inset: 6pt,
      stroke: 0.5pt + rgb("#cbd5e0"),
      fill: (col, row) => if row == 0 { rgb("#eef4fb") } else { none },
    )
    // Inline raw: zero-width space after break-friendly chars so long
    // tokens (e.g. HKDF-SHA256(...)) wrap inside narrow table cells.
    #show raw.where(block: false): it => {
      show regex("[_/(),=.|\-]"): c => c + "\u{200B}"
      set text(size: 9pt, font: ("DejaVu Sans Mono", "Cascadia Code", "Consolas", "Courier New"))
      it
    }
    #show raw.where(block: true): it => {
      set text(size: 8.5pt, font: ("DejaVu Sans Mono", "Cascadia Code", "Consolas", "Courier New"))
      block(
        width: 100%,
        breakable: true,
        fill: rgb("#f7f7f7"),
        stroke: 0.5pt + rgb("#e0e0e0"),
        inset: 8pt,
        radius: 3pt,
        it,
      )
    }

    """)
    return cover_code + "\n" + body_setup + "\n// -- Content -----------------------------------------------------\n\n" + body_code


# -- Build: Source Appendix PDF ---------------------------------------------
def build_appendix(tools: dict[str, str]) -> None:
    """Build the VelaFlow Source Appendix A4 PDF (complete file listing)."""
    print("\n=== Building Source Appendix PDF ===")

    BUILD_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Collect all project files
    all_files = _collect_project_files()
    print(f"  Collected {len(all_files)} files for appendix.")

    # 2. Build the markdown content
    md_lines: list[str] = []

    # Title and introduction
    md_lines.append("# VelaFlow â€” Complete Source Appendix\n")
    md_lines.append(f"*Generated: {date.today().isoformat()}*\n")
    md_lines.append(textwrap.dedent("""\
        This document contains the complete source code and configuration
        of the VelaFlow project. Every file is reproduced in full, organized
        by directory. Together with the VelaFlow Technical Reference, this
        appendix provides sufficient context for any engineer or AI system
        to fully understand, reproduce, or extend the project.

        **Security note:** Credential files (`config/.env`) are excluded.
        Only the template (`config/.env.example`) is included.
    """))

    # Directory tree
    md_lines.append("## Directory Tree\n")
    md_lines.append("```")
    md_lines.append(_build_directory_tree())
    md_lines.append("```\n")

    # File inventory table
    md_lines.append("## File Inventory\n")
    md_lines.append("| Path | Lines | Size |")
    md_lines.append("|------|------:|-----:|")
    for rel, full in all_files:
        try:
            lines = len(full.read_text(encoding="utf-8", errors="replace").splitlines())
            size = full.stat().st_size
            size_str = f"{size:,} B" if size < 10240 else f"{size // 1024} KB"
            md_lines.append(f"| `{rel}` | {lines} | {size_str} |")
        except Exception:
            md_lines.append(f"| `{rel}` | ? | ? |")
    md_lines.append("")

    # Full file contents
    md_lines.append("\\newpage\n")
    md_lines.append("## Complete File Contents\n")

    # Inject both READMEs FIRST inside Complete File Contents so the appendix
    # carries the exact document that ships at the repo root and in docs/.
    readmes = _read_readmes_for_pdf()
    if readmes:
        md_lines.append(readmes)
        md_lines.append("\\newpage\n")

    for rel, full in all_files:
        # Determine language for syntax highlighting
        lang = _lang_for_file(full)
        md_lines.append(f"### `{rel}`\n")

        try:
            text = full.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            md_lines.append(f"*Could not read file: {exc}*\n")
            continue

        # Security: redact any credential patterns
        text = _redact_credentials(text)

        # For very large files (JSON workflows), truncate with notice
        max_lines = 500
        file_lines = text.splitlines()
        if len(file_lines) > max_lines and full.suffix == ".json":
            text = "\n".join(file_lines[:max_lines])
            text += f"\n\n... [{len(file_lines) - max_lines} more lines truncated] ..."

        md_lines.append(f"```{lang}")
        md_lines.append(text)
        md_lines.append("```\n")

    content = "\n".join(md_lines)

    # 3. Write processed markdown
    appendix_md = BUILD_DIR / "appendix-processed.md"
    appendix_md.write_text(content, encoding="utf-8")

    # 4. Generate cover page
    cover_typ = BUILD_DIR / "appendix-cover.typ"
    today_str = date.today().strftime("%d %B %Y")
    cover_typ.write_text(
        _typst_cover_page(
            "VELAFLOW",
            "COMPLETE SOURCE APPENDIX",
            f"{today_str}  -  v1.0  -  {len(all_files)} files",
        ),
        encoding="utf-8",
    )

    # 5. Convert markdown to Typst
    intermediate_typ = BUILD_DIR / "appendix-content.typ"
    pandoc_cmd = [
        tools["pandoc"],
        str(appendix_md),
        "--from", "markdown-citations",
        "--to", "typst",
        "--toc", "--toc-depth=2",
        "-o", str(intermediate_typ),
    ]
    result = _run(pandoc_cmd)
    if result.returncode != 0:
        _die(f"Pandoc appendix failed: {result.stderr[:300]}")

    # 6. Assemble final Typst document
    final_typ = BUILD_DIR / "appendix-final.typ"
    cover_code = cover_typ.read_text(encoding="utf-8")
    body_code = intermediate_typ.read_text(encoding="utf-8")
    final_doc = _assemble_appendix_typst(cover_code, body_code)
    final_typ.write_text(final_doc, encoding="utf-8")

    # 7. Compile
    typst_cmd = [tools["typst"], "compile", "--root", str(PROJECT_ROOT), str(final_typ), str(APPENDIX_PDF)]
    result = _run(typst_cmd)
    if result.returncode != 0:
        _die(f"Typst appendix compilation failed: {result.stderr[:500]}")

    size_kb = APPENDIX_PDF.stat().st_size // 1024
    print(f"\n  Source Appendix: {APPENDIX_PDF} ({size_kb} KB)")


def _assemble_appendix_typst(cover_code: str, body_code: str) -> str:
    """Assemble the appendix Typst document.

    Same layout pattern as the Technical Reference: cover first (its own
    ``#set page(margin: 0pt)``), then body page setup for the listing.
    """
    body_setup = textwrap.dedent(r"""\
    #set page(
      paper: "a4",
      margin: (top: 2cm, bottom: 2cm, left: 2cm, right: 2cm),
      header: context {
        if counter(page).get().first() > 1 [
          #set text(size: 9pt, fill: rgb("#666666"))
          VelaFlow Source Appendix
          #h(1fr)
          #counter(page).display()
        ]
      },
      footer: none,
      numbering: none,
    )
    #set text(size: 10pt, font: "New Computer Modern")
    #set par(justify: true, leading: 0.55em)
    #let horizontalrule = line(length: 100%, stroke: 0.5pt + rgb("#cccccc"))
    #show heading.where(level: 1): it => {
      pagebreak(weak: true)
      v(1em)
      text(size: 16pt, weight: "bold", fill: rgb("#14365d"))[#it.body]
      v(0.5em)
    }
    #show heading.where(level: 2): it => {
      v(0.6em)
      text(size: 13pt, weight: "bold", fill: rgb("#1a365d"))[#it.body]
      v(0.3em)
    }
    #show heading.where(level: 3): it => {
      v(0.4em)
      text(size: 10pt, weight: "bold", fill: rgb("#2d3748"))[#it.body]
      v(0.2em)
    }
    #show image: it => box(width: 100%, it)
    #show figure.where(kind: table): it => {
      set block(breakable: true)
      it.body
    }
    #set table(
      inset: 5pt,
      stroke: 0.5pt + rgb("#cbd5e0"),
      fill: (col, row) => if row == 0 { rgb("#eef4fb") } else { none },
    )
    #show raw.where(block: false): it => {
      show regex("[_/(),=.|\-]"): c => c + "\u{200B}"
      set text(size: 8pt, font: ("DejaVu Sans Mono", "Cascadia Code", "Consolas", "Courier New"))
      it
    }
    #show raw.where(block: true): it => {
      set text(size: 7pt, font: ("DejaVu Sans Mono", "Cascadia Code", "Consolas", "Courier New"))
      block(
        width: 100%,
        breakable: true,
        fill: rgb("#f7f7f7"),
        stroke: 0.5pt + rgb("#e0e0e0"),
        inset: 6pt,
        radius: 2pt,
        it,
      )
    }

    """)
    return cover_code + "\n" + body_setup + "\n// -- Content -----------------------------------------------------\n\n" + body_code


def _collect_project_files() -> list[tuple[str, Path]]:
    """Collect all project files, sorted by path, excluding sensitive items."""
    files: list[tuple[str, Path]] = []

    for path in sorted(PROJECT_ROOT.rglob("*")):
        if not path.is_file():
            continue

        # Get relative path
        try:
            rel = path.relative_to(PROJECT_ROOT)
        except ValueError:
            continue

        rel_str = str(rel).replace("\\", "/")

        # Skip excluded directories
        parts = rel.parts
        if any(p in APPENDIX_EXCLUDE_DIRS for p in parts):
            continue

        # Skip excluded files
        if rel_str in APPENDIX_EXCLUDE_FILES:
            continue

        # Skip excluded extensions
        if path.suffix.lower() in APPENDIX_EXCLUDE_EXTENSIONS:
            continue

        # Skip binary files (try reading first few bytes)
        try:
            sample = path.read_bytes()[:512]
            if b"\x00" in sample:
                continue  # Likely binary
        except (OSError, PermissionError):
            continue

        files.append((rel_str, path))

    return files


def _build_directory_tree() -> str:
    """Build a text-based directory tree of the project."""
    lines = ["VelaFlow/"]
    _tree_recurse(PROJECT_ROOT, "", lines, depth=0)
    return "\n".join(lines)


def _tree_recurse(
    directory: Path, prefix: str, lines: list[str], depth: int
) -> None:
    """Recursively build directory tree lines."""
    if depth > 5:
        return

    entries = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    # Filter out excluded directories and files
    entries = [
        e for e in entries
        if e.name not in APPENDIX_EXCLUDE_DIRS
        and not (e.is_file() and e.suffix.lower() in APPENDIX_EXCLUDE_EXTENSIONS)
        and e.name != ".git"
    ]

    for i, entry in enumerate(entries):
        is_last = i == len(entries) - 1
        connector = "`-- " if is_last else "|-- "
        child_prefix = "    " if is_last else "|   "

        if entry.is_dir():
            lines.append(f"{prefix}{connector}{entry.name}/")
            _tree_recurse(entry, prefix + child_prefix, lines, depth + 1)
        else:
            lines.append(f"{prefix}{connector}{entry.name}")


def _lang_for_file(path: Path) -> str:
    """Determine syntax highlighting language from file extension."""
    ext_map = {
        ".py": "python", ".sh": "bash", ".md": "markdown",
        ".json": "json", ".yml": "yaml", ".yaml": "yaml",
        ".toml": "toml", ".ini": "ini", ".cfg": "ini",
        ".service": "ini", ".timer": "ini",
        ".ps1": "powershell", ".env": "bash",
        ".txt": "", ".gitignore": "bash",
    }
    return ext_map.get(path.suffix.lower(), "")


def _redact_credentials(text: str) -> str:
    """Replace any detected credential patterns with <REDACTED>."""
    for pattern in _CREDENTIAL_PATTERNS:
        text = pattern.sub("<REDACTED>", text)
    return text


# â”€â”€ Security Audit â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def _security_audit_markdown(md_path: Path) -> bool:
    """Scan a generated markdown file for leaked credentials."""
    content = md_path.read_text(encoding="utf-8", errors="replace")
    issues = []
    for pattern in _CREDENTIAL_PATTERNS:
        matches = pattern.findall(content)
        if matches:
            issues.extend(matches)

    if issues:
        print(f"  SECURITY WARNING: {len(issues)} potential credential(s) detected!")
        for m in issues[:5]:
            print(f"    - {m[:20]}...")
        return False
    return True


# â”€â”€ Main â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def main() -> None:
    parser = argparse.ArgumentParser(description="Build VelaFlow PDF documentation")
    parser.add_argument("--book-only", action="store_true", help="Build only the technical reference")
    parser.add_argument("--appendix-only", action="store_true", help="Build only the source appendix")
    parser.add_argument("--skip-mermaid", action="store_true", help="Skip Mermaid diagram rendering")
    args = parser.parse_args()

    print("VelaFlow PDF Builder")
    print(f"  Project root: {PROJECT_ROOT}")
    print(f"  Platform:     {platform.system()} {platform.machine()}")

    # Resolve tools
    tools = _resolve_tools()
    for name, path in tools.items():
        status = path if path else "NOT FOUND"
        print(f"  {name:10s} {status}")

    build_book_flag = not args.appendix_only
    build_appendix_flag = not args.book_only

    if build_book_flag:
        build_book(tools, skip_mermaid=args.skip_mermaid)

    if build_appendix_flag:
        build_appendix(tools)

    # Final security check on generated intermediates
    print("\n=== Security Audit ===")
    ok = True
    for md in BUILD_DIR.glob("*-processed.md"):
        if not _security_audit_markdown(md):
            ok = False
    if ok:
        print("  No credentials detected.  PDFs are safe for publication.")
    else:
        print("  WARNING: Review flagged items before publishing.")

    print("\n=== Done ===")
    if build_book_flag:
        print(f"  {BOOK_PDF.name}")
    if build_appendix_flag:
        print(f"  {APPENDIX_PDF.name}")


if __name__ == "__main__":
    main()
