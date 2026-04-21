#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
# VelaFlow — Post-Install Health Check
#
# Validates that every component of the VelaFlow stack is operational.
# Run inside the LXC container after install.sh:
#   bash scripts/health-check.sh
#
# Or from the Proxmox host:
#   pct exec <CTID> -- bash /opt/brain/scripts/health-check.sh
#
# Exit codes:
#   0 — All checks passed
#   1 — One or more critical checks failed
#   2 — Warnings only (non-critical failures)
# ═══════════════════════════════════════════════════════════════════════
set -uo pipefail

BRAIN_USER="${BRAIN_USER:-brain}"
BRAIN_HOME="${BRAIN_HOME:-/opt/brain}"
VENV="${BRAIN_HOME}/venv"
ENV_FILE="${BRAIN_CONFIG:-/etc/brain}/secrets.env"
NLM_HOME="${BRAIN_HOME}/.notebooklm"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BOLD='\033[1m'; NC='\033[0m'

PASS=0; FAIL=0; WARN=0; TOTAL=0

check() {
    local label="$1"; shift
    TOTAL=$((TOTAL + 1))
    if "$@" >/dev/null 2>&1; then
        echo -e "  ${GREEN}✓${NC} ${label}"
        PASS=$((PASS + 1))
        return 0
    else
        echo -e "  ${RED}✗${NC} ${label}"
        FAIL=$((FAIL + 1))
        return 1
    fi
}

check_warn() {
    local label="$1"; shift
    TOTAL=$((TOTAL + 1))
    if "$@" >/dev/null 2>&1; then
        echo -e "  ${GREEN}✓${NC} ${label}"
        PASS=$((PASS + 1))
        return 0
    else
        echo -e "  ${YELLOW}⚠${NC} ${label} (non-critical)"
        WARN=$((WARN + 1))
        return 0
    fi
}

echo ""
echo -e "${BOLD}═══════════════════════════════════════════════════════════${NC}"
echo -e "${BOLD}  VelaFlow — Post-Install Health Check${NC}"
echo -e "${BOLD}═══════════════════════════════════════════════════════════${NC}"
echo ""

# ── 1. System Prerequisites ──────────────────────────────────────────
echo -e "${BOLD}[1/8] System Prerequisites${NC}"

check "Python 3.11+ installed" \
    python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)"

check "brain user exists" \
    id "${BRAIN_USER}"

check "Brain home directory exists" \
    test -d "${BRAIN_HOME}"

check "Virtual environment exists" \
    test -f "${VENV}/bin/python"

check "Secrets file exists" \
    test -f "${ENV_FILE}"

check "Secrets file permissions (600)" \
    bash -c "[[ \$(stat -c '%a' '${ENV_FILE}' 2>/dev/null || stat -f '%Lp' '${ENV_FILE}' 2>/dev/null) == '600' ]]"

# ── 2. Python Environment ────────────────────────────────────────────
echo ""
echo -e "${BOLD}[2/8] Python Environment${NC}"

check "brain package importable" \
    sudo -u "${BRAIN_USER}" "${VENV}/bin/python" -c "import brain"

check "brain CLI responds" \
    sudo -u "${BRAIN_USER}" "${VENV}/bin/brain" --help

check "FastAPI importable" \
    sudo -u "${BRAIN_USER}" "${VENV}/bin/python" -c "from brain.api.app import create_app"

check "Worker importable" \
    sudo -u "${BRAIN_USER}" "${VENV}/bin/python" -c "from brain.queue.worker import QueueWorker"

check "RAG pipeline importable" \
    sudo -u "${BRAIN_USER}" "${VENV}/bin/python" -c "from brain.rag import RAGPipeline"

check "Audit log importable" \
    sudo -u "${BRAIN_USER}" "${VENV}/bin/python" -c "from brain.security.audit_log import AuditLog"

check "Demo manager importable" \
    sudo -u "${BRAIN_USER}" "${VENV}/bin/python" -c "from brain.tenant.demo_manager import DemoManager"

check "Local LLM client importable" \
    sudo -u "${BRAIN_USER}" "${VENV}/bin/python" -c "from brain.llm_local import LocalLLMClient"

# ── 3. Services ──────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}[3/8] System Services${NC}"

check_warn "Redis is running" \
    systemctl is-active redis-server

check "brain-daily.timer enabled" \
    systemctl is-enabled brain-daily.timer

check "brain-weekly.timer enabled" \
    systemctl is-enabled brain-weekly.timer

check "brain-weekend.timer enabled" \
    systemctl is-enabled brain-weekend.timer

check "brain-sync.timer enabled" \
    systemctl is-enabled brain-sync.timer

check_warn "brain-notebooklm.timer installed" \
    test -f /etc/systemd/system/brain-notebooklm.timer

# ── 4. Secrets Configuration ────────────────────────────────────────
echo ""
echo -e "${BOLD}[4/8] Secrets Configuration${NC}"

_env_has() {
    grep -qE "^${1}=.+" "${ENV_FILE}" 2>/dev/null
}

check "LITELLM_PROXY_URL configured" \
    _env_has "LITELLM_PROXY_URL"

check "LITELLM_PROXY_TOKEN configured" \
    _env_has "LITELLM_PROXY_TOKEN"

check "TODOIST_API_TOKEN configured" \
    _env_has "TODOIST_API_TOKEN"

check_warn "NOTION_API_TOKEN configured" \
    _env_has "NOTION_API_TOKEN"

check_warn "SMTP_HOST configured" \
    _env_has "SMTP_HOST"

# ── 5. Network Connectivity ─────────────────────────────────────────
echo ""
echo -e "${BOLD}[5/8] Network Connectivity${NC}"

check "DNS resolution works" \
    bash -c "getent hosts google.com"

check "HTTPS outbound works" \
    curl -sf --max-time 10 -o /dev/null https://api.todoist.com

# Test LiteLLM proxy reachability
PROXY_URL=$(grep -E '^LITELLM_PROXY_URL=' "${ENV_FILE}" 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'")
if [[ -n "${PROXY_URL}" ]]; then
    check "LiteLLM proxy reachable (${PROXY_URL})" \
        curl -sf --max-time 10 -o /dev/null "${PROXY_URL}/health"
else
    echo -e "  ${YELLOW}⚠${NC} LiteLLM proxy URL not set — skipping connectivity check"
    WARN=$((WARN + 1))
    TOTAL=$((TOTAL + 1))
fi

# ── 6. Data Directories ─────────────────────────────────────────────
echo ""
echo -e "${BOLD}[6/8] Data & Storage${NC}"

check "Data directory exists" \
    test -d "${BRAIN_HOME}"

check "Log directory exists" \
    test -d "/var/log/brain"

check "brain user can write data" \
    sudo -u "${BRAIN_USER}" bash -c "touch '${BRAIN_HOME}/.health-check-test' && rm '${BRAIN_HOME}/.health-check-test'"

check "brain user can write logs" \
    sudo -u "${BRAIN_USER}" bash -c "touch '/var/log/brain/.health-check-test' && rm '/var/log/brain/.health-check-test'"

# ── 7. NotebookLM (Optional) ────────────────────────────────────────
echo ""
echo -e "${BOLD}[7/8] NotebookLM Integration (Optional)${NC}"

check_warn "notebooklm CLI installed" \
    test -f "${VENV}/bin/notebooklm"

check_warn "Playwright Chromium installed" \
    sudo -u "${BRAIN_USER}" PLAYWRIGHT_BROWSERS_PATH="${BRAIN_HOME}/.playwright" \
    "${VENV}/bin/python" -c "from playwright.sync_api import sync_playwright; p = sync_playwright().start(); b = p.chromium.launch(headless=True); b.close(); p.stop()"

NLM_COOKIE="${NLM_HOME}/storage_state.json"
if [[ -f "${NLM_COOKIE}" ]]; then
    check "NotebookLM auth cookie present" \
        test -f "${NLM_COOKIE}"
    check "NotebookLM cookie is valid JSON" \
        sudo -u "${BRAIN_USER}" "${VENV}/bin/python" -c "import json; json.load(open('${NLM_COOKIE}'))"
else
    echo -e "  ${YELLOW}⚠${NC} NotebookLM auth cookie not found (non-critical)"
    echo -e "      Run: bash scripts/notebooklm-lxc-login.sh"
    echo -e "      Or:  .\\scripts\\notebooklm-push-auth.ps1 -ProxmoxHost <ip>"
    WARN=$((WARN + 1))
    TOTAL=$((TOTAL + 1))
fi

# ── 8. Functional Smoke Test ────────────────────────────────────────
echo ""
echo -e "${BOLD}[8/8] Functional Smoke Test${NC}"

check "Settings load from environment" \
    sudo -u "${BRAIN_USER}" "${VENV}/bin/python" -c "
import os; os.environ.setdefault('TODOIST_API_TOKEN', 'test')
from brain.config import Settings; Settings.from_env()
"

check "FastAPI app creates successfully" \
    sudo -u "${BRAIN_USER}" "${VENV}/bin/python" -c "
import os; os.environ.setdefault('TODOIST_API_TOKEN', 'test')
os.environ.setdefault('VELAFLOW_MASTER_KEY', '$(openssl rand -base64 32)')
os.environ.setdefault('JWT_SECRET', 'test-health-check')
from brain.api.app import create_app; app = create_app()
assert '/api/v1' in str([r.path for r in app.routes])
"

check "PII detector loads" \
    sudo -u "${BRAIN_USER}" "${VENV}/bin/python" -c "
from brain.security.pii import PIIDetector; d = PIIDetector(); assert d.scan('hello') == []
"

check "Encryption round-trip works" \
    sudo -u "${BRAIN_USER}" "${VENV}/bin/python" -c "
from brain.security.encryption import FieldEncryptor
e = FieldEncryptor(b'healthcheck_key_32_bytes_long!!!!')
assert e.decrypt(e.encrypt('test')) == 'test'
"

check "Task scoring algorithm works" \
    sudo -u "${BRAIN_USER}" "${VENV}/bin/python" -c "
from brain.planner import score_task
from brain.models import Task
t = Task(id='1', content='Test', project_name='Inbox', priority=4, labels=[], due_date=None, is_recurring=False, created_at='2026-01-01T00:00:00Z', order=1, section_name=None)
s = score_task(t)
assert s.score > 0
"

# ── Summary ──────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}═══════════════════════════════════════════════════════════${NC}"

if [[ ${FAIL} -eq 0 && ${WARN} -eq 0 ]]; then
    echo -e "${GREEN}  ALL ${TOTAL} CHECKS PASSED${NC}"
    EXIT_CODE=0
elif [[ ${FAIL} -eq 0 ]]; then
    echo -e "${GREEN}  ${PASS}/${TOTAL} passed${NC}, ${YELLOW}${WARN} warnings${NC}"
    echo -e "  ${YELLOW}Warnings are non-critical — system is functional.${NC}"
    EXIT_CODE=2
else
    echo -e "${RED}  ${FAIL} FAILED${NC}, ${GREEN}${PASS} passed${NC}, ${YELLOW}${WARN} warnings${NC}"
    echo -e "  ${RED}Fix the failed checks before using the system.${NC}"
    EXIT_CODE=1
fi

echo -e "${BOLD}═══════════════════════════════════════════════════════════${NC}"
echo ""
exit ${EXIT_CODE}
