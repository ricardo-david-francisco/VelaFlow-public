"""Task scoring engine, digest builder, and weekend planner.

This is the core of VelaFlow. It deterministically scores
and ranks tasks, then builds human-readable digests that can optionally
be polished by an LLM.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from brain.config import Settings
from brain.models import (
    CalendarEvent,
    DigestResult,
    EmailAlert,
    ScoredTask,
    Task,
)

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"


# =============================================================================
# Scoring
# =============================================================================

def score_task(task: Task, settings: Settings, weekend_mode: bool = False) -> ScoredTask:
    """Compute a priority score for a single task."""
    score = 0
    reasons: list[str] = []
    today = date.today()

    # --- Overdue ---
    if task.due_date and task.due_date < today:
        days_overdue = (today - task.due_date).days
        capped = min(days_overdue, 7)
        points = capped * 20
        score += points
        reasons.append(f"Overdue {days_overdue}d → +{points}")

    # --- Due today ---
    elif task.due_date and task.due_date == today:
        score += 25
        reasons.append("Due today → +25")

    # --- Due soon ---
    elif task.due_date:
        days_until = (task.due_date - today).days
        if days_until == 1:
            score += 16
            reasons.append("Due tomorrow → +16")
        elif days_until == 2:
            score += 8
            reasons.append("Due in 2 days → +8")
        elif days_until == 3:
            score += 4
            reasons.append("Due in 3 days → +4")

    # --- Priority (Todoist: 4=p1/urgent, 3=p2, 2=p3, 1=none) ---
    priority_points = {4: 18, 3: 10, 2: 4, 1: 0}
    pp = priority_points.get(task.priority, 0)
    if pp:
        score += pp
        reasons.append(f"Priority p{5 - task.priority} → +{pp}")

    # --- Focus label ---
    if settings.todoist_focus_label in task.labels:
        score += 14
        reasons.append(f"@{settings.todoist_focus_label} → +14")

    # --- Weekend label ---
    if weekend_mode and settings.todoist_weekend_label in task.labels:
        score += 10
        reasons.append(f"@{settings.todoist_weekend_label} → +10")

    # --- Duration bonuses ---
    duration = task.duration_minutes or settings.default_task_duration_minutes
    if duration <= 30:
        score += 4
        reasons.append("Quick win (≤30min) → +4")
    elif duration >= 120:
        score -= 6
        reasons.append("Long task (≥2h) → -6")

    # --- No due date penalty ---
    if not task.due_date and settings.todoist_focus_label not in task.labels:
        score -= 4
        reasons.append("No due date, no @focus → -4")

    # --- Section-aware scoring (Kanban board) ---
    sec = task.section_name.lower() if task.section_name else ""
    if "urgent" in sec or "today" in sec:
        score += 12
        reasons.append("Section: Urgent/Today → +12")
    elif "doing" in sec:
        score += 10
        reasons.append("Section: Doing → +10")
    elif "high" in sec:
        score += 6
        reasons.append("Section: High → +6")
    elif "blocked" in sec:
        score -= 20
        reasons.append("Section: Blocked → -20")
    elif "rejected" in sec:
        score -= 30
        reasons.append("Section: Rejected → -30")
    elif "backlog" in sec:
        # Backlog tasks with high priority are hidden gems
        if task.priority >= 3:
            score += 5
            reasons.append("Backlog high-pri discovery → +5")
        else:
            score -= 2
            reasons.append("Section: Backlog → -2")

    return ScoredTask(task=task, score=score, reasons=reasons)


def rank_tasks(
    tasks: list[Task], settings: Settings, weekend_mode: bool = False
) -> list[ScoredTask]:
    """Score and sort tasks by priority."""
    scored = [score_task(t, settings, weekend_mode) for t in tasks]
    scored.sort(
        key=lambda s: (
            -s.score,
            s.task.due_date or date.max,
            -s.task.priority,
            s.task.content.lower(),
        )
    )
    return scored


# =============================================================================
# Digest Builders
# =============================================================================

def _priority_icon(priority: int) -> str:
    """Map Todoist priority (1-4) to emoji."""
    return {4: "🔴", 3: "🟠", 2: "🟡", 1: "⚪"}.get(priority, "⚪")


def _format_task_line(st: ScoredTask, show_score: bool = False) -> str:
    """Format a single task as a text line."""
    t = st.task
    icon = _priority_icon(t.priority)
    project = f" [{t.project_name}]" if t.project_name else ""
    section = f" §{t.section_name}" if t.section_name else ""
    due = ""
    if t.due_date:
        if t.due_date < date.today():
            days = (date.today() - t.due_date).days
            due = f" ⚠️ {days}d overdue"
        elif t.due_date == date.today():
            due = " 📅 today"
        else:
            due = f" 📅 {t.due_date.strftime('%a %d')}"
    duration = ""
    if t.duration_minutes:
        if t.duration_minutes >= 60:
            h = t.duration_minutes // 60
            m = t.duration_minutes % 60
            duration = f" ⏱️ {h}h{m:02d}m" if m else f" ⏱️ {h}h"
        else:
            duration = f" ⏱️ {t.duration_minutes}min"
    score_str = f" (score: {st.score})" if show_score else ""
    return f"  {icon} {t.content}{project}{section}{due}{duration}{score_str}"


def build_daily_digest(
    tasks: list[Task],
    events: list[CalendarEvent],
    emails: list[EmailAlert],
    settings: Settings,
) -> DigestResult:
    """Build the daily morning briefing digest."""
    today = date.today()
    ranked = rank_tasks(tasks, settings)

    # Categorize
    overdue = [s for s in ranked if s.task.due_date and s.task.due_date < today]
    due_today = [
        s for s in ranked
        if s.task.due_date and s.task.due_date == today
    ]
    focus = [
        s for s in ranked
        if settings.todoist_focus_label in s.task.labels
        and s not in overdue and s not in due_today
    ]
    upcoming = [
        s for s in ranked
        if s.task.due_date and s.task.due_date > today
        and s not in focus
    ][:7]

    # Top priorities (merged and deduplicated)
    seen_ids: set[str] = set()
    top: list[ScoredTask] = []
    for s in ranked:
        if s.task.id not in seen_ids and len(top) < settings.daily_top_task_limit:
            top.append(s)
            seen_ids.add(s.task.id)

    lines: list[str] = []
    lines.append(f"📋 Daily Briefing — {today.strftime('%A, %B %d, %Y')}")
    lines.append("=" * 50)

    # Stats
    total = len(tasks)
    overdue_count = len(overdue)
    today_count = len(due_today)
    lines.append(f"\n📊 {total} active tasks | {overdue_count} overdue | {today_count} due today")

    # Top priorities
    lines.append(f"\n🎯 TOP {len(top)} PRIORITIES")
    lines.append("-" * 30)
    for i, s in enumerate(top, 1):
        lines.append(f"  {i}. {s.task.content}")
        if s.reasons:
            lines.append(f"     → {', '.join(s.reasons[:3])}")

    # Overdue section
    if overdue:
        lines.append(f"\n⚠️ OVERDUE ({len(overdue)} tasks)")
        lines.append("-" * 30)
        for s in overdue[: settings.overdue_section_limit]:
            lines.append(_format_task_line(s))

    # Due today
    if due_today:
        lines.append(f"\n📅 DUE TODAY ({len(due_today)} tasks)")
        lines.append("-" * 30)
        for s in due_today:
            lines.append(_format_task_line(s))

    # Calendar events
    if events:
        lines.append(f"\n📆 CALENDAR ({len(events)} events)")
        lines.append("-" * 30)
        for ev in events:
            time_str = ""
            if ev.start and not ev.all_day:
                time_str = ev.start.strftime("%H:%M")
                if ev.end:
                    time_str += f"-{ev.end.strftime('%H:%M')}"
            elif ev.all_day:
                time_str = "All day"
            lines.append(f"  🕐 {time_str}  {ev.summary}")

    # Focus / long-term
    if focus:
        lines.append(f"\n🎯 LONG-TERM PRIORITIES (@{settings.todoist_focus_label})")
        lines.append("-" * 30)
        for s in focus[:5]:
            lines.append(_format_task_line(s))

    # Upcoming
    if upcoming:
        lines.append(f"\n📆 COMING UP (next 7 days)")
        lines.append("-" * 30)
        for s in upcoming:
            lines.append(_format_task_line(s))

    # Email alerts
    if emails:
        lines.append(f"\n📧 UNREAD IMPORTANT EMAILS ({len(emails)})")
        lines.append("-" * 30)
        for ea in emails[:10]:
            sender_short = ea.sender.split("<")[0].strip() if "<" in ea.sender else ea.sender
            lines.append(f"  📩 {sender_short}: {ea.subject}")

    body = "\n".join(lines)
    return DigestResult(
        subject=f"🧠 Daily Briefing — {today.strftime('%a %b %d')} | {overdue_count} overdue, {today_count} today",
        body_text=body,
    )


def build_weekend_digest(
    tasks: list[Task],
    events: list[CalendarEvent],
    settings: Settings,
) -> DigestResult:
    """Build the Friday evening weekend planner digest."""
    today = date.today()
    # Find Saturday and Sunday
    days_to_sat = (5 - today.weekday()) % 7
    if days_to_sat == 0 and today.weekday() != 5:
        days_to_sat = 7
    saturday = today + timedelta(days=days_to_sat)
    sunday = saturday + timedelta(days=1)

    ranked = rank_tasks(tasks, settings, weekend_mode=True)
    capacity_min = settings.weekend_capacity_hours * 60  # per day

    # Split events by day
    sat_events = [e for e in events if e.start and e.start.date() == saturday]
    sun_events = [e for e in events if e.start and e.start.date() == sunday]
    sat_booked = sum(e.duration_minutes for e in sat_events)
    sun_booked = sum(e.duration_minutes for e in sun_events)
    sat_free = max(0, capacity_min - sat_booked)
    sun_free = max(0, capacity_min - sun_booked)

    # Greedy allocation
    sat_tasks: list[ScoredTask] = []
    sun_tasks: list[ScoredTask] = []
    overflow: list[ScoredTask] = []
    sat_used = 0
    sun_used = 0

    for s in ranked[: settings.weekend_task_limit * 2]:
        dur = s.task.duration_minutes or settings.default_task_duration_minutes
        # Prefer Saturday for @focus tasks
        prefer_sat = settings.todoist_focus_label in s.task.labels
        placed = False

        if prefer_sat and sat_used + dur <= sat_free:
            sat_tasks.append(s)
            sat_used += dur
            placed = True
        elif sun_used + dur <= sun_free:
            sun_tasks.append(s)
            sun_used += dur
            placed = True
        elif sat_used + dur <= sat_free:
            sat_tasks.append(s)
            sat_used += dur
            placed = True

        if not placed:
            overflow.append(s)

        if len(sat_tasks) + len(sun_tasks) >= settings.weekend_task_limit:
            break

    lines: list[str] = []
    lines.append(f"🗓️ Weekend Planner — {saturday.strftime('%B %d')}-{sunday.strftime('%d, %Y')}")
    lines.append("=" * 50)
    lines.append(
        f"\n⏰ Capacity: {settings.weekend_capacity_hours}h/day | "
        f"Sat free: {sat_free}min | Sun free: {sun_free}min"
    )

    # Saturday
    lines.append(f"\n📅 SATURDAY — {saturday.strftime('%B %d')}")
    lines.append("-" * 30)
    if sat_events:
        lines.append("  Events:")
        for ev in sat_events:
            t = ev.start.strftime("%H:%M") if ev.start and not ev.all_day else "All day"
            lines.append(f"    🕐 {t}  {ev.summary}")
    if sat_tasks:
        lines.append("  Tasks:")
        for s in sat_tasks:
            lines.append(_format_task_line(s))
    elif not sat_events:
        lines.append("  No tasks scheduled. Enjoy your Saturday!")

    # Sunday
    lines.append(f"\n📅 SUNDAY — {sunday.strftime('%B %d')}")
    lines.append("-" * 30)
    if sun_events:
        lines.append("  Events:")
        for ev in sun_events:
            t = ev.start.strftime("%H:%M") if ev.start and not ev.all_day else "All day"
            lines.append(f"    🕐 {t}  {ev.summary}")
    if sun_tasks:
        lines.append("  Tasks:")
        for s in sun_tasks:
            lines.append(_format_task_line(s))
    elif not sun_events:
        lines.append("  No tasks scheduled. Enjoy your Sunday!")

    # Overflow
    if overflow:
        lines.append(f"\n⏳ DEFER OR SPLIT ({len(overflow)} tasks didn't fit)")
        lines.append("-" * 30)
        for s in overflow[:5]:
            lines.append(_format_task_line(s))
        lines.append("\n  💡 Consider: splitting long tasks, moving to next week, or delegating.")

    # Family time reminder
    lines.append("\n👨‍👩‍👧 FAMILY TIME PROTECTION")
    lines.append("-" * 30)
    lines.append("  Remember: Tasks are scheduled around events.")
    lines.append("  Demanding tasks → morning (peak energy).")
    lines.append("  Keep buffer time between tasks (30 min).")
    lines.append("  Family activities always take priority.")

    body = "\n".join(lines)
    return DigestResult(
        subject=f"🗓️ Weekend Plan — {saturday.strftime('%b %d')}-{sunday.strftime('%d')} | {len(sat_tasks)+len(sun_tasks)} tasks",
        body_text=body,
    )


def build_weekly_review(
    active_tasks: list[Task],
    completed_items: list[dict],
    settings: Settings,
) -> DigestResult:
    """Build the Sunday evening weekly review digest."""
    today = date.today()
    week_start = today - timedelta(days=today.weekday())  # Monday
    week_end = today

    ranked = rank_tasks(active_tasks, settings)

    # Analyze completed
    completed_count = len(completed_items)
    completed_names = [
        c.get("content", c.get("task", {}).get("content", "?"))
        for c in completed_items[:20]
    ]

    # Analyze active
    overdue = [s for s in ranked if s.task.due_date and s.task.due_date < today]
    high_priority = [s for s in ranked if s.task.priority >= 3]

    # Projects breakdown
    projects: dict[str, int] = {}
    for s in ranked:
        name = s.task.project_name or "No Project"
        projects[name] = projects.get(name, 0) + 1

    lines: list[str] = []
    lines.append(
        f"📊 Weekly Review — {week_start.strftime('%B %d')} to {week_end.strftime('%B %d, %Y')}"
    )
    lines.append("=" * 50)

    # Summary stats
    lines.append(f"\n📈 WEEK SUMMARY")
    lines.append("-" * 30)
    lines.append(f"  ✅ Completed: {completed_count} tasks")
    lines.append(f"  📋 Active: {len(active_tasks)} tasks")
    lines.append(f"  ⚠️ Overdue: {len(overdue)} tasks")
    lines.append(f"  🔴 High priority: {len(high_priority)} tasks")

    # Completed tasks
    if completed_names:
        lines.append(f"\n✅ COMPLETED THIS WEEK ({completed_count})")
        lines.append("-" * 30)
        for name in completed_names:
            lines.append(f"  ✓ {name}")

    # Overdue / falling behind
    if overdue:
        lines.append(f"\n⚠️ FALLING BEHIND ({len(overdue)} overdue)")
        lines.append("-" * 30)
        for s in overdue[:10]:
            lines.append(_format_task_line(s))
        lines.append("\n  💡 Consider: reschedule, delegate, or break into smaller tasks.")

    # Project health
    if projects:
        lines.append(f"\n📁 PROJECT HEALTH")
        lines.append("-" * 30)
        for proj, count in sorted(projects.items(), key=lambda x: -x[1]):
            lines.append(f"  {proj}: {count} active tasks")

    # Next week priorities
    next_week = [
        s for s in ranked
        if s.task.due_date and today < s.task.due_date <= today + timedelta(days=7)
    ]
    if next_week:
        lines.append(f"\n🎯 NEXT WEEK PRIORITIES")
        lines.append("-" * 30)
        for s in next_week[:7]:
            lines.append(_format_task_line(s))

    # Long-term alignment
    focus_tasks = [
        s for s in ranked if settings.todoist_focus_label in s.task.labels
    ]
    if focus_tasks:
        lines.append(f"\n🧭 LONG-TERM GOAL ALIGNMENT (@{settings.todoist_focus_label})")
        lines.append("-" * 30)
        for s in focus_tasks[:5]:
            lines.append(_format_task_line(s))

    body = "\n".join(lines)
    return DigestResult(
        subject=f"📊 Weekly Review — {week_start.strftime('%b %d')}-{week_end.strftime('%d')} | {completed_count} done, {len(overdue)} overdue",
        body_text=body,
    )


def build_overdue_alert(tasks: list[Task], settings: Settings) -> str | None:
    """Build a lightweight WhatsApp-only overdue alert. Returns None if no overdue tasks."""
    today = date.today()
    overdue = [t for t in tasks if t.due_date and t.due_date < today]

    if not overdue:
        return None

    # Sort by days overdue (most overdue first)
    overdue.sort(key=lambda t: t.due_date or today)

    lines = [f"⚠️ {len(overdue)} OVERDUE TASKS", ""]
    for t in overdue[:10]:
        days = (today - t.due_date).days if t.due_date else 0
        icon = _priority_icon(t.priority)
        project = f" [{t.project_name}]" if t.project_name else ""
        lines.append(f"{icon} {t.content}{project} ({days}d late)")

    if len(overdue) > 10:
        lines.append(f"\n... and {len(overdue) - 10} more")

    lines.append(f"\n🔗 Open Todoist to reschedule")
    return "\n".join(lines)


def load_prompt(name: str) -> str:
    """Load a prompt template from the prompts/ directory."""
    path = PROMPTS_DIR / f"{name}.md"
    if path.is_file():
        return path.read_text(encoding="utf-8")
    logger.warning("Prompt template not found: %s", path)
    return _default_prompts().get(name, "Rewrite for clarity without changing facts.")


def _default_prompts() -> dict[str, str]:
    """Fallback prompts if files are missing."""
    return {
        "daily-summary": (
            "You are a productivity coach. "
            "Rewrite this daily briefing to be scannable, actionable, and structured. "
            "Keep all facts. Use clear sections. "
            "Highlight the top 3 things to focus on. Keep it under 500 words."
        ),
        "weekend-planner": (
            "You are a weekend planning assistant. "
            "Rewrite this weekend plan to be realistic and well-structured. "
            "Protect personal commitments. Schedule demanding tasks in the morning. "
            "Add 30-min buffers between tasks. Keep it under 400 words."
        ),
        "weekly-review": (
            "You are a productivity coach reviewing someone's week. "
            "Rewrite this review to highlight completed work, flag recurring deferrals, "
            "and suggest 3 concrete actions for next week. "
            "Be direct and data-driven. Keep it under 500 words."
        ),
        "task-prioritization": (
            "You are a task prioritization expert using the Eisenhower matrix. "
            "Analyze the tasks and categorize as: Urgent+Important, Important, Urgent, Neither. "
            "Be concise. Output a prioritized list."
        ),
    }
