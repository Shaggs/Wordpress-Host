#!/usr/bin/env bash
#
# WordPress Hosting Platform - Host Hardening (Part 2: SSH key-only login)
#
# This disables SSH password authentication, forcing key-based login only.
#
# THIS IS DELIBERATELY SEPARATE FROM harden_firewall.sh AND REQUIRES AN
# EXPLICIT FLAG TO RUN. Disabling password auth before confirming key-based
# login actually works is the single most common way to permanently lock
# yourself out of a remote server. There is no "undo" if you get this wrong
# and don't have console/physical access to the machine.
#
# BEFORE RUNNING THIS SCRIPT:
#   1. Generate an SSH key pair on your OWN machine (not this server) if you
#      don't already have one:
#         ssh-keygen -t ed25519 -C "your-email@example.com"
#   2. Copy the public key to this server:
#         ssh-copy-id -p <port> your-username@this-server
#   3. Open a NEW terminal window (keep your current session open as a
#      safety net) and confirm you can log in using ONLY the key:
#         ssh -o PubkeyAuthentication=yes -o PasswordAuthentication=no \
#             -p <port> your-username@this-server
#      If that fails, STOP. Do not run this script until it succeeds.
#
# Usage (only after the above is confirmed):
#   sudo ./harden_ssh_keys_only.sh --i-have-verified-key-login
#
# Safe to re-run once applied (idempotent).

set -euo pipefail

if [[ "${1:-}" != "--i-have-verified-key-login" ]]; then
    cat >&2 <<'MSG'
Refusing to run without explicit confirmation.

This script disables SSH password authentication. If key-based login is
not already working for your account, this WILL lock you out of this
server with no remote way back in.

Before running this, in a SEPARATE terminal window, confirm you can log
in using ONLY a key (no password prompt):

    ssh -o PubkeyAuthentication=yes -o PasswordAuthentication=no \
        -p <port> your-username@this-server

Once that succeeds, run this script again with:

    sudo ./harden_ssh_keys_only.sh --i-have-verified-key-login
MSG
    exit 1
fi

if [[ $EUID -ne 0 ]]; then
    echo "This script must be run as root (use sudo). Aborting." >&2
    exit 1
fi

echo "=== SSH key-only hardening ==="
echo

# ---------------------------------------------------------------------------
# A weak but worthwhile sanity check: refuse to proceed if there is no
# authorized_keys file with at least one key for ANY real user on this
# system. This can't prove key login works for YOUR specific client, but
# catching "there are simply no keys configured at all" is cheap insurance
# against the worst-case mistake.
# ---------------------------------------------------------------------------
FOUND_A_KEY=false
for home in /root /home/*; do
    keyfile="$home/.ssh/authorized_keys"
    if [[ -s "$keyfile" ]]; then
        FOUND_A_KEY=true
        echo "[OK] Found authorized_keys with content: $keyfile"
    fi
done

if ! $FOUND_A_KEY; then
    echo >&2
    echo "[ABORT] No authorized_keys file with any content was found for any user." >&2
    echo "        This means no SSH key login is configured at all yet." >&2
    echo "        Set that up first (see the instructions at the top of this script)." >&2
    exit 1
fi

echo
echo "Proceeding - you confirmed key-based login is already verified working."
echo

SSHD_CONFIG="/etc/ssh/sshd_config"
DROPIN_DIR="/etc/ssh/sshd_config.d"
DROPIN_FILE="$DROPIN_DIR/99-wp-host-key-only.conf"

mkdir -p "$DROPIN_DIR"
cat > "$DROPIN_FILE" <<'EOF'
# Managed by WordPress Hosting Platform hardening (harden_ssh_keys_only.sh)
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
EOF

echo "--- Wrote $DROPIN_FILE ---"
cat "$DROPIN_FILE"
echo

echo "--- Validating sshd config ---"
if ! sshd -t; then
    echo "[ABORT] sshd -t reported a config error. Removing the drop-in and leaving SSH untouched." >&2
    rm -f "$DROPIN_FILE"
    exit 1
fi
echo "[OK] Config is valid."
echo

echo "--- Reloading sshd ---"
systemctl reload sshd || systemctl reload ssh

echo
echo "=== Done ==="
echo "IMPORTANT: before closing this terminal, open a NEW terminal window and"
echo "confirm you can still log in via SSH key. If you get locked out now,"
echo "you will need console/physical access to this machine to recover -"
echo "there is no remote fix at that point."
