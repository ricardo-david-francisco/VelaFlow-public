#!/usr/bin/env bash
# =============================================================================
# NotebookLM LXC Authentication
#
# Run INSIDE the LXC container as root to complete the one-time Google login.
# Uses a virtual framebuffer (Xvfb) + VNC server so you can see and interact
# with the Chromium browser from any VNC viewer on your desktop.
#
# Usage (from Proxmox host):
#   pct exec 200 -- bash /opt/brain/scripts/notebooklm-lxc-login.sh
#
# After running, connect via VNC from your desktop:
#   vnc://<lxc-ip>:5900
# Then complete the Google sign-in and press ENTER in this terminal.
# =============================================================================
set -euo pipefail

BRAIN_USER="${BRAIN_USER:-brain}"
BRAIN_HOME="${BRAIN_HOME:-/opt/brain}"
VENV="${BRAIN_HOME}/venv"
NLM_HOME="${BRAIN_HOME}/.notebooklm"
DISPLAY_NUM=":99"
VNC_PORT=5900

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'
info()  { echo -e "${GREEN}[notebooklm-auth]${NC} $*"; }
warn()  { echo -e "${YELLOW}[notebooklm-auth]${NC} $*"; }
die()   { echo -e "${RED}[notebooklm-auth] ERROR:${NC} $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root: sudo bash scripts/notebooklm-lxc-login.sh"
[[ -f "${VENV}/bin/notebooklm" ]] || die \
  "notebooklm CLI not found at ${VENV}/bin/notebooklm\n  Run: bash scripts/install.sh first"

# ── Install Xvfb + x11vnc if not present ──────────────────────────────────────
PKGS_NEEDED=()
command -v Xvfb   &>/dev/null || PKGS_NEEDED+=(xvfb)
command -v x11vnc &>/dev/null || PKGS_NEEDED+=(x11vnc)

if [[ ${#PKGS_NEEDED[@]} -gt 0 ]]; then
    info "Installing display packages: ${PKGS_NEEDED[*]}"
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${PKGS_NEEDED[@]}"
fi

# ── Prepare NLM home directory ─────────────────────────────────────────────────
mkdir -p "${NLM_HOME}"
chown "${BRAIN_USER}:${BRAIN_USER}" "${NLM_HOME}"
chmod 700 "${NLM_HOME}"

# ── Clean up any stale virtual display ────────────────────────────────────────
pkill -f "Xvfb ${DISPLAY_NUM}" 2>/dev/null || true
pkill x11vnc 2>/dev/null || true
rm -f "/tmp/.X99-lock" 2>/dev/null || true
sleep 1

# ── Start virtual framebuffer ─────────────────────────────────────────────────
info "Starting virtual display ${DISPLAY_NUM}..."
Xvfb "${DISPLAY_NUM}" -screen 0 1280x800x24 -nolisten tcp &
XVFB_PID=$!
sleep 2

# Verify Xvfb started
kill -0 "${XVFB_PID}" 2>/dev/null || die "Xvfb failed to start"

# ── Start VNC server ──────────────────────────────────────────────────────────
info "Starting VNC server on port ${VNC_PORT}..."
x11vnc -display "${DISPLAY_NUM}" \
       -nopw \
       -listen 0.0.0.0 \
       -xkb \
       -forever \
       -bg \
       -quiet \
       -logfile /tmp/x11vnc.log

# ── Print connection instructions ─────────────────────────────────────────────
LXC_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "<lxc-ip>")

echo ""
echo -e "${BOLD}══════════════════════════════════════════════════════════${NC}"
echo -e "${BOLD}  NotebookLM — One-Time Google Login${NC}"
echo -e "${BOLD}══════════════════════════════════════════════════════════${NC}"
echo ""
echo -e "  ${YELLOW}Step 1${NC} — Open a VNC viewer on your desktop and connect to:"
echo ""
echo -e "            ${BOLD}${LXC_IP}:${VNC_PORT}${NC}"
echo ""
echo "  Recommended VNC clients:"
echo "    Windows : RealVNC Viewer, TigerVNC"
echo "    Mac     : Built-in Screen Sharing, RealVNC"
echo "    Linux   : Remmina, TigerVNC"
echo ""
echo -e "  ${YELLOW}Step 2${NC} — The Chromium browser will open automatically."
echo "           Sign in with the Google account you use for NotebookLM."
echo ""
echo -e "  ${YELLOW}Step 3${NC} — Wait until the NotebookLM homepage loads."
echo ""
echo -e "  ${YELLOW}Step 4${NC} — Press ENTER HERE to save cookies and exit."
echo ""
echo -e "${BOLD}══════════════════════════════════════════════════════════${NC}"
echo ""

# ── Run notebooklm login inside the virtual display ───────────────────────────
NOTEBOOKLM_HOME="${NLM_HOME}" DISPLAY="${DISPLAY_NUM}" \
  sudo -u "${BRAIN_USER}" \
  NOTEBOOKLM_HOME="${NLM_HOME}" \
  DISPLAY="${DISPLAY_NUM}" \
  "${VENV}/bin/notebooklm" login

LOGIN_EXIT=$?

# ── Clean up virtual display ──────────────────────────────────────────────────
info "Stopping virtual display and VNC server..."
pkill x11vnc 2>/dev/null || true
kill "${XVFB_PID}" 2>/dev/null || true
rm -f "/tmp/.X99-lock" 2>/dev/null || true

if [[ ${LOGIN_EXIT} -eq 0 ]]; then
    chmod 600 "${NLM_HOME}/storage_state.json" 2>/dev/null || true
    echo ""
    echo -e "${GREEN}Authentication complete.${NC}"
    echo "  Cookie: ${NLM_HOME}/storage_state.json"
    echo ""
    info "Run first sync:"
    echo "  sudo -u ${BRAIN_USER} NOTEBOOKLM_HOME=${NLM_HOME} ${VENV}/bin/brain notebooklm-sync --stdout"
    echo ""
    info "Enable weekly timer:"
    echo "  systemctl enable --now brain-notebooklm.timer"
else
    die "Login failed (exit ${LOGIN_EXIT}). Check /tmp/x11vnc.log for display errors."
fi
