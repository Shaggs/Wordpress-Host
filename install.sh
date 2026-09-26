#!/usr/bin/env bash
set -Eeuo pipefail

[[ $EUID -eq 0 ]] || { echo "Run with sudo/root."; exit 1; }
command -v apt-get >/dev/null || { echo "Ubuntu/Debian with apt is required."; exit 1; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="9.2.0"

PLATFORM_DIR="/opt/wp-host"
SITES_DIR="$PLATFORM_DIR/sites"
BACKUP_DIR="$PLATFORM_DIR/backups"
NPM_DIR="$PLATFORM_DIR/nginx-proxy-manager"
MANAGER_DIR="$PLATFORM_DIR/manager"
PROXY_NETWORK="wp-proxy"

echo "============================================================"
echo " WordPress Hosting Platform ${VERSION}"
echo " Clean Installation"
echo "============================================================"
echo

# This release is intentionally not an upgrade/patch mechanism.
if [[ -e "$MANAGER_DIR/app.py" || -e "$PLATFORM_DIR/users.json" || -e "$PLATFORM_DIR/platform.db" ]]; then
    echo "ERROR: An existing WordPress Hosting Platform installation was detected:"
    echo "  $PLATFORM_DIR"
    echo
    echo "This GitHub release is a CLEAN installer and will not patch or overwrite"
    echo "an existing deployment. Move/remove the old deployment only after taking"
    echo "whatever site/data backups you require."
    exit 1
fi

read -rp "Dashboard port [8088]: " DASHBOARD_PORT
DASHBOARD_PORT="${DASHBOARD_PORT:-8088}"

read -rp "Initial admin username [admin]: " DASHBOARD_USER
DASHBOARD_USER="${DASHBOARD_USER:-admin}"

read -rp "Initial admin email: " DASHBOARD_EMAIL
[[ "$DASHBOARD_EMAIL" =~ ^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$ ]] || {
    echo "A valid admin email address is required."
    exit 1
}

while true; do
    read -rsp "Initial admin password (minimum 12 characters): " DASHBOARD_PASSWORD
    echo
    [[ ${#DASHBOARD_PASSWORD} -ge 12 ]] || {
        echo "Password too short."
        continue
    }
    read -rsp "Confirm admin password: " D2
    echo
    [[ "$DASHBOARD_PASSWORD" == "$D2" ]] && break
    echo "Passwords did not match."
done

read -rp "Let's Encrypt / Nginx Proxy Manager admin email [$DASHBOARD_EMAIL]: " LE_EMAIL
LE_EMAIL="${LE_EMAIL:-$DASHBOARD_EMAIL}"

read -rp "Company / Platform Name [WP Host]: " PLATFORM_NAME
PLATFORM_NAME="${PLATFORM_NAME:-WP Host}"

read -rp "Dashboard Title [$PLATFORM_NAME WordPress Hosting]: " PLATFORM_TITLE
PLATFORM_TITLE="${PLATFORM_TITLE:-$PLATFORM_NAME WordPress Hosting}"

read -rp "Management Domain [hosting.example.com]: " MANAGER_DOMAIN
MANAGER_DOMAIN="${MANAGER_DOMAIN:-hosting.example.com}"

export DEBIAN_FRONTEND=noninteractive

echo
echo "[1/9] Installing OS dependencies..."
apt-get update
apt-get install -y \
    ca-certificates curl gnupg openssl \
    python3 python3-venv python3-pip \
    jq rsync gzip ufw

echo "[2/9] Installing / enabling Docker..."
if ! command -v docker >/dev/null; then
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc

    . /etc/os-release
    CODENAME="${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}"

    cat >/etc/apt/sources.list.d/docker.sources <<DOCKERREPO
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${CODENAME}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
DOCKERREPO

    apt-get update
    apt-get install -y \
        docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin
fi

systemctl enable --now docker

echo "[3/9] Creating clean platform directories..."
mkdir -p \
    "$SITES_DIR" \
    "$BACKUP_DIR" \
    "$NPM_DIR" \
    "$MANAGER_DIR/templates" \
    "$MANAGER_DIR/static/css" \
    "$MANAGER_DIR/static/js"

docker network inspect "$PROXY_NETWORK" >/dev/null 2>&1 \
    || docker network create "$PROXY_NETWORK" >/dev/null

echo "[4/9] Deploying Nginx Proxy Manager..."
NPM_ADMIN_PASSWORD="$(openssl rand -base64 36 | tr -d '\n' | tr '/+' '_-')"

cp "$ROOT/docker/npm/compose.yml" "$NPM_DIR/compose.yml"

cat >"$NPM_DIR/.env" <<NPMENV
NPM_ADMIN_EMAIL=${LE_EMAIL}
NPM_ADMIN_PASSWORD=${NPM_ADMIN_PASSWORD}
PROXY_NETWORK=${PROXY_NETWORK}
NPMENV

chmod 600 "$NPM_DIR/.env"

(
    cd "$NPM_DIR"
    docker compose pull
    docker compose up -d
)

echo "[5/9] Installing manager source..."
cp "$ROOT/manager/app.py" "$MANAGER_DIR/app.py"
cp "$ROOT/manager/migration_engine.py" "$MANAGER_DIR/migration_engine.py"
cp "$ROOT/manager/requirements.txt" "$MANAGER_DIR/requirements.txt"
cp "$ROOT/manager/site-compose.yml.tpl" "$MANAGER_DIR/site-compose.yml.tpl"
cp -a "$ROOT/manager/templates/." "$MANAGER_DIR/templates/"
cp -a "$ROOT/manager/static/." "$MANAGER_DIR/static/"

echo "[6/9] Creating Python environment..."
python3 -m venv "$MANAGER_DIR/venv"
"$MANAGER_DIR/venv/bin/pip" install --upgrade pip
"$MANAGER_DIR/venv/bin/pip" install -r "$MANAGER_DIR/requirements.txt"

# Generate a real scrypt hash via the venv's own installed Werkzeug, rather
# than a single plain SHA-256 pass — this must happen after the venv exists.
DASHBOARD_HASH="$(
    "$MANAGER_DIR/venv/bin/python" -c '
import sys
from werkzeug.security import generate_password_hash
print(generate_password_hash(sys.argv[1], method="scrypt"))
' "${DASHBOARD_PASSWORD}"
)"
FLASK_SECRET="$(openssl rand -hex 32)"

cat >"$MANAGER_DIR/manager.env" <<MANAGERENV
PLATFORM_NAME="${PLATFORM_NAME}"
PLATFORM_TITLE="${PLATFORM_TITLE}"
MANAGER_DOMAIN="${MANAGER_DOMAIN}"

WP_HOST_DIR=${PLATFORM_DIR}
WP_SITES_DIR=${SITES_DIR}
WP_BACKUP_DIR=${BACKUP_DIR}
WP_PROXY_NETWORK=${PROXY_NETWORK}
WP_DASHBOARD_PORT=${DASHBOARD_PORT}

WP_DASHBOARD_USER=${DASHBOARD_USER}
WP_DASHBOARD_EMAIL=${DASHBOARD_EMAIL}
WP_DASHBOARD_PASSWORD_HASH=${DASHBOARD_HASH}
WP_FLASK_SECRET=${FLASK_SECRET}

WP_NPM_URL=http://127.0.0.1:81
WP_NPM_EMAIL=${LE_EMAIL}
WP_NPM_PASSWORD=${NPM_ADMIN_PASSWORD}
WP_LE_EMAIL=${LE_EMAIL}
MANAGERENV

chmod 600 "$MANAGER_DIR/manager.env"

echo "[7/9] Running source + authentication preflight..."
set -a
source "$MANAGER_DIR/manager.env"
set +a

(
    cd "$MANAGER_DIR"
    "$MANAGER_DIR/venv/bin/python" -m py_compile \
        app.py migration_engine.py
)

# Initialise blank databases/user store with scheduler disabled.
(
    cd "$MANAGER_DIR"
    WP_SCHEDULER_STARTED=1 \
        "$MANAGER_DIR/venv/bin/python" -c \
        'import app; app.init_auth(); print("Manager import/init: PASS")'
)

# Run the repository's auth workflow test against the installed clean source.
WP_PREFLIGHT_APP="$MANAGER_DIR/app.py" \
WP_PREFLIGHT_MANAGER="$MANAGER_DIR" \
WP_SCHEDULER_STARTED=1 \
    "$MANAGER_DIR/venv/bin/python" "$ROOT/tests/auth_preflight.py"

echo "[8/9] Installing service + operational commands..."
cp "$ROOT/systemd/wp-host-manager.service" \
   /etc/systemd/system/wp-host-manager.service

systemctl daemon-reload
systemctl enable --now wp-host-manager

for f in "$ROOT"/scripts/*; do
    install -m 0755 "$f" "/usr/local/bin/$(basename "$f")"
done

cp "$ROOT/config/firewall.example" "$PLATFORM_DIR/FIREWALL.txt"

echo "[9/9] Final health check..."
sleep 3

if ! systemctl is-active --quiet wp-host-manager; then
    echo "ERROR: wp-host-manager failed to start."
    journalctl -u wp-host-manager -n 80 --no-pager
    exit 1
fi

PRIVATE_IP="$(hostname -I | awk '{print $1}')"

cat >"$NPM_DIR/ADMIN-CREDENTIALS.txt" <<CREDS
Nginx Proxy Manager
Admin URL: http://${PRIVATE_IP}:81
Email: ${LE_EMAIL}
Password: ${NPM_ADMIN_PASSWORD}
CREDS
chmod 600 "$NPM_DIR/ADMIN-CREDENTIALS.txt"

echo
echo "============================================================"
echo " Installation complete"
echo "============================================================"
echo
echo "Manager: http://${PRIVATE_IP}:${DASHBOARD_PORT}"
echo "NPM:     http://${PRIVATE_IP}:81"
echo
echo "Initial admin: ${DASHBOARD_USER}"
echo
echo "IMPORTANT: the first admin login is required to enrol MFA before"
echo "normal dashboard access."
echo
echo "Next:"
echo "  1. Put ${MANAGER_DOMAIN} behind Nginx Proxy Manager."
echo "  2. Issue a valid HTTPS certificate."
echo "  3. Configure SMTP from Admin -> Email Settings."
echo "  4. Test account email before creating additional users."


echo
echo "Temporary UFW management access:"
echo "  ufw is installed but NOT enabled or reconfigured by this installer."
echo "  Before enabling UFW remotely, allow your actual SSH port plus TCP 80/443."
echo "  Then set default deny incoming and enable UFW manually."
echo "  Do not permanently allow 21000-22999 in UFW; the manager opens exact ports temporarily."
