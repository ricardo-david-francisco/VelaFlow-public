#!/usr/bin/env python3
"""Convert VS Code Copilot Chat JSON export to clean readable Markdown.

Extracts user messages and assistant responses from the VS Code Copilot
chat session JSON format (found in workspaceStorage debug-logs).

Usage:
    python scripts/chat_to_markdown.py <input.json> [output.md]
    python scripts/chat_to_markdown.py chat_session.json           # → chat_session.md
    python scripts/chat_to_markdown.py chat_session.json out.md    # → out.md

    # Convert all JSON files in a folder:
    for f in *.json; do python scripts/chat_to_markdown.py "$f"; done

The JSON format expected is the VS Code Copilot chat export with:
- .requests[] array
- .requests[].message.text  → user input
- .requests[].response[]    → assistant response entries
"""

from __future__ import annotations

import json
import re
import sys
import textwrap
from pathlib import Path


def extract_chat(data: dict) -> list[dict]:
    """Extract user/assistant message pairs from Copilot JSON.

    Returns a list of dicts: [{"user": "...", "assistant": "..."}, ...]
    """
    pairs = []
    requests = data.get("requests", [])

    for req in requests:
        # ── User message ──────────────────────────────────────────────
        user_text = ""
        msg = req.get("message", {})
        if isinstance(msg, dict):
            user_text = msg.get("text", "")
        elif isinstance(msg, str):
            user_text = msg

        # ── Assistant response ────────────────────────────────────────
        assistant_parts = []
        response = req.get("response", [])
        if isinstance(response, str):
            assistant_parts.append(response)
        elif isinstance(response, list):
            for entry in response:
                if not isinstance(entry, dict):
                    continue
                kind = entry.get("kind", "")

                # Direct text content
                if kind == "markdownContent":
                    content = entry.get("content", {})
                    if isinstance(content, dict):
                        assistant_parts.append(content.get("value", ""))
                    elif isinstance(content, str):
                        assistant_parts.append(content)

                # Plain text value
                elif kind == "textEditGroup":
                    for edit in entry.get("edits", []):
                        if isinstance(edit, dict):
                            assistant_parts.append(edit.get("text", ""))

                # Thinking blocks
                elif kind == "thinking":
                    value = entry.get("value", "")
                    if value and value.strip():
                        title = entry.get("generatedTitle", "Thinking")
                        assistant_parts.append(
                            f"<details><summary>{title}</summary>\n\n{value}\n</details>"
                        )

                # Tool invocations (summarised)
                elif kind == "toolInvocationSerialized":
                    tool_id = entry.get("toolId", "unknown")
                    inv_msg = entry.get("invocationMessage", "")
                    past_msg = entry.get("pastTenseMessage", "")

                    # Extract text from message objects
                    if isinstance(inv_msg, dict):
                        inv_msg = inv_msg.get("value", "")
                    if isinstance(past_msg, dict):
                        past_msg = past_msg.get("value", "")

                    # Skip empty tool calls
                    if not inv_msg and not past_msg:
                        continue

                    display = past_msg or inv_msg
                    # Clean up file URIs for readability
                    display = re.sub(
                        r"file:///c%3A/[^\s)]+",
                        lambda m: _decode_uri(m.group()),
                        display,
                    )
                    assistant_parts.append(f"> **[{tool_id}]** {display}")

                    # Include subagent results if present
                    tool_data = entry.get("toolSpecificData", {})
                    if isinstance(tool_data, dict):
                        result = tool_data.get("result", "")
                        if result and isinstance(result, str) and len(result) > 50:
                            # Truncate very long results
                            if len(result) > 2000:
                                result = result[:2000] + "\n\n*... (truncated)*"
                            assistant_parts.append(
                                f"```\n{result}\n```"
                            )

                # Content references
                elif kind == "contentReference":
                    ref = entry.get("reference", {})
                    if isinstance(ref, dict):
                        uri = ref.get("uri", "")
                        if uri:
                            assistant_parts.append(f"*Referenced: {_decode_uri(uri)}*")

        assistant_text = "\n\n".join(
            part for part in assistant_parts if part and part.strip()
        )

        if user_text or assistant_text:
            pairs.append({
                "user": user_text.strip(),
                "assistant": assistant_text.strip(),
            })

    return pairs


def _decode_uri(uri: str) -> str:
    """Decode a file:// URI to a readable path."""
    from urllib.parse import unquote
    path = unquote(uri)
    path = re.sub(r"^file:///", "", path)
    # Normalise forward slashes
    path = path.replace("/", "\\") if "\\" in path else path
    return path


def to_markdown(pairs: list[dict], title: str = "Chat History") -> str:
    """Convert message pairs to clean Markdown."""
    lines = [
        f"# {title}",
        "",
        f"*Extracted from VS Code Copilot Chat session*",
        "",
        "---",
        "",
    ]

    for i, pair in enumerate(pairs, 1):
        # User message
        lines.append(f"## Turn {i}")
        lines.append("")
        if pair["user"]:
            lines.append("### User")
            lines.append("")
            lines.append(pair["user"])
            lines.append("")

        # Assistant response
        if pair["assistant"]:
            lines.append("### Assistant")
            lines.append("")
            lines.append(pair["assistant"])
            lines.append("")

        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python chat_to_markdown.py <input.json> [output.md]")
        print()
        print("Converts VS Code Copilot Chat JSON to clean Markdown.")
        print()
        print("The JSON file is typically found at:")
        print("  %APPDATA%\\Code\\User\\workspaceStorage\\<id>\\GitHub.copilot-chat\\debug-logs\\")
        sys.exit(1)

    input_path = Path(sys.argv[1])
    if not input_path.exists():
        print(f"ERROR: File not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    output_path = (
        Path(sys.argv[2]) if len(sys.argv) > 2
        else input_path.with_suffix(".md")
    )

    # Snyk CWE-22 sanitizer: both paths come from argv. Route through
    # the project-wide allow-list so the dataflow sanitizer is visible
    # at every downstream sink (read_text / write_text).
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
        from brain.security.safe_path import default_bases, safe_resolve

        input_path = safe_resolve(
            input_path, allowed_bases=default_bases(), must_exist=True
        )
        output_path = safe_resolve(
            output_path, allowed_bases=default_bases(), create_parents=True
        )
    except Exception as e:  # noqa: BLE001 — CLI boundary
        print(f"ERROR: refusing unsafe path: {e}", file=sys.stderr)
        sys.exit(2)

    # Load JSON
    try:
        raw = input_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    # Extract and convert
    pairs = extract_chat(data)
    if not pairs:
        print("WARNING: No chat messages found in the JSON.", file=sys.stderr)
        sys.exit(0)

    title = data.get("responderUsername", "Chat History")
    md = to_markdown(pairs, title=f"{title} — Session Log")

    output_path.write_text(md, encoding="utf-8")
    print(f"Converted {len(pairs)} turns → {output_path}")
    print(f"Size: {len(md):,} characters")


if __name__ == "__main__":
    main()
