#!/usr/bin/env bash
set -Eeuo pipefail

# WordPress Docker Hosting Platform Installer
# Target: Ubuntu 22.04 / 24.04 / 26.04-style Debian-based Ubuntu hosts
#
# Installs:
#   - Docker Engine + Docker Compose plugin
#   - Nginx Proxy Manager
#   - Shared Docker proxy network
#   - WordPress Host Manager web dashboard with login/logout and audit logging
#   - Per-site WordPress + MariaDB Compose stacks
#   - CLI helper commands
#
# Run:
#   chmod +x install-wordpress-host.sh
#   sudo ./install-wordpress-host.sh

if [[ $EUID -ne 0 ]]; then
  echo "Please run this installer with sudo/root:"
  echo "  sudo ./install-wordpress-host.sh"
  exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
  echo "This installer currently supports Ubuntu/Debian systems using apt."
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive

PLATFORM_DIR="/opt/wp-host"
SITES_DIR="${PLATFORM_DIR}/sites"
BACKUP_DIR="${PLATFORM_DIR}/backups"
NPM_DIR="${PLATFORM_DIR}/nginx-proxy-manager"
MANAGER_DIR="${PLATFORM_DIR}/manager"
PROXY_NETWORK="wp-proxy"

echo
echo "============================================================"
echo "  WordPress Docker Hosting Platform"
echo "============================================================"
echo

read -rp "Dashboard port [8088]: " DASHBOARD_PORT
DASHBOARD_PORT="${DASHBOARD_PORT:-8088}"

read -rp "Dashboard username [admin]: " DASHBOARD_USER
DASHBOARD_USER="${DASHBOARD_USER:-admin}"

while true; do
  read -rsp "Dashboard password: " DASHBOARD_PASSWORD
  echo
  if [[ ${#DASHBOARD_PASSWORD} -lt 10 ]]; then
    echo "Use at least 10 characters."
    continue
  fi
  read -rsp "Confirm dashboard password: " DASHBOARD_PASSWORD_2
  echo
  [[ "$DASHBOARD_PASSWORD" == "$DASHBOARD_PASSWORD_2" ]] && break
  echo "Passwords did not match."
done

read -rp "Let's Encrypt / NPM admin email: " LE_EMAIL
while [[ ! "$LE_EMAIL" =~ ^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$ ]]; do
  echo "Please enter a valid email address."
  read -rp "Let's Encrypt / NPM admin email: " LE_EMAIL
done

NPM_ADMIN_EMAIL="$(printf '%s' "$LE_EMAIL" | tr '[:upper:]' '[:lower:]')"
NPM_ADMIN_PASSWORD="$(openssl rand -base64 36 | tr -d '\n' | tr '/+' '_-')"

HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
HOST_IP="${HOST_IP:-SERVER-IP}"

echo
echo "[1/8] Installing prerequisites..."
apt-get update
apt-get install -y ca-certificates curl gnupg openssl python3 python3-venv python3-pip jq

echo
echo "[2/8] Installing/updating Docker from Docker's Ubuntu repository..."

install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc

. /etc/os-release
UBUNTU_CODENAME="${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}"
if [[ -z "$UBUNTU_CODENAME" ]]; then
  echo "Could not determine the Ubuntu codename."
  exit 1
fi

cat >/etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${UBUNTU_CODENAME}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker

mkdir -p "$SITES_DIR" "$BACKUP_DIR" "$NPM_DIR" "$MANAGER_DIR"

if ! docker network inspect "$PROXY_NETWORK" >/dev/null 2>&1; then
  docker network create "$PROXY_NETWORK" >/dev/null
fi

echo
echo "[3/8] Installing Nginx Proxy Manager..."

cat >"${NPM_DIR}/compose.yml" <<EOF
services:
  npm:
    image: jc21/nginx-proxy-manager:latest
    container_name: wp-npm
    restart: unless-stopped
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
    ports:
      - "80:80"
      - "81:81"
      - "443:443"
    environment:
      DISABLE_IPV6: "true"
      INITIAL_ADMIN_EMAIL: "${NPM_ADMIN_EMAIL}"
      INITIAL_ADMIN_PASSWORD: "${NPM_ADMIN_PASSWORD}"
    volumes:
      - ./data:/data
      - ./letsencrypt:/etc/letsencrypt
    networks:
      - wp-proxy

networks:
  wp-proxy:
    external: true
    name: ${PROXY_NETWORK}
EOF

(
  cd "$NPM_DIR"
  docker compose pull
  docker compose up -d
)

echo
echo "Waiting for Nginx Proxy Manager API..."
for i in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:81/api/" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

cat >"${NPM_DIR}/ADMIN-CREDENTIALS.txt" <<EOF
Nginx Proxy Manager
Admin URL: http://${HOST_IP}:81
Email: ${NPM_ADMIN_EMAIL}
Password: ${NPM_ADMIN_PASSWORD}

Keep this file root-readable only.
EOF
chmod 600 "${NPM_DIR}/ADMIN-CREDENTIALS.txt"

echo
echo "[4/8] Building WordPress Host Manager..."

DASHBOARD_SALT="$(openssl rand -hex 16)"
DASHBOARD_HASH="$(printf '%s' "${DASHBOARD_SALT}${DASHBOARD_PASSWORD}" | sha256sum | awk '{print $1}')"
FLASK_SECRET="$(openssl rand -hex 32)"

cat >"${MANAGER_DIR}/requirements.txt" <<'EOF'
Flask>=3.1,<4
docker>=7.1,<8
gunicorn>=23,<24
psutil>=6,<8
Flask-WTF>=1.2,<2
requests>=2.32,<3
EOF

cat >"${MANAGER_DIR}/manager.env" <<EOF
WP_HOST_DIR=${PLATFORM_DIR}
WP_SITES_DIR=${SITES_DIR}
WP_BACKUP_DIR=${BACKUP_DIR}
WP_PROXY_NETWORK=${PROXY_NETWORK}
WP_DASHBOARD_PORT=${DASHBOARD_PORT}
WP_DASHBOARD_USER=${DASHBOARD_USER}
WP_DASHBOARD_PASSWORD_HASH=${DASHBOARD_HASH}
WP_DASHBOARD_PASSWORD_SALT=${DASHBOARD_SALT}
WP_FLASK_SECRET=${FLASK_SECRET}
WP_NPM_URL=http://127.0.0.1:81
WP_NPM_EMAIL=${NPM_ADMIN_EMAIL}
WP_NPM_PASSWORD=${NPM_ADMIN_PASSWORD}
WP_LE_EMAIL=${LE_EMAIL}
EOF
chmod 600 "${MANAGER_DIR}/manager.env"

cat >"${MANAGER_DIR}/app.py" <<'PYEOF'
import os
import re
import json
import time
import shutil
import secrets
import sqlite3
import subprocess
import threading
import socket
from pathlib import Path
from functools import wraps
from datetime import datetime, timedelta, timezone

import docker
from flask import (
    Flask, request, redirect, url_for, flash,
    render_template_string, session, abort
)
from werkzeug.security import generate_password_hash, check_password_hash
from flask_wtf.csrf import CSRFProtect
import psutil
import requests

APP = Flask(__name__)
APP.secret_key = os.environ["WP_FLASK_SECRET"]
APP.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    WTF_CSRF_TIME_LIMIT=3600,
)
csrf = CSRFProtect(APP)

BASE = Path(os.environ.get("WP_HOST_DIR", "/opt/wp-host"))
SITES = Path(os.environ.get("WP_SITES_DIR", str(BASE / "sites")))
BACKUPS = Path(os.environ.get("WP_BACKUP_DIR", str(BASE / "backups")))
PROXY_NETWORK = os.environ.get("WP_PROXY_NETWORK", "wp-proxy")
AUTH_USER = os.environ["WP_DASHBOARD_USER"]
BOOTSTRAP_PASSWORD = os.environ["WP_DASHBOARD_BOOTSTRAP_PASSWORD"]

AUTH_DB = BASE / "auth.db"
USERS_FILE = BASE / "users.json"
PLATFORM_DB = BASE / "platform.db"
BACKUP_RETENTION_DAYS = 14
BACKUP_HOUR = 2
LOGIN_WINDOW_MINUTES = 15
LOGIN_MAX_FAILURES = 5
LOGIN_LOCK_MINUTES = 15
NPM_URL = os.environ.get("WP_NPM_URL", "http://127.0.0.1:81").rstrip("/")
NPM_EMAIL = os.environ.get("WP_NPM_EMAIL", "")
NPM_PASSWORD = os.environ.get("WP_NPM_PASSWORD", "")
LE_EMAIL = os.environ.get("WP_LE_EMAIL", NPM_EMAIL)

docker_client = docker.from_env()

SITE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[A-Za-z]{2,63}$")

LOGIN_HTML = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>WordPress Host Manager - Login</title>
  <style>
    body { font-family: system-ui,sans-serif; margin:0; background:#111827; color:#e5e7eb; display:flex; min-height:100vh; align-items:center; justify-content:center; }
    .login { width:min(420px,90vw); background:#1f2937; border:1px solid #374151; border-radius:14px; padding:28px; box-shadow:0 20px 50px #0005; }
    h1 { margin-top:0; font-size:24px; }
    label { display:block; margin:14px 0 5px; color:#cbd5e1; }
    input { width:100%; box-sizing:border-box; padding:11px; border-radius:8px; border:1px solid #4b5563; background:#111827; color:#fff; }
    button { width:100%; margin-top:18px; padding:11px; border:0; border-radius:8px; background:#2563eb; color:#fff; cursor:pointer; font-weight:600; }
    .flash { padding:10px 12px; border-radius:8px; background:#7f1d1d; margin:12px 0; }
    .small { font-size:12px; color:#94a3b8; margin-top:16px; }
  </style>
</head>
<body>
  <div class="login">
    <h1>WordPress Host Manager</h1>
    <p>Sign in to manage hosted WordPress sites.</p>
    {% with messages = get_flashed_messages() %}
      {% for m in messages %}<div class="flash">{{ m }}</div>{% endfor %}
    {% endwith %}
    <form method="post" action="/login">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <label>Username</label>
      <input name="username" autocomplete="username" required autofocus>
      <label>Password</label>
      <input type="password" name="password" autocomplete="current-password" required>
      <button type="submit">Sign In</button>
    </form>
    <div class="small">Authentication activity is logged for security auditing.</div>
  </div>
</body>
</html>
"""

HTML = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>WordPress Host Manager</title>
  <style>
    :root { color-scheme: light dark; }
    body { font-family: system-ui, sans-serif; margin: 0; background:#111827; color:#e5e7eb; }
    header { background:#0b1220; padding:18px 24px; display:flex; justify-content:space-between; align-items:center; }
    main { max-width:1450px; margin:auto; padding:24px; }
    h1,h2 { margin-top:0; }
    .card { background:#1f2937; border:1px solid #374151; border-radius:12px; padding:18px; margin-bottom:20px; }
    .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; }
    label { display:block; font-size:13px; margin-bottom:4px; color:#cbd5e1; }
    input, select { width:100%; box-sizing:border-box; padding:9px; border-radius:7px; border:1px solid #4b5563; background:#111827; color:#fff; }
    button,.button { border:0; border-radius:7px; padding:8px 11px; cursor:pointer; color:#fff; background:#2563eb; text-decoration:none; display:inline-block; }
    .danger { background:#b91c1c; } .warn { background:#b45309; } .good { background:#047857; } .muted { background:#4b5563; }
    table { width:100%; border-collapse:collapse; font-size:14px; }
    th,td { padding:10px 8px; border-bottom:1px solid #374151; text-align:left; vertical-align:middle; }
    th { color:#93c5fd; }
    form.inline { display:inline; }
    .status-running { color:#34d399; font-weight:700; }
    .status-stopped { color:#f87171; font-weight:700; }
    .flash { padding:10px 12px; border-radius:7px; background:#334155; margin-bottom:12px; }
    .small { font-size:12px; color:#94a3b8; }
    code { background:#111827; padding:2px 5px; border-radius:4px; }
    @media(max-width:900px){ table{display:block;overflow-x:auto;white-space:nowrap;} }
  </style>
</head>
<body>
<header>
  <div>
    <strong>WordPress Host Manager</strong>
    <div class="small">{{ host }} · Signed in as {{ current_user }} ({{ current_role }})</div>
  </div>
  <div>
    <a class="button muted" href="/">Refresh</a>
    <a class="button danger" href="/logout">Logout</a>
  </div>
</header>
<main>
{% with messages = get_flashed_messages() %}
  {% for m in messages %}<div class="flash">{{ m }}</div>{% endfor %}
{% endwith %}

<div class="card">
  <h2>Host Health</h2>
  <div class="grid">
    <div><label>CPU</label><strong>{{ host_stats.cpu }}%</strong></div>
    <div><label>RAM</label><strong>{{ host_stats.ram_used }} / {{ host_stats.ram_total }} ({{ host_stats.ram_percent }}%)</strong></div>
    <div><label>Disk</label><strong>{{ host_stats.disk_used }} / {{ host_stats.disk_total }} ({{ host_stats.disk_percent }}%)</strong></div>
    <div><label>Load Average</label><strong>{{ host_stats.load }}</strong></div>
    <div><label>Nginx Proxy Manager API</label><strong>{{ host_stats.npm }}</strong></div>
  </div>
</div>

{% if alerts %}
<div class="card">
  <h2>Active Alerts</h2>
  {% for a in alerts %}
  <div class="flash"><strong>{{ a.site }}</strong>: {{ a.message }}</div>
  {% endfor %}
</div>
{% endif %}

{% if current_role in ['admin', 'user'] %}
<div class="card">
  <h2>Create WordPress Site</h2>
  <form method="post" action="/create"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <div class="grid">
      <div><label>Site ID</label><input required name="site" placeholder="acme"></div>
      <div><label>Domain</label><input required name="domain" placeholder="acme.com"></div>
      <div><label>WordPress RAM</label>
        <select name="memory">
          <option value="512m">512 MB</option>
          <option value="1g" selected>1 GB</option>
          <option value="2g">2 GB</option>
          <option value="4g">4 GB</option>
        </select>
      </div>
      <div><label>WordPress CPU</label>
        <select name="cpus">
          <option value="0.50">0.5 CPU</option>
          <option value="1.00" selected>1 CPU</option>
          <option value="2.00">2 CPU</option>
          <option value="4.00">4 CPU</option>
        </select>
      </div>
      <div><label>DB RAM</label>
        <select name="db_memory">
          <option value="256m">256 MB</option>
          <option value="512m" selected>512 MB</option>
          <option value="1g">1 GB</option>
          <option value="2g">2 GB</option>
        </select>
      </div>
      <div><label>Admin username</label><input required name="wp_admin" value="admin"></div>
      <div><label>Admin email</label><input required type="email" name="wp_email" placeholder="admin@acme.com"></div>
      <div><label>Admin password</label><input name="wp_password" placeholder="blank = auto-generate"></div>
      <div><label>Provisioning</label>
        <select name="auto_proxy">
          <option value="yes" selected>Automatic proxy + Let's Encrypt SSL</option>
          <option value="no">Create WordPress only</option>
        </select>
      </div>
    </div>
    <p><button class="good" type="submit">Create Site</button></p>
    <div class="small">For automatic SSL, the domain's public DNS must already resolve to this site's public WAN IP and TCP 80/443 must reach Nginx Proxy Manager. If certificate issuance fails, the WordPress instance is retained and you can retry later.</div>
  </form>
</div>
{% else %}
<div class="card">
  <h2>View Only Access</h2>
  <p>This account can view site status and audit information but cannot create or control instances.</p>
</div>
{% endif %}

<div class="card">
  <h2>Hosted Sites</h2>
  <table>
    <thead><tr>
      <th>Site</th><th>Domain</th><th>Status</th><th>CPU</th><th>RAM</th>
      <th>Storage</th><th>WP</th><th>Plugins</th><th>Themes</th><th>Health</th><th>Proxy/SSL</th><th>Container IP</th><th>Backup</th><th>Actions</th>
    </tr></thead>
    <tbody>
    {% for s in sites %}
      <tr>
        <td><strong>{{ s.site }}</strong></td>
        <td>{{ s.domain }}</td>
        <td class="{{ 'status-running' if s.status == 'running' else 'status-stopped' }}">{{ s.status }}</td>
        <td>{{ s.cpu }}</td>
        <td>{{ s.memory }}</td>
        <td>{{ s.storage }}</td>
        <td>{{ s.wp_version }}</td>
        <td>{{ s.plugins }}</td>
        <td>{{ s.themes }}</td>
        <td class="{{ 'status-running' if s.health == 'Healthy' else 'status-stopped' }}">{{ s.health }}</td>
        <td>{{ s.proxy_ssl }}</td>
        <td>{{ s.ip }}</td>
        <td>{{ s.backup }}</td>
        <td>
          {% if current_role in ['admin', 'user'] %}
          <form class="inline" method="post" action="/action/{{ s.site }}/start"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="good">Start</button></form>
          <form class="inline" method="post" action="/action/{{ s.site }}/stop"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="warn">Stop</button></form>
          <form class="inline" method="post" action="/action/{{ s.site }}/restart"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button>Restart</button></form>
          <form class="inline" method="post" action="/action/{{ s.site }}/update"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button>Update</button></form>
          <form class="inline" method="post" action="/action/{{ s.site }}/backup"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="muted">Backup</button></form>
          <form class="inline" method="post" action="/action/{{ s.site }}/restore-latest" onsubmit="return confirm('Restore latest backup for {{ s.site }}? Current site data will be replaced.');"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="warn">Restore Latest</button></form>
          <form class="inline" method="post" action="/action/{{ s.site }}/provision-ssl"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="good">Provision SSL</button></form>
          <form class="inline" method="post" action="/action/{{ s.site }}/delete" onsubmit="return confirm('Delete containers for {{ s.site }}? Site files/backups are retained unless removed manually.');"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="danger">Delete</button></form>
          {% else %}
          <span class="small">Read only</span>
          {% endif %}
        </td>
      </tr>
    {% else %}
      <tr><td colspan="10">No WordPress sites have been created yet.</td></tr>
    {% endfor %}
    </tbody>
  </table>
</div>

<div class="card">
  <h2>My Account</h2>
  <p>Signed in as <strong>{{ current_user }}</strong> — role: <strong>{{ current_role }}</strong></p>
  {% if current_role in ['admin', 'user'] %}
  <form method="post" action="/change-password"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <div class="grid">
      <div><label>Current password</label><input type="password" name="current_password" required></div>
      <div><label>New password</label><input type="password" name="new_password" required></div>
      <div><label>Confirm new password</label><input type="password" name="confirm_password" required></div>
    </div>
    <p><button type="submit">Change My Password</button></p>
  </form>
  {% endif %}
</div>

{% if current_role == 'admin' %}
<div class="card">
  <h2>User Management</h2>
  <form method="post" action="/users/create"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <div class="grid">
      <div><label>Username</label><input name="username" required></div>
      <div><label>Temporary password</label><input type="password" name="password" required></div>
      <div><label>Role</label>
        <select name="role">
          <option value="user" selected>User</option>
          <option value="view">View Only</option>
          <option value="admin">Admin</option>
        </select>
      </div>
    </div>
    <p><button class="good" type="submit">Create User</button></p>
  </form>
  <table>
    <thead><tr><th>Username</th><th>Role</th><th>Status</th><th>Created</th><th>Actions</th></tr></thead>
    <tbody>
    {% for u in users %}
      <tr>
        <td><strong>{{ u.username }}</strong></td>
        <td>{{ u.role }}</td>
        <td>{{ 'Enabled' if u.enabled else 'Disabled' }}</td>
        <td>{{ u.created }}</td>
        <td>
          {% if u.username != current_user %}
          <form class="inline" method="post" action="/users/{{ u.username }}/toggle"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            <button class="{{ 'warn' if u.enabled else 'good' }}">{{ 'Disable' if u.enabled else 'Enable' }}</button>
          </form>
          <form class="inline" method="post" action="/users/{{ u.username }}/delete" onsubmit="return confirm('Delete dashboard user {{ u.username }}?');"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            <button class="danger">Delete</button>
          </form>
          {% else %}
          <span class="small">Current account</span>
          {% endif %}
        </td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
</div>
{% endif %}

<div class="card">
  <h2>Platform Action Audit</h2>
  <table>
    <thead><tr><th>Time</th><th>User</th><th>Action</th><th>Target</th><th>Result</th><th>IP</th><th>Detail</th></tr></thead>
    <tbody>
    {% for a in audit_events %}
      <tr><td>{{ a.timestamp }}</td><td>{{ a.username }}</td><td>{{ a.action }}</td><td>{{ a.target }}</td><td>{{ a.result }}</td><td>{{ a.ip }}</td><td class="small">{{ a.detail }}</td></tr>
    {% else %}
      <tr><td colspan="7">No platform actions logged yet.</td></tr>
    {% endfor %}
    </tbody>
  </table>
</div>

<div class="card">
  <h2>Authentication Audit Log</h2>
  <table>
    <thead><tr><th>Time</th><th>Username</th><th>Event</th><th>Result</th><th>IP Address</th><th>User Agent</th></tr></thead>
    <tbody>
    {% for a in auth_events %}
      <tr>
        <td>{{ a.timestamp }}</td>
        <td>{{ a.username }}</td>
        <td>{{ a.event }}</td>
        <td>{{ a.result }}</td>
        <td>{{ a.ip }}</td>
        <td class="small">{{ a.user_agent }}</td>
      </tr>
    {% else %}
      <tr><td colspan="6">No authentication events yet.</td></tr>
    {% endfor %}
    </tbody>
  </table>
</div>

<div class="card">
  <h2>Proxy Setup</h2>
  <p>Nginx Proxy Manager: <a href="http://{{ host_only }}:81" target="_blank">http://{{ host_only }}:81</a></p>
  <p class="small">
    For each site create a Proxy Host: Domain = the public domain, Scheme = HTTP,
    Forward Hostname = <code>SITEID-wp</code>, Forward Port = <code>80</code>.
    Then request a Let's Encrypt certificate and enable Force SSL.
  </p>
</div>
</main>
</body>
</html>
"""

def init_auth():
    BASE.mkdir(parents=True, exist_ok=True)

    if not USERS_FILE.exists():
        users = {
            AUTH_USER: {
                "password_hash": generate_password_hash(BOOTSTRAP_PASSWORD, method="scrypt"),
                "enabled": True,
                "role": "admin",
                "created": datetime.now(timezone.utc).isoformat(),
            }
        }
        USERS_FILE.write_text(json.dumps(users, indent=2))
        os.chmod(USERS_FILE, 0o600)
    else:
        users = json.loads(USERS_FILE.read_text())
        changed = False
        for username, entry in users.items():
            if "role" not in entry:
                entry["role"] = "admin" if username == AUTH_USER else "user"
                changed = True
            if "enabled" not in entry:
                entry["enabled"] = True
                changed = True
        if changed:
            USERS_FILE.write_text(json.dumps(users, indent=2))
            os.chmod(USERS_FILE, 0o600)

    with sqlite3.connect(AUTH_DB) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS auth_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                username TEXT NOT NULL,
                event TEXT NOT NULL,
                result TEXT NOT NULL,
                ip TEXT,
                user_agent TEXT
            )
        """)
        db.commit()
    os.chmod(AUTH_DB, 0o600)

    with sqlite3.connect(PLATFORM_DB) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                username TEXT NOT NULL,
                action TEXT NOT NULL,
                target TEXT,
                result TEXT NOT NULL,
                ip TEXT,
                detail TEXT
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                site TEXT NOT NULL,
                severity TEXT NOT NULL,
                message TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            )
        """)
        db.commit()
    os.chmod(PLATFORM_DB, 0o600)

def load_users():
    return json.loads(USERS_FILE.read_text())

def save_users(users):
    tmp = USERS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(users, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(USERS_FILE)
    os.chmod(USERS_FILE, 0o600)

def current_role():
    username = session.get("username")
    if not username:
        return None
    return load_users().get(username, {}).get("role", "view")

def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login", next=request.path))
        if current_role() != "admin":
            abort(403)
        return fn(*args, **kwargs)
    return wrapper

def operator_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login", next=request.path))
        if current_role() not in {"admin", "user"}:
            abort(403)
        return fn(*args, **kwargs)
    return wrapper

def client_ip():
    # Dashboard should normally be LAN-only. If later proxied, use a trusted
    # reverse proxy configuration before relying on X-Forwarded-For.
    return request.remote_addr or "-"

def log_auth(username, event, result):
    with sqlite3.connect(AUTH_DB) as db:
        db.execute(
            "INSERT INTO auth_events(timestamp,username,event,result,ip,user_agent) VALUES(?,?,?,?,?,?)",
            (
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                username or "-",
                event,
                result,
                client_ip(),
                (request.headers.get("User-Agent") or "-")[:300],
            ),
        )
        db.commit()

def recent_auth_events(limit=50):
    with sqlite3.connect(AUTH_DB) as db:
        rows = db.execute(
            "SELECT timestamp,username,event,result,ip,user_agent "
            "FROM auth_events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        dict(timestamp=r[0], username=r[1], event=r[2], result=r[3], ip=r[4], user_agent=r[5])
        for r in rows
    ]

def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)
    return wrapper

def log_action(action, target="-", result="success", detail=""):
    username = session.get("username", "system") if request else "system"
    ip = client_ip() if request else "-"
    with sqlite3.connect(PLATFORM_DB) as db:
        db.execute(
            "INSERT INTO audit_events(timestamp,username,action,target,result,ip,detail) VALUES(?,?,?,?,?,?,?)",
            (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
             username, action, target, result, ip, str(detail)[:1000])
        )
        db.commit()

def recent_audit_events(limit=100):
    with sqlite3.connect(PLATFORM_DB) as db:
        rows = db.execute(
            "SELECT timestamp,username,action,target,result,ip,detail FROM audit_events ORDER BY id DESC LIMIT ?",
            (limit,)
        ).fetchall()
    return [dict(timestamp=r[0], username=r[1], action=r[2], target=r[3], result=r[4], ip=r[5], detail=r[6]) for r in rows]

def host_stats():
    disk = psutil.disk_usage("/")
    vm = psutil.virtual_memory()
    npm = "Unavailable"
    try:
        wait_for_npm(timeout=5)
        npm = "Online"
    except Exception:
        pass
    return {
        "npm": npm,
        "cpu": psutil.cpu_percent(interval=0.1),
        "ram_used": human_bytes(vm.used),
        "ram_total": human_bytes(vm.total),
        "ram_percent": vm.percent,
        "disk_used": human_bytes(disk.used),
        "disk_total": human_bytes(disk.total),
        "disk_percent": disk.percent,
        "load": ", ".join(f"{x:.2f}" for x in psutil.getloadavg()) if hasattr(psutil, "getloadavg") else "-",
    }

def login_locked(username, ip):
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=LOGIN_WINDOW_MINUTES)).strftime("%Y-%m-%d %H:%M:%S UTC")
    with sqlite3.connect(AUTH_DB) as db:
        count = db.execute(
            "SELECT COUNT(*) FROM auth_events WHERE event='login' AND result='failed' AND timestamp>=? AND (username=? OR ip=?)",
            (cutoff, username, ip)
        ).fetchone()[0]
    return count >= LOGIN_MAX_FAILURES

def npm_token():
    r = requests.post(
        f"{NPM_URL}/api/tokens",
        json={"identity": NPM_EMAIL, "secret": NPM_PASSWORD},
        timeout=15,
    )
    r.raise_for_status()
    token = r.json().get("token")
    if not token:
        raise RuntimeError("Nginx Proxy Manager did not return an API token.")
    return token

def npm_headers():
    return {"Authorization": f"Bearer {npm_token()}", "Content-Type": "application/json"}

def npm_existing_proxy(domain):
    r = requests.get(f"{NPM_URL}/api/nginx/proxy-hosts", headers=npm_headers(), timeout=15)
    r.raise_for_status()
    for host in r.json():
        if domain in host.get("domain_names", []):
            return host
    return None

def npm_create_certificate(domain):
    payload = {
        "provider": "letsencrypt",
        "nice_name": domain,
        "domain_names": [domain],
        "meta": {
            "dns_challenge": False,
            "letsencrypt_email": LE_EMAIL,
            "letsencrypt_agree": True,
        },
    }
    r = requests.post(
        f"{NPM_URL}/api/nginx/certificates",
        headers=npm_headers(),
        json=payload,
        timeout=180,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"SSL request failed: HTTP {r.status_code}: {r.text[:500]}")
    return r.json()

def npm_create_proxy(site, domain, certificate_id=0):
    existing = npm_existing_proxy(domain)
    if existing:
        return existing

    payload = {
        "domain_names": [domain],
        "forward_scheme": "http",
        "forward_host": f"{site}-wp",
        "forward_port": 80,
        "access_list_id": 0,
        "certificate_id": int(certificate_id or 0),
        "ssl_forced": bool(certificate_id),
        "caching_enabled": False,
        "block_exploits": True,
        "advanced_config": "",
        "meta": {},
        "allow_websocket_upgrade": True,
        "http2_support": True,
        "hsts_enabled": False,
        "hsts_subdomains": False,
        "trust_forwarded_proto": True,
        "enabled": True,
        "locations": [],
    }
    r = requests.post(
        f"{NPM_URL}/api/nginx/proxy-hosts",
        headers=npm_headers(),
        json=payload,
        timeout=30,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Proxy-host creation failed: HTTP {r.status_code}: {r.text[:500]}")
    return r.json()

def npm_attach_certificate(proxy, certificate_id):
    proxy_id = proxy.get("id")
    if not proxy_id:
        raise RuntimeError("NPM proxy object has no id.")
    payload = {
        "domain_names": proxy.get("domain_names", []),
        "forward_scheme": proxy.get("forward_scheme", "http"),
        "forward_host": proxy.get("forward_host"),
        "forward_port": proxy.get("forward_port", 80),
        "access_list_id": proxy.get("access_list_id", 0) or 0,
        "certificate_id": int(certificate_id),
        "ssl_forced": True,
        "caching_enabled": bool(proxy.get("caching_enabled", False)),
        "block_exploits": True,
        "advanced_config": proxy.get("advanced_config", ""),
        "meta": proxy.get("meta", {}),
        "allow_websocket_upgrade": True,
        "http2_support": True,
        "hsts_enabled": bool(proxy.get("hsts_enabled", False)),
        "hsts_subdomains": bool(proxy.get("hsts_subdomains", False)),
        "trust_forwarded_proto": True,
        "enabled": True,
        "locations": proxy.get("locations", []),
    }
    r = requests.put(
        f"{NPM_URL}/api/nginx/proxy-hosts/{proxy_id}",
        headers=npm_headers(),
        json=payload,
        timeout=30,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Attaching SSL failed: HTTP {r.status_code}: {r.text[:500]}")
    return r.json()

def wait_for_npm(timeout=180):
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            r = requests.get(f"{NPM_URL}/api/", timeout=5)
            if r.status_code < 500:
                return True
            last = f"HTTP {r.status_code}"
        except Exception as exc:
            last = str(exc)
        time.sleep(3)
    raise RuntimeError(f"Nginx Proxy Manager API did not become ready: {last}")

def resolve_domain(domain):
    try:
        return sorted({x[4][0] for x in socket.getaddrinfo(domain, 80, type=socket.SOCK_STREAM)})
    except Exception:
        return []

def provision_proxy_ssl(site, domain):
    wait_for_npm()
    proxy = npm_create_proxy(site, domain, 0)
    cert = npm_create_certificate(domain)
    cert_id = cert.get("id")
    if not cert_id:
        raise RuntimeError("Let's Encrypt certificate was created without an id.")
    proxy = npm_attach_certificate(proxy, cert_id)
    return {"proxy_id": proxy.get("id"), "certificate_id": cert_id}

def site_health(site, domain):
    result = {"container": "unknown", "db": "unknown", "http": "unknown", "healthy": False}
    try:
        wp = docker_client.containers.get(f"{site}-wp")
        wp.reload()
        result["container"] = wp.status
        if wp.status == "running":
            ex = wp.exec_run(["php","-r","echo 'ok';"])
            result["container"] = "running" if ex.exit_code == 0 else "degraded"
    except Exception:
        result["container"] = "missing"
    try:
        db = docker_client.containers.get(f"{site}-db")
        db.reload()
        result["db"] = db.status
    except Exception:
        result["db"] = "missing"
    try:
        p = subprocess.run(["curl","-kfsS","--max-time","5",f"https://{domain}/"], capture_output=True, timeout=8)
        result["http"] = "ok" if p.returncode == 0 else "failed"
    except Exception:
        result["http"] = "failed"
    result["healthy"] = result["container"] == "running" and result["db"] == "running" and result["http"] == "ok"
    return result

def wp_versions(site):
    data = {"core":"-","plugins":"-","themes":"-"}
    try:
        c = docker_client.containers.get(f"{site}-wp")
        if c.status != "running":
            return data
        core = c.exec_run(["sh","-lc","grep -m1 \"\\$wp_version =\" /var/www/html/wp-includes/version.php | sed -E \"s/.*'([^']+)'.*/\\1/\""])
        if core.exit_code == 0:
            data["core"] = core.output.decode(errors="ignore").strip() or "-"
        plugins = c.exec_run(["sh","-lc","find /var/www/html/wp-content/plugins -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l"])
        themes = c.exec_run(["sh","-lc","find /var/www/html/wp-content/themes -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l"])
        data["plugins"] = plugins.output.decode().strip() if plugins.exit_code == 0 else "-"
        data["themes"] = themes.output.decode().strip() if themes.exit_code == 0 else "-"
    except Exception:
        pass
    return data

def active_alerts():
    alerts = []
    for d in sorted([p for p in SITES.iterdir() if p.is_dir()]) if SITES.exists() else []:
        meta = site_metadata(d)
        if not meta: continue
        h = site_health(meta.get("site",d.name), meta.get("domain",""))
        if not h["healthy"]:
            alerts.append({
                "site": meta.get("site",d.name),
                "severity": "critical",
                "message": f"Container={h['container']}, DB={h['db']}, HTTPS={h['http']}"
            })
    return alerts

def enforce_retention(site):
    bdir = BACKUPS / site
    if not bdir.exists(): return
    cutoff = time.time() - BACKUP_RETENTION_DAYS * 86400
    for p in bdir.iterdir():
        if p.is_file() and p.stat().st_mtime < cutoff:
            p.unlink(missing_ok=True)

def scheduled_backup_all():
    while True:
        now = datetime.now()
        target = now.replace(hour=BACKUP_HOUR, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        time.sleep(max(60, (target-now).total_seconds()))
        for d in list(SITES.iterdir()) if SITES.exists() else []:
            if d.is_dir() and (d/"site.json").exists():
                try:
                    backup_site(d.name)
                    enforce_retention(d.name)
                except Exception:
                    pass

def run(cmd, cwd=None, timeout=600, check=True):
    p = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or "Command failed").strip())
    return p.stdout.strip()

def human_bytes(n):
    try: n = int(n)
    except Exception: return "-"
    units = ["B","KB","MB","GB","TB"]
    x = float(n)
    for u in units:
        if x < 1024 or u == units[-1]:
            return f"{x:.1f} {u}"
        x /= 1024

def site_metadata(site_dir):
    meta = site_dir / "site.json"
    if meta.exists():
        return json.loads(meta.read_text())
    return {}

def list_sites():
    result = []
    SITES.mkdir(parents=True, exist_ok=True)
    for d in sorted([p for p in SITES.iterdir() if p.is_dir()]):
        meta = site_metadata(d)
        if not meta:
            continue
        site = meta.get("site", d.name)
        wp_name = f"{site}-wp"
        try:
            c = docker_client.containers.get(wp_name)
            c.reload()
            status = c.status
            attrs = c.attrs
            mem_bytes = attrs["HostConfig"].get("Memory", 0)
            nano = attrs["HostConfig"].get("NanoCpus", 0)
            cpu = f"{nano/1_000_000_000:g}" if nano else meta.get("cpus","-")
            memory = human_bytes(mem_bytes) if mem_bytes else meta.get("memory","-")
            nets = attrs.get("NetworkSettings",{}).get("Networks",{})
            ip = nets.get(PROXY_NETWORK,{}).get("IPAddress","-")
            size = "-"
            try:
                raw = docker_client.api.inspect_container(c.id, size=True)
                size = human_bytes(raw.get("SizeRw",0))
            except Exception:
                pass
            wp_version = "-"
            if status == "running":
                try:
                    ex = c.exec_run(["sh","-lc","grep -m1 \"\\$wp_version =\" /var/www/html/wp-includes/version.php | sed -E \"s/.*'([^']+)'.*/\\1/\""])
                    if ex.exit_code == 0:
                        wp_version = ex.output.decode(errors="ignore").strip() or "-"
                except Exception:
                    pass
        except Exception:
            status, cpu, memory, size, wp_version, ip = "not-created", "-", "-", "-", "-", "-"

        bdir = BACKUPS / site
        latest = "-"
        if bdir.exists():
            items = sorted(bdir.glob("*.tar.gz"), key=lambda p:p.stat().st_mtime, reverse=True)
            if items:
                latest = datetime.fromtimestamp(items[0].stat().st_mtime).strftime("%Y-%m-%d %H:%M")

        health = site_health(site, meta.get("domain","-"))
        versions = wp_versions(site)
        result.append(dict(
            site=site,
            domain=meta.get("domain","-"),
            status=status,
            cpu=cpu,
            memory=memory,
            storage=size,
            wp_version=versions["core"],
            plugins=versions["plugins"],
            themes=versions["themes"],
            health="Healthy" if health["healthy"] else "UNHEALTHY",
            proxy_ssl=("SSL #" + str(meta.get("certificate_id"))) if meta.get("certificate_id") else ("Proxy #" + str(meta.get("proxy_id"))) if meta.get("proxy_id") else "Not provisioned",
            http_health=health["http"],
            db_health=health["db"],
            ip=ip,
            backup=latest,
        ))
    return result

def valid_memory(v):
    return re.fullmatch(r"[0-9]+(?:m|g)", v or "") is not None

def create_site(site, domain, memory, cpus, db_memory, wp_admin, wp_email, wp_password, auto_proxy=True):
    site = site.lower().strip()
    domain = domain.lower().strip()
    if not SITE_RE.match(site):
        raise ValueError("Site ID must use lowercase letters, numbers and hyphens only (max 40).")
    if not DOMAIN_RE.match(domain):
        raise ValueError("Domain does not look valid.")
    if not valid_memory(memory) or not valid_memory(db_memory):
        raise ValueError("Invalid memory setting.")
    if cpus not in {"0.50","1.00","2.00","4.00"}:
        raise ValueError("Invalid CPU setting.")

    site_dir = SITES / site
    if site_dir.exists():
        raise ValueError("That Site ID already exists.")

    site_dir.mkdir(parents=True)
    (site_dir / "wordpress").mkdir()

    db_name = "wordpress"
    db_user = "wordpress"
    db_pass = secrets.token_urlsafe(32)
    db_root = secrets.token_urlsafe(40)
    wp_password = wp_password.strip() or secrets.token_urlsafe(18)

    env = f"""SITE={site}
DOMAIN={domain}
DB_NAME={db_name}
DB_USER={db_user}
DB_PASSWORD={db_pass}
DB_ROOT_PASSWORD={db_root}
WP_MEMORY={memory}
WP_CPUS={cpus}
DB_MEMORY={db_memory}
"""
    (site_dir / ".env").write_text(env)
    os.chmod(site_dir / ".env", 0o600)

    compose = f"""services:
  db:
    image: mariadb:11
    container_name: {site}-db
    restart: unless-stopped
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
    environment:
      MARIADB_DATABASE: ${{DB_NAME}}
      MARIADB_USER: ${{DB_USER}}
      MARIADB_PASSWORD: ${{DB_PASSWORD}}
      MARIADB_ROOT_PASSWORD: ${{DB_ROOT_PASSWORD}}
    volumes:
      - db_data:/var/lib/mysql
    mem_limit: ${{DB_MEMORY}}
    cpus: 0.50
    networks:
      - internal
    healthcheck:
      test: ["CMD", "healthcheck.sh", "--connect", "--innodb_initialized"]
      interval: 10s
      timeout: 5s
      retries: 20

  wordpress:
    image: wordpress:latest
    container_name: {site}-wp
    restart: unless-stopped
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
    depends_on:
      db:
        condition: service_healthy
    environment:
      WORDPRESS_DB_HOST: db:3306
      WORDPRESS_DB_NAME: ${{DB_NAME}}
      WORDPRESS_DB_USER: ${{DB_USER}}
      WORDPRESS_DB_PASSWORD: ${{DB_PASSWORD}}
      WORDPRESS_CONFIG_EXTRA: |
        define('WP_HOME', 'https://{domain}');
        define('WP_SITEURL', 'https://{domain}');
    volumes:
      - ./wordpress:/var/www/html
    mem_limit: ${{WP_MEMORY}}
    cpus: ${{WP_CPUS}}
    networks:
      - internal
      - wp-proxy

volumes:
  db_data:

networks:
  internal:
    internal: true
  wp-proxy:
    external: true
    name: {PROXY_NETWORK}
"""
    (site_dir / "compose.yml").write_text(compose)

    meta = dict(site=site, domain=domain, memory=memory, cpus=cpus, db_memory=db_memory,
                wp_admin=wp_admin, wp_email=wp_email, created=datetime.now().isoformat())
    (site_dir / "site.json").write_text(json.dumps(meta, indent=2))

    run(["docker","compose","pull"], cwd=site_dir)
    run(["docker","compose","up","-d"], cwd=site_dir)

    for _ in range(90):
        try:
            c = docker_client.containers.get(f"{site}-wp")
            c.reload()
            if c.status == "running" and (site_dir/"wordpress"/"wp-includes"/"version.php").exists():
                break
        except Exception:
            pass
        time.sleep(2)

    cli_env = [
        "-e", f"WORDPRESS_DB_HOST={site}-db:3306",
        "-e", f"WORDPRESS_DB_NAME={db_name}",
        "-e", f"WORDPRESS_DB_USER={db_user}",
        "-e", f"WORDPRESS_DB_PASSWORD={db_pass}",
    ]
    cmd = [
        "docker","run","--rm",
        "--network", f"{site}_internal",
        "--volumes-from", f"{site}-wp",
        *cli_env,
        "wordpress:cli",
        "wp","core","install",
        f"--url=https://{domain}",
        f"--title={domain}",
        f"--admin_user={wp_admin}",
        f"--admin_password={wp_password}",
        f"--admin_email={wp_email}",
        "--skip-email",
        "--allow-root",
    ]
    p = subprocess.run(cmd, text=True, capture_output=True, timeout=300)
    if p.returncode != 0 and "already installed" not in (p.stderr + p.stdout).lower():
        meta["wp_cli_warning"] = (p.stderr or p.stdout)[-1000:]
    else:
        meta["wp_password"] = wp_password
    if auto_proxy:
        meta["dns_addresses"] = resolve_domain(domain)
        try:
            provisioned = provision_proxy_ssl(site, domain)
            meta.update(provisioned)
            meta["proxy_status"] = "provisioned"
        except Exception as exc:
            meta["proxy_status"] = "failed"
            meta["proxy_error"] = str(exc)
    else:
        meta["proxy_status"] = "not_requested"

    (site_dir / "site.json").write_text(json.dumps(meta, indent=2))
    return wp_password, meta

def backup_site(site):
    site_dir = SITES / site
    if not site_dir.exists():
        raise ValueError("Unknown site.")
    dest = BACKUPS / site
    dest.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    env = {}
    for line in (site_dir/".env").read_text().splitlines():
        if "=" in line:
            k,v=line.split("=",1); env[k]=v

    dbfile = dest / f"{stamp}-database.sql"
    with dbfile.open("wb") as f:
        p = subprocess.run(
            ["docker","exec",f"{site}-db","mariadb-dump",
             "-u",env["DB_USER"],f"-p{env['DB_PASSWORD']}",env["DB_NAME"]],
            stdout=f, stderr=subprocess.PIPE, timeout=600
        )
        if p.returncode != 0:
            raise RuntimeError(p.stderr.decode(errors="ignore"))

    archive = dest / f"{stamp}.tar.gz"
    run(["tar","-czf",str(archive),"-C",str(site_dir),"wordpress","compose.yml",".env","site.json"], timeout=1200)
    run(["gzip","-f",str(dbfile)])
    return archive.name

def restore_latest(site):
    site_dir = SITES / site
    bdir = BACKUPS / site
    if not site_dir.exists() or not bdir.exists():
        raise ValueError("Site or backup directory not found.")
    archives = sorted(bdir.glob("*.tar.gz"), key=lambda p:p.stat().st_mtime, reverse=True)
    dbs = sorted(bdir.glob("*-database.sql.gz"), key=lambda p:p.stat().st_mtime, reverse=True)
    if not archives or not dbs:
        raise ValueError("No complete backup set found.")

    archive = archives[0]
    dbgz = dbs[0]

    run(["docker","compose","stop","wordpress"], cwd=site_dir)
    wpdir = site_dir / "wordpress"
    safety = site_dir / f"wordpress.pre-restore-{int(time.time())}"
    if wpdir.exists():
        wpdir.rename(safety)
    run(["tar","-xzf",str(archive),"-C",str(site_dir)], timeout=1200)

    env = {}
    for line in (site_dir/".env").read_text().splitlines():
        if "=" in line:
            k,v=line.split("=",1); env[k]=v

    run(["docker","compose","up","-d","db"], cwd=site_dir)
    time.sleep(8)
    cmd = f"gunzip -c {str(dbgz)} | docker exec -i {site}-db mariadb -u{env['DB_USER']} -p'{env['DB_PASSWORD']}' {env['DB_NAME']}"
    run(["bash","-lc",cmd], timeout=1200)
    run(["docker","compose","up","-d"], cwd=site_dir)
    shutil.rmtree(safety, ignore_errors=True)
    return archive.name

def list_dashboard_users():
    users = load_users()
    return [
        {
            "username": username,
            "role": entry.get("role", "view"),
            "enabled": entry.get("enabled", True),
            "created": entry.get("created", "-"),
        }
        for username, entry in sorted(users.items())
    ]

@APP.route("/login", methods=["GET","POST"])
def login():
    if session.get("authenticated"):
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username","").strip()
        password = request.form.get("password","")
        if login_locked(username, client_ip()):
            log_auth(username, "login", "rate_limited")
            flash("Too many failed login attempts. Try again in about 15 minutes.")
            return render_template_string(LOGIN_HTML), 429
        users = load_users()
        entry = users.get(username)

        if entry and entry.get("enabled", True) and check_password_hash(entry["password_hash"], password):
            session.clear()
            session.permanent = True
            session["authenticated"] = True
            session["username"] = username
            session["role"] = entry.get("role", "view")
            log_auth(username, "login", "success")
            return redirect(url_for("index"))

        log_auth(username, "login", "failed")
        flash("Invalid username or password.")

    return render_template_string(LOGIN_HTML)

@APP.route("/logout")
def logout():
    username = session.get("username", "-")
    if session.get("authenticated"):
        log_auth(username, "logout", "success")
    session.clear()
    return redirect(url_for("login"))

@APP.route("/")
@login_required
def index():
    host_only = request.host.split(":")[0]
    return render_template_string(
        HTML,
        sites=list_sites(),
        auth_events=recent_auth_events(),
        network=PROXY_NETWORK,
        host=request.host,
        host_only=host_only,
        current_user=session.get("username","-"),
        current_role=current_role(),
        users=list_dashboard_users() if current_role() == "admin" else [],
        host_stats=host_stats(),
        alerts=active_alerts(),
        audit_events=recent_audit_events(),
    )

@APP.post("/change-password")
@operator_required
def change_password():
    username = session.get("username")
    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    users = load_users()
    entry = users.get(username)
    if not entry or not check_password_hash(entry["password_hash"], current_password):
        flash("Current password is incorrect.")
        return redirect(url_for("index"))
    if len(new_password) < 10:
        flash("New password must be at least 10 characters.")
        return redirect(url_for("index"))
    if new_password != confirm_password:
        flash("New password and confirmation do not match.")
        return redirect(url_for("index"))

    entry["password_hash"] = generate_password_hash(new_password, method="scrypt")
    entry["password_changed"] = datetime.now(timezone.utc).isoformat()
    save_users(users)
    log_auth(username, "password_change", "success")
    log_action("password_change", username, "success")
    flash("Your password has been changed.")
    return redirect(url_for("index"))

@APP.post("/users/create")
@admin_required
def user_create():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    role = request.form.get("role", "user")

    if not re.fullmatch(r"[A-Za-z0-9._-]{3,40}", username):
        flash("Username must be 3-40 characters using letters, numbers, dot, underscore or hyphen.")
        return redirect(url_for("index"))
    if len(password) < 10:
        flash("Temporary password must be at least 10 characters.")
        return redirect(url_for("index"))
    if role not in {"admin", "user", "view"}:
        flash("Invalid role.")
        return redirect(url_for("index"))

    users = load_users()
    if username in users:
        flash("That username already exists.")
        return redirect(url_for("index"))

    users[username] = {
        "password_hash": generate_password_hash(password, method="scrypt"),
        "enabled": True,
        "role": role,
        "created": datetime.now(timezone.utc).isoformat(),
        "created_by": session.get("username"),
    }
    save_users(users)
    log_auth(session.get("username"), f"user_create:{username}:{role}", "success")
    log_action("user_create", username, "success", f"role={role}")
    flash(f"User {username} created with role {role}.")
    return redirect(url_for("index"))

@APP.post("/users/<username>/toggle")
@admin_required
def user_toggle(username):
    users = load_users()
    if username == session.get("username"):
        flash("You cannot disable your own account.")
        return redirect(url_for("index"))
    if username not in users:
        flash("User not found.")
        return redirect(url_for("index"))
    users[username]["enabled"] = not users[username].get("enabled", True)
    save_users(users)
    state = "enabled" if users[username]["enabled"] else "disabled"
    log_auth(session.get("username"), f"user_{state}:{username}", "success")
    log_action(f"user_{state}", username, "success")
    flash(f"User {username} {state}.")
    return redirect(url_for("index"))

@APP.post("/users/<username>/delete")
@admin_required
def user_delete(username):
    users = load_users()
    if username == session.get("username"):
        flash("You cannot delete your own account.")
        return redirect(url_for("index"))
    if username not in users:
        flash("User not found.")
        return redirect(url_for("index"))
    del users[username]
    save_users(users)
    log_auth(session.get("username"), f"user_delete:{username}", "success")
    log_action("user_delete", username, "success")
    flash(f"User {username} deleted.")
    return redirect(url_for("index"))

@APP.post("/create")
@operator_required
def create():
    try:
        password, meta = create_site(
            request.form.get("site",""),
            request.form.get("domain",""),
            request.form.get("memory","1g"),
            request.form.get("cpus","1.00"),
            request.form.get("db_memory","512m"),
            request.form.get("wp_admin","admin"),
            request.form.get("wp_email",""),
            request.form.get("wp_password",""),
            request.form.get("auto_proxy","yes") == "yes",
        )
        log_action("site_create", request.form.get("site",""), "success", request.form.get("domain",""))
        if meta.get("proxy_status") == "provisioned":
            flash(f"Site created and HTTPS provisioned. WordPress admin password: {password} — save this now.")
        elif meta.get("proxy_status") == "failed":
            flash(f"Site created, but proxy/SSL provisioning failed: {meta.get('proxy_error')}. WordPress admin password: {password}")
        else:
            flash(f"Site created. WordPress admin password: {password} — save this now.")
    except Exception as e:
        log_action("site_create", request.form.get("site",""), "failed", str(e))
        flash(f"Create failed: {e}")
    return redirect(url_for("index"))

@APP.post("/action/<site>/<action>")
@operator_required
def action(site, action):
    if not SITE_RE.match(site):
        flash("Invalid site.")
        return redirect(url_for("index"))
    site_dir = SITES / site
    if not site_dir.exists():
        flash("Unknown site.")
        return redirect(url_for("index"))

    try:
        if action == "start":
            run(["docker","compose","up","-d"], cwd=site_dir)
        elif action == "stop":
            run(["docker","compose","stop"], cwd=site_dir)
        elif action == "restart":
            run(["docker","compose","restart"], cwd=site_dir)
        elif action == "update":
            run(["docker","compose","pull"], cwd=site_dir)
            run(["docker","compose","up","-d"], cwd=site_dir)
        elif action == "backup":
            name = backup_site(site)
            enforce_retention(site)
            log_action("site_backup", site, "success", name)
            flash(f"Backup created: {name}")
            return redirect(url_for("index"))
        elif action == "restore-latest":
            name = restore_latest(site)
            log_action("site_restore", site, "success", name)
            flash(f"Restored latest backup: {name}")
            return redirect(url_for("index"))
        elif action == "provision-ssl":
            meta = site_metadata(site_dir)
            provisioned = provision_proxy_ssl(site, meta.get("domain",""))
            meta.update(provisioned)
            meta["proxy_status"] = "provisioned"
            meta.pop("proxy_error", None)
            (site_dir / "site.json").write_text(json.dumps(meta, indent=2))
            log_action("site_provision_ssl", site, "success", meta.get("domain",""))
            flash(f"{site}: proxy and SSL provisioned.")
            return redirect(url_for("index"))
        elif action == "delete":
            run(["docker","compose","down"], cwd=site_dir)
            flash("Containers removed. Site directory and persistent database volume were retained.")
            return redirect(url_for("index"))
        else:
            raise ValueError("Unknown action.")
        log_action(f"site_{action}", site, "success")
        flash(f"{site}: {action} completed.")
    except Exception as e:
        log_action(f"site_{action}", site, "failed", str(e))
        flash(f"{site}: {action} failed: {e}")
    return redirect(url_for("index"))

init_auth()

if os.environ.get("WP_SCHEDULER_STARTED") != "1":
    os.environ["WP_SCHEDULER_STARTED"] = "1"
    threading.Thread(target=scheduled_backup_all, daemon=True).start()

if __name__ == "__main__":
    APP.run(host="0.0.0.0", port=int(os.environ.get("WP_DASHBOARD_PORT","8088")))
PYEOF

python3 -m venv "${MANAGER_DIR}/venv"
"${MANAGER_DIR}/venv/bin/pip" install --upgrade pip
"${MANAGER_DIR}/venv/bin/pip" install -r "${MANAGER_DIR}/requirements.txt"

cat >/etc/systemd/system/wp-host-manager.service <<EOF
[Unit]
Description=WordPress Host Manager
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=simple
WorkingDirectory=${MANAGER_DIR}
EnvironmentFile=${MANAGER_DIR}/manager.env
ExecStart=${MANAGER_DIR}/venv/bin/gunicorn --workers 1 --threads 4 --bind 0.0.0.0:${DASHBOARD_PORT} app:APP
Restart=always
RestartSec=3
User=root

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now wp-host-manager

echo
echo "[5/8] Installing CLI helper commands..."

cat >/usr/local/bin/wordpress-list <<'EOF'
#!/usr/bin/env bash
set -e
BASE="/opt/wp-host/sites"
printf "%-20s %-35s %-15s\n" "SITE" "DOMAIN" "STATUS"
printf "%-20s %-35s %-15s\n" "----" "------" "------"
for d in "$BASE"/*; do
  [[ -d "$d" && -f "$d/site.json" ]] || continue
  site="$(jq -r .site "$d/site.json")"
  domain="$(jq -r .domain "$d/site.json")"
  status="$(docker inspect -f '{{.State.Status}}' "${site}-wp" 2>/dev/null || echo not-created)"
  printf "%-20s %-35s %-15s\n" "$site" "$domain" "$status"
done
EOF

cat >/usr/local/bin/wordpress-backup <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
[[ $# -eq 1 ]] || { echo "Usage: wordpress-backup SITE"; exit 1; }
SITE="$1"
DIR="/opt/wp-host/sites/$SITE"
DEST="/opt/wp-host/backups/$SITE"
[[ -d "$DIR" ]] || { echo "Unknown site: $SITE"; exit 1; }
mkdir -p "$DEST"
STAMP="$(date +%Y%m%d-%H%M%S)"
set -a; source "$DIR/.env"; set +a
docker exec "${SITE}-db" mariadb-dump -u"$DB_USER" -p"$DB_PASSWORD" "$DB_NAME" | gzip >"$DEST/${STAMP}-database.sql.gz"
tar -czf "$DEST/${STAMP}.tar.gz" -C "$DIR" wordpress compose.yml .env site.json
echo "Backup written to: $DEST"
EOF

cat >/usr/local/bin/wordpress-update <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
[[ $# -eq 1 ]] || { echo "Usage: wordpress-update SITE"; exit 1; }
DIR="/opt/wp-host/sites/$1"
[[ -d "$DIR" ]] || { echo "Unknown site: $1"; exit 1; }
cd "$DIR"
docker compose pull
docker compose up -d
EOF

cat >/usr/local/bin/wordpress-restart <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
[[ $# -eq 1 ]] || { echo "Usage: wordpress-restart SITE"; exit 1; }
DIR="/opt/wp-host/sites/$1"
[[ -d "$DIR" ]] || { echo "Unknown site: $1"; exit 1; }
cd "$DIR"
docker compose restart
EOF

chmod +x /usr/local/bin/wordpress-list /usr/local/bin/wordpress-backup /usr/local/bin/wordpress-update /usr/local/bin/wordpress-restart

echo
echo "[6/8] Creating firewall guidance file..."
cat >"${PLATFORM_DIR}/FIREWALL.txt" <<EOF
Recommended exposure:

PUBLIC / ROUTER PORT FORWARDS
  TCP 80  -> ${HOST_IP}:80
  TCP 443 -> ${HOST_IP}:443

LAN ONLY
  Dashboard:           http://${HOST_IP}:${DASHBOARD_PORT}
  Nginx Proxy Manager: http://${HOST_IP}:81

Do NOT port-forward ${DASHBOARD_PORT} or 81 to the public Internet.

If using UFW, an example LAN-only policy for 192.168.0.0/16 is:

  sudo ufw allow 80/tcp
  sudo ufw allow 443/tcp
  sudo ufw allow from 192.168.0.0/16 to any port ${DASHBOARD_PORT} proto tcp
  sudo ufw allow from 192.168.0.0/16 to any port 81 proto tcp

Adjust the LAN subnet before applying.
EOF

echo
echo "[7/8] Verifying services..."
docker compose -f "${NPM_DIR}/compose.yml" ps
systemctl --no-pager --full status wp-host-manager | sed -n '1,12p' || true

echo
echo "Running V5 self-tests..."
docker info >/dev/null
docker network inspect "${PROXY_NETWORK}" >/dev/null
curl -fsS "http://127.0.0.1:81/api/" >/dev/null
curl -fsS -u "${DASHBOARD_USER}:${DASHBOARD_PASSWORD}" "http://127.0.0.1:${DASHBOARD_PORT}/login" >/dev/null || true
systemctl is-enabled docker | grep -q enabled
systemctl is-enabled wp-host-manager | grep -q enabled
echo "Core service self-tests passed."

echo
echo "[8/8] Installation complete."
echo
echo "============================================================"
echo " WORDPRESS HOST PLATFORM READY"
echo "============================================================"
echo
echo "Management Dashboard (login protected):"
echo "  http://${HOST_IP}:${DASHBOARD_PORT}"
echo
echo "Nginx Proxy Manager:"
echo "  http://${HOST_IP}:81"
echo "  User: ${NPM_ADMIN_EMAIL}"
echo "  Password stored at: ${NPM_DIR}/ADMIN-CREDENTIALS.txt"
echo
echo "Dashboard username:"
echo "  ${DASHBOARD_USER}"
echo "V5 features:"
echo "  CSRF protection"
echo "  Login rate limiting (5 failures / 15 minutes)"
echo "  Full platform action audit"
echo "  Host CPU/RAM/disk/load monitoring"
echo "  WordPress/DB/HTTPS health checks"
echo "  Nightly backups at 02:00 with 14-day retention"
echo "  One-click latest-backup restore"
echo "  Docker JSON log rotation (10 MB x 3)"
echo "  WordPress core/plugin/theme visibility"
echo "  Unhealthy-site dashboard alerts"
echo "  Automatic NPM proxy-host creation"
echo "  Automatic Let's Encrypt certificate request"
echo "  Automatic HTTPS + Force SSL"
echo "  Retry SSL provisioning from dashboard"
echo "  Installation self-tests"
echo
echo "Dashboard roles:"
echo "  admin = full site access + user management"
echo "  user  = full site access + own password change"
echo "  view  = dashboard read-only"
echo
echo "Authentication log database:"
echo "  ${PLATFORM_DIR}/auth.db"
echo
echo "Site directory:"
echo "  ${SITES_DIR}"
echo
echo "Backups:"
echo "  ${BACKUP_DIR}"
echo
echo "CLI commands:"
echo "  wordpress-list"
echo "  wordpress-backup SITE"
echo "  wordpress-update SITE"
echo "  wordpress-restart SITE"
echo
echo "Boot persistence:"
echo "  Docker service: enabled at startup"
echo "  Dashboard service: enabled at startup"
echo "  NPM/WordPress/MariaDB containers: restart unless-stopped"
echo
echo "IMPORTANT:"
echo "  Automatic Let's Encrypt requires each site's DNS to point to your public IP before creation."
echo "  Check Point/NAT must pass TCP 80 and 443 to this Ubuntu VM/NPM."
echo "  Keep ports ${DASHBOARD_PORT} and 81 LAN-only."
echo "  Only forward TCP 80 and 443 from your router to this server."
echo
echo "After creating a site in the dashboard, create an NPM Proxy Host:"
echo "  Domain:           acme.com"
echo "  Scheme:           http"
echo "  Forward Hostname: acme-wp"
echo "  Forward Port:     80"
echo "  SSL:              Request Let's Encrypt certificate + Force SSL"
echo
echo "Firewall notes:"
echo "  ${PLATFORM_DIR}/FIREWALL.txt"
echo
