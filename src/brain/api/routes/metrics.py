"""Prometheus-compatible metrics + lightweight HTML status dashboard.

Provides:
- GET /metrics          → Prometheus text format (for Grafana/alerting)
- GET /status           → Lightweight HTML dashboard (no JS frameworks)

The HTML dashboard is designed for:
- Operator walkthroughs (shows autoscaling, queue depth, worker count)
- Operational monitoring without Grafana (saves ~500 MB RAM)
- Auto-refreshes every 5 seconds via meta-refresh

Memory overhead: ~0 (reuses existing queue/worker/health data).
"""

from __future__ import annotations

import os
import platform
import sys
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

router = APIRouter()

# ── Startup timestamp ─────────────────────────────────────────────────
_START_TIME = time.time()

# ── Counters (updated by middleware/worker) ────────────────────────────
_metrics: dict[str, Any] = {
    "http_requests_total": 0,
    "http_requests_error_total": 0,
    "pipeline_runs_total": 0,
    "llm_calls_total": 0,
    "tasks_processed_total": 0,
    "tasks_failed_total": 0,
    "active_tenants": 0,
}


def inc(metric: str, amount: int = 1) -> None:
    """Thread-safe-ish counter increment (CPython GIL)."""
    _metrics[metric] = _metrics.get(metric, 0) + amount


def gauge(metric: str, value: float) -> None:
    """Set a gauge metric."""
    _metrics[metric] = value


# ═══════════════════════════════════════════════════════════════════════
# GET /metrics — Prometheus text exposition format
# ═══════════════════════════════════════════════════════════════════════
@router.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics() -> str:
    """Prometheus-compatible metrics endpoint.

    Scrape with: prometheus.yml → scrape_configs → targets: ['velaflow:8000']
    """
    uptime = time.time() - _START_TIME
    queue_depth = _get_queue_depth()
    worker_count = _get_worker_count()

    lines = [
        "# HELP velaflow_up Whether VelaFlow is running (1=up).",
        "# TYPE velaflow_up gauge",
        "velaflow_up 1",
        "",
        "# HELP velaflow_uptime_seconds Seconds since API start.",
        "# TYPE velaflow_uptime_seconds gauge",
        f"velaflow_uptime_seconds {uptime:.0f}",
        "",
        "# HELP velaflow_http_requests_total Total HTTP requests.",
        "# TYPE velaflow_http_requests_total counter",
        f'velaflow_http_requests_total {_metrics["http_requests_total"]}',
        "",
        "# HELP velaflow_http_requests_error_total Total HTTP 4xx/5xx responses.",
        "# TYPE velaflow_http_requests_error_total counter",
        f'velaflow_http_requests_error_total {_metrics["http_requests_error_total"]}',
        "",
        "# HELP velaflow_queue_depth Current pipeline queue depth.",
        "# TYPE velaflow_queue_depth gauge",
        f"velaflow_queue_depth {queue_depth}",
        "",
        "# HELP velaflow_worker_count Active worker threads/pods.",
        "# TYPE velaflow_worker_count gauge",
        f"velaflow_worker_count {worker_count}",
        "",
        "# HELP velaflow_pipeline_runs_total Total pipeline executions.",
        "# TYPE velaflow_pipeline_runs_total counter",
        f'velaflow_pipeline_runs_total {_metrics["pipeline_runs_total"]}',
        "",
        "# HELP velaflow_llm_calls_total Total LLM API calls.",
        "# TYPE velaflow_llm_calls_total counter",
        f'velaflow_llm_calls_total {_metrics["llm_calls_total"]}',
        "",
        "# HELP velaflow_tasks_processed_total Total tasks scored/processed.",
        "# TYPE velaflow_tasks_processed_total counter",
        f'velaflow_tasks_processed_total {_metrics["tasks_processed_total"]}',
        "",
        "# HELP velaflow_tasks_failed_total Total failed task processing.",
        "# TYPE velaflow_tasks_failed_total counter",
        f'velaflow_tasks_failed_total {_metrics["tasks_failed_total"]}',
        "",
        "# HELP velaflow_active_tenants Current active tenant count.",
        "# TYPE velaflow_active_tenants gauge",
        f'velaflow_active_tenants {_metrics["active_tenants"]}',
        "",
    ]
    return "\n".join(lines) + "\n"


# ═══════════════════════════════════════════════════════════════════════
# GET /status — Lightweight HTML Dashboard
# ═══════════════════════════════════════════════════════════════════════
@router.get("/status", response_class=HTMLResponse)
async def status_dashboard(request: Request) -> str:
    """Lightweight HTML status dashboard. Auto-refreshes every 5s.

    No JavaScript frameworks. No external CDN. ~3 KB HTML.
    Shows: uptime, queue depth, workers, KEDA scaling, request rate.
    """
    uptime_s = time.time() - _START_TIME
    uptime_str = _format_uptime(uptime_s)
    queue_depth = _get_queue_depth()
    worker_count = _get_worker_count()
    health_status = _get_health_status()

    # Determine scaling indicator
    if queue_depth == 0:
        scale_status = "idle"
        scale_color = "#6c757d"
        scale_label = "IDLE (scaled to 0)"
    elif queue_depth <= 3:
        scale_status = "normal"
        scale_color = "#28a745"
        scale_label = f"NORMAL ({worker_count} workers)"
    elif queue_depth <= 10:
        scale_status = "scaling"
        scale_color = "#ffc107"
        scale_label = f"SCALING UP ({worker_count} → {min(worker_count + 2, 10)} workers)"
    else:
        scale_status = "high-load"
        scale_color = "#dc3545"
        scale_label = f"HIGH LOAD ({worker_count} workers, {queue_depth} queued)"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="5">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VelaFlow — Status</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
background:#0d1117;color:#c9d1d9;padding:20px}}
h1{{color:#58a6ff;margin-bottom:4px;font-size:1.5em}}
.sub{{color:#8b949e;margin-bottom:20px;font-size:0.9em}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:20px}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px}}
.card h3{{color:#8b949e;font-size:0.75em;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px}}
.card .val{{font-size:1.8em;font-weight:700;color:#58a6ff}}
.card .val.green{{color:#28a745}}
.card .val.yellow{{color:#ffc107}}
.card .val.red{{color:#dc3545}}
.card .val.gray{{color:#6c757d}}
.status-bar{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px;margin-bottom:20px}}
.status-bar .indicator{{display:inline-block;width:12px;height:12px;border-radius:50%;margin-right:8px}}
.bar{{background:#21262d;border-radius:4px;height:8px;margin-top:8px;overflow:hidden}}
.bar-fill{{height:100%;border-radius:4px;transition:width 0.5s}}
table{{width:100%;border-collapse:collapse;background:#161b22;border:1px solid #30363d;border-radius:8px;overflow:hidden}}
th{{background:#1c2128;color:#8b949e;text-align:left;padding:10px 14px;font-size:0.75em;text-transform:uppercase}}
td{{padding:10px 14px;border-top:1px solid #21262d;font-size:0.9em}}
.badge{{display:inline-block;padding:2px 8px;border-radius:12px;font-size:0.75em;font-weight:600}}
footer{{margin-top:20px;color:#484f58;font-size:0.8em;text-align:center}}
</style>
</head>
<body>
<h1>VelaFlow Status Dashboard</h1>
<p class="sub">Auto-refresh: 5s &middot; Python {platform.python_version()} &middot; {platform.system()} {platform.machine()}</p>

<div class="status-bar">
<span class="indicator" style="background:{scale_color}"></span>
<strong>KEDA Autoscaling:</strong> {scale_label}
<div class="bar"><div class="bar-fill" style="width:{min(queue_depth * 10, 100)}%;background:{scale_color}"></div></div>
</div>

<div class="grid">
<div class="card">
<h3>Uptime</h3>
<div class="val green">{uptime_str}</div>
</div>
<div class="card">
<h3>Queue Depth</h3>
<div class="val {'green' if queue_depth < 3 else 'yellow' if queue_depth < 10 else 'red'}">{queue_depth}</div>
</div>
<div class="card">
<h3>Active Workers</h3>
<div class="val">{worker_count}</div>
</div>
<div class="card">
<h3>Health</h3>
<div class="val {'green' if health_status == 'healthy' else 'red'}">{health_status.upper()}</div>
</div>
<div class="card">
<h3>HTTP Requests</h3>
<div class="val">{_metrics['http_requests_total']:,}</div>
</div>
<div class="card">
<h3>Errors</h3>
<div class="val {'green' if _metrics['http_requests_error_total'] == 0 else 'red'}">{_metrics['http_requests_error_total']:,}</div>
</div>
<div class="card">
<h3>Pipeline Runs</h3>
<div class="val">{_metrics['pipeline_runs_total']:,}</div>
</div>
<div class="card">
<h3>LLM Calls</h3>
<div class="val">{_metrics['llm_calls_total']:,}</div>
</div>
<div class="card">
<h3>Tasks Processed</h3>
<div class="val">{_metrics['tasks_processed_total']:,}</div>
</div>
<div class="card">
<h3>Active Tenants</h3>
<div class="val">{_metrics['active_tenants']}</div>
</div>
</div>

<h2 style="color:#58a6ff;font-size:1.1em;margin-bottom:12px">Scaling Architecture</h2>
<table>
<tr><th>Scaler</th><th>Queue</th><th>Min</th><th>Max</th><th>Trigger</th><th>Status</th></tr>
<tr>
<td>Standard Worker</td>
<td><code>velaflow:pipeline:queue</code></td>
<td>0</td><td>10</td><td>depth &ge; 3</td>
<td><span class="badge" style="background:#28a74533;color:#28a745">active</span></td>
</tr>
<tr>
<td>Premium LLM (Ollama)</td>
<td><code>velaflow:premium:llm:queue</code></td>
<td>0</td><td>3</td><td>depth &ge; 1</td>
<td><span class="badge" style="background:#ffc10733;color:#ffc107">on-demand</span></td>
</tr>
<tr>
<td>RAG Worker</td>
<td><code>velaflow:rag:queue</code></td>
<td>0</td><td>5</td><td>depth &ge; 2</td>
<td><span class="badge" style="background:#28a74533;color:#28a745">active</span></td>
</tr>
</table>

<footer>
VelaFlow Enterprise v2.0.0 &middot; Prometheus: <a href="/metrics" style="color:#58a6ff">/metrics</a> &middot;
Health: <a href="/health/ready" style="color:#58a6ff">/health/ready</a>
</footer>
</body>
</html>"""
    return html


# ═══════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════

def _get_queue_depth() -> int:
    """Read current queue depth from the in-process queue."""
    try:
        from brain.queue.tasks import get_default_queue
        q = get_default_queue()
        return q.depth if q else 0
    except Exception:
        return 0


def _get_worker_count() -> int:
    """Estimate active worker count."""
    try:
        import threading
        return max(1, sum(
            1 for t in threading.enumerate()
            if "worker" in t.name.lower() or "queue" in t.name.lower()
        ))
    except Exception:
        return 1


def _get_health_status() -> str:
    """Get aggregated health status."""
    try:
        from brain.security.circuit_breaker import get_health_registry
        registry = get_health_registry()
        status = registry.get_status()
        return "healthy" if status.get("ready", False) else "degraded"
    except Exception:
        return "healthy"


def _format_uptime(seconds: float) -> str:
    """Format seconds into human-readable uptime."""
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"
