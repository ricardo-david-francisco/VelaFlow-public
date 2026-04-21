"""CLI entrypoint for VelaFlow.

Usage:
    python -m brain daily [--stdout] [--no-llm]
    python -m brain weekend [--stdout] [--no-llm]
    python -m brain weekly [--stdout] [--no-llm]
    python -m brain alerts [--hours N] [--stdout]
    python -m brain analyze [--stdout]
    python -m brain organize [--apply] [--no-move] [--no-label] [--stdout]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone

from brain.config import Settings
from brain.email_sender import send_digest
from brain.gmail import get_unread_alerts
from brain.notebooklm import sync_notion_to_notebooklm
from brain.llm import polish_digest
from brain.models import DigestResult
from brain.notion import NotionClient
from brain.organizer import (
    analyze_board,
    format_reorganize_report,
    llm_analyze_board,
    reorganize_board,
)
from brain.planner import (
    build_daily_digest,
    build_overdue_alert,
    build_weekend_digest,
    build_weekly_review,
    load_prompt,
)
from brain.todoist import TodoistClient
from brain.whatsapp import send_to_user, send_to_secondary

logger = logging.getLogger("brain")


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _get_calendar_events(settings: Settings, start=None, end=None):
    """Safely import and call calendar integration."""
    try:
        from brain.calendar_ctx import get_events

        return get_events(settings, start=start, end=end)
    except Exception:
        logger.info("Calendar events unavailable.")
        return []


def cmd_daily(args: argparse.Namespace, settings: Settings) -> int:
    """Daily morning briefing."""
    logger.info("Building daily briefing...")

    client = TodoistClient(settings)
    tasks = client.get_today_tasks()
    # Also get upcoming for the full picture
    upcoming = client.get_upcoming_tasks(days=7)
    # Merge without duplicates
    seen = {t.id for t in tasks}
    for t in upcoming:
        if t.id not in seen:
            tasks.append(t)
            seen.add(t.id)

    events = _get_calendar_events(settings)
    emails = get_unread_alerts(settings, hours=24)

    digest = build_daily_digest(tasks, events, emails, settings)

    # LLM polish
    if not args.no_llm:
        prompt = load_prompt("daily-summary")
        polished = polish_digest(settings, digest.body_text, prompt)
        digest = DigestResult(
            subject=digest.subject,
            body_text=polished,
        )

    if args.stdout:
        print(digest.subject)
        print()
        print(digest.body_text)
        return 0

    # Deliver
    success = send_digest(settings, digest)
    send_to_user(settings, digest.body_text)

    return 0 if success else 1


def cmd_weekend(args: argparse.Namespace, settings: Settings) -> int:
    """Friday evening weekend planner."""
    logger.info("Building weekend plan...")

    client = TodoistClient(settings)
    tasks = client.get_weekend_tasks()
    # Also include overdue tasks that could be done on weekend
    overdue = client.get_overdue_tasks()
    seen = {t.id for t in tasks}
    for t in overdue:
        if t.id not in seen:
            tasks.append(t)
            seen.add(t.id)

    # Get weekend calendar events
    from datetime import date

    today = date.today()
    days_to_sat = (5 - today.weekday()) % 7
    if days_to_sat == 0 and today.weekday() != 5:
        days_to_sat = 7
    sat = datetime(today.year, today.month, today.day, tzinfo=timezone.utc) + timedelta(
        days=days_to_sat
    )
    sun_end = sat + timedelta(days=2)
    events = _get_calendar_events(settings, start=sat, end=sun_end)

    digest = build_weekend_digest(tasks, events, settings)

    if not args.no_llm:
        prompt = load_prompt("weekend-planner")
        polished = polish_digest(settings, digest.body_text, prompt)
        digest = DigestResult(subject=digest.subject, body_text=polished)

    if args.stdout:
        print(digest.subject)
        print()
        print(digest.body_text)
        return 0

    success = send_digest(settings, digest)
    send_to_user(settings, digest.body_text)
    send_to_secondary(settings, digest.body_text)

    return 0 if success else 1


def cmd_weekly(args: argparse.Namespace, settings: Settings) -> int:
    """Sunday evening weekly review."""
    logger.info("Building weekly review...")

    client = TodoistClient(settings)
    active = client.get_tasks()

    # Fetch completed tasks for the past week
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    completed = client.get_completed_tasks(since=week_ago)

    digest = build_weekly_review(active, completed, settings)

    if not args.no_llm:
        prompt = load_prompt("weekly-review")
        polished = polish_digest(settings, digest.body_text, prompt)
        digest = DigestResult(subject=digest.subject, body_text=polished)

    if args.stdout:
        print(digest.subject)
        print()
        print(digest.body_text)
        return 0

    success = send_digest(settings, digest)
    return 0 if success else 1


def cmd_alerts(args: argparse.Namespace, settings: Settings) -> int:
    """Check overdue tasks and send WhatsApp alert."""
    logger.info("Checking for overdue tasks...")

    client = TodoistClient(settings)
    tasks = client.get_overdue_tasks()

    message = build_overdue_alert(tasks, settings)
    if not message:
        logger.info("No overdue tasks. Skipping alert.")
        return 0

    if args.stdout:
        print(message)
        return 0

    send_to_user(settings, message)
    return 0


def cmd_analyze(args: argparse.Namespace, settings: Settings) -> int:
    """Read-only Kanban board analysis with LLM intelligence."""
    project_id = settings.todoist_kanban_project_id
    if not project_id:
        logger.error("TODOIST_KANBAN_PROJECT_ID not set. Check config/.env")
        return 1

    logger.info("Analyzing Kanban board...")
    client = TodoistClient(settings)
    tasks = client.get_tasks(project_id=project_id)
    section_map = client.get_section_map(project_id)

    analysis = analyze_board(tasks, section_map)
    output = analysis.summary

    # LLM-powered deep analysis (uses best model)
    if not args.no_llm:
        logger.info("Generating AI insights (best model)...")
        ai_insights = llm_analyze_board(analysis, tasks, settings)
        if ai_insights:
            output = f"{analysis.summary}\n\n{'=' * 50}\nAI INSIGHTS (Gemini)\n{'=' * 50}\n\n{ai_insights}"

    if args.stdout:
        print(output)
        return 0

    send_to_user(settings, output)
    return 0


def cmd_organize(args: argparse.Namespace, settings: Settings) -> int:
    """Reorganize the Kanban board (move tasks, apply labels)."""
    project_id = settings.todoist_kanban_project_id
    if not project_id:
        logger.error("TODOIST_KANBAN_PROJECT_ID not set. Check config/.env")
        return 1

    dry_run = not args.apply
    logger.info("Reorganizing board (%s)...", "DRY RUN" if dry_run else "APPLYING")

    client = TodoistClient(settings)
    result = reorganize_board(
        client,
        settings,
        dry_run=dry_run,
        move_tasks=not args.no_move,
        auto_label=not args.no_label,
    )

    report = format_reorganize_report(result, dry_run)

    if args.stdout:
        print(report)
        return 0

    send_to_user(settings, report)
    return 0 if not result.errors else 1


def cmd_notion_setup(args: argparse.Namespace, settings: Settings) -> int:
    """One-time setup: create Todoist planner sections + build Notion dashboard."""
    if not settings.notion_api_token:
        logger.error("NOTION_API_TOKEN not set. Check config/.env")
        return 1

    project_id = settings.todoist_kanban_project_id
    if not project_id:
        logger.error("TODOIST_KANBAN_PROJECT_ID not set. Check config/.env")
        return 1

    todoist = TodoistClient(settings)
    notion = NotionClient(settings)

    # ── Create Todoist planner sections ──────────────────────────────────────
    logger.info("Creating Todoist planner sections...")
    existing_sections = todoist.get_sections(project_id)
    existing_names = {s["name"] for s in existing_sections}

    new_section_ids: dict[str, str] = {}
    # Existing section IDs (preserve if already created)
    for s in existing_sections:
        if s["name"] in ("Daily Planner", "Weekly Planner", "Weekend Planner"):
            new_section_ids[s["name"]] = s["id"]

    # Create missing sections
    for name in ("Weekend Planner", "Weekly Planner", "Daily Planner"):
        if name not in existing_names:
            logger.info("  Creating Todoist section: %s", name)
            created = todoist.create_section(project_id, name)
            new_section_ids[name] = created["id"]
            logger.info("    → ID: %s", created["id"])
        else:
            logger.info("  Section already exists: %s (ID: %s)", name, new_section_ids.get(name, "?"))

    # ── Build Notion dashboard ────────────────────────────────────────────────
    root_page_id = settings.notion_root_page_id
    if not root_page_id:
        logger.error("NOTION_ROOT_PAGE_ID not set.")
        return 1

    logger.info("Building Notion dashboard inside page %s...", root_page_id[:8])
    notion_ids = notion.setup_dashboard(root_page_id)

    # ── Print .env snippet ────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("SETUP COMPLETE — add these to your config/.env:")
    print("=" * 60)
    for name, var in (
        ("Daily Planner", "TODOIST_DAILY_PLANNER_SECTION_ID"),
        ("Weekly Planner", "TODOIST_WEEKLY_PLANNER_SECTION_ID"),
        ("Weekend Planner", "TODOIST_WEEKEND_PLANNER_SECTION_ID"),
    ):
        print(f"{var}={new_section_ids.get(name, 'MISSING')}")
    print()
    print(f"NOTION_ROOT_PAGE_ID={root_page_id}")
    print(f"NOTION_COMMAND_CENTER_ID={notion_ids.get('command_center_id', '')}")
    print(f"NOTION_DAILY_PLANNER_DB_ID={notion_ids.get('daily_planner_db_id', '')}")
    print(f"NOTION_WEEKLY_PLANNER_DB_ID={notion_ids.get('weekly_planner_db_id', '')}")
    print(f"NOTION_WEEKEND_PLANNER_DB_ID={notion_ids.get('weekend_planner_db_id', '')}")
    print(f"NOTION_BOARD_DB_ID={notion_ids.get('board_db_id', '')}")
    print("=" * 60)
    print("NOTE: Drag 'Weekend Planner', 'Weekly Planner', 'Daily Planner'")
    print("      sections above 'Doing' in your Todoist board.")
    print("=" * 60 + "\n")
    return 0


def cmd_notion_sync(args: argparse.Namespace, settings: Settings) -> int:
    """Two-way sync between Notion databases and Todoist planner sections."""
    if not settings.notion_api_token:
        logger.error("NOTION_API_TOKEN not set. Check config/.env")
        return 1

    todoist = TodoistClient(settings)
    notion = NotionClient(settings)
    project_id = settings.todoist_kanban_project_id

    total_created = 0
    total_updated = 0
    errors = 0

    # ── Todoist → Notion sync ─────────────────────────────────────────────────
    planner_map = [
        (
            "Daily Planner",
            settings.todoist_daily_planner_section_id,
            settings.notion_daily_planner_db_id,
        ),
        (
            "Weekly Planner",
            settings.todoist_weekly_planner_section_id,
            settings.notion_weekly_planner_db_id,
        ),
        (
            "Weekend Planner",
            settings.todoist_weekend_planner_section_id,
            settings.notion_weekend_planner_db_id,
        ),
    ]

    for name, section_id, db_id in planner_map:
        if not section_id or not db_id:
            logger.info("Skipping %s — section_id or db_id not configured.", name)
            continue
        logger.info("Syncing %s → Notion...", name)
        try:
            tasks = todoist.get_tasks(project_id=project_id, section_id=section_id)
            c, u = notion.sync_tasks_to_database(db_id, tasks)
            total_created += c
            total_updated += u
            logger.info("  %s: %d created, %d updated in Notion", name, c, u)
        except Exception as exc:
            logger.error("Failed to sync %s: %s", name, exc)
            errors += 1

    # ── Full board sync (optional) ────────────────────────────────────────────
    if args.full and settings.notion_board_db_id and project_id:
        logger.info("Full board sync → Notion board DB...")
        try:
            all_tasks = todoist.get_tasks(project_id=project_id)
            section_map = todoist.get_section_map(project_id)
            c, u = notion.sync_board_to_database(
                settings.notion_board_db_id, all_tasks, section_map
            )
            logger.info("  Board: %d created, %d updated", c, u)
            total_created += c
            total_updated += u
        except Exception as exc:
            logger.error("Board sync failed: %s", exc)
            errors += 1

    # ── Notion → Todoist (new tasks added in Notion) ──────────────────────────
    if settings.brain_read_only:
        logger.info("BRAIN_READ_ONLY=true — skipping Notion→Todoist direction.")
    else:
        for name, section_id, db_id in planner_map:
            if not section_id or not db_id:
                continue
            try:
                notion_only = notion.get_notion_only_tasks(db_id)
                if not notion_only:
                    continue
                logger.info(
                    "Found %d Notion-only tasks in %s → pushing to Todoist...",
                    len(notion_only), name,
                )
                for item in notion_only:
                    fields = NotionClient.notion_item_to_task_fields(item)
                    content = fields.pop("content", "")
                    if not content:
                        continue
                    # Create task in Todoist planner section
                    body = {
                        "content": content,
                        "project_id": project_id,
                        "section_id": section_id,
                        **{k: v for k, v in fields.items() if v},
                    }
                    try:
                        new_task = todoist._post(
                            f"https://api.todoist.com/api/v1/tasks", body
                        )
                        task_id = new_task.get("id", "")
                        # Update Notion item with Todoist ID
                        if task_id:
                            from brain.notion import _rich_text
                            notion.update_page(
                                item["id"],
                                {
                                    "Todoist ID": {"rich_text": _rich_text(str(task_id))},
                                    "Todoist URL": {"url": new_task.get("url")},
                                },
                            )
                        total_created += 1
                        logger.info("  Created Todoist task: %s", content[:50])
                    except Exception as exc:
                        logger.error("  Failed to create Todoist task '%s': %s", content[:50], exc)
                        errors += 1
            except Exception as exc:
                logger.error("Notion→Todoist sync failed for %s: %s", name, exc)
                errors += 1

    # ── Update Command Center sync timestamp ──────────────────────────────────
    if settings.notion_command_center_id:
        notion.update_sync_status(settings.notion_command_center_id)

    summary = (
        f"Notion sync complete — {total_created} created, {total_updated} updated"
        + (f", {errors} errors" if errors else "")
    )
    logger.info(summary)
    if args.stdout:
        print(summary)

    return 0 if not errors else 1


def cmd_notebooklm_sync(args: argparse.Namespace, settings: Settings) -> int:
    """Sync Notion 2nd-Brain workspace to a NotebookLM notebook."""
    if not settings.notion_api_token:
        logger.error("NOTION_API_TOKEN not set. Check config/.env")
        return 1
    if not settings.notion_root_page_id:
        logger.error("NOTION_ROOT_PAGE_ID not set. Check config/.env")
        return 1

    rebuild = not args.no_rebuild
    logger.info(
        "Syncing Notion \u2192 NotebookLM (%s)...",
        "rebuild" if rebuild else "append",
    )

    try:
        result = sync_notion_to_notebooklm(settings, rebuild=rebuild)
    except (RuntimeError, ValueError) as exc:
        logger.error("%s", exc)
        return 1

    summary = (
        f"NotebookLM sync complete \u2014 "
        f"{result['added']} sources added, "
        f"{result['deleted']} deleted. "
        f"Notebook: {result['notebook_id']}"
    )
    logger.info(summary)
    if args.stdout:
        print(summary)
    return 0


def cmd_notion_rebuild(args: argparse.Namespace, settings: Settings) -> int:  # noqa: ARG001
    """Archive + rebuild the root 2nd-Brain page and Command Center layout.

    Safe to run at any time on an existing dashboard.
    Planner databases and their task data are NOT touched.
    After rebuilding, run 'brain notion-sync' to refresh DB data.
    """
    if not settings.notion_api_token:
        logger.error("NOTION_API_TOKEN not set. Check config/.env")
        return 1
    if not settings.notion_root_page_id:
        logger.error("NOTION_ROOT_PAGE_ID not set. Run 'brain notion-setup' first.")
        return 1

    notion = NotionClient(settings)
    logger.info("Rebuilding Notion dashboard layout...")
    try:
        notion.rebuild_dashboard(settings)
        logger.info("Rebuild complete.")
        return 0
    except Exception as exc:
        logger.error("Rebuild failed: %s", exc)
        return 1


def main() -> None:
    """Main CLI entrypoint."""
    parser = argparse.ArgumentParser(
        prog="brain",
        description="VelaFlow — Productivity Automation System",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging"
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Daily
    p_daily = subparsers.add_parser("daily", help="Send daily morning briefing")
    p_daily.add_argument("--stdout", action="store_true", help="Print to stdout instead of sending")
    p_daily.add_argument("--no-llm", action="store_true", help="Skip AI polishing")

    # Weekend
    p_weekend = subparsers.add_parser("weekend", help="Send weekend planner")
    p_weekend.add_argument("--stdout", action="store_true", help="Print to stdout")
    p_weekend.add_argument("--no-llm", action="store_true", help="Skip AI polishing")

    # Weekly
    p_weekly = subparsers.add_parser("weekly", help="Send weekly review")
    p_weekly.add_argument("--stdout", action="store_true", help="Print to stdout")
    p_weekly.add_argument("--no-llm", action="store_true", help="Skip AI polishing")

    # Alerts
    p_alerts = subparsers.add_parser("alerts", help="Send overdue task WhatsApp alerts")
    p_alerts.add_argument("--hours", type=int, default=4, help="Lookback hours for email alerts")
    p_alerts.add_argument("--stdout", action="store_true", help="Print to stdout")

    # Analyze (read-only board intelligence)
    p_analyze = subparsers.add_parser("analyze", help="Analyze Kanban board (read-only)")
    p_analyze.add_argument("--stdout", action="store_true", help="Print to stdout")
    p_analyze.add_argument("--no-llm", action="store_true", help="Skip AI insights")

    # Organize (write operations)
    p_organize = subparsers.add_parser("organize", help="Reorganize Kanban board")
    p_organize.add_argument("--apply", action="store_true", help="Apply changes (default is dry-run)")
    p_organize.add_argument("--no-move", action="store_true", help="Skip section moves")
    p_organize.add_argument("--no-label", action="store_true", help="Skip auto-labeling")
    p_organize.add_argument("--stdout", action="store_true", help="Print report to stdout")

    # Notion setup (one-time)
    p_nsetup = subparsers.add_parser("notion-setup", help="Create Todoist sections + Notion dashboard")
    p_nsetup.add_argument("--stdout", action="store_true", help="Print IDs to stdout")

    # Notion sync (two-way)
    p_nsync = subparsers.add_parser("notion-sync", help="Two-way sync Notion ↔ Todoist planners")
    p_nsync.add_argument("--full", action="store_true", help="Also sync full Kanban board to Notion")
    p_nsync.add_argument("--stdout", action="store_true", help="Print summary to stdout")
    # Notion rebuild (refresh layout, keep data)
    subparsers.add_parser(
        "notion-rebuild",
        help="Rebuild root page + Command Center layout (data is preserved)",
    )

    # NotebookLM sync
    p_nlm = subparsers.add_parser(
        "notebooklm-sync",
        help="Sync Notion 2nd-Brain workspace to a NotebookLM notebook",
    )
    p_nlm.add_argument(
        "--no-rebuild",
        action="store_true",
        help="Append sources instead of deleting and re-adding all text sources",
    )
    p_nlm.add_argument("--stdout", action="store_true", help="Print summary to stdout")

    args = parser.parse_args()
    _setup_logging(args.verbose)

    if not args.command:
        parser.print_help()
        sys.exit(1)

    settings = Settings.from_env()

    if not settings.todoist_api_token:
        logger.error("TODOIST_API_TOKEN not set. Check config/.env")
        sys.exit(1)

    commands = {
        "daily": cmd_daily,
        "weekend": cmd_weekend,
        "weekly": cmd_weekly,
        "alerts": cmd_alerts,
        "analyze": cmd_analyze,
        "organize": cmd_organize,
        "notion-setup": cmd_notion_setup,
        "notion-sync": cmd_notion_sync,
        "notion-rebuild": cmd_notion_rebuild,
        "notebooklm-sync": cmd_notebooklm_sync,
    }

    handler = commands[args.command]
    exit_code = handler(args, settings)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
