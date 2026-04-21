"""Kanban board intelligence — analyze, categorize, and reorganize tasks."""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from brain.config import Settings
from brain.models import Task
from brain.todoist import TodoistClient

logger = logging.getLogger(__name__)

# -------------------------------------------------------------------------
# Label inference from Portuguese task names
# -------------------------------------------------------------------------

_LABEL_KEYWORDS: dict[str, list[str]] = {
    "Manutenção": [
        "arranjar", "reparar", "pintar", "limpar", "limpeza", "fixar", "trocar",
        "substituir", "instalar", "montar", "desmontar", "canalização",
        "silicone", "calafetagem", "porta", "janela", "cortina", "estore",
        "fechadura", "lampada", "lâmpada", "torneira", "chuveiro", "wc",
        "parede", "chão", "teto", "telhado", "calha",
    ],
    "Tecnologia": [
        "pc", "computador", "servidor", "proxmox", "docker", "backup",
        "wifi", "router", "rede", "cable", "cabo", "nas", "raspberry",
        "pi", "smart", "automação", "home assistant", "n8n", "vpn",
        "ssh", "linux", "ubuntu", "debian", "plex", "app", "software",
        "setup", "config", "usb", "impressora", "printer",
    ],
    "Finanças": [
        "banco", "seguro", "imposto", "irs", "factura", "fatura",
        "pagamento", "pagar", "transferir", "conta", "crédito",
        "hipoteca", "financiamento", "subscrição", "subscricao",
        "cancelar assinatura", "mensalidade", "anuidade",
    ],
    "Burocracia": [
        "renovar", "documento", "certidão", "atestado", "procuração",
        "notário", "conservatória", "finanças", "registo", "carta",
        "condução", "cc", "passaporte", "nif", "niss", "segurança social",
        "formulário", "requerimento", "autoridade", "câmara", "junta",
    ],
    "Saúde": [
        "médico", "consulta", "dentista", "exame", "análises",
        "vacina", "farmácia", "receita", "medicamento", "saúde",
        "hospital", "urgência", "fisioterapia", "dermatologista",
        "oftalmologista", "máscara",
    ],
    "Lojas": [
        "comprar", "encomendar", "leroy", "ikea", "worten", "amazon",
        "ali express", "aliexpress", "olx", "loja", "supermercado",
        "lidl", "continente", "pingo", "auchan", "mercado", "fnac",
    ],
    "Entretenimento": [
        "filme", "série", "jogo", "netflix", "spotify", "youtube",
        "livro", "ler", "podcast", "concerto", "espetáculo", "bilhete",
        "viagem", "férias", "passeio", "praia", "restaurante",
    ],
    "decoração": [
        "decorar", "vaso", "planta", "quadro", "prateleira", "estante",
        "organizar", "arrumação", "móvel", "sofá", "mesa", "cadeira",
        "tapete", "almofada", "cortinado",
    ],
    "Família": [
        "família", "mãe", "pai", "irmã", "irmão", "avó", "avô",
        "sobrinho", "primo", "casamento", "aniversário", "natal",
        "páscoa", "presente",
    ],
}


def infer_labels(task: Task) -> list[str]:
    """Suggest labels based on task content and description."""
    text = f"{task.content} {task.description}".lower()
    suggested: list[str] = []
    for label, keywords in _LABEL_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                suggested.append(label)
                break
    return suggested


# -------------------------------------------------------------------------
# Section placement logic
# -------------------------------------------------------------------------

# Canonical section names expected on the Kanban Casa board
SECTIONS_ORDER = [
    "Rejected",
    "Backlog",
    "To Do - Low",
    "To Do - Normal",
    "To Do - High",
    "To Do - Urgent/Today",
    "Weekend Planner",
    "Weekly Planner",
    "Daily Planner",
    "Doing",
    "Ongoing recurring",
    "Blocked",
    "Done",
]


def suggest_section(task: Task, section_name_map: dict[str, str]) -> str | None:
    """Suggest the ideal section_id for a task based on priority and context.

    Returns section_id if a move is recommended, None otherwise.
    """
    current_section = task.section_name
    target_name = _ideal_section_name(task)

    if target_name and target_name != current_section:
        # Find matching section_id
        for sid, sname in section_name_map.items():
            if sname == target_name:
                return sid
    return None


def _ideal_section_name(task: Task) -> str | None:
    """Determine ideal section name based on task attributes."""
    # Blocked tasks stay in Blocked
    if "blocked" in task.labels:
        return "Blocked"

    if task.is_recurring:
        return "Ongoing recurring"

    # Tasks already in AI-managed planner sections — leave them alone
    if task.section_name in ("Daily Planner", "Weekly Planner", "Weekend Planner"):
        return None

    pri = task.priority  # 4=urgent, 3=high, 2=normal, 1=low

    # If task has an imminent due date, it should be in Urgent/Today
    if task.due_date:
        today = date.today()
        days_until = (task.due_date - today).days
        if days_until <= 0:
            return "To Do - Urgent/Today"
        if days_until <= 2 and pri >= 3:
            return "To Do - Urgent/Today"

    # Priority-based placement
    if pri == 4:
        return "To Do - Urgent/Today"
    if pri == 3:
        return "To Do - High"
    if pri == 2:
        return "To Do - Normal"
    if pri == 1:
        # P4 tasks without due date → Backlog or Low
        if task.due_date:
            return "To Do - Low"
        return "Backlog"
    return None


# -------------------------------------------------------------------------
# Analysis — read-only board intelligence
# -------------------------------------------------------------------------

@dataclass
class BoardAnalysis:
    """Read-only analysis of the Kanban board state."""

    total_tasks: int = 0
    tasks_per_section: dict[str, int] = field(default_factory=dict)
    priority_distribution: dict[int, int] = field(default_factory=dict)
    unlabeled_count: int = 0
    no_due_date_count: int = 0
    overdue_count: int = 0
    misplaced_tasks: list[dict] = field(default_factory=list)
    duplicate_candidates: list[list[str]] = field(default_factory=list)
    label_suggestions: list[dict] = field(default_factory=list)
    vague_tasks: list[str] = field(default_factory=list)
    urgent_overload: bool = False
    backlog_overload: bool = False
    summary: str = ""


def analyze_board(tasks: list[Task], section_map: dict[str, str]) -> BoardAnalysis:
    """Perform comprehensive analysis of a Kanban board."""
    analysis = BoardAnalysis()
    analysis.total_tasks = len(tasks)
    today = date.today()

    # Section counts
    sec_counts: dict[str, int] = Counter()
    for t in tasks:
        name = t.section_name or "(no section)"
        sec_counts[name] += 1
    analysis.tasks_per_section = dict(sec_counts)

    # Priority distribution
    pri_counts: dict[int, int] = Counter()
    for t in tasks:
        pri_counts[t.priority] += 1
    analysis.priority_distribution = dict(pri_counts)

    # Unlabeled / no due date / overdue
    for t in tasks:
        if not t.labels:
            analysis.unlabeled_count += 1
        if not t.due_date:
            analysis.no_due_date_count += 1
        elif t.due_date < today:
            analysis.overdue_count += 1

    # Misplaced tasks (priority doesn't match section)
    for t in tasks:
        ideal_sid = suggest_section(t, section_map)
        if ideal_sid and ideal_sid != t.section_id:
            ideal_name = section_map.get(ideal_sid, "?")
            analysis.misplaced_tasks.append({
                "task_id": t.id,
                "content": t.content,
                "current_section": t.section_name,
                "suggested_section": ideal_name,
                "priority": t.priority,
            })

    # Label suggestions
    for t in tasks:
        if not t.labels:
            suggestions = infer_labels(t)
            if suggestions:
                analysis.label_suggestions.append({
                    "task_id": t.id,
                    "content": t.content,
                    "suggested_labels": suggestions,
                })

    # Vague task names (very short, no context)
    for t in tasks:
        name = t.content.strip()
        if len(name) <= 12 and not t.description:
            analysis.vague_tasks.append(f"{name} (id={t.id})")

    # Duplicate detection (simple fuzzy: normalized name match)
    name_groups: dict[str, list[str]] = defaultdict(list)
    for t in tasks:
        key = _normalize_name(t.content)
        if key:
            name_groups[key].append(f"{t.content} (id={t.id})")
    analysis.duplicate_candidates = [
        ids for ids in name_groups.values() if len(ids) > 1
    ]

    # Overload flags
    urgent_count = sec_counts.get("To Do - Urgent/Today", 0)
    backlog_count = sec_counts.get("Backlog", 0)
    analysis.urgent_overload = urgent_count > 10
    analysis.backlog_overload = backlog_count > 50

    # Summary text
    analysis.summary = _build_summary(analysis)
    return analysis


def _normalize_name(name: str) -> str:
    """Normalize a task name for duplicate detection."""
    s = name.lower().strip()
    s = re.sub(r"[^a-záàâãéèêíïóôõúüç\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _build_summary(a: BoardAnalysis) -> str:
    """Generate a human-readable summary of the board analysis."""
    lines = [
        f"Board overview: {a.total_tasks} active tasks",
        "",
        "Tasks per section:",
    ]
    for sec, count in sorted(a.tasks_per_section.items(), key=lambda x: -x[1]):
        lines.append(f"  {sec}: {count}")

    lines.append("")
    lines.append("Priority distribution:")
    pri_labels = {4: "P1 (urgent)", 3: "P2 (high)", 2: "P3 (normal)", 1: "P4 (low)"}
    for pri in [4, 3, 2, 1]:
        count = a.priority_distribution.get(pri, 0)
        lines.append(f"  {pri_labels[pri]}: {count}")

    lines.append("")
    lines.append(f"Unlabeled tasks: {a.unlabeled_count}")
    lines.append(f"Tasks without due date: {a.no_due_date_count}")
    lines.append(f"Overdue tasks: {a.overdue_count}")

    if a.misplaced_tasks:
        lines.append(f"\nMisplaced tasks: {len(a.misplaced_tasks)}")
        for m in a.misplaced_tasks[:10]:
            lines.append(
                f"  '{m['content']}': {m['current_section']} -> {m['suggested_section']}"
            )
        if len(a.misplaced_tasks) > 10:
            lines.append(f"  ... and {len(a.misplaced_tasks) - 10} more")

    if a.duplicate_candidates:
        lines.append(f"\nPossible duplicates: {len(a.duplicate_candidates)} groups")
        for group in a.duplicate_candidates[:5]:
            lines.append(f"  {group}")

    if a.vague_tasks:
        lines.append(f"\nVague task names ({len(a.vague_tasks)}):")
        for v in a.vague_tasks[:10]:
            lines.append(f"  {v}")

    if a.label_suggestions:
        lines.append(f"\nLabel suggestions: {len(a.label_suggestions)} tasks")
        for s in a.label_suggestions[:10]:
            lines.append(
                f"  '{s['content']}' -> {', '.join(s['suggested_labels'])}"
            )

    issues = []
    if a.urgent_overload:
        issues.append("Urgent/Today section is overloaded")
    if a.backlog_overload:
        issues.append("Backlog is bloated — consider triaging")
    if a.unlabeled_count > 20:
        issues.append(f"{a.unlabeled_count} tasks need labels")

    if issues:
        lines.append("\nAction items:")
        for i in issues:
            lines.append(f"  - {i}")

    return "\n".join(lines)


# -------------------------------------------------------------------------
# Write — batch reorganization
# -------------------------------------------------------------------------

@dataclass
class ReorganizeResult:
    """Outcome of a board reorganization run."""

    tasks_moved: int = 0
    tasks_labeled: int = 0
    errors: list[str] = field(default_factory=list)
    moves: list[dict] = field(default_factory=list)
    label_updates: list[dict] = field(default_factory=list)


def reorganize_board(
    client: TodoistClient,
    settings: Settings,
    *,
    dry_run: bool = True,
    move_tasks: bool = True,
    auto_label: bool = True,
) -> ReorganizeResult:
    """Analyze and optionally reorganize the Kanban board.

    Args:
        client: Todoist API client.
        settings: App settings (needs todoist_kanban_project_id).
        dry_run: If True, only report what would change (no writes).
        move_tasks: Whether to move misplaced tasks.
        auto_label: Whether to apply inferred labels.

    Returns:
        ReorganizeResult with details of changes made (or planned).
    """
    project_id = settings.todoist_kanban_project_id
    if not project_id:
        return ReorganizeResult(errors=["TODOIST_KANBAN_PROJECT_ID not set."])

    result = ReorganizeResult()

    tasks = client.get_tasks(project_id=project_id)
    section_map = client.get_section_map(project_id)

    for task in tasks:
        # --- Section moves ---
        if move_tasks:
            ideal_sid = suggest_section(task, section_map)
            if ideal_sid and ideal_sid != task.section_id:
                move_info = {
                    "task_id": task.id,
                    "content": task.content,
                    "from_section": task.section_name,
                    "to_section": section_map.get(ideal_sid, "?"),
                    "to_section_id": ideal_sid,
                }
                result.moves.append(move_info)

                if not dry_run:
                    try:
                        client.move_task(task.id, ideal_sid)
                        result.tasks_moved += 1
                    except Exception as exc:
                        result.errors.append(
                            f"Failed to move '{task.content}': {exc}"
                        )

        # --- Auto-labeling ---
        if auto_label and not task.labels:
            suggestions = infer_labels(task)
            if suggestions:
                label_info = {
                    "task_id": task.id,
                    "content": task.content,
                    "labels": suggestions,
                }
                result.label_updates.append(label_info)

                if not dry_run:
                    try:
                        client.update_task(task.id, labels=suggestions)
                        result.tasks_labeled += 1
                    except Exception as exc:
                        result.errors.append(
                            f"Failed to label '{task.content}': {exc}"
                        )

    if dry_run:
        result.tasks_moved = len(result.moves)
        result.tasks_labeled = len(result.label_updates)

    return result


def format_reorganize_report(result: ReorganizeResult, dry_run: bool) -> str:
    """Format a human-readable report of the reorganization."""
    mode = "DRY RUN" if dry_run else "APPLIED"
    lines = [f"=== Reorganization Report ({mode}) ===", ""]

    if result.moves:
        lines.append(f"Section moves: {result.tasks_moved}")
        for m in result.moves[:20]:
            lines.append(
                f"  '{m['content']}': {m['from_section']} -> {m['to_section']}"
            )
        if len(result.moves) > 20:
            lines.append(f"  ... and {len(result.moves) - 20} more")
        lines.append("")

    if result.label_updates:
        lines.append(f"Label updates: {result.tasks_labeled}")
        for l in result.label_updates[:20]:
            lines.append(f"  '{l['content']}' -> {', '.join(l['labels'])}")
        if len(result.label_updates) > 20:
            lines.append(f"  ... and {len(result.label_updates) - 20} more")
        lines.append("")

    if result.errors:
        lines.append(f"Errors: {len(result.errors)}")
        for e in result.errors:
            lines.append(f"  {e}")

    if not result.moves and not result.label_updates:
        lines.append("Board is already well-organized. No changes needed.")

    return "\n".join(lines)


# -------------------------------------------------------------------------
# LLM-powered board intelligence
# -------------------------------------------------------------------------

_BOARD_ANALYSIS_PROMPT = """\
You are an expert productivity coach and Kanban board consultant.
You are analyzing a personal task board ("Kanban Casa") for a knowledge worker
managing a high volume of tasks. The tasks are in Portuguese.

RULES:
- NEVER suggest deleting any task. Tasks must only be moved, relabeled, or reprioritized.
- Be specific and actionable. Name the exact tasks.
- Focus on what matters most THIS WEEK.
- Identify the top 5 most important tasks the person should tackle first.
- Identify tasks that are clearly misplaced (wrong section for their priority/urgency).
- Spot vague tasks that need clarification before they can be acted on.
- Detect patterns: recurring procrastination, category clusters, blocked dependencies.
- Suggest a realistic daily plan (3-5 tasks per day maximum).
- Output in English, but keep task names in their original Portuguese.
"""


def llm_analyze_board(
    analysis: BoardAnalysis,
    tasks: list[Task],
    settings: Settings,
) -> str:
    """Use the best available LLM to generate intelligent board insights.

    Returns LLM analysis text, or empty string if no LLM available.
    """
    from brain.llm import call_llm

    # Build a compact task summary for the LLM (avoid sending 200 full tasks)
    task_lines = []
    for t in tasks:
        pri_label = {4: "P1", 3: "P2", 2: "P3", 1: "P4"}.get(t.priority, "P4")
        due_str = str(t.due_date) if t.due_date else "no date"
        labels_str = ", ".join(t.labels) if t.labels else "unlabeled"
        sec = t.section_name or "no section"
        task_lines.append(
            f"- [{pri_label}] {t.content} | section: {sec} | due: {due_str} | labels: {labels_str}"
        )

    board_data = (
        f"{analysis.summary}\n\n"
        f"=== ALL TASKS ===\n"
        + "\n".join(task_lines)
    )

    result = call_llm(
        settings,
        board_data,
        _BOARD_ANALYSIS_PROMPT,
        prefer_quality=True,
    )
    return result or ""
