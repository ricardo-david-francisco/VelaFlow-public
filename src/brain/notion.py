"""Notion API client — dashboard setup and two-way sync with Todoist.

Responsibilities:
- Create and maintain the 2nd-Brain dashboard structure inside Notion
- Sync tasks between Todoist planner sections and Notion databases
- Update the Command Center with AI-generated briefings
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

import requests

from brain.config import Settings
from brain.models import Task

logger = logging.getLogger(__name__)

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# ── Input sanitisation ───────────────────────────────────────────────────────────
# Strip C0/C1 control characters (null bytes, ESC, BEL, etc.) but keep \n, \r, \t.
_CTRL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x80-\x9f]")


def _sanitize_text(text: str, max_len: int = 2000) -> str:
    """Remove control characters and enforce a length limit.

    Prevents null-byte injection and oversized payloads being sent to Notion.
    Newlines and tabs are intentionally preserved.
    """
    if not isinstance(text, str):
        text = str(text)
    return _CTRL_CHARS_RE.sub("", text)[:max_len]


# ── Section colour mapping (keyword-based, case-insensitive) ──────────────────────
# Keys are lowercase substrings matched against section names at sync time.
# No fixed IDs — purely name-based so it works even if the user renames sections.
_SECTION_COLOR_MAP: dict[str, str] = {
    "urgent": "red",
    "high": "orange",
    "normal": "yellow",
    "low": "default",
    "backlog": "gray",
    "daily": "blue",
    "weekly": "purple",
    "weekend": "green",
    "doing": "pink",
    "done": "green",
    "blocked": "red",
    "recurring": "brown",
    "rejected": "gray",
}


def _section_color(name: str) -> str:
    """Return a Notion select colour for a section name (keyword match)."""
    lower = name.lower()
    for keyword, color in _SECTION_COLOR_MAP.items():
        if keyword in lower:
            return color
    return "default"

_PRIORITY_OPTIONS = [
    {"name": "🔴 P1 Urgent", "color": "red"},
    {"name": "🟠 P2 High", "color": "orange"},
    {"name": "🟡 P3 Normal", "color": "yellow"},
    {"name": "⚪ P4 Low", "color": "default"},
]
_PRIORITY_MAP = {4: "🔴 P1 Urgent", 3: "🟠 P2 High", 2: "🟡 P3 Normal", 1: "⚪ P4 Low"}

_STATUS_OPTIONS = [
    {"name": "📋 Todo", "color": "gray"},
    {"name": "⚡ Doing", "color": "blue"},
    {"name": "✅ Done", "color": "green"},
    {"name": "🚧 Blocked", "color": "red"},
]

_LABEL_COLORS = [
    "blue", "brown", "default", "gray", "green", "orange",
    "pink", "purple", "red", "yellow",
]


def _rich_text(content: str) -> list[dict]:
    """Build a Notion rich_text array from a plain string."""
    return [{"type": "text", "text": {"content": content}}]


def _heading(level: int, text: str, emoji: str = "") -> dict:
    """Build a heading block."""
    heading_type = f"heading_{level}"
    prefix = f"{emoji} " if emoji else ""
    return {
        "object": "block",
        "type": heading_type,
        heading_type: {"rich_text": _rich_text(f"{prefix}{text}")},
    }


def _paragraph(text: str) -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": _rich_text(text)},
    }


def _callout(text: str, emoji: str = "💡", color: str = "blue_background") -> dict:
    return {
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": _rich_text(text),
            "icon": {"type": "emoji", "emoji": emoji},
            "color": color,
        },
    }


def _divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def _toggle(title: str, children: list[dict], emoji: str = "") -> dict:
    prefix = f"{emoji} " if emoji else ""
    return {
        "object": "block",
        "type": "toggle",
        "toggle": {
            "rich_text": _rich_text(f"{prefix}{title}"),
            "children": children,
        },
    }


def _bullet(text: str) -> dict:
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": _rich_text(text)},
    }


def _quote(text: str) -> dict:
    return {
        "object": "block",
        "type": "quote",
        "quote": {"rich_text": _rich_text(text)},
    }


def _table_of_contents() -> dict:
    return {"object": "block", "type": "table_of_contents", "table_of_contents": {"color": "default"}}


def _code(text: str, language: str = "shell") -> dict:
    """Inline code block (monospaced, syntax-highlighted)."""
    return {
        "object": "block",
        "type": "code",
        "code": {
            "rich_text": _rich_text(text),
            "language": language,
        },
    }


def _numbered(text: str) -> dict:
    return {
        "object": "block",
        "type": "numbered_list_item",
        "numbered_list_item": {"rich_text": _rich_text(text)},
    }


def _columns(*col_lists: list[dict]) -> dict:
    """Create a multi-column layout (2-3 columns).  Each arg is a list of blocks."""
    return {
        "object": "block",
        "type": "column_list",
        "column_list": {
            "children": [
                {"type": "column", "column": {"children": col}}
                for col in col_lists
            ]
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# NotionClient
# ─────────────────────────────────────────────────────────────────────────────

class NotionClient:
    """Notion API v1 client — dashboard management and Todoist sync."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._token = settings.notion_api_token
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {self._token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            }
        )
        # SSL certificate verification MUST remain True.
        # Never set verify=False — Notion API tokens would be exposed to MITM attacks.
        self._session.verify = True
        self._timeout = 30

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self._session.close()

    def __enter__(self) -> NotionClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── HTTP helpers ─────────────────────────────────────────────────────────

    def _get(self, path: str, params: dict | None = None) -> Any:
        resp = self._session.get(
            f"{NOTION_API_BASE}{path}", params=params, timeout=self._timeout
        )
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: dict) -> Any:
        resp = self._session.post(
            f"{NOTION_API_BASE}{path}", json=body, timeout=self._timeout
        )
        resp.raise_for_status()
        return resp.json()

    def _patch(self, path: str, body: dict) -> Any:
        resp = self._session.patch(
            f"{NOTION_API_BASE}{path}", json=body, timeout=self._timeout
        )
        resp.raise_for_status()
        return resp.json()

    def _paginate_blocks(self, block_id: str) -> list[dict]:
        """Fetch all child blocks of a block/page."""
        results = []
        params: dict[str, Any] = {"page_size": 100}
        while True:
            data = self._get(f"/blocks/{block_id}/children", params=params)
            results.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            params["start_cursor"] = data["next_cursor"]
        return results

    def _query_database(self, db_id: str, filter_body: dict | None = None) -> list[dict]:
        """Query all pages from a database."""
        results = []
        body: dict[str, Any] = {"page_size": 100}
        if filter_body:
            body["filter"] = filter_body
        while True:
            data = self._post(f"/databases/{db_id}/query", body)
            results.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            body["start_cursor"] = data["next_cursor"]
        return results

    # ── Page / Block creation ─────────────────────────────────────────────────

    def create_page(self, parent_page_id: str, title: str, emoji: str = "") -> dict:
        """Create a new child page under parent_page_id."""
        body: dict[str, Any] = {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "properties": {
                "title": {"title": _rich_text(title)},
            },
        }
        if emoji:
            body["icon"] = {"type": "emoji", "emoji": emoji}
        return self._post("/pages", body)

    def append_blocks(self, block_id: str, blocks: list[dict]) -> dict:
        """Append blocks as children of a page or block."""
        # Notion API accepts max 100 blocks per request
        for i in range(0, len(blocks), 100):
            self._patch(
                f"/blocks/{block_id}/children",
                {"children": blocks[i : i + 100]},
            )
        return {}

    def create_database(
        self,
        parent_page_id: str,
        title: str,
        properties: dict,
        emoji: str = "",
        is_inline: bool = True,
    ) -> dict:
        """Create a Notion database inside a page."""
        body: dict[str, Any] = {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": _rich_text(title),
            "is_inline": is_inline,
            "properties": properties,
        }
        if emoji:
            body["icon"] = {"type": "emoji", "emoji": emoji}
        return self._post("/databases", body)

    def update_page(self, page_id: str, properties: dict) -> dict:
        """Update properties on an existing Notion page/database item."""
        return self._patch(f"/pages/{page_id}", {"properties": properties})

    def archive_blocks(self, block_id: str) -> None:
        """Delete (archive) all non-page child blocks of a page to allow re-writing.

        Child pages and inline databases are skipped — only content blocks
        (callouts, headings, paragraphs, dividers, etc.) are removed.
        """
        blocks = self._paginate_blocks(block_id)
        # Block types that must NOT be archived via the blocks API
        _SKIP_TYPES = {"child_page", "child_database"}
        for blk in blocks:
            if blk.get("type") in _SKIP_TYPES:
                continue
            try:
                self._patch(f"/blocks/{blk['id']}", {"archived": True})
            except requests.RequestException as exc:
                logger.warning("Could not archive block %s (%s): %s", blk['id'][:8], blk.get("type"), exc)

    # ── Database schema ───────────────────────────────────────────────────────

    @staticmethod
    def _task_db_schema(extra_labels: list[str] | None = None) -> dict:
        """Return the standard Notion database schema for task sync."""
        labels = list(set(extra_labels or []))
        label_options = [
            {"name": lbl, "color": _LABEL_COLORS[i % len(_LABEL_COLORS)]}
            for i, lbl in enumerate(labels)
        ]
        return {
            "Name": {"title": {}},
            "Priority": {"select": {"options": _PRIORITY_OPTIONS}},
            "Due Date": {"date": {}},
            "Labels": {
                "multi_select": {
                    "options": label_options or [
                        {"name": "Saúde", "color": "green"},
                        {"name": "Tecnologia", "color": "blue"},
                        {"name": "Finanças", "color": "yellow"},
                        {"name": "Burocracia", "color": "gray"},
                        {"name": "Manutenção", "color": "orange"},
                        {"name": "Lojas", "color": "pink"},
                        {"name": "Família", "color": "purple"},
                        {"name": "Trabalho", "color": "red"},
                        {"name": "Entretenimento", "color": "brown"},
                        {"name": "decoração", "color": "default"},
                    ]
                }
            },
            "Status": {"select": {"options": _STATUS_OPTIONS}},
            "Todoist ID": {"rich_text": {}},
            "Todoist URL": {"url": {}},
            "Duration (min)": {"number": {}},
            "Notes": {"rich_text": {}},
        }

    # ── Task → Notion item ────────────────────────────────────────────────────

    @staticmethod
    def _task_to_properties(task: Task, *, include_section: bool = False) -> dict:
        """Convert a Task model into Notion page properties.

        All user-supplied strings are sanitised before being sent to the API.
        include_section=True adds a 'Section' select property (used for board DB).
        """
        props: dict[str, Any] = {
            "Name": {"title": _rich_text(_sanitize_text(task.content, 2000))},
            "Priority": {"select": {"name": _PRIORITY_MAP.get(task.priority, "⚪ P4 Low")}},
            "Status": {"select": {"name": "📋 Todo"}},
            "Todoist ID": {"rich_text": _rich_text(_sanitize_text(task.id, 100))},
            "Todoist URL": {"url": task.url or None},
        }
        if task.due_date:
            if task.due_datetime and task.due_has_time:
                props["Due Date"] = {
                    "date": {"start": task.due_datetime.isoformat()}
                }
            else:
                props["Due Date"] = {"date": {"start": task.due_date.isoformat()}}
        if task.labels:
            props["Labels"] = {
                "multi_select": [{"name": _sanitize_text(lbl, 100)} for lbl in task.labels]
            }
        if task.duration_minutes:
            props["Duration (min)"] = {"number": task.duration_minutes}
        if task.description:
            props["Notes"] = {"rich_text": _rich_text(_sanitize_text(task.description, 2000))}
        if include_section:
            section_name = _sanitize_text(task.section_name or "No Section", 100)
            props["Section"] = {"select": {"name": section_name}}
        return props

    @staticmethod
    def _board_db_schema() -> dict:
        """Schema for the full board database.

        Identical to _task_db_schema but with an added 'Section' select property
        (options are populated/updated dynamically from Todoist at every sync).
        No hardcoded section names — the schema is intentionally empty here.
        """
        schema = NotionClient._task_db_schema()
        schema["Section"] = {"select": {"options": []}}
        return schema

    # ── Dashboard structure builder ───────────────────────────────────────────

    # ── Dashboard root page ──────────────────────────────────────────────────

    def _populate_root_page(self, page_id: str) -> None:
        """Fill the 2nd-Brain root page with a rich landing layout."""
        blocks = [
            _callout(
                "VelaFlow — Self-hosted productivity automation system.\n"
                "Todoist is your task engine. Notion is your control panel. "
                "Gemini AI runs every morning to plan your day, week, and weekend automatically.",
                emoji="🧠",
                color="purple_background",
            ),
            _divider(),
            _heading(2, "Navigation", "🗺️"),
            _columns(
                [
                    _callout(
                        "🧠 Command Center\nSystem status, CLI reference, and how-it-works guide.",
                        emoji="⚙️", color="gray_background",
                    ),
                    _callout(
                        "📅 Daily Planner\nAI picks your top tasks every morning at 7:00.",
                        emoji="☀️", color="blue_background",
                    ),
                    _callout(
                        "📆 Weekly Planner\nThis week's priority stack. Sunday review trigger.",
                        emoji="📌", color="yellow_background",
                    ),
                ],
                [
                    _callout(
                        "🏖️ Weekend Planner\nCapacity-aware Saturday/Sunday plan. Max 3h.",
                        emoji="🌴", color="green_background",
                    ),
                    _callout(
                        "📊 Task Board\nFull Todoist board mirrored here. Read-only overview.",
                        emoji="📋", color="orange_background",
                    ),
                    _callout(
                        "📝 Blog & Notes\nBraindumps, articles, ideas, and reference notes.",
                        emoji="✍️", color="pink_background",
                    ),
                ],
            ),
            _divider(),
            _heading(2, "Quick Start", "🚀"),
            _columns(
                [
                    _heading(3, "Daily Workflow", "☀️"),
                    _numbered("AI runs 'brain daily' at 07:00 automatically."),
                    _numbered("Top tasks land in Daily Planner → Notion DB."),
                    _numbered("Work through tasks — check off in Todoist or Notion."),
                    _numbered("Run 'brain notion-sync' if you added tasks in Notion."),
                ],
                [
                    _heading(3, "Weekly Workflow", "📆"),
                    _numbered("Run 'brain weekly' Sunday evening for a review."),
                    _numbered("AI scores and loads next week's top tasks."),
                    _numbered("Weekend plan builds on Friday via 'brain weekend'."),
                    _numbered("Check architecture docs if you need to extend the system."),
                ],
            ),
            _divider(),
            _heading(2, "CLI Cheat Sheet", "💻"),
            _code(
                "brain daily              # morning briefing + Daily Planner sync\n"
                "brain weekly             # weekly review + Weekly Planner sync\n"
                "brain weekend            # weekend plan + Weekend Planner sync\n"
                "brain analyze            # AI analysis of your Todoist board\n"
                "brain organize --apply   # reorganise tasks by priority\n"
                "brain notion-sync        # two-way sync Notion ↔ Todoist\n"
                "brain notion-rebuild     # rebuild root page + Command Center layout\n"
                "brain alerts             # check overdue tasks → WhatsApp",
                language="shell",
            ),
            _divider(),
            _heading(2, "Productivity Ground Rules", "target"),
            _callout(
                "3-Task Rule: pick only 3 non-negotiable tasks per day. Everything else is bonus.\n"
                "2-Week Rule: if a task has been in Backlog for 14+ days, schedule it or delete it.\n"
                "Blocked Rule: add 'blocked' label in Todoist and the AI will deprioritise it automatically.",
                emoji="💡",
                color="green_background",
            ),
        ]
        self.append_blocks(page_id, blocks)

    def setup_dashboard(self, root_page_id: str) -> dict[str, str]:
        """Build the complete 2nd-Brain dashboard inside root_page_id.

        Creates sub-pages and databases. Returns a dict of IDs:
        {
          'command_center_id': ...,
          'daily_planner_page_id': ...,
          'daily_planner_db_id': ...,
          'weekly_planner_page_id': ...,
          'weekly_planner_db_id': ...,
          'weekend_planner_page_id': ...,
          'weekend_planner_db_id': ...,
          'board_page_id': ...,
          'board_db_id': ...,
          'blog_page_id': ...,
        }
        """
        ids: dict[str, str] = {}
        logger.info("Creating 2nd-Brain dashboard structure...")

        # ── Root page ─────────────────────────────────────────────────────────
        logger.info("  Populating root 2nd-Brain page...")
        self._populate_root_page(root_page_id)

        # ── Command Center ────────────────────────────────────────────────────
        logger.info("  Creating Command Center page...")
        cc = self.create_page(root_page_id, "🧠 Command Center", "🧠")
        cc_id = cc["id"]
        ids["command_center_id"] = cc_id
        self._populate_command_center(cc_id)

        # ── Daily Planner ─────────────────────────────────────────────────────
        logger.info("  Creating Daily Planner page...")
        dp = self.create_page(root_page_id, "📅 Daily Planner", "📅")
        dp_id = dp["id"]
        ids["daily_planner_page_id"] = dp_id
        dp_db = self._create_planner_page(
            dp_id,
            title="Daily Tasks",
            description=(
                "AI-managed daily plan. Every weekday at 07:00 the 'brain daily' command scores "
                "all your Todoist tasks and moves the top picks into this section. "
                "Edit freely here — changes sync back to Todoist automatically."
            ),
            emoji="📅",
            instructions=[
                "Cron runs 'brain daily' on the LXC at 07:00 (Mon–Fri).",
                "AI scores tasks by urgency, priority, and due date.",
                "Top tasks land in the Todoist 'Daily Planner' section.",
                "'brain notion-sync' pushes them into this Notion database.",
            ],
            extra_tip=(
                "3-Task Rule: identify your 3 non-negotiable tasks and mark them P1. "
                "Complete those first before anything else."
            ),
        )
        ids["daily_planner_db_id"] = dp_db["id"]

        # ── Weekly Planner ────────────────────────────────────────────────────
        logger.info("  Creating Weekly Planner page...")
        wp = self.create_page(root_page_id, "📆 Weekly Planner", "📆")
        wp_id = wp["id"]
        ids["weekly_planner_page_id"] = wp_id
        wp_db = self._create_planner_page(
            wp_id,
            title="Weekly Tasks",
            description=(
                "AI-managed weekly plan. Run 'brain weekly' on Sunday evenings to score and "
                "load next week's top tasks. Synced with the Todoist 'Weekly Planner' section."
            ),
            emoji="📆",
            instructions=[
                "Run 'brain weekly' on Sunday evening (or cron at 20:00).",
                "AI scores and prioritises tasks across the full Todoist board.",
                "Top tasks are moved to the Todoist 'Weekly Planner' section.",
                "'brain notion-sync' pushes them into this Notion database.",
            ],
            extra_tip=(
                "Review the weekly plan each Monday morning. "
                "Adjust priorities if anything changed over the weekend."
            ),
        )
        ids["weekly_planner_db_id"] = wp_db["id"]

        # ── Weekend Planner ───────────────────────────────────────────────────
        logger.info("  Creating Weekend Planner page...")
        wkp = self.create_page(root_page_id, "🏖️ Weekend Planner", "🏖️")
        wkp_id = wkp["id"]
        ids["weekend_planner_page_id"] = wkp_id
        wkp_db = self._create_planner_page(
            wkp_id,
            title="Weekend Tasks",
            description=(
                f"AI-curated weekend plan. Hard-capped at {self._settings.weekend_capacity_hours}h total. "
                "Run 'brain weekend' on Friday evening to generate a realistic Saturday/Sunday plan."
            ),
            emoji="🏖️",
            instructions=[
                "Cron runs 'brain weekend' on Friday at 18:00.",
                f"AI selects tasks that fit within the {self._settings.weekend_capacity_hours}h capacity budget.",
                "Tasks land in the Todoist 'Weekend Planner' section.",
                "'brain notion-sync' pushes them into this Notion database.",
            ],
            extra_tip=(
                "Protect family time. The AI respects the weekend capacity limit — "
                "don't add more tasks manually unless you adjust the budget."
            ),
        )
        ids["weekend_planner_db_id"] = wkp_db["id"]

        # ── Full Board (Kanban overview) ───────────────────────────────────────
        logger.info("  Creating Board Overview page...")
        bp = self.create_page(root_page_id, "📊 Task Board", "📊")
        bp_id = bp["id"]
        ids["board_page_id"] = bp_id
        bp_db = self._create_board_page(bp_id)
        ids["board_db_id"] = bp_db["id"]

        # ── Blog & Notes ──────────────────────────────────────────────────────
        logger.info("  Creating Blog & Notes page...")
        blog = self.create_page(root_page_id, "📝 Blog & Notes", "📝")
        blog_id = blog["id"]
        ids["blog_page_id"] = blog_id
        self._populate_blog_page(blog_id)

        logger.info("Dashboard setup complete. IDs: %s", ids)
        return ids

    def _populate_command_center(self, page_id: str) -> None:
        """Fill the Command Center page with its initial layout."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        blocks = [
            # ── Hero ─────────────────────────────────────────────────────────
            _callout(
                "VelaFlow — Self-hosted productivity automation system.\n"
                "This page is your system dashboard. All critical operations, status, "
                "and configuration live here. Sub-pages are managed automatically.",
                emoji="🧠",
                color="purple_background",
            ),
            _divider(),
            # ── Status ───────────────────────────────────────────────────────
            _heading(2, "System Status", "🟢"),
            _columns(
                [
                    _callout(
                        f"Last synced: {now}\nStatus: ✅ Online",
                        emoji="🕐", color="yellow_background",
                    ),
                    _callout(
                        "AI Engine: Gemini 2.5 Pro\nFallback: Flash → Flash-Lite → Groq",
                        emoji="🤖", color="blue_background",
                    ),
                ],
                [
                    _callout(
                        "Task Source: Todoist\nSync: Two-way (Notion ↔ Todoist)",
                        emoji="✅", color="green_background",
                    ),
                    _callout(
                        "Notifications: Gmail digest + WhatsApp\nSchedule: Cron on Proxmox LXC",
                        emoji="📨", color="gray_background",
                    ),
                ],
            ),
            _divider(),
            # ── How it works ─────────────────────────────────────────────────
            _heading(2, "How It Works", "🔄"),
            _columns(
                [
                    _heading(3, "Morning Flow (07:00)", "☀️"),
                    _numbered("Cron triggers 'brain daily' on the LXC container."),
                    _numbered("AI reads all Todoist tasks and scores by urgency + priority."),
                    _numbered("Top tasks are moved into the 'Daily Planner' Todoist section."),
                    _numbered("brain notion-sync pushes those tasks to the Notion Daily DB."),
                    _numbered("Email digest + WhatsApp message sent with the day plan."),
                ],
                [
                    _heading(3, "Sync Architecture", "🔁"),
                    _numbered("Todoist is the source of truth for task data."),
                    _numbered("Notion databases mirror planner sections via Todoist IDs."),
                    _numbered("Tasks added in Notion (no Todoist ID) sync back to Todoist."),
                    _numbered("Run 'brain notion-sync' at any time to force a sync."),
                    _numbered("Kanban board sections control AI prioritisation logic."),
                ],
            ),
            _divider(),
            # ── CLI reference ─────────────────────────────────────────────────
            _heading(2, "CLI Reference", "💻"),
            _code(
                "# Planning commands\n"
                "brain daily              # morning briefing + Daily Planner sync\n"
                "brain weekly             # weekly review + Weekly Planner sync\n"
                "brain weekend            # weekend plan + Weekend Planner sync\n\n"
                "# Board management\n"
                "brain analyze            # AI deep-analysis of your Todoist board\n"
                "brain organize --apply   # reorganise tasks by priority (live)\n"
                "brain organize           # dry-run (show what would change)\n\n"
                "# Notion\n"
                "brain notion-sync        # two-way sync Notion ↔ Todoist\n"
                "brain notion-sync --full # also sync full board DB\n"
                "brain notion-rebuild     # rebuild root page + Command Center content\n"
                "brain notion-setup       # first-time setup (creates pages + Todoist sections)\n\n"
                "# Alerts\n"
                "brain alerts             # check overdue tasks → WhatsApp",
                language="shell",
            ),
            _divider(),
            # ── Configuration ─────────────────────────────────────────────────
            _heading(2, "Key Configuration", "⚙️"),
            _toggle(
                "Environment Variables (config/.env)",
                [
                    _code(
                        "TODOIST_API_TOKEN=<your-token>\n"
                        "GOOGLE_AI_API_KEY=<gemini-key>\n"
                        "NOTION_API_TOKEN=<integration-token>\n"
                        "NOTION_ROOT_PAGE_ID=<2nd-brain-page-id>\n\n"
                        "# ── Demo / Zero-Trust Proxy (optional) ──\n"
                        "LITELLM_PROXY_URL=https://your-proxy.example.com\n"
                        "LITELLM_PROXY_TOKEN=<your-proxy-token>\n"
                        "LITELLM_PROXY_MODEL=gemini/gemini-2.5-flash",
                        language="shell",
                    )
                ],
                emoji="🔑",
            ),
            _toggle(
                "Proxmox LXC Cron Schedule",
                [
                    _code(
                        "0  7 * * 1-5  brain daily    # weekdays 07:00\n"
                        "30 7 * * 1-5  brain notion-sync\n"
                        "0 18 * * 5    brain weekend  # friday 18:00\n"
                        "0 20 * * 0    brain weekly   # sunday  20:00\n"
                        "0  * * * *    brain alerts   # hourly overdue check",
                        language="shell",
                    )
                ],
                emoji="⏰",
            ),
            _divider(),
            # ── Productivity tips ─────────────────────────────────────────────
            _heading(2, "Productivity Ground Rules", "target"),
            _callout(
                "3-Task Rule: pick only 3 non-negotiable tasks per day. Everything else is bonus.\n"
                "2-Week Rule: if a task has been in Backlog for 14+ days, schedule it or delete it.\n"
                "Blocked Rule: add 'blocked' label in Todoist → AI deprioritises it automatically.\n"
                "Energy Rule: high-priority tasks in the morning when focus is peak.",
                emoji="💡",
                color="green_background",
            ),
        ]
        self.append_blocks(page_id, blocks)

    def _create_planner_page(
        self,
        page_id: str,
        title: str,
        description: str,
        emoji: str,
        instructions: list[str],
        extra_tip: str = "",
    ) -> dict:
        """Populate a planner sub-page with header, instructions, and create DB."""
        blocks = [
            _callout(description, emoji=emoji, color="blue_background"),
            _divider(),
            _heading(2, "How to use this planner", "ℹ️"),
            _columns(
                [
                    _heading(3, "Automated", "🤖"),
                ] + [_numbered(i) for i in instructions],
                [
                    _heading(3, "Manual (in Notion)", "✏️"),
                    _numbered("Click '+ New' in the database below to add a task."),
                    _numbered("Fill in Name, Priority, and Due Date."),
                    _numbered("Leave Todoist ID blank — it will be created on next sync."),
                    _numbered("Run: brain notion-sync to push it to Todoist."),
                ],
            ),
        ]
        if extra_tip:
            blocks.append(_callout(extra_tip, emoji="💡", color="green_background"))
        blocks.append(_divider())
        blocks.append(_heading(2, title, emoji))
        self.append_blocks(page_id, blocks)

        # Create the inline database inside the page
        db = self.create_database(
            parent_page_id=page_id,
            title=title,
            properties=self._task_db_schema(),
            emoji=emoji,
            is_inline=True,
        )
        return db

    def _create_board_page(self, page_id: str) -> dict:
        """Populate the full board page and create the All Tasks database."""
        blocks = [
            _callout(
                "Full Todoist board mirrored here. Sections are synced dynamically — "
                "add or rename sections in Todoist and the next sync updates this database automatically. "
                "Run: brain notion-sync --full",
                emoji="📊",
                color="gray_background",
            ),
            _callout(
                "To see this as a Kanban board matching Todoist:\n"
                "1. Click the \"All Tasks\" database below\n"
                "2. Click \"Add a view\" at the top\n"
                "3. Choose \"Board\"\n"
                "4. In the board settings, set Group by → Section",
                emoji="📋",
                color="blue_background",
            ),
            _divider(),
            _heading(2, "Sections (live from Todoist)", "📌"),
            _paragraph(
                "The 'Section' column is populated from your live Todoist board sections. "
                "If you delete or rename a section in Todoist, the next "
                "'brain notion-sync --full' will update the options automatically. "
                "Existing tasks are never deleted — they are updated in place."
            ),
            _divider(),
            _heading(2, "All Active Tasks", "📊"),
        ]
        self.append_blocks(page_id, blocks)
        db = self.create_database(
            parent_page_id=page_id,
            title="All Tasks",
            properties=self._board_db_schema(),
            emoji="📋",
            is_inline=True,
        )
        return db

    def _populate_blog_page(self, page_id: str) -> None:
        """Set up the Blog & Notes page with sub-sections."""
        blocks = [
            _callout(
                "Personal knowledge base and writing space. "
                "Create sub-pages for blog posts, technical notes, and project ideas. "
                "This page is manually managed — not synced with Todoist.",
                emoji="✍️",
                color="purple_background",
            ),
            _divider(),
            _columns(
                [
                    _heading(2, "Blog Posts", "📰"),
                    _paragraph(
                        "Create one sub-page per article. "
                        "Use H1 for title, H2 for sections. "
                        "Add cover image and icon for polish."
                    ),
                    _callout(
                        "Tip: write the TL;DR first, then expand each point.",
                        emoji="⚡", color="yellow_background",
                    ),
                ],
                [
                    _heading(2, "Braindump", "🧠"),
                    _paragraph(
                        "Raw thoughts, half-baked ideas, and unprocessed notes. "
                        "Review weekly and move actionable items to Todoist."
                    ),
                    _callout(
                        "Tip: date your braindumps. Use 'YYYY-MM-DD: topic' as title.",
                        emoji="📅", color="gray_background",
                    ),
                ],
            ),
            _divider(),
            _columns(
                [
                    _heading(2, "Ideas & Projects", "💡"),
                    _paragraph(
                        "Quick ideas not yet in Todoist. "
                        "Rate each: High/Medium/Low potential before scheduling."
                    ),
                ],
                [
                    _heading(2, "Reference Notes", "📚"),
                    _paragraph(
                        "Links, quotes, and reference material. "
                        "Tag with topic: [AI], [Infra], [Finance], [Health]."
                    ),
                ],
            ),
        ]
        self.append_blocks(page_id, blocks)

    # ── Dashboard rebuild (for existing dashboards) ─────────────────────────────

    def rebuild_dashboard(self, settings: "Settings") -> None:  # noqa: F821
        """Archive and rebuild the root page + Command Center content.

        Safe to run on an existing dashboard: planner DBs and their data
        are NOT touched. Only the root page blocks and Command Center blocks
        are cleared and rewritten with the latest layout.
        """
        root_page_id = settings.notion_root_page_id
        cc_id = settings.notion_command_center_id

        if root_page_id:
            logger.info("Rebuilding root page content (%s)...", root_page_id[:8])
            self.archive_blocks(root_page_id)
            self._populate_root_page(root_page_id)
            logger.info("  Root page rebuilt.")

        if cc_id:
            logger.info("Rebuilding Command Center content (%s)...", cc_id[:8])
            self.archive_blocks(cc_id)
            self._populate_command_center(cc_id)
            logger.info("  Command Center rebuilt.")

        if root_page_id:
            self.update_sync_status(settings.notion_command_center_id or root_page_id)

    # ── Task sync ─────────────────────────────────────────────────────────────

    def sync_board_to_database(
        self,
        db_id: str,
        tasks: list[Task],
        section_map: dict[str, str],
    ) -> tuple[int, int]:
        """Sync all board tasks with dynamic Section grouping.

        Design principles:
        - Sections are fetched live from Todoist (section_map: id → name).
        - The Notion database's Section select options are updated on every call.
          If you add, rename, or DELETE a section in Todoist, the next sync
          automatically reflects that. Existing items with a stale section name
          are updated to their current name on the next sync pass.
        - No hardcoded section names anywhere.
        - If a task has no section_id, it gets Section='No Section'.
        """
        # 1. Build live section options from current Todoist section_map
        unique_sections: dict[str, str] = {}  # name -> color (deduplicated)
        for raw_name in section_map.values():
            name = _sanitize_text(raw_name, 100)
            if name:
                unique_sections[name] = _section_color(name)
        if not unique_sections:
            unique_sections["No Section"] = "gray"

        section_options = [
            {"name": name, "color": color}
            for name, color in unique_sections.items()
        ]

        # 2. Push the live section list into the database schema
        #    This also ADDS the 'Section' property if it doesn't exist yet.
        try:
            self._patch(f"/databases/{db_id}", {
                "properties": {
                    "Section": {"select": {"options": section_options}}
                }
            })
            logger.info("Section options updated: %s", [o["name"] for o in section_options])
        except requests.RequestException as exc:
            logger.warning("Could not update Section options in DB %s: %s", db_id[:8], exc)

        # 3. Sync tasks (with section name on each task) + mark completed ones Done
        return self.sync_tasks_to_database(
            db_id, tasks, include_section=True, mark_stale_done=True
        )

    def sync_tasks_to_database(
        self,
        db_id: str,
        tasks: list[Task],
        *,
        include_section: bool = False,
        mark_stale_done: bool = False,
    ) -> tuple[int, int]:
        """Sync tasks into a Notion database. Returns (created, updated).

        include_section: if True, writes task.section_name to the 'Section' property.
        mark_stale_done: if True, any Notion item whose Todoist ID is no longer in the
            active task list is marked '✅ Done'. This keeps Notion an exact mirror of
            the Todoist active board — completed tasks disappear from active view.
        """
        if not tasks and not mark_stale_done:
            return 0, 0

        # Build a map of existing items by Todoist ID
        existing = self._query_database(db_id)
        existing_map: dict[str, str] = {}  # todoist_id → notion_page_id
        for item in existing:
            tid_prop = item.get("properties", {}).get("Todoist ID", {})
            rt = tid_prop.get("rich_text", [])
            if rt:
                tid = rt[0].get("plain_text", "")
                if tid:
                    existing_map[tid] = item["id"]

        created = 0
        updated = 0

        active_ids: set[str] = set()
        for task in tasks:
            active_ids.add(task.id)
            props = self._task_to_properties(task, include_section=include_section)
            if task.id in existing_map:
                # Update existing item
                try:
                    self.update_page(existing_map[task.id], props)
                    updated += 1
                except requests.RequestException as exc:
                    logger.warning("Failed to update Notion item for task %s: %s", task.id, exc)
            else:
                # Create new item
                try:
                    body = {
                        "parent": {"database_id": db_id},
                        "properties": props,
                    }
                    self._post("/pages", body)
                    created += 1
                except requests.RequestException as exc:
                    logger.warning("Failed to create Notion item for task %s: %s", task.id, exc)

        # Mark stale tasks (completed in Todoist but still in Notion) as Done
        if mark_stale_done:
            done_props = {"Status": {"select": {"name": "✅ Done"}}}
            stale_count = 0
            for tid, page_id in existing_map.items():
                if tid not in active_ids:
                    try:
                        self.update_page(page_id, done_props)
                        stale_count += 1
                    except requests.RequestException as exc:
                        logger.warning(
                            "Failed to mark stale task %s as Done: %s", tid[:8], exc
                        )
            if stale_count:
                logger.info(
                    "Marked %d stale tasks as Done in DB %s "
                    "(completed in Todoist, removed from active board)",
                    stale_count, db_id[:8],
                )

        logger.info(
            "Sync to DB %s: %d created, %d updated (of %d active tasks)",
            db_id[:8], created, updated, len(tasks),
        )
        return created, updated

    def get_notion_only_tasks(self, db_id: str) -> list[dict]:
        """Return Notion database items that have no Todoist ID (created in Notion)."""
        all_items = self._query_database(db_id)
        notion_only = []
        for item in all_items:
            tid_prop = item.get("properties", {}).get("Todoist ID", {})
            rt = tid_prop.get("rich_text", [])
            tid = rt[0].get("plain_text", "") if rt else ""
            if not tid:
                notion_only.append(item)
        return notion_only

    def update_sync_status(self, command_center_id: str) -> None:
        """Update the sync timestamp callout in Command Center.

        This is a best-effort update — failures are logged but not raised.
        """
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        try:
            blocks = self._paginate_blocks(command_center_id)
            for blk in blocks:
                if blk.get("type") == "callout":
                    rt = blk.get("callout", {}).get("rich_text", [])
                    text = rt[0].get("plain_text", "") if rt else ""
                    if text.startswith("Last updated:"):
                        self._patch(
                            f"/blocks/{blk['id']}",
                            {
                                "callout": {
                                    "rich_text": _rich_text(f"Last updated: {now}"),
                                    "icon": {"type": "emoji", "emoji": "🕐"},
                                    "color": "yellow_background",
                                }
                            },
                        )
                        return
        except requests.RequestException as exc:
            logger.warning("Could not update sync status: %s", exc)

    # ── Notion → Todoist (read from Notion, push new tasks to Todoist) ────────

    @staticmethod
    def notion_item_to_task_fields(item: dict) -> dict:
        """Convert a Notion database item to Todoist task fields.

        All strings are sanitised before being returned to prevent injection
        of control characters or oversized content into Todoist.
        Returns a dict suitable for passing to TodoistClient.update_task / create.
        """
        props = item.get("properties", {})
        content = ""
        name_prop = props.get("Name", {}).get("title", [])
        if name_prop:
            content = _sanitize_text(name_prop[0].get("plain_text", ""), 2000)

        priority = 1
        prio_name = props.get("Priority", {}).get("select", {})
        if prio_name:
            pmap = {"🔴 P1 Urgent": 4, "🟠 P2 High": 3, "🟡 P3 Normal": 2, "⚪ P4 Low": 1}
            priority = pmap.get(prio_name.get("name", ""), 1)

        due_date = None
        date_prop = props.get("Due Date", {}).get("date")
        if date_prop and date_prop.get("start"):
            due_date = date_prop["start"]

        labels = []
        for opt in props.get("Labels", {}).get("multi_select", []):
            labels.append(opt.get("name", ""))

        notes = ""
        notes_rt = props.get("Notes", {}).get("rich_text", [])
        if notes_rt:
            notes = notes_rt[0].get("plain_text", "")

        fields: dict[str, Any] = {"content": content, "priority": priority, "labels": labels}
        if due_date:
            fields["due_string"] = due_date
        if notes:
            fields["description"] = notes
        return fields
