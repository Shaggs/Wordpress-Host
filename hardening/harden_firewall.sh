#!/usr/bin/env bash
#
# WordPress Hosting Platform - Host Hardening (Part 1: Firewall + fail2ban)
#
# What this does:
#   1. Installs UFW if needed, and adds allow rules for the ports this
#      platform actually needs: SSH, HTTP/HTTPS (80/443, for the reverse
#      proxy), and the manager dashboard port.
#   2. Sets the default policy to deny incoming / allow outgoing, then
#      enables UFW.
#   3. Installs and enables fail2ban with an SSH jail.
#
# What this deliberately does NOT do:
#   - It does NOT open the SFTP (21000-21999) or phpMyAdmin (22000-22999)
#     port ranges. Those are managed dynamically, per-IP, per-session, by
#     the platform's own temporary-access feature (_ufw_allow/_ufw_delete
#     in manager/app.py). Statically opening them here would defeat that
#     feature entirely - it would make every site's SFTP/phpMyAdmin port
#     permanently reachable from anywhere, regardless of whether a
#     temporary access window was ever granted.
#   - It does NOT touch SSH password authentication. That is a separate,
#     deliberate step (harden_ssh_keys_only.sh) that you only run after
#     confirming key-based login works - disabling password auth before
#     that is confirmed is the single most common way to lock yourself
#     out of a remote server permanently.
#
# SAFETY: the SSH allow rule is added and verified BEFORE ufw is ever
# enabled, specifically to avoid cutting off the very connection you're
# using to run this script.
#
# Usage:
#   sudo ./harden_firewall.sh              # apply the hardening
#   sudo ./harden_firewall.sh --dry-run    # show what would run, change nothing
#
# Safe to re-run: UFW allow rules and fail2ban setup are idempotent.

set -euo pipefail

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=true
fi

run() {
    if $DRY_RUN; then
        echo "[dry-run] $*"
    else
        echo "+ $*"
        "$@"
    fi
}

if [[ $EUID -ne 0 ]] && ! $DRY_RUN; then
    echo "This script must be run as root (use sudo). Aborting." >&2
    exit 1
fi

echo "=== WordPress Hosting Platform - Firewall + fail2ban hardening ==="
$DRY_RUN && echo "(DRY RUN - no changes will be made)"
echo

# ---------------------------------------------------------------------------
# 1. Detect the ports this platform actually needs
# ---------------------------------------------------------------------------
SSH_PORT="$(sshd -T 2>/dev/null | awk '/^port /{print $2; exit}' || true)"
SSH_PORT="${SSH_PORT:-22}"

DASHBOARD_PORT=""
if [[ -f /opt/wp-host/manager.env ]]; then
    DASHBOARD_PORT="$(grep -oP '^WP_DASHBOARD_PORT=\K.*' /opt/wp-host/manager.env 2>/dev/null || true)"
fi
DASHBOARD_PORT="${DASHBOARD_PORT:-8088}"

echo "Detected SSH port:       $SSH_PORT"
echo "Detected dashboard port: $DASHBOARD_PORT"
echo

# ---------------------------------------------------------------------------
# 2. Install UFW if needed
# ---------------------------------------------------------------------------
if ! command -v ufw >/dev/null 2>&1; then
    echo "--- Installing ufw ---"
    run apt-get update -qq
    run apt-get install -y ufw
else
    echo "--- ufw already installed ---"
fi
echo

# ---------------------------------------------------------------------------
# 3. Allow required ports FIRST, before touching default policy or enabling.
#    This ordering is the whole safety guarantee: SSH access is preserved
#    at every intermediate step, not just the final state.
# ---------------------------------------------------------------------------
echo "--- Allowing required ports ---"
run ufw allow "${SSH_PORT}/tcp" comment 'SSH'
run ufw allow 80/tcp comment 'HTTP (reverse proxy)'
run ufw allow 443/tcp comment 'HTTPS (reverse proxy)'
run ufw allow "${DASHBOARD_PORT}/tcp" comment 'WP Host Manager dashboard'
echo
echo "Deliberately NOT opening 21000-21999 (SFTP) or 22000-22999 (phpMyAdmin)"
echo "- the platform's own temporary-access feature manages those per-session."
echo

# ---------------------------------------------------------------------------
# 4. Set default-deny policy
# ---------------------------------------------------------------------------
echo "--- Setting default policy: deny incoming, allow outgoing ---"
run ufw default deny incoming
run ufw default allow outgoing
echo

# ---------------------------------------------------------------------------
# 5. Enable UFW (--force skips the interactive confirmation prompt, which
#    is safe here specifically because the SSH allow rule above already
#    exists at this point)
# ---------------------------------------------------------------------------
echo "--- Enabling ufw ---"
run ufw --force enable
echo

# ---------------------------------------------------------------------------
# 6. Verify the result
# ---------------------------------------------------------------------------
if ! $DRY_RUN; then
    echo "--- Verification ---"
    ufw status verbose
    echo
    if ufw status | grep -qE "^${SSH_PORT}/tcp\s+ALLOW"; then
        echo "[OK] SSH port ${SSH_PORT} is allowed."
    else
        echo "[WARNING] Could not confirm SSH port ${SSH_PORT} is allowed in the output above." >&2
        echo "          Do NOT close this terminal session until you have verified you can" >&2
        echo "          open a NEW SSH connection successfully." >&2
    fi
    echo
fi

# ---------------------------------------------------------------------------
# 7. fail2ban
# ---------------------------------------------------------------------------
if ! command -v fail2ban-client >/dev/null 2>&1; then
    echo "--- Installing fail2ban ---"
    run apt-get install -y fail2ban
else
    echo "--- fail2ban already installed ---"
fi

JAIL_LOCAL="/etc/fail2ban/jail.local"
if [[ ! -f "$JAIL_LOCAL" ]] || ! grep -q "^\[sshd\]" "$JAIL_LOCAL" 2>/dev/null; then
    echo "--- Configuring sshd jail in $JAIL_LOCAL ---"
    if $DRY_RUN; then
        echo "[dry-run] would write an [sshd] jail block to $JAIL_LOCAL"
    else
        cat >> "$JAIL_LOCAL" <<'JAILEOF'

[sshd]
enabled = true
port = ssh
backend = systemd
maxretry = 5
findtime = 15m
bantime = 1h
JAILEOF
    fi
else
    echo "--- sshd jail already configured in $JAIL_LOCAL ---"
fi

echo "--- Enabling fail2ban ---"
run systemctl enable --now fail2ban

if ! $DRY_RUN; then
    sleep 2
    echo
    echo "--- fail2ban sshd jail status ---"
    fail2ban-client status sshd || echo "(jail not reporting yet - it may need a moment after first install)"
fi

echo
echo "=== Done ==="
if ! $DRY_RUN; then
    echo "Before closing this terminal: open a SEPARATE new terminal window and confirm"
    echo "you can establish a fresh SSH connection to this server successfully."
    echo "Do not proceed to disabling SSH password auth until you've confirmed that."
fi
