"""Notion + Todoist → NotebookLM automated sync module.

Exports the entire Notion 2nd-Brain workspace (root page + all subpages
+ database rows) AND all active Todoist tasks as pasted-text sources,
keeping a NotebookLM notebook up to date for AI-assisted querying.

Optional dependency — install once on the target machine:
    pip install "notebooklm-py[browser]"
    playwright install chromium

One-time authentication (interactive, run from the machine that will run
the scheduled sync):
    notebooklm login

After the first login, the library auto-refreshes CSRF tokens transparently.
Google session cookies typically last 2–4 weeks; when they expire, re-run
`notebooklm login`.  All subsequent scheduled syncs run unattended.

See docs/notebooklm-setup.md for the full step-by-step guide.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from brain.config import Settings
from brain.notion import NotionClient

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

_MAX_SOURCE_CHARS = 200_000   # well below NotebookLM's ~2 M char limit per source
_MAX_DEPTH = 4                # max recursion depth when following child pages
_MAX_DB_ROWS = 200            # cap database rows per source to avoid runaway sizes

# ── Block → Markdown conversion ────────────────────────────────────────────────


def _rt(rich_text: list[dict]) -> str:
    """Concatenate plain_text fields from a Notion rich_text array."""
    return "".join(seg.get("plain_text", "") for seg in (rich_text or []))


def _blocks_to_md(blocks: list[dict]) -> str:
    """Convert a flat list of Notion block objects to plain Markdown text.

    Only content blocks are rendered; child_page / child_database are handled
    by the recursive caller and intentionally skipped here.
    """
    lines: list[str] = []
    for blk in blocks:
        btype = blk.get("type", "")
        data = blk.get(btype, {})
        text = _rt(data.get("rich_text", []))

        if btype == "heading_1":
            lines.append(f"# {text}")
        elif btype == "heading_2":
            lines.append(f"## {text}")
        elif btype == "heading_3":
            lines.append(f"### {text}")
        elif btype == "paragraph":
            lines.append(text)
        elif btype == "bulleted_list_item":
            lines.append(f"- {text}")
        elif btype == "numbered_list_item":
            lines.append(f"1. {text}")
        elif btype == "quote":
            lines.append(f"> {text}")
        elif btype == "callout":
            lines.append(f"> {text}")
        elif btype == "toggle":
            lines.append(f"**{text}**")
        elif btype == "code":
            lang = data.get("language", "")
            lines.append(f"```{lang}\n{text}\n```")
        elif btype == "divider":
            lines.append("---")
        # Unsupported types (table, image, embed, etc.) are silently omitted.

    return "\n".join(lines)


def _db_entry_to_text(entry: dict) -> str:
    """Serialise a database row's properties to a compact key: value string."""
    parts: list[str] = []
    for name, prop in entry.get("properties", {}).items():
        ptype = prop.get("type", "")
        if ptype == "title":
            val = _rt(prop.get("title", []))
        elif ptype == "rich_text":
            val = _rt(prop.get("rich_text", []))
        elif ptype == "select":
            val = (prop.get("select") or {}).get("name", "")
        elif ptype == "multi_select":
            val = ", ".join(s.get("name", "") for s in prop.get("multi_select", []))
        elif ptype == "date":
            val = (prop.get("date") or {}).get("start", "")
        elif ptype in ("number", "checkbox"):
            val = str(prop.get(ptype, ""))
        elif ptype == "url":
            val = prop.get("url", "") or ""
        else:
            val = ""
        if val:
            parts.append(f"{name}: {val}")
    return " | ".join(parts)


# ── Notion workspace extraction ─────────────────────────────────────────────────


@dataclass
class _SourceBundle:
    """Content destined for one NotebookLM text source.

    Corresponds to one top-level child (page or database) of the root Notion
    page.  All recursive subpages are inlined into ``sections`` so the total
    source count stays small (typically 5–20).
    """

    title: str
    sections: list[str] = field(default_factory=list)

    def full_text(self) -> str:
        """Return the combined text, capped at _MAX_SOURCE_CHARS."""
        return "\n\n".join(self.sections)[:_MAX_SOURCE_CHARS]


def _extract_page_md(
    client: NotionClient,
    page_id: str,
    title: str,
    depth: int,
) -> str:
    """Recursively convert a Notion page and all its children to Markdown.

    Args:
        client:   An authenticated NotionClient instance.
        page_id:  Notion page or block UUID.
        title:    Display title (used as the heading).
        depth:    Current recursion depth (1 = top-level child of root).

    Returns:
        Markdown string for the page and all descendants.
    """
    if depth > _MAX_DEPTH:
        return f"{'#' * min(depth, 6)} {title}\n_(subpage skipped — max depth reached)_"

    try:
        blocks = client._paginate_blocks(page_id)
    except Exception as exc:
        logger.warning("Cannot fetch page %s (%s): %s", title, page_id[:8], exc)
        return f"{'#' * min(depth, 6)} {title}\n_(content unavailable)_"

    parts: list[str] = [f"{'#' * min(depth, 6)} {title}"]

    flat_md = _blocks_to_md(blocks)
    if flat_md.strip():
        parts.append(flat_md)

    for blk in blocks:
        btype = blk.get("type", "")
        if btype == "child_page":
            child_title = blk.get("child_page", {}).get("title", "Untitled")
            child_md = _extract_page_md(client, blk["id"], child_title, depth + 1)
            if child_md:
                parts.append(child_md)
        elif btype == "child_database":
            db_title = blk.get("child_database", {}).get("title", "Database")
            db_parts = [f"{'#' * min(depth + 1, 6)} {db_title} (database)"]
            try:
                rows = client._query_database(blk["id"])
                for row in rows[:_MAX_DB_ROWS]:
                    row_text = _db_entry_to_text(row)
                    if row_text:
                        db_parts.append(f"- {row_text}")
            except Exception as exc:
                logger.warning("Cannot query database %s: %s", db_title, exc)
                db_parts.append("_(database unavailable)_")
            parts.append("\n".join(db_parts))

    return "\n\n".join(parts)


def build_source_bundles(
    client: NotionClient,
    root_page_id: str,
) -> list[_SourceBundle]:
    """Return one _SourceBundle per direct child of the root Notion page.

    Groups content by top-level section so the NotebookLM source count stays
    low (one source per top-level page) while preserving full content depth.
    Top-level databases in the root are each their own source.

    Args:
        client:        An authenticated NotionClient instance.
        root_page_id:  UUID of the 2nd-Brain root page.

    Returns:
        List of _SourceBundle ready to be uploaded as NotebookLM sources.

    Raises:
        RuntimeError: If the root page cannot be read.
    """
    logger.info("Fetching root Notion page structure (%s)...", root_page_id[:8])
    try:
        root_blocks = client._paginate_blocks(root_page_id)
    except Exception as exc:
        raise RuntimeError(
            f"Cannot read root Notion page {root_page_id}: {exc}"
        ) from exc

    bundles: list[_SourceBundle] = []
    for blk in root_blocks:
        btype = blk.get("type", "")
        if btype == "child_page":
            title = blk.get("child_page", {}).get("title", "Untitled")
            logger.info("  Extracting page: %s", title)
            md = _extract_page_md(client, blk["id"], title, depth=1)
            bundle = _SourceBundle(title=title)
            bundle.sections.append(md)
            bundles.append(bundle)
        elif btype == "child_database":
            title = blk.get("child_database", {}).get("title", "Database")
            logger.info("  Extracting database: %s", title)
            parts = [f"# {title} (database)"]
            try:
                rows = client._query_database(blk["id"])
                for row in rows[:_MAX_DB_ROWS]:
                    row_text = _db_entry_to_text(row)
                    if row_text:
                        parts.append(f"- {row_text}")
            except Exception as exc:
                logger.warning("Cannot query database %s: %s", title, exc)
            bundle = _SourceBundle(title=title)
            bundle.sections.append("\n".join(parts))
            bundles.append(bundle)

    logger.info("Extracted %d top-level sections from Notion.", len(bundles))
    return bundles


# ── Todoist workspace extraction ───────────────────────────────────────────────

_PRIORITY_LABEL = {4: "P1", 3: "P2", 2: "P3", 1: ""}


def build_todoist_bundle(settings: Settings) -> "_SourceBundle | None":
    """Return a single _SourceBundle with all active Todoist tasks.

    Tasks are grouped by project then by section, sorted by priority then
    due date.  Returns None if TODOIST_API_TOKEN is not configured or the
    API call fails.
    """
    if not settings.todoist_api_token:
        logger.info("TODOIST_API_TOKEN not set — skipping Todoist source.")
        return None

    from brain.todoist import TodoistClient  # local import avoids hard dependency

    client = TodoistClient(settings)
    logger.info("Fetching active Todoist tasks...")
    try:
        tasks = client.get_tasks()
    except Exception as exc:
        logger.warning("Could not fetch Todoist tasks: %s", exc)
        return None

    if not tasks:
        logger.info("No active Todoist tasks found.")
        return None

    logger.info("  Fetched %d active Todoist tasks.", len(tasks))

    # Group: project → section → [Task]
    grouped: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for t in tasks:
        pname = t.project_name or "Inbox"
        sname = t.section_name or ""
        grouped[pname][sname].append(t)

    lines: list[str] = ["# Todoist Active Tasks", ""]
    for pname in sorted(grouped):
        lines.append(f"## {pname}")
        sections = grouped[pname]
        for sname in sorted(sections):
            if sname:
                lines.append(f"### {sname}")
            sorted_tasks = sorted(
                sections[sname],
                key=lambda x: (-x.priority, str(x.due_date or "9999")),
            )
            for t in sorted_tasks:
                meta: list[str] = []
                plabel = _PRIORITY_LABEL.get(t.priority, "")
                if plabel:
                    meta.append(plabel)
                if t.due_date:
                    meta.append(f"due:{t.due_date}")
                if t.labels:
                    meta.append(f"labels:{','.join(t.labels)}")
                if t.is_recurring:
                    meta.append("recurring")
                suffix = f" ({' '.join(meta)})" if meta else ""
                lines.append(f"- {t.content}{suffix}")
                if t.description:
                    lines.append(f"  {t.description}")
        lines.append("")

    bundle = _SourceBundle(title="Todoist Active Tasks")
    bundle.sections.append("\n".join(lines))
    return bundle


# ── NotebookLM async sync ───────────────────────────────────────────────────────


async def _async_sync(settings: Settings, rebuild: bool) -> dict[str, Any]:
    """Async implementation — do not call directly; use sync_notion_to_notebooklm."""
    try:
        from notebooklm import NotebookLMClient, RPCError  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError(
            "notebooklm-py is not installed.\n"
            "Install it with:\n"
            "    pip install 'notebooklm-py[browser]'\n"
            "    playwright install chromium\n"
            "Then authenticate once:\n"
            "    notebooklm login"
        ) from exc

    if not settings.notion_root_page_id:
        raise ValueError("NOTION_ROOT_PAGE_ID is not configured in config/.env.")

    # ── Step 1: extract all Notion content ───────────────────────────────────
    notion = NotionClient(settings)
    bundles = build_source_bundles(notion, settings.notion_root_page_id)
    if not bundles:
        logger.warning("No content found in Notion workspace — nothing to sync.")
        return {"added": 0, "deleted": 0, "notebook_id": ""}

    # ── Step 1b: extract Todoist tasks ────────────────────────────────────────
    todoist_bundle = build_todoist_bundle(settings)
    if todoist_bundle:
        bundles.append(todoist_bundle)

    # ── Step 2: connect to NotebookLM ─────────────────────────────────────────
    async with await NotebookLMClient.from_storage() as client:

        nb_id = settings.notebooklm_notebook_id
        if not nb_id:
            nb_name = settings.notebooklm_notebook_name or "VelaFlow"
            logger.info("Creating NotebookLM notebook: %s", nb_name)
            try:
                nb = await client.notebooks.create(nb_name)
                nb_id = nb.id
                logger.info(
                    "Notebook created. Add this to config/.env:\n  NOTEBOOKLM_NOTEBOOK_ID=%s",
                    nb_id,
                )
            except RPCError as exc:
                raise RuntimeError(f"Failed to create notebook '{nb_name}': {exc}") from exc
        else:
            logger.info("Using configured notebook: %s", nb_id)

        # ── Step 3: optional rebuild — remove all existing sources ──────────
        deleted = 0
        if rebuild:
            logger.info("Rebuild mode: removing all existing sources...")
            try:
                existing = await client.sources.list(nb_id)
                for src in existing:
                    try:
                        await client.sources.delete(nb_id, src.id)
                        deleted += 1
                    except RPCError as exc:
                        logger.warning("Could not delete source %s: %s", src.id[:8], exc)
                logger.info("Deleted %d source(s).", deleted)
            except RPCError as exc:
                logger.warning("Could not list existing sources: %s", exc)

        # ── Step 4: add fresh sources ──────────────────────────────────────────
        added = 0
        for bundle in bundles:
            text = bundle.full_text()
            if not text.strip():
                continue
            try:
                await client.sources.add_text(nb_id, bundle.title, text)
                added += 1
                logger.info(
                    "  + %-45s (%d chars)",
                    bundle.title,
                    len(text),
                )
            except RPCError as exc:
                logger.error("  ! Failed to add '%s': %s", bundle.title, exc)

    result: dict[str, Any] = {
        "added": added,
        "deleted": deleted,
        "notebook_id": nb_id,
    }
    logger.info(
        "NotebookLM sync complete — %d sources added, %d deleted. Notebook: %s",
        added,
        deleted,
        nb_id,
    )
    return result


def sync_notion_to_notebooklm(settings: Settings, rebuild: bool = True) -> dict[str, Any]:
    """Synchronously pull all Notion content and push it to a NotebookLM notebook.

    This is the main entry point called by the CLI command
    ``brain notebooklm-sync``.

    Extraction strategy: one NotebookLM source per top-level child of the
    2nd-Brain root page (child pages + databases).  Subpages are inlined
    into their parent source up to _MAX_DEPTH levels deep.  This keeps the
    source count small (typically 5–20) and each source semantically coherent.

    Args:
        settings: Populated Settings dataclass (loaded via Settings.from_env()).
        rebuild:  When True (default), all existing pasted-text sources are
                  deleted before re-adding.  Ensures stale content is purged.
                  Set False to append sources without touching existing ones.

    Returns:
        Dict with integer keys ``added``, ``deleted``, and string ``notebook_id``.

    Raises:
        RuntimeError: notebooklm-py not installed, or Google auth has expired.
        ValueError:   Required settings (NOTION_ROOT_PAGE_ID) are missing.
    """
    return asyncio.run(_async_sync(settings, rebuild=rebuild))
