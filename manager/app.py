# WordPress Hosting Platform v9.4.0
# Clean upstream source release
import os
import re
import json
import hmac
import time
import shutil
import secrets
import sqlite3
import subprocess
import threading
import socket
import smtplib
import ssl
import hashlib
import string
import base64
import ipaddress
from io import BytesIO
import pyotp
import qrcode
import migration_engine
from email.message import EmailMessage
from pathlib import Path
from functools import wraps
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import docker
from flask import (
    Flask, request, redirect, url_for, flash,
    render_template_string, session, abort
)
from werkzeug.security import generate_password_hash, check_password_hash
from flask_wtf.csrf import CSRFProtect
import psutil
import requests

PLATFORM_NAME = os.environ.get("PLATFORM_NAME", "WP Host")
PLATFORM_TITLE = os.environ.get("PLATFORM_TITLE", f"{PLATFORM_NAME} WordPress Hosting")
MANAGER_DOMAIN = os.environ.get("MANAGER_DOMAIN", "")

APP = Flask(__name__)
APP.secret_key = os.environ["WP_FLASK_SECRET"]
APP.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    WTF_CSRF_TIME_LIMIT=3600,
)
csrf = CSRFProtect(APP)
APP.jinja_env.globals.update(
    platform_name=PLATFORM_NAME,
    platform_title=PLATFORM_TITLE,
    manager_domain=MANAGER_DOMAIN,
)


ADELAIDE_TZ = ZoneInfo("Australia/Adelaide")

def format_adelaide(value, fmt="%d %b %Y, %I:%M %p %Z"):
    if value in (None, "", "-"):
        return "-"
    try:
        if isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(value, timezone.utc)
        elif isinstance(value, datetime):
            dt = value
        else:
            raw = str(value).strip()
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            try:
                dt = datetime.fromisoformat(raw)
            except ValueError:
                for pattern in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
                    try:
                        dt = datetime.strptime(raw, pattern).replace(tzinfo=timezone.utc)
                        break
                    except ValueError:
                        dt = None
                if dt is None:
                    return str(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ADELAIDE_TZ).strftime(fmt)
    except Exception:
        return str(value)

APP.jinja_env.filters["adelaide"] = format_adelaide

BASE = Path(os.environ.get("WP_HOST_DIR", "/opt/wp-host"))
SITES = Path(os.environ.get("WP_SITES_DIR", str(BASE / "sites")))
BACKUPS = Path(os.environ.get("WP_BACKUP_DIR", str(BASE / "backups")))
_DEFAULT_BACKUP_ROOT = BACKUPS  # static default, used as the fallback inside backup_root()
PROXY_NETWORK = os.environ.get("WP_PROXY_NETWORK", "wp-proxy")
AUTH_USER = os.environ["WP_DASHBOARD_USER"]
AUTH_EMAIL = os.environ.get("WP_DASHBOARD_EMAIL", "")
BOOTSTRAP_HASH = os.environ["WP_DASHBOARD_PASSWORD_HASH"]
BOOTSTRAP_SALT = os.environ.get("WP_DASHBOARD_PASSWORD_SALT", "")

AUTH_DB = BASE / "auth.db"
USERS_FILE = BASE / "users.json"
PLATFORM_DB = BASE / "platform.db"
EMAIL_SETTINGS_FILE = BASE / "email-settings.json"
BACKUP_RETENTION_DAYS = 14
BACKUP_HOUR = 2
LOGIN_WINDOW_MINUTES = 15
LOGIN_MAX_FAILURES = 5
LOGIN_LOCK_MINUTES = 15
SECURITY_SCAN_INTERVAL_MINUTES = 15
SECURITY_EMAIL_SEVERITIES = {"HIGH", "CRITICAL"}
SECURITY_HASH_FILES = [
    "index.php", "wp-config.php", ".htaccess", "wp-load.php",
    "wp-settings.php", "wp-login.php", "wp-blog-header.php", "xmlrpc.php"
]
METRIC_RETENTION_DAYS = 30
METRIC_INTERVAL_SECONDS = 60
SFTP_PORT_START = 21000
SFTP_PORT_END = 21999
PHPMYADMIN_PORT_START = 22000
PHPMYADMIN_PORT_END = 22999
FIREWALL_SESSIONS_FILE = BASE / "firewall-sessions.json"
FIREWALL_DEFAULT_MINUTES = 30
FIREWALL_ALLOWED_MINUTES = {15, 30, 60}
FIREWALL_LOCK = threading.RLock()
NPM_URL = os.environ.get("WP_NPM_URL", "http://127.0.0.1:81").rstrip("/")
NPM_EMAIL = os.environ.get("WP_NPM_EMAIL", "")
NPM_PASSWORD = os.environ.get("WP_NPM_PASSWORD", "")
LE_EMAIL = os.environ.get("WP_LE_EMAIL", NPM_EMAIL)

docker_client = docker.from_env()

SITE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[A-Za-z]{2,63}$")

FORGOT_PASSWORD_HTML = r"""
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Forgot Password · {{ platform_title }}</title>
<style>:root{color-scheme:dark;--bg:#07111f;--panel:#0f1b2d;--line:#263850;--text:#e7eef8;--muted:#91a3bb;--blue:#2563eb}*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--text);font-family:Inter,system-ui,Segoe UI,sans-serif}.card{width:min(480px,92vw);background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:28px}.muted{color:var(--muted)}label{display:block;margin:14px 0 5px}input{width:100%;padding:11px;background:#0b1727;border:1px solid var(--line);border-radius:8px;color:#fff}button{width:100%;margin-top:16px;padding:11px;border:0;border-radius:8px;background:var(--blue);color:#fff;font-weight:800}.flash{padding:10px;background:#17263b;border:1px solid var(--line);border-radius:8px;margin-bottom:10px}a{color:#82b6ff;text-decoration:none}</style></head><body><main class="card"><h1>Forgot Password</h1><p class="muted">Enter the email address attached to your account.</p>{% with messages=get_flashed_messages() %}{% for m in messages %}<div class="flash">{{m}}</div>{% endfor %}{% endwith %}<form method="post"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><label>Email Address</label><input type="email" name="email" autocomplete="email" required><button type="submit">Send Reset Link</button></form><p><a href="/login">← Back to login</a></p></main></body></html>
"""

RESET_PASSWORD_HTML = r"""
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Reset Password · {{ platform_title }}</title>
<style>:root{color-scheme:dark;--bg:#07111f;--panel:#0f1b2d;--line:#263850;--text:#e7eef8;--muted:#91a3bb;--blue:#2563eb}*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--text);font-family:Inter,system-ui,Segoe UI,sans-serif}.card{width:min(480px,92vw);background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:28px}label{display:block;margin:14px 0 5px}input{width:100%;padding:11px;background:#0b1727;border:1px solid var(--line);border-radius:8px;color:#fff}button{width:100%;margin-top:16px;padding:11px;border:0;border-radius:8px;background:var(--blue);color:#fff;font-weight:800}.flash{padding:10px;background:#17263b;border:1px solid var(--line);border-radius:8px;margin-bottom:10px}</style></head><body><main class="card"><h1>Reset Password</h1>{% with messages=get_flashed_messages() %}{% for m in messages %}<div class="flash">{{m}}</div>{% endfor %}{% endwith %}<form method="post"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><label>New Password</label><input type="password" name="password" minlength="12" autocomplete="new-password" required><label>Confirm Password</label><input type="password" name="confirm" minlength="12" autocomplete="new-password" required><button type="submit">Set New Password</button></form></main></body></html>
"""

FORCE_PASSWORD_HTML = r"""
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Change Password · {{ platform_title }}</title>
<style>:root{color-scheme:dark;--bg:#07111f;--panel:#0f1b2d;--line:#263850;--text:#e7eef8;--muted:#91a3bb;--blue:#2563eb}*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--text);font-family:Inter,system-ui,Segoe UI,sans-serif}.card{width:min(500px,92vw);background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:28px}.muted{color:var(--muted)}label{display:block;margin:14px 0 5px}input{width:100%;padding:11px;background:#0b1727;border:1px solid var(--line);border-radius:8px;color:#fff}button{width:100%;margin-top:16px;padding:11px;border:0;border-radius:8px;background:var(--blue);color:#fff;font-weight:800}.flash{padding:10px;background:#17263b;border:1px solid var(--line);border-radius:8px;margin-bottom:10px}</style></head><body><main class="card"><h1>{{ 'Set Your Password' if forced else 'Change Password' }}</h1><p class="muted">{{ 'You must replace the temporary password before continuing.' if forced else 'Confirm your current password, then choose a new password.' }}</p>{% with messages=get_flashed_messages() %}{% for m in messages %}<div class="flash">{{m}}</div>{% endfor %}{% endwith %}<form method="post"><input type="hidden" name="csrf_token" value="{{csrf_token()}}">{% if not forced %}<label>Current Password</label><input type="password" name="current_password" autocomplete="current-password" required>{% endif %}<label>New Password</label><input type="password" name="password" minlength="12" autocomplete="new-password" required><label>Confirm Password</label><input type="password" name="confirm" minlength="12" autocomplete="new-password" required><button type="submit">{{ 'Continue' if forced else 'Change Password' }}</button></form></main></body></html>
"""

LOGIN_HTML = r"""
<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ platform_title }} Login</title>
<style>
:root{color-scheme:dark;--bg:#07111f;--panel:#0f1b2d;--line:#263850;--text:#e7eef8;--muted:#91a3bb;--blue:#2563eb}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);font-family:Inter,system-ui,Segoe UI,sans-serif;color:var(--text)}
.box{width:min(420px,92vw);background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:28px}
h1{margin:0 0 6px}p{color:var(--muted)}label{display:block;font-size:12px;color:var(--muted);margin:12px 0 5px}
input{width:100%;padding:11px 12px;border-radius:8px;border:1px solid var(--line);background:#0b1727;color:#fff}
button{width:100%;margin-top:18px;padding:11px;border:0;border-radius:8px;background:var(--blue);color:#fff;font-weight:800}
.flash{padding:10px 12px;background:#17263b;border:1px solid var(--line);border-radius:8px;margin-bottom:12px}
</style></head><body><div class="box">
<h1>{{ platform_name }}</h1><p>Sign in to manage hosted WordPress sites.</p>
{% with messages = get_flashed_messages() %}{% for m in messages %}<div class="flash">{{ m }}</div>{% endfor %}{% endwith %}
<form method="post" action="/login"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<label>Username</label><input name="username" autocomplete="username" required>
<label>Password</label><input type="password" name="password" autocomplete="current-password" required>
<button type="submit">Sign In</button></form><p style="text-align:center"><a href="/forgot-password">Forgot password?</a></p></div></body></html>
"""

EMAIL_SETTINGS_V911_HTML = r"""
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Email Settings · {{ platform_title }}</title>
<style>:root{color-scheme:dark;--bg:#07111f;--panel:#0f1b2d;--line:#263850;--text:#e7eef8;--muted:#91a3bb;--blue:#2563eb;--green:#059669}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,system-ui,Segoe UI,sans-serif}.wrap{max-width:900px;margin:42px auto;padding:0 18px}.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:20px;margin-bottom:18px}.muted{color:var(--muted)}a{color:#82b6ff;text-decoration:none}label{display:block;font-weight:700;margin-top:14px}input,select{width:100%;padding:10px;margin-top:5px;background:#091522;border:1px solid var(--line);border-radius:8px;color:#fff}.grid{display:grid;grid-template-columns:2fr 1fr;gap:12px}.btn{border:0;border-radius:7px;padding:10px 14px;color:white;font-weight:800;cursor:pointer;background:var(--blue);margin-top:16px}.green{background:var(--green)}.flash{background:#162941;border:1px solid var(--line);padding:10px;border-radius:8px;margin-bottom:10px}@media(max-width:700px){.grid{grid-template-columns:1fr}}</style></head><body><div class="wrap"><p><a href="/">← Dashboard</a> · <a href="/users">Users</a></p><h1>Email Settings</h1>
{% with messages=get_flashed_messages() %}{% for m in messages %}<div class="flash">{{m}}</div>{% endfor %}{% endwith %}
<section class="card"><h2>SMTP</h2><p class="muted">Shared by platform alerts, new-user temporary passwords and password recovery.</p><form method="post" action="/admin/email-settings"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><input type="hidden" name="mode" value="save"><div class="grid"><label>SMTP Host<input name="smtp_host" value="{{e.smtp_host}}"></label><label>Port<input name="smtp_port" type="number" value="{{e.smtp_port}}"></label></div><label>Username<input name="smtp_username" value="{{e.smtp_username}}"></label><label>Password<input name="smtp_password" type="password" placeholder="{{'Saved — leave blank to keep current password' if password_saved else 'SMTP password'}}"></label><label>From Address<input name="from_email" type="email" value="{{e.from_email}}"></label><label>Security<select name="security"><option value="starttls" {% if e.security=='starttls' %}selected{% endif %}>STARTTLS</option><option value="ssl" {% if e.security=='ssl' %}selected{% endif %}>SSL/TLS</option><option value="none" {% if e.security=='none' %}selected{% endif %}>None</option></select></label><h3>Platform Alerts</h3><label><input style="width:auto" type="checkbox" name="enabled" {% if e.enabled %}checked{% endif %}> Enable alert emails</label><label>Alert Recipients<input name="recipients" value="{{e.recipients}}"></label><div class="grid"><label>Health check minutes<input name="check_minutes" type="number" min="1" max="60" value="{{e.check_minutes}}"></label><label>Failure threshold<input name="failure_threshold" type="number" min="1" max="10" value="{{e.failure_threshold}}"></label></div><label><input style="width:auto" type="checkbox" name="send_recovery" {% if e.send_recovery %}checked{% endif %}> Send recovery notifications</label><button class="btn">Save Email Settings</button></form></section><section class="card"><h2>Test SMTP</h2><p class="muted">Save settings first. The test is sent to the Alert Recipients above.</p><form method="post" action="/admin/email-settings"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><input type="hidden" name="mode" value="test">{% for name in ['smtp_host','smtp_port','smtp_username','from_email','recipients','security','check_minutes','failure_threshold'] %}<input type="hidden" name="{{name}}" value="{{e[name]}}">{% endfor %}{% if e.enabled %}<input type="hidden" name="enabled" value="on">{% endif %}{% if e.send_recovery %}<input type="hidden" name="send_recovery" value="on">{% endif %}<button class="btn green">Send Test Email</button></form></section></div></body></html>
"""

ALERTS_HTML = r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Security Alerts · {{ platform_title }}</title>
<style>
:root{color-scheme:dark;--bg:#07111f;--side:#081322;--panel:#0f1b2d;--line:#263850;--text:#e7eef8;--muted:#91a3bb;--blue:#2563eb;--green:#059669;--orange:#d97706;--red:#dc2626}
*{box-sizing:border-box}body{margin:0;font-family:Inter,system-ui,Segoe UI,sans-serif;background:var(--bg);color:var(--text)}a{color:#82b6ff;text-decoration:none}
.sidebar{position:fixed;inset:0 auto 0 0;width:224px;background:var(--side);border-right:1px solid var(--line);padding:18px 14px}.content{margin-left:224px;padding:22px}.brand{font-size:20px;font-weight:800;margin-bottom:22px}.nav a{display:block;padding:11px 12px;margin:3px 0;border-radius:8px;color:#c9d5e5}.nav a.active{background:#1d4ed8;color:white}
.card{background:var(--panel);border:1px solid var(--line);border-radius:11px;padding:16px;margin-bottom:14px}.head{display:flex;align-items:center;justify-content:space-between;gap:12px}.badge{padding:5px 8px;border-radius:6px;font-size:11px;font-weight:800}.CRITICAL{background:rgba(220,38,38,.2);color:#fb7185}.HIGH{background:rgba(217,119,6,.2);color:#fbbf24}.MEDIUM{background:rgba(37,99,235,.2);color:#93c5fd}.muted{color:var(--muted);font-size:12px}.btn{border:0;border-radius:7px;color:#fff;padding:8px 11px;background:#2563eb;font-weight:700;cursor:pointer}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin-top:12px}.field{background:#0b1727;border:1px solid var(--line);border-radius:8px;padding:10px}.field b{display:block;font-size:11px;color:var(--muted);margin-bottom:4px}
</style>
</head>
<body>
<aside class="sidebar"><div class="brand">{{ platform_name }}<br><span class="muted">Hosting Manager</span></div><nav class="nav"><a href="/">⌂ Dashboard</a><a class="active" href="/alerts">⚠ Alerts ({{ findings|length }})</a><a href="/#sites">▦ Sites</a><a href="/#logs">▤ Logs</a></nav></aside>
<main class="content">
<div class="head"><div><h1>Security Alerts</h1><p class="muted">Host-level WordPress integrity and redirect monitoring. Resolved findings automatically disappear on the next clean scan.</p></div>
<form method="post" action="/alerts/scan"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="btn">Run Scan Now</button></form></div>

{% with messages = get_flashed_messages() %}{% for m in messages %}<div class="card">{{ m }}</div>{% endfor %}{% endwith %}

{% if not findings %}<div class="card"><h2 style="color:#4ade80">✓ No active security findings</h2><p class="muted">The most recent scan did not reproduce any security alerts.</p></div>{% endif %}

{% for f in findings %}
<section class="card">
<div class="head"><div><span class="badge {{ f.severity }}">{{ f.severity }}</span> <strong>{{ f.site }}</strong></div><span class="muted">Last seen {{ f.last_seen }}</span></div>
<h3>{{ f.message }}</h3>
<div class="grid">
<div class="field"><b>Type</b>{{ f.finding_type }}</div>
<div class="field"><b>Path / Object</b>{{ f.path }}</div>
<div class="field"><b>Email</b>{{ 'Sent' if f.email_sent else 'Not sent / not configured' }}</div>
<div class="field"><b>First Seen</b>{{ f.first_seen }}</div>
</div>
{% if f.evidence and f.evidence != '-' %}<p><b>Evidence:</b> {{ f.evidence }}</p>{% endif %}
</section>
{% endfor %}

<h2>Site Operations & Details</h2>
{% for s in sites %}{% set op=site_ops[s.site] %}
<div class="card"><div class="head"><div><strong>{{ s.site }}</strong><div class="muted">{{ s.domain }} · {{ op.mode|upper }} · External {{ op.last_external_status or '-' }}</div></div></div>
<form method="post" action="/site-ops/{{ s.site }}"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><div class="grid">
<div class="field"><b>Owner</b><input name="owner_name" value="{{ op.owner_name }}"></div><div class="field"><b>Email</b><input name="owner_email" value="{{ op.owner_email }}"></div><div class="field"><b>Phone</b><input name="owner_phone" value="{{ op.owner_phone }}"></div><div class="field"><b>External Monitor URL</b><input name="external_url" value="{{ op.external_url }}"></div></div><p><b>Notes</b><br><textarea name="notes" rows="3" style="width:100%">{{ op.notes }}</textarea></p><button class="btn">Save Details</button></form>
<p><form style="display:inline" method="post" action="/site-mode/{{ s.site }}/maintenance"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="btn">Maintenance</button></form>
<form style="display:inline" method="post" action="/site-mode/{{ s.site }}/quarantine"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="btn" style="background:#dc2626">Quarantine</button></form>
<form style="display:inline" method="post" action="/site-mode/{{ s.site }}/live"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="btn" style="background:#059669">Return Live</button></form>
<form style="display:inline" method="post" action="/staging-clone/{{ s.site }}"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="btn">Create Staging Clone</button></form></p></div>{% endfor %}
<h2>Trusted Baselines</h2>
{% for s in sites %}
<div class="card"><div class="head"><div><strong>{{ s.site }}</strong><div class="muted">{{ s.domain }}</div></div>
<form method="post" action="/alerts/baseline/{{ s.site }}" onsubmit="return confirm('Replace the trusted security baseline for {{ s.site }} with its current state? Only do this when you believe the site is clean.')"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="btn">Set Current State as Trusted</button></form></div></div>
{% endfor %}
</main></body></html>
"""

HTML = r"""
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

SITES_HTML = r"""
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sites · WP Host Manager</title>
<style>
:root{color-scheme:dark;--bg:#07111f;--side:#081322;--panel:#0f1b2d;--line:#263850;--text:#e7eef8;--muted:#91a3bb;--blue:#2563eb;--green:#059669;--orange:#d97706;--red:#dc2626}
*{box-sizing:border-box}body{margin:0;font-family:Inter,system-ui,Segoe UI,sans-serif;background:var(--bg);color:var(--text)}a{color:#82b6ff;text-decoration:none}
.sidebar{position:fixed;inset:0 auto 0 0;width:224px;background:var(--side);border-right:1px solid var(--line);padding:18px 14px}.brand{font-size:19px;font-weight:850;margin:6px 8px 22px}.brand small{display:block;color:var(--muted);font-size:12px}.nav a{display:block;padding:11px 12px;margin:3px 0;border-radius:8px;color:#c9d5e5}.nav a.active,.nav a:hover{background:#1d4ed8;color:#fff}
.main{margin-left:224px;min-height:100vh}.top{height:66px;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:0 24px}.top h1{margin:0;font-size:22px}.content{padding:22px}.toolbar{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:18px}.search{width:min(360px,45vw);background:#0b1727;border:1px solid var(--line);border-radius:8px;color:#fff;padding:10px}.btn{border:0;border-radius:8px;padding:9px 12px;background:var(--blue);color:#fff;font-weight:750;cursor:pointer}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:16px}.site{display:block;background:linear-gradient(180deg,#101e31,#0c1828);border:1px solid var(--line);border-radius:13px;padding:18px;color:var(--text);transition:.15s}.site:hover{transform:translateY(-2px);border-color:#3b82f6}.head{display:flex;justify-content:space-between;gap:12px}.name{font-size:19px;font-weight:850}.domain{color:#82b6ff;margin-top:3px}.badge{font-size:11px;font-weight:800;padding:5px 8px;border-radius:7px;height:max-content}.live{background:#0c4a3a;color:#59e3a0}.bad{background:#541b28;color:#ff8299}.maint{background:#573513;color:#ffc66b}
.stats{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-top:18px}.stat{background:#0a1524;border:1px solid #203249;border-radius:8px;padding:10px}.stat b{display:block;font-size:11px;color:var(--muted);margin-bottom:3px}.footer{display:flex;justify-content:space-between;align-items:center;margin-top:15px;color:var(--muted);font-size:12px}.owner{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
@media(max-width:760px){.sidebar{display:none}.main{margin-left:0}.content{padding:14px}}
</style></head><body>
<aside class="sidebar"><div class="brand">WP Host Manager<small>{{ platform_title }}</small></div><nav class="nav"><a href="/">⌂ Dashboard</a><a class="active" href="/sites">▦ Sites</a>{% if current_role in ['admin','user'] %}<a href="/alerts">⚠ Alerts</a>{% endif %}<a href="/#logs">▤ Logs</a></nav></aside>
<div class="main"><header class="top"><h1>Sites</h1><a href="/">← Dashboard</a></header><main class="content">
<div class="toolbar"><input class="search" placeholder="Search sites..." oninput="filterSites(this.value)"><div>{% if current_role in ['admin','user'] %}<button class="btn" type="button" onclick="const x=document.getElementById('newSite');x.style.display=x.style.display==='none'?'block':'none'">＋ New Site</button>{% endif %} &nbsp; {{ sites|length }} hosted site(s)</div></div>
{% if current_role in ['admin','user'] %}<div id="newSite" style="display:none;background:#0f1b2d;border:1px solid #263850;border-radius:11px;padding:16px;margin-bottom:16px"><h3>Create WordPress Site</h3><form method="post" action="/create"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:9px"><input name="site" required placeholder="Site ID"><input name="domain" required placeholder="Domain"><select name="memory"><option>1g</option><option>2g</option><option>4g</option></select><select name="cpus"><option>1.00</option><option>2.00</option><option>4.00</option></select><select name="db_memory"><option>512m</option><option>1g</option><option>2g</option></select><input name="wp_admin" value="admin" required><input name="wp_email" type="email" placeholder="Admin email" required><input name="wp_password" placeholder="Admin password (blank = generate)"><select name="auto_proxy"><option value="yes">Proxy + SSL</option><option value="no">WordPress only</option></select></div><p><button class="btn">Create Site</button></p></form></div>{% endif %}
<div class="grid" id="sitesGrid">
{% for s in sites %}
<a class="site" href="/site/{{ s.site }}" data-find="{{ (s.site ~ ' ' ~ s.domain ~ ' ' ~ s.status ~ ' ' ~ s.owner_name)|lower }}">
<div class="head"><div><div class="name">{{ s.site }}</div><div class="domain">{{ s.domain }}</div></div>
<span class="badge {{ 'live' if s.status=='running' and s.mode=='live' else 'maint' if s.mode=='maintenance' else 'bad' }}">{{ s.mode|upper if s.mode!='live' else s.status|upper }}</span></div>
<div class="stats"><div class="stat"><b>Health</b>{{ s.health }}</div><div class="stat"><b>WordPress</b>{{ s.wp_version }}</div><div class="stat"><b>CPU / RAM</b>{{ s.live_cpu }}% / {{ s.live_memory }}</div><div class="stat"><b>SFTP</b>{{ ('ON · ' ~ s.sftp_port) if s.sftp_enabled else 'OFF' }}</div><div class="stat"><b>Backup</b>{{ s.backup }}</div></div>
<div class="footer"><span class="owner">{{ s.owner_name or 'No owner assigned' }}</span><span>Open dashboard →</span></div>
</a>
{% endfor %}
</div></main></div>
<script>function filterSites(q){q=(q||'').toLowerCase();document.querySelectorAll('.site').forEach(x=>x.style.display=x.dataset.find.includes(q)?'block':'none')}</script>
</body></html>
"""

SITE_HTML = r"""
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ site.domain }} · WP Host Manager</title>
<style>
:root{color-scheme:dark;--bg:#07111f;--side:#081322;--panel:#0f1b2d;--panel2:#0b1727;--line:#263850;--text:#e7eef8;--muted:#91a3bb;--blue:#2563eb;--green:#059669;--orange:#d97706;--red:#dc2626;--purple:#7c3aed}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,system-ui,Segoe UI,sans-serif}a{color:#60a5fa;text-decoration:none}.sidebar{position:fixed;inset:0 auto 0 0;width:224px;background:var(--side);border-right:1px solid var(--line);padding:18px 14px}.brand{font-size:18px;font-weight:850;margin:5px 8px 22px}.nav a{display:block;padding:11px 12px;margin:3px 0;border-radius:8px;color:#c9d5e5}.nav a.active,.nav a:hover{background:#173c7a;color:#fff}.main{margin-left:224px;padding:22px;min-height:100vh}.back{display:inline-block;margin-bottom:15px;color:#d6e0ed}.hero{display:grid;grid-template-columns:1fr 370px;gap:16px}.card{background:linear-gradient(180deg,#101e31,#0d192a);border:1px solid var(--line);border-radius:12px;padding:17px}.title{font-size:27px;font-weight:900;margin:0 0 5px}.chips{display:flex;gap:8px;flex-wrap:wrap;margin-top:13px}.chip{background:#17263b;padding:6px 9px;border-radius:7px;font-size:12px}.good{color:#4ade80}.bad{color:#fb7185}.warn{color:#fbbf24}.statusline{font-size:18px;font-weight:850}.metrics{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin:16px 0}.metric{background:var(--panel);border:1px solid var(--line);border-radius:11px;padding:15px}.metric b{display:block;color:var(--muted);font-size:12px;margin-bottom:7px}.metric strong{font-size:20px}.actionsbar{display:grid;grid-template-columns:1.2fr .8fr auto;gap:20px;align-items:end}.sectiontitle{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);margin-bottom:8px}.buttonrow{display:flex;gap:9px;flex-wrap:wrap}.btn{border:1px solid #2f5ea5;background:#12294b;color:#fff;border-radius:8px;padding:10px 16px;font-weight:800;cursor:pointer}.btn.green{background:#0a493b;border-color:#087e61}.btn.orange{background:#5b360e;border-color:#b5670b}.btn.red{background:#561b29;border-color:#c62b43}.btn.purple{background:#34205b;border-color:#7241b5}.access{display:flex;gap:9px}.morewrap{position:relative}.morepanel{display:none;position:absolute;right:0;top:50px;width:760px;z-index:20;background:#0b1727;border:1px solid #31506f;border-radius:12px;padding:13px;box-shadow:0 18px 50px #0009}.morewrap.open .morepanel{display:grid;grid-template-columns:repeat(5,1fr);gap:9px}.moreitem{border:1px solid var(--line);border-radius:9px;padding:10px}.moreitem p{font-size:11px;color:var(--muted);min-height:42px}.moreitem .btn{width:100%;padding:8px}.tabs{display:flex;gap:4px;margin-top:17px;border-bottom:1px solid var(--line);overflow-x:auto}.tab{padding:11px 15px;color:#b8c5d6;cursor:pointer;border-bottom:2px solid transparent}.tab.active{color:#60a5fa;border-color:#3b82f6}.tabpane{display:none;padding-top:16px}.tabpane.active{display:block}.info{display:grid;grid-template-columns:repeat(3,1fr);gap:13px}.info .card .row{display:flex;justify-content:space-between;gap:12px;margin:9px 0;font-size:13px}.table{width:100%;border-collapse:collapse;font-size:12px}.table th,.table td{text-align:left;padding:9px;border-bottom:1px solid var(--line)}.table th{color:#93c5fd}.flash{background:#162941;border:1px solid var(--line);padding:10px;border-radius:8px;margin-bottom:10px}.note{white-space:pre-wrap;color:#cad5e3}.fields{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}.fields input,.fields textarea{width:100%;background:#091522;border:1px solid var(--line);color:#fff;border-radius:7px;padding:9px}
@media(max-width:1150px){.hero{grid-template-columns:1fr}.metrics{grid-template-columns:repeat(2,1fr)}.actionsbar{grid-template-columns:1fr}.morepanel{position:fixed;left:10%;right:10%;width:auto;top:20%}.info{grid-template-columns:1fr}}@media(max-width:760px){.sidebar{display:none}.main{margin-left:0;padding:13px}.metrics{grid-template-columns:1fr}}
</style></head><body>
<aside class="sidebar"><div class="brand">WP Host Manager</div><nav class="nav"><a href="/">⌂ Dashboard</a><a class="active" href="/sites">▦ Sites</a>{% if current_role in ['admin','user'] %}<a href="/alerts">⚠ Alerts {% if security_count %}<span class="bad">({{ security_count }})</span>{% endif %}</a>{% endif %}<a href="/#logs">▤ Logs</a></nav></aside>
<main class="main">
<a class="back" href="/sites">← Back to Sites</a>
{% with messages=get_flashed_messages() %}{% for m in messages %}<div class="flash">{{ m }}</div>{% endfor %}{% endwith %}
<div class="hero">
<section class="card"><h1 class="title">{{ site.domain }}</h1><a target="_blank" href="https://{{ site.domain }}">https://{{ site.domain }} ↗</a>
<div class="chips"><span class="chip">WordPress {{ site.wp_version }}</span><span class="chip">PHP {{ runtime.php_version }}</span><span class="chip">Plugins {{ runtime.active_plugins }}</span><span class="chip">Container {{ site.site }}-wp</span></div></section>
<section class="card"><div class="statusline {{ 'good' if site.status=='running' else 'bad' }}">● {{ site.status|title }}</div><div class="row">Mode: <b>{{ site.mode|upper }}</b></div><div class="row">Uptime: <b>{{ runtime.uptime }}</b></div><div class="row">Last backup: <b>{{ site.backup }}</b></div><div class="row">Restarts: <b>{{ runtime.restart_count }}</b></div></section>
</div>

<div class="metrics"><div class="metric"><b>CPU Usage</b><strong>{{ site.live_cpu }}%</strong></div><div class="metric"><b>RAM Usage</b><strong>{{ site.live_memory }}</strong><div>{{ site.memory }} limit</div></div><div class="metric"><b>Storage</b><strong>{{ site.storage }}</strong></div><div class="metric"><b>Core Integrity</b><strong class="{{ 'good' if runtime.core_checksum=='Verified' else 'bad' }}">{{ runtime.core_checksum }}</strong></div><div class="metric"><b>External Uptime</b><strong>{{ ops.last_external_status or '-' }}</strong><div>{{ ops.last_external_ms or '-' }} ms</div></div></div>

<section class="card actionsbar">
<div><div class="sectiontitle">Actions</div><div class="buttonrow">
{% if current_role in ['admin','user'] %}
<form method="post" action="/action/{{ site.site }}/restart"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="btn">↻ Restart</button></form>
<form method="post" action="/action/{{ site.site }}/update"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="btn">⇧ Update</button></form>
<form method="post" action="/action/{{ site.site }}/backup"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="btn green">☁ Backup</button></form>
<form method="post" action="/action/{{ site.site }}/restore-latest"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="btn green">↶ Restore</button></form>
{% else %}<span class="muted">Read only</span>{% endif %}
</div></div>
<div><div class="sectiontitle">Access</div><div class="access">
<span class="btn green">{{ '✓ SSL Active' if site.proxy_ssl.startswith('SSL') else 'SSL Not Provisioned' }}</span>
{% if current_role in ['admin','user'] %}
  {% if site.sftp_enabled %}
    {% if sftp_access.open %}
    <form method="post" action="/access/{{ site.site }}/sftp/close"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="btn purple" type="submit">SFTP Firewall: OPEN · Close Now</button></form>
    {% else %}
    <form method="post" action="/access/{{ site.site }}/sftp/open/30"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><input type="hidden" name="source_ip" value="{{ client_ip }}"><button class="btn purple" type="submit">SFTP: Open 30m · {{ site.sftp_port }}</button></form>
    {% endif %}
    <form method="post" action="/action/{{ site.site }}/sftp-disable"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="btn purple" type="submit">Disable SFTP Container</button></form>
    <form method="post" action="/action/{{ site.site }}/sftp-reset-password"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="btn purple" type="submit">Reset SFTP Password</button></form>
  {% else %}
    <form method="post" action="/action/{{ site.site }}/sftp-enable"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="btn purple" type="submit">SFTP: OFF · Enable</button></form>
  {% endif %}
{% else %}
<span class="btn purple">SFTP: {{ ('ON · Port ' ~ site.sftp_port) if site.sftp_enabled else 'OFF' }}</span>
{% endif %}
{% if current_role in ['admin','user'] %}
  {% if pma_access.open %}
  <a class="btn green" target="_blank" rel="noopener" href="http://{{ manager_public_host }}:{{ site.phpmyadmin_port }}">phpMyAdmin OPEN ↗</a>
  <form method="post" action="/access/{{ site.site }}/phpmyadmin/close"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="btn red" type="submit">Close phpMyAdmin Firewall</button></form>
  {% else %}
  <form method="post" target="_blank" action="/access/{{ site.site }}/phpmyadmin/open/30"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><input type="hidden" name="source_ip" value="{{ client_ip }}"><input type="hidden" name="launch" value="1"><button class="btn" type="submit">phpMyAdmin · Open 30m ↗</button></form>
  {% endif %}
{% endif %}
</div></div>
{% if current_role in ['admin','user'] %}<div class="morewrap" id="moreWrap"><button class="btn" type="button" onclick="document.getElementById('moreWrap').classList.toggle('open')">More ⋯</button><div class="morepanel">
<div class="moreitem"><b class="warn">Maintenance</b><p>Show a maintenance page while keeping the site online.</p><form method="post" action="/site-mode/{{ site.site }}/maintenance"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="btn orange">Maintenance</button></form></div>
<div class="moreitem"><b>Staging</b><p>Create an isolated WordPress and database clone.</p><form method="post" action="/staging-clone/{{ site.site }}"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="btn">Clone</button></form></div>
<div class="moreitem"><b class="bad">Quarantine</b><p>Immediately remove this WordPress container from public proxy access.</p><form method="post" action="/site-mode/{{ site.site }}/quarantine"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="btn red">Quarantine</button></form></div>
<div class="moreitem"><b>Stop</b><p>Stop the WordPress container.</p><form method="post" action="/action/{{ site.site }}/stop"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="btn">Stop Site</button></form></div>
<div class="moreitem"><b class="bad">Delete</b><p>Delete the site containers and associated site record.</p><form method="post" action="/action/{{ site.site }}/delete" onsubmit="return confirm('Delete {{ site.site }}?')"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="btn red">Delete</button></form></div>
</div></div>{% endif %}
</section>

<nav class="tabs">{% for id,label in [('overview','Overview'),('backups','Backups'),('plugins','Plugins'),('themes','Themes'),('database','Database'),('logs','Logs'),('security','Security'),('migration','Migration'),('settings','Settings')] %}<div class="tab {{ 'active' if loop.first else '' }}" onclick="showTab('{{ id }}',this)">{{ label }}</div>{% endfor %}</nav>

<section class="tabpane active" id="tab-overview"><div class="info">
<div class="card"><h3>Site Information</h3><div class="row"><span>Site ID</span><b>{{ site.site }}</b></div><div class="row"><span>Domain</span><b>{{ site.domain }}</b></div><div class="row"><span>WordPress</span><b>{{ site.wp_version }}</b></div><div class="row"><span>PHP</span><b>{{ runtime.php_version }}</b></div><div class="row"><span>Owner</span><b>{{ ops.owner_name or '-' }}</b></div></div>
<div class="card"><h3>Resource Limits</h3><div class="row"><span>CPU</span><b>{{ site.cpu }} cores</b></div><div class="row"><span>RAM</span><b>{{ site.memory }}</b></div><div class="row"><span>Current RAM</span><b>{{ site.live_memory }}</b></div><div class="row"><span>Restarts</span><b>{{ runtime.restart_count }}</b></div></div>
<div class="card"><h3>Database</h3><div class="row"><span>Name</span><b>{{ runtime.db_name }}</b></div><div class="row"><span>User</span><b>{{ runtime.db_user }}</b></div><div class="row"><span>Host</span><b>{{ runtime.db_host }}</b></div><div class="row"><span>Health</span><b>{{ site.db_health }}</b></div></div>
</div></section>

<section class="tabpane" id="tab-backups"><div class="card"><h3>Backup History</h3><table class="table"><tr><th>Backup</th><th>Created (Adelaide)</th><th>Size</th></tr>{% for b in backups %}<tr><td>{{ b.name }}</td><td>{{ b.created }}</td><td>{{ b.size }}</td></tr>{% else %}<tr><td colspan="3">No backups found.</td></tr>{% endfor %}</table></div></section>
<section class="tabpane" id="tab-plugins"><div class="card"><h3>Plugins</h3><table class="table"><tr><th>Plugin</th><th>Version</th></tr>{% for p in runtime.plugin_items %}<tr><td>{{ p.name }}</td><td>{{ p.version }}</td></tr>{% else %}<tr><td colspan="2">No plugin inventory available.</td></tr>{% endfor %}</table></div></section>
<section class="tabpane" id="tab-themes"><div class="card"><h3>Themes</h3><table class="table"><tr><th>Theme</th><th>Version</th></tr>{% for p in runtime.theme_items %}<tr><td>{{ p.name }}</td><td>{{ p.version }}</td></tr>{% else %}<tr><td colspan="2">No theme inventory available.</td></tr>{% endfor %}</table></div></section>
<section class="tabpane" id="tab-database"><div class="card"><h3>Database</h3><p>Database host: <b>{{ runtime.db_host }}</b></p><p>Database name: <b>{{ runtime.db_name }}</b></p><p>Database user: <b>{{ runtime.db_user }}</b></p>{% if current_role in ['admin','user'] %}{% if pma_access.open %}<a class="btn green" target="_blank" rel="noopener" href="http://{{ manager_public_host }}:{{ site.phpmyadmin_port }}">Open phpMyAdmin ↗</a> <form method="post" action="/access/{{ site.site }}/phpmyadmin/close" style="display:inline"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="btn red">Close Firewall</button></form>{% else %}<form method="post" target="_blank" action="/access/{{ site.site }}/phpmyadmin/open/30"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><input type="hidden" name="source_ip" value="{{ client_ip }}"><input type="hidden" name="launch" value="1"><button class="btn">Open phpMyAdmin for 30m ↗</button></form>{% endif %}{% endif %}</div></section>
<section class="tabpane" id="tab-logs"><div class="card"><h3>Site Audit Log</h3><table class="table"><tr><th>Time (Adelaide)</th><th>User</th><th>Action</th><th>Result</th><th>Detail</th></tr>{% for x in audits %}<tr><td>{{ x.timestamp }}</td><td>{{ x.username }}</td><td>{{ x.action }}</td><td>{{ x.result }}</td><td>{{ x.detail }}</td></tr>{% else %}<tr><td colspan="5">No site-specific audit events.</td></tr>{% endfor %}</table></div></section>
<section class="tabpane" id="tab-security"><div class="card"><h3>Security</h3><p>WordPress core checksums: <b class="{{ 'good' if runtime.core_checksum=='Verified' else 'bad' }}">{{ runtime.core_checksum }}</b></p><table class="table"><tr><th>Severity</th><th>Finding</th><th>Path</th><th>Last Seen (Adelaide)</th></tr>{% for f in findings %}<tr><td>{{ f.severity }}</td><td>{{ f.message }}</td><td>{{ f.path }}</td><td>{{ f.last_seen }}</td></tr>{% else %}<tr><td colspan="4">No active security findings.</td></tr>{% endfor %}</table></div></section>
{% if site.sftp_enabled %}<section class="card" style="margin-top:16px"><h3>SFTP Access</h3><div class="row"><span>Status</span><b class="good">ENABLED</b></div><div class="row"><span>Host</span><b>{{ manager_public_host }}</b></div><div class="row"><span>Allocated Port</span><b>{{ site.sftp_port }}</b></div><div class="row"><span>Username</span><b>wordpress</b></div><div class="row"><span>Protocol</span><b>SFTP / SSH</b></div><p class="muted">Use the allocated port shown above. The password is shown only when SFTP is enabled/reset. Use SFTP, not FTP/FTPS.</p></section>{% endif %}
<section class="tabpane" id="tab-migration">
<div class="card">
<h3>WordPress Migration / Import</h3><p class="muted">Technician workflow: pre-flight, safe first boot, component quarantine, full validation and rollback. No client-facing status is generated.</p>
<p class="muted">Upload one complete WordPress ZIP and one SQL dump using SFTP. V8 preserves the source package, takes a rollback backup, rebuilds the live site, normalises Docker/HTTPS configuration and validates the result.</p>
<div class="fields"><div><b>Target URL</b><br>https://{{ site.domain }}</div><div><b>Source folder</b><br>/opt/wp-host/sites/{{ site.site }}/migration-source</div></div>
<h4>Detected Source Files</h4>
<table class="table"><tr><th>File</th><th>Type</th><th>Size</th><th>Modified (Adelaide)</th></tr>
{% for f in migration_files %}<tr><td>{{ f.name }}</td><td>{{ f.kind }}</td><td>{{ f.size }}</td><td>{{ f.modified }}</td></tr>
{% else %}<tr><td colspan="4">No ZIP/SQL source files detected. Files placed in the site's WordPress/SFTP upload folder are detected automatically.</td></tr>{% endfor %}</table>
{% if current_role in ['admin','user'] %}
<form method="post" action="/site/{{ site.site }}/migrate" onsubmit="return confirm('This replaces the live WordPress files and recreates/imports the site database after taking a rollback backup. Continue?')">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<p><label>Old URL override (optional)<br><input name="old_url" style="width:100%;max-width:560px" placeholder="https://old-domain.example"></label></p>
<button class="btn orange">Start Migration</button>
</form>{% endif %}
</div>
{% if migration_report %}<div class="card"><h3>Latest Migration Report</h3>
<p><b>Status:</b> <span class="{{ 'good' if migration_report.status=='success' else 'warn' if migration_report.status=='attention' else 'bad' }}">{{ migration_report.status|upper }}</span></p>
{% if migration_report.rollback_path and current_role in ['admin','user'] %}
<form method="post" action="/site/{{ site.site }}/migration-rollback" onsubmit="return confirm('Rollback this site to the pre-migration files and database?');" style="margin:10px 0">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<button class="btn red">Rollback Migration</button>
</form>
{% endif %}
<p><b>Started:</b> {{ migration_report.started or '-' }} &nbsp; <b>Finished:</b> {{ migration_report.finished or '-' }}</p>
<p><b>Source:</b> {{ migration_report.zip or '-' }} + {{ migration_report.sql or '-' }}</p>
{% if migration_report.old_url %}<p><b>URL:</b> {{ migration_report.old_url }} → {{ migration_report.new_url }}</p>{% endif %}
<table class="table"><tr><th>Step</th><th>Result</th><th>Detail</th></tr>{% for s in migration_report.steps or [] %}<tr><td>{{ s.name }}</td><td class="{{ 'good' if s.ok else 'bad' }}">{{ 'PASS' if s.ok else 'FAIL' }}</td><td>{{ s.detail }}</td></tr>{% endfor %}</table>
{% if migration_report.checks %}<h4>Validation</h4><table class="table"><tr><th>Check</th><th>Result</th><th>Detail</th></tr>{% for c in migration_report.checks %}<tr><td>{{ c.check }}</td><td class="{{ 'good' if c.ok else 'bad' }}">{{ 'PASS' if c.ok else 'FAIL' }}</td><td>{{ c.detail }}</td></tr>{% endfor %}</table>{% endif %}
</div>{% endif %}
</section>
<section class="tabpane" id="tab-settings"><div class="card"><h3>Owner & Site Details</h3><form method="post" action="/site-ops/{{ site.site }}"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><div class="fields"><input name="owner_name" value="{{ ops.owner_name }}" placeholder="Owner name"><input name="owner_email" value="{{ ops.owner_email }}" placeholder="Owner email"><input name="owner_phone" value="{{ ops.owner_phone }}" placeholder="Owner phone"><input name="external_url" value="{{ ops.external_url }}" placeholder="External monitor URL"></div><p><textarea name="notes" rows="5" placeholder="Site notes">{{ ops.notes }}</textarea></p>{% if current_role in ['admin','user'] %}<button class="btn" type="submit">Save Site Details</button>{% endif %}</form></div></section>
</main>
<script>
function showTab(id,el){document.querySelectorAll('.tabpane').forEach(x=>x.classList.remove('active'));document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));document.getElementById('tab-'+id).classList.add('active');el.classList.add('active')}
document.addEventListener('click',e=>{const w=document.getElementById('moreWrap');if(w&&w.classList.contains('open')&&!w.contains(e.target))w.classList.remove('open')})
</script></body></html>
"""


HTML = r"""
<!doctype html>
<html data-theme="{{ current_theme }}">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ platform_title }}</title>
<style>
:root{color-scheme:dark;--bg:#07111f;--side:#081322;--panel:#0f1b2d;--line:#263850;--text:#e7eef8;--muted:#91a3bb;--blue:#2563eb;--green:#059669;--orange:#d97706;--red:#dc2626}
[data-theme="light"]{color-scheme:light;--bg:#f4f6fb;--side:#ffffff;--panel:#ffffff;--line:#dde3ee;--text:#101828;--muted:#5b6b83;--blue:#2563eb;--green:#059669;--orange:#d97706;--red:#dc2626}
[data-theme="forest"]{color-scheme:dark;--bg:#07130f;--side:#081a13;--panel:#0e2019;--line:#1f3a2d;--text:#e7f3ec;--muted:#8fb3a0;--blue:#2563eb;--green:#22c55e;--orange:#d97706;--red:#dc2626}
[data-theme="sunset"]{color-scheme:dark;--bg:#1a0f07;--side:#221208;--panel:#2a170c;--line:#4a2c17;--text:#fbeee3;--muted:#c9a488;--blue:#3b82f6;--green:#059669;--orange:#f59e0b;--red:#ef4444}
*{box-sizing:border-box}body{margin:0;font-family:Inter,system-ui,Segoe UI,sans-serif;background:var(--bg);color:var(--text)}a{color:#82b6ff;text-decoration:none}
.sidebar{position:fixed;inset:0 auto 0 0;width:224px;background:var(--side);border-right:1px solid var(--line);padding:18px 14px;display:flex;flex-direction:column}.content{margin-left:224px}
.brand{display:flex;gap:12px;align-items:center;padding:4px 7px 20px}.brandmark{width:42px;height:42px;border-radius:50%;display:grid;place-items:center;background:#155eef;font-weight:900}.brand strong{font-size:18px}.brand small{display:block;color:var(--muted)}
.nav a{display:block;padding:11px 12px;margin:3px 0;border-radius:8px;color:var(--muted)}.nav a:hover,.nav a.active{background:#1d4ed8;color:#fff}.sidefoot{margin-top:auto}
.health,.userbox,.section,.kpi,.flash{background:var(--panel);border:1px solid var(--line);border-radius:10px}.health{padding:13px}.health h4{margin:0 0 12px;color:#55db92}.mrow{display:flex;justify-content:space-between;font-size:12px;margin:8px 0 4px}.track{height:5px;background:#203047;border-radius:9px;overflow:hidden}.track b{display:block;height:100%;background:#19b56b}
.userbox{display:flex;gap:9px;align-items:center;padding:12px;margin-top:14px}.avatar{width:34px;height:34px;border-radius:50%;display:grid;place-items:center;background:#293a52}
.top{height:66px;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:0 22px;position:sticky;top:0;background:rgba(7,17,31,.95);z-index:3}.top h1{margin:0;font-size:22px}
.search,input,select{background:#0b1727;border:1px solid var(--line);border-radius:8px;color:#fff;padding:9px 10px}.search{width:260px}main{padding:20px 22px 38px}.flash{padding:10px 12px;margin-bottom:10px}.section{padding:16px;margin-bottom:18px}
.kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:14px;margin-bottom:18px}.kpi{padding:16px}.kpi strong{display:block;font-size:22px;margin-bottom:2px}.kpi small{display:block;color:var(--muted);font-size:12px;margin-top:2px}.muted{color:var(--muted);font-size:12px}.sectionhead,.toolbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}.sectionhead h2{margin:0;font-size:17px}
.btn{border:0;border-radius:7px;color:white;padding:8px 10px;font-weight:700;font-size:12px;cursor:pointer;background:#475569}.blue{background:var(--blue)}.green{background:var(--green)}.orange{background:var(--orange)}.red{background:var(--red)}
.siteswrap{overflow-x:auto}.sitecard{min-width:1380px;display:grid;grid-template-columns:220px 140px 215px 175px 180px 155px 135px 175px;border:1px solid var(--line);border-radius:10px;background:#0c1828;margin:12px 0;overflow:hidden}.cell{padding:15px;border-right:1px solid var(--line);min-height:185px}.cell:last-child{border-right:0}.title{font-size:17px;font-weight:800}.label{text-transform:uppercase;font-size:10px;letter-spacing:.06em;color:var(--muted);margin-bottom:8px}.row{display:flex;justify-content:space-between;gap:8px;font-size:12px;margin:10px 0}.good{color:#4ade80;font-weight:800}.bad{color:#fb7185;font-weight:800}.warn{color:#fbbf24;font-weight:800}.pill{display:inline-block;padding:5px 8px;border-radius:6px;margin-top:8px;background:#17334b;font-size:11px}
.actions{display:grid;grid-template-columns:1fr 1fr;gap:6px}.actions .wide{grid-column:1/-1}.actions button{width:100%}.audit{max-height:350px;overflow:auto}table{width:100%;border-collapse:collapse;font-size:12px}th,td{padding:8px;border-bottom:1px solid var(--line);text-align:left}th{color:#8ec1ff}.formgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}
@media(max-width:1000px){.kpis{grid-template-columns:repeat(2,1fr)}}@media(max-width:760px){.sidebar{display:none}.content{margin-left:0}.kpis{grid-template-columns:1fr}.search{display:none}main{padding:12px}}
</style></head><body>
<aside class="sidebar">
<div class="brand"><div class="brandmark">{{ platform_name[:1]|upper }}</div><div><strong>{{ platform_name }}</strong><small>Hosting Manager</small></div></div>
<nav class="nav"><a class="active" href="/">⌂ Dashboard</a><a href="/sites">▦ Sites</a><a href="#backups">◫ Backups</a>{% if current_role in ['admin','user'] %}<a href="/alerts">⚠ Alerts {% if security_alert_count %}<span class="bad">({{ security_alert_count }})</span>{% endif %}</a>{% endif %}{% if current_role == 'admin' %}<a href="/users">♟ Users</a><a href="/admin/email-settings">✉ Email Settings</a><a href="/admin/backup-destination">💾 Backup Destination</a><a href="/admin/ports">⇄ Port Manager</a>{% endif %}<a href="#system">⚙ System</a><a href="#logs">▤ Logs</a></nav>
<div class="sidefoot"><div class="health"><h4>● System Healthy</h4><div class="mrow"><span>CPU</span><span>{{ host_stats.cpu }}%</span></div><div class="track"><b style="width:{{ host_stats.cpu }}%"></b></div><div class="mrow"><span>RAM</span><span>{{ host_stats.ram_percent }}%</span></div><div class="track"><b style="width:{{ host_stats.ram_percent }}%"></b></div>{% for d in host_stats.disks %}<div class="mrow"><span>{{ d.mountpoint }}</span><span>{{ d.percent }}%</span></div><div class="track"><b style="width:{{ d.percent }}%"></b></div>{% endfor %}</div>
<div class="userbox"><div class="avatar">{{ current_user[:1]|upper }}</div><div><strong>{{ current_user }}</strong><div class="muted">{{ current_role|title }}</div></div></div><p><a href="/logout">⇱ Log out</a></p></div>
</aside>
<div class="content"><header class="top"><h1>Dashboard</h1><div><form method="post" action="/account/theme" style="display:inline-block;margin-right:8px"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><select name="theme" onchange="this.form.submit()" title="Colour scheme"><option value="midnight" {{ 'selected' if current_theme=='midnight' else '' }}>🌙 Midnight</option><option value="light" {{ 'selected' if current_theme=='light' else '' }}>☀ Light</option><option value="forest" {{ 'selected' if current_theme=='forest' else '' }}>🌲 Forest</option><option value="sunset" {{ 'selected' if current_theme=='sunset' else '' }}>🌇 Sunset</option></select></form><input class="search" placeholder="Search sites..." oninput="filterSites(this.value)"> <a class="btn blue" href="/">↻ Refresh</a></div></header><main>
{% with messages = get_flashed_messages() %}{% for m in messages %}<div class="flash">{{ m }}</div>{% endfor %}{% endwith %}
<div class="kpis"><div class="kpi"><strong>{{ site_summary.total }}</strong>Total Sites<small>All sites on this server</small></div><div class="kpi"><strong class="good">{{ site_summary.running }}</strong>Running<small>Active and healthy</small></div><div class="kpi"><strong class="warn">{{ site_summary.not_created }}</strong>Not Created<small>Awaiting setup</small></div><div class="kpi"><strong>{{ site_summary.backups }}</strong>Total Backups<small>Across all sites</small></div><div class="kpi"><strong>{{ host_stats.disk_used }}</strong>Storage Used<small>Host filesystem</small></div></div>
{% if alerts %}<section class="section"><div class="sectionhead"><h2>Active Alerts</h2></div>{% for a in alerts %}<div class="flash"><b>{{ a.site }}</b>: {{ a.message }}</div>{% endfor %}</section>{% endif %}
<section class="section">
<div class="sectionhead"><h2>Site Overview</h2><a class="btn blue" href="/sites">Open Sites →</a></div>
<div class="kpis" style="margin:0">
<div class="kpi"><strong>{{ site_summary.total }}</strong>Total Sites<small>All hosted WordPress sites</small></div>
<div class="kpi"><strong class="good">{{ site_summary.running }}</strong>Healthy<small>Running and healthy</small></div>
<div class="kpi"><strong class="warn">{{ site_summary.not_created }}</strong>Attention<small>Stopped, unhealthy or awaiting setup</small></div>
<div class="kpi"><strong>{{ site_summary.backups }}</strong>Backups<small>Sites with backup history</small></div>
<div class="kpi"><strong>{{ security_alert_count }}</strong>Security Alerts<small>Active findings</small></div>
</div></section>

<section class="section">
<div class="sectionhead"><h2>Recent Security Alerts</h2>{% if current_role in ['admin','user'] %}<a class="btn blue" href="/alerts">View Alerts →</a>{% endif %}</div>
{% if recent_security %}<table><thead><tr><th>Site</th><th>Severity</th><th>Finding</th><th>Last Seen</th></tr></thead><tbody>
{% for f in recent_security %}<tr><td>{{ f.site }}</td><td>{{ f.severity }}</td><td>{{ f.message }}</td><td>{{ f.last_seen }}</td></tr>{% endfor %}
</tbody></table>{% else %}<div class="flash">No active security alerts.</div>{% endif %}
</section>

<section class="section">
<div class="sectionhead"><h2>Recent Activity</h2><a class="btn blue" href="#logs">View Logs ↓</a></div>
<table><thead><tr><th>Time</th><th>User</th><th>Action</th><th>Target</th><th>Result</th></tr></thead><tbody>
{% for a in recent_activity %}<tr><td>{{ a.timestamp }}</td><td>{{ a.username }}</td><td>{{ a.action }}</td><td>{{ a.target }}</td><td>{{ a.result }}</td></tr>
{% else %}<tr><td colspan="5">No recent activity.</td></tr>{% endfor %}
</tbody></table></section>

<section class="section" id="system"><div class="sectionhead"><h2>System</h2></div><div class="kpis" style="margin:0"><div class="kpi"><strong>{{ host_stats.cpu }}%</strong>CPU</div><div class="kpi"><strong>{{ host_stats.ram_percent }}%</strong>RAM</div><div class="kpi"><strong>{{ host_stats.disk_percent }}%</strong>Disk</div><div class="kpi"><strong>{{ host_stats.uptime }}</strong>Uptime</div><div class="kpi"><strong>{{ host_stats.docker_running }}/{{ host_stats.docker_total }}</strong>Docker</div></div>
<table style="margin-top:16px"><tr><th>Mountpoint</th><th>Device</th><th>FS Type</th><th>Used</th><th>Total</th><th>Free</th><th>Used %</th></tr>{% for d in host_stats.disks %}<tr><td>{{ d.mountpoint }}</td><td>{{ d.device }}</td><td>{{ d.fstype }}</td><td>{{ d.used }}</td><td>{{ d.total }}</td><td>{{ d.free }}</td><td><span class="{{ 'bad' if d.percent >= 90 else ('warn' if d.percent >= 75 else 'good') }}">{{ d.percent }}%</span></td></tr>{% else %}<tr><td colspan="7">No disks detected.</td></tr>{% endfor %}</table>
</section>
<section class="section" id="logs"><div class="sectionhead"><h2>Platform Action Audit</h2></div><div class="audit"><table><thead><tr><th>Time</th><th>User</th><th>Action</th><th>Target</th><th>Result</th><th>IP</th><th>Detail</th></tr></thead><tbody>{% for a in audit_events %}<tr><td>{{ a.timestamp }}</td><td>{{ a.username }}</td><td>{{ a.action }}</td><td>{{ a.target }}</td><td>{{ a.result }}</td><td>{{ a.ip }}</td><td>{{ a.detail }}</td></tr>{% endfor %}</tbody></table></div></section>
</main></div>
<script>function filterSites(q){q=(q||'').toLowerCase();document.querySelectorAll('.sitecard').forEach(x=>x.style.display=(x.dataset.site+' '+x.dataset.domain+' '+x.dataset.status).includes(q)?'grid':'none')}function sortSites(){const k=document.getElementById('sorter').value,w=document.getElementById('sitecards');[...w.querySelectorAll('.sitecard')].sort((a,b)=>(a.dataset[k]||'').localeCompare(b.dataset[k]||'')).forEach(x=>w.appendChild(x))}</script>
</body></html>
"""

def init_auth():
    BASE.mkdir(parents=True, exist_ok=True)

    if not USERS_FILE.exists():
        users = {
            AUTH_USER: {
                "password_hash": (
                    BOOTSTRAP_HASH if BOOTSTRAP_HASH.startswith(("scrypt:", "pbkdf2:"))
                    else f"legacy_sha256${BOOTSTRAP_SALT}${BOOTSTRAP_HASH}"
                ),
                "email": AUTH_EMAIL,
                "enabled": True,
                "role": "admin",
                "force_password_change": False,
                "mfa_enabled": False,
                "mfa_required": True,
                "mfa_recovery_hashes": [],
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
            if "email" not in entry:
                entry["email"] = ""
                changed = True
            if "force_password_change" not in entry:
                entry["force_password_change"] = False
                changed = True
            if "mfa_enabled" not in entry:
                entry["mfa_enabled"] = False
                changed = True
            if "mfa_required" not in entry:
                entry["mfa_required"] = not bool(entry.get("mfa_enabled", False))
                changed = True
            if "mfa_recovery_hashes" not in entry:
                entry["mfa_recovery_hashes"] = []
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
        db.execute("""
            CREATE TABLE IF NOT EXISTS host_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER NOT NULL,
                cpu_percent REAL NOT NULL,
                ram_percent REAL NOT NULL,
                disk_percent REAL NOT NULL,
                net_rx_bytes INTEGER NOT NULL,
                net_tx_bytes INTEGER NOT NULL
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_host_metrics_timestamp ON host_metrics(timestamp)")
        db.execute("""
            CREATE TABLE IF NOT EXISTS security_findings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site TEXT NOT NULL,
                severity TEXT NOT NULL,
                finding_key TEXT NOT NULL,
                finding_type TEXT NOT NULL,
                path TEXT,
                message TEXT NOT NULL,
                evidence TEXT,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                email_sent INTEGER NOT NULL DEFAULT 0
            )
        """)
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_security_finding_key ON security_findings(site,finding_key)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_security_findings_active ON security_findings(active)")
        db.execute("""CREATE TABLE IF NOT EXISTS site_operations (
            site TEXT PRIMARY KEY, mode TEXT NOT NULL DEFAULT 'live',
            notes TEXT NOT NULL DEFAULT '', owner_name TEXT NOT NULL DEFAULT '',
            owner_email TEXT NOT NULL DEFAULT '', owner_phone TEXT NOT NULL DEFAULT '',
            external_url TEXT NOT NULL DEFAULT '', last_external_status TEXT,
            last_external_ms INTEGER, last_external_check TEXT,
            restart_count INTEGER NOT NULL DEFAULT 0, last_restart_seen INTEGER NOT NULL DEFAULT 0)""")
        db.execute("""CREATE TABLE IF NOT EXISTS host_policy (
            id INTEGER PRIMARY KEY CHECK(id=1), disk_warn INTEGER NOT NULL DEFAULT 75,
            disk_high INTEGER NOT NULL DEFAULT 85, disk_critical INTEGER NOT NULL DEFAULT 95,
            external_check_minutes INTEGER NOT NULL DEFAULT 5, restart_alert_delta INTEGER NOT NULL DEFAULT 3)""")
        db.execute("INSERT OR IGNORE INTO host_policy(id) VALUES(1)")
        db.execute("""
            CREATE TABLE IF NOT EXISTS security_baselines (
                site TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL,
                root_entries TEXT NOT NULL,
                file_hashes TEXT NOT NULL,
                admin_users TEXT NOT NULL,
                page_slugs TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS health_notification_state (
                site TEXT PRIMARY KEY,
                unhealthy INTEGER NOT NULL DEFAULT 0,
                last_message TEXT,
                last_changed TEXT,
                last_email TEXT,
                failure_count INTEGER NOT NULL DEFAULT 0
            )
        """)
        try:
            db.execute("ALTER TABLE health_notification_state ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        db.commit()
    os.chmod(PLATFORM_DB, 0o600)

def load_users():
    return json.loads(USERS_FILE.read_text())

def verify_password(stored_hash, password):
    if stored_hash.startswith("legacy_sha256$"):
        try:
            import hashlib
            _, salt, expected = stored_hash.split("$", 2)
            actual = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
            return hmac.compare_digest(actual, expected)
        except Exception:
            return False
    return check_password_hash(stored_hash, password)

def save_users(users):
    tmp = USERS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(users, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(USERS_FILE)
    os.chmod(USERS_FILE, 0o600)

def generate_temporary_password(length=18):
    alphabet = string.ascii_letters + string.digits + "!@#$%_-"
    while True:
        value = "".join(secrets.choice(alphabet) for _ in range(length))
        if any(c.islower() for c in value) and any(c.isupper() for c in value) and any(c.isdigit() for c in value) and any(c in "!@#$%_-" for c in value):
            return value

def account_public_url(path=""):
    base = f"https://{MANAGER_DOMAIN}" if MANAGER_DOMAIN else request.url_root.rstrip("/")
    return base.rstrip("/") + "/" + path.lstrip("/")

def send_account_email(address, subject, body):
    settings = dict(load_email_settings())
    settings["enabled"] = True
    settings["recipients"] = address
    return send_alert_email(subject, body, settings)

def ensure_auth_v91_schema():
    with sqlite3.connect(PLATFORM_DB) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS password_resets_v91(
            token_hash TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL
        )""")
        db.commit()

def new_password_reset(username):
    ensure_auth_v91_schema()
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    now = int(time.time())
    with sqlite3.connect(PLATFORM_DB) as db:
        db.execute("UPDATE password_resets_v91 SET used=1 WHERE username=?", (username,))
        db.execute("INSERT INTO password_resets_v91(token_hash,username,expires_at,used,created_at) VALUES(?,?,?,0,?)", (token_hash, username, now + 1800, now))
        db.commit()
    return token

def lookup_password_reset(token):
    ensure_auth_v91_schema()
    digest = hashlib.sha256(token.encode()).hexdigest()
    with sqlite3.connect(PLATFORM_DB) as db:
        row = db.execute("SELECT username,expires_at,used FROM password_resets_v91 WHERE token_hash=?", (digest,)).fetchone()
    if not row or row[2] or int(row[1]) < int(time.time()):
        return None
    return row[0]

def consume_password_resets(username):
    ensure_auth_v91_schema()
    with sqlite3.connect(PLATFORM_DB) as db:
        db.execute("UPDATE password_resets_v91 SET used=1 WHERE username=?", (username,))
        db.commit()

def mfa_qr_data(secret, username):
    uri = pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=PLATFORM_NAME)
    image = qrcode.make(uri)
    buf = BytesIO(); image.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

def new_recovery_codes(count=10):
    raw = [f"{secrets.token_hex(4).upper()}-{secrets.token_hex(4).upper()}" for _ in range(count)]
    hashes = [hashlib.sha256(x.encode()).hexdigest() for x in raw]
    return raw, hashes

def authenticate_session(username, entry):
    session.clear(); session.permanent = True
    session["authenticated"] = True
    session["username"] = username
    session["role"] = entry.get("role", "view")
    session["force_password_change"] = bool(entry.get("force_password_change", False))

def user_listing_v91():
    users = load_users()
    return [{"username":u,"email":e.get("email",""),"role":e.get("role","view"),"enabled":e.get("enabled",True),"created":e.get("created","-"),"mfa_enabled":bool(e.get("mfa_enabled",False))} for u,e in sorted(users.items())]

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

def alerts_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login", next=request.path))
        if current_role() not in {"admin", "user"}:
            abort(403)
        return fn(*args, **kwargs)
    return wrapper


@APP.before_request
def mandatory_account_security_gate():

    if not session.get("authenticated"):
        return None

    endpoint = request.endpoint or ""

    allowed = {
        "login",
        "logout",
        "forced_password_change",
        "mfa_setup_route",
        "mfa_verify_route",
        "static",
    }

    if endpoint in allowed:
        return None

    username = session.get("username")

    users = load_users()

    entry = users.get(username)

    if not entry:
        session.clear()
        return redirect(url_for("login"))

    if not entry.get("enabled", True):
        session.clear()
        return redirect(url_for("login"))

    if (
        entry.get("force_password_change", False)
        or session.get("force_password_change")
    ):
        return redirect(
            url_for("forced_password_change")
        )

    if (
        entry.get("mfa_required", False)
        and
        not entry.get("mfa_enabled", False)
    ):
        return redirect(
            url_for("mfa_setup_route")
        )

    return None


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
        dict(timestamp=format_adelaide(r[0]), username=r[1], event=r[2], result=r[3], ip=r[4], user_agent=r[5])
        for r in rows
    ]

def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)
    return wrapper

VALID_THEMES = {"midnight", "light", "forest", "sunset"}

def current_user_theme():
    username = session.get("username")
    if not username:
        return "midnight"
    entry = load_users().get(username, {})
    theme = entry.get("theme", "midnight")
    return theme if theme in VALID_THEMES else "midnight"

@APP.post("/account/theme")
@login_required
def account_theme_save():
    theme = request.form.get("theme", "midnight")
    if theme not in VALID_THEMES:
        theme = "midnight"
    username = session.get("username")
    users = load_users()
    if username in users:
        users[username]["theme"] = theme
        save_users(users)
    return redirect(request.referrer or url_for("index"))

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
    return [dict(timestamp=format_adelaide(r[0]), username=r[1], action=r[2], target=r[3], result=r[4], ip=r[5], detail=r[6]) for r in rows]

def all_disk_usage():
    """Enumerate every real (non-pseudo) mounted filesystem with usage stats."""
    disks = []
    seen = set()
    try:
        partitions = psutil.disk_partitions(all=False)
    except Exception:
        partitions = []
    for part in partitions:
        if part.mountpoint in seen:
            continue
        if part.fstype in ("tmpfs", "devtmpfs", "overlay", "squashfs", "proc", "sysfs",
                            "cgroup", "cgroup2", "devpts", "mqueue", "debugfs", "tracefs"):
            continue
        try:
            du = psutil.disk_usage(part.mountpoint)
        except Exception:
            continue
        seen.add(part.mountpoint)
        disks.append({
            "device": part.device,
            "mountpoint": part.mountpoint,
            "fstype": part.fstype,
            "used": human_bytes(du.used),
            "total": human_bytes(du.total),
            "free": human_bytes(du.free),
            "percent": du.percent,
        })
    disks.sort(key=lambda d: d["mountpoint"])
    return disks

def host_stats():
    try:
        disk = psutil.disk_usage(str(BASE))
    except Exception:
        disk = psutil.disk_usage("/")
    disks = all_disk_usage()
    vm = psutil.virtual_memory()
    npm = "Unavailable"
    try:
        wait_for_npm(timeout=5)
        npm = "Online"
    except Exception:
        pass
    net = psutil.net_io_counters()
    uptime_seconds = max(0, int(time.time() - psutil.boot_time()))
    days, rem = divmod(uptime_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    try:
        containers = docker_client.containers.list(all=True)
        docker_running = sum(1 for c in containers if c.status == "running")
        docker_total = len(containers)
    except Exception:
        docker_running = docker_total = 0
    return {
        "npm": npm,
        "cpu": psutil.cpu_percent(interval=0.1),
        "ram_used": human_bytes(vm.used),
        "ram_total": human_bytes(vm.total),
        "ram_percent": vm.percent,
        "disk_used": human_bytes(disk.used),
        "disk_total": human_bytes(disk.total),
        "disk_percent": disk.percent,
        "disks": disks,
        "load": ", ".join(f"{x:.2f}" for x in psutil.getloadavg()) if hasattr(psutil, "getloadavg") else "-",
        "net_rx": human_bytes(net.bytes_recv),
        "net_tx": human_bytes(net.bytes_sent),
        "uptime": f"{days}d {hours}h {minutes}m",
        "docker_running": docker_running,
        "docker_total": docker_total,
    }

def collect_host_metric():
    vm = psutil.virtual_memory()
    try:
        disk = psutil.disk_usage(str(BASE))
    except Exception:
        disk = psutil.disk_usage("/")
    net = psutil.net_io_counters()
    cpu = psutil.cpu_percent(interval=0.2)
    ts = int(time.time())
    with sqlite3.connect(PLATFORM_DB) as db:
        db.execute(
            "INSERT INTO host_metrics(timestamp,cpu_percent,ram_percent,disk_percent,net_rx_bytes,net_tx_bytes) VALUES(?,?,?,?,?,?)",
            (ts, cpu, vm.percent, disk.percent, int(net.bytes_recv), int(net.bytes_sent))
        )
        cutoff = ts - METRIC_RETENTION_DAYS * 86400
        db.execute("DELETE FROM host_metrics WHERE timestamp < ?", (cutoff,))
        db.commit()

def metrics_collector():
    while True:
        try:
            collect_host_metric()
        except Exception:
            pass
        time.sleep(METRIC_INTERVAL_SECONDS)

def recent_host_metrics(hours=24, limit=1440):
    cutoff = int(time.time()) - int(hours * 3600)
    with sqlite3.connect(PLATFORM_DB) as db:
        rows = db.execute(
            "SELECT timestamp,cpu_percent,ram_percent,disk_percent,net_rx_bytes,net_tx_bytes "
            "FROM host_metrics WHERE timestamp>=? ORDER BY timestamp ASC LIMIT ?",
            (cutoff, limit)
        ).fetchall()
    return [
        dict(timestamp=r[0], cpu=r[1], ram=r[2], disk=r[3], rx=r[4], tx=r[5])
        for r in rows
    ]

def container_live_stats(container_name):
    try:
        c = docker_client.containers.get(container_name)
        raw = c.stats(stream=False)
        cpu_delta = raw["cpu_stats"]["cpu_usage"]["total_usage"] - raw["precpu_stats"]["cpu_usage"]["total_usage"]
        system_delta = raw["cpu_stats"].get("system_cpu_usage",0) - raw["precpu_stats"].get("system_cpu_usage",0)
        online = raw["cpu_stats"].get("online_cpus") or len(raw["cpu_stats"]["cpu_usage"].get("percpu_usage",[]) or [1])
        cpu_pct = (cpu_delta / system_delta * online * 100.0) if system_delta > 0 and cpu_delta >= 0 else 0.0
        mem_usage = raw["memory_stats"].get("usage",0)
        mem_limit = raw["memory_stats"].get("limit",0)
        return {
            "cpu": round(cpu_pct, 2),
            "memory": human_bytes(mem_usage),
            "memory_limit": human_bytes(mem_limit),
        }
    except Exception:
        return {"cpu":"-", "memory":"-", "memory_limit":"-"}

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
    desired_host = f"{site}-wp"
    if existing:
        if existing.get("forward_host") != desired_host or int(existing.get("forward_port", 80)) != 80 or existing.get("forward_scheme") != "http":
            proxy_id = existing.get("id")
            payload = {
                "domain_names": existing.get("domain_names", [domain]),
                "forward_scheme": "http",
                "forward_host": desired_host,
                "forward_port": 80,
                "access_list_id": existing.get("access_list_id", 0) or 0,
                "certificate_id": int(existing.get("certificate_id", 0) or certificate_id or 0),
                "ssl_forced": bool(existing.get("ssl_forced", False)),
                "caching_enabled": bool(existing.get("caching_enabled", False)),
                "block_exploits": True,
                "advanced_config": existing.get("advanced_config", ""),
                "meta": existing.get("meta", {}),
                "allow_websocket_upgrade": True,
                "http2_support": True,
                "hsts_enabled": bool(existing.get("hsts_enabled", False)),
                "hsts_subdomains": bool(existing.get("hsts_subdomains", False)),
                "trust_forwarded_proto": True,
                "enabled": True,
                "locations": existing.get("locations", []),
            }
            r = requests.put(
                f"{NPM_URL}/api/nginx/proxy-hosts/{proxy_id}",
                headers=npm_headers(),
                json=payload,
                timeout=30,
            )
            if r.status_code not in (200, 201):
                raise RuntimeError(f"Existing proxy update failed: HTTP {r.status_code}: {r.text[:500]}")
            return r.json()
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

def phpmyadmin_container_name(site):
    return f"{site}-phpmyadmin"

def phpmyadmin_status(site):
    try:
        c = docker_client.containers.get(phpmyadmin_container_name(site))
        c.reload()
        port = "-"
        bindings = c.attrs.get("NetworkSettings", {}).get("Ports", {}).get("80/tcp") or []
        if bindings:
            port = bindings[0].get("HostPort", "-")
        return {"enabled": c.status == "running", "status": c.status, "port": port}
    except Exception:
        return {"enabled": False, "status": "missing", "port": "-"}

def _container_published_ports(container):
    """Return {host_port: container_port} for Docker port mappings, including stopped containers."""
    result = {}
    try:
        container.reload()
        sources = [
            container.attrs.get("NetworkSettings", {}).get("Ports", {}) or {},
            container.attrs.get("HostConfig", {}).get("PortBindings", {}) or {},
        ]
        for source in sources:
            for container_port, bindings in source.items():
                for binding in bindings or []:
                    hp = str(binding.get("HostPort") or "").strip()
                    if hp.isdigit():
                        result[int(hp)] = container_port
    except Exception:
        pass
    return result


def _docker_port_owners(port):
    owners = []
    try:
        for container in docker_client.containers.list(all=True):
            if int(port) in _container_published_ports(container):
                owners.append(container.name)
    except Exception:
        pass
    return owners


def _metadata_port_reservations(service, exclude_site=None):
    key = "phpmyadmin_port" if service == "phpmyadmin" else "sftp_port"
    reservations = {}
    try:
        SITES.mkdir(parents=True, exist_ok=True)
        for site_dir in SITES.iterdir():
            if not site_dir.is_dir() or site_dir.name == exclude_site:
                continue
            try:
                meta = site_metadata(site_dir)
                value = meta.get(key)
                if value not in (None, "", "-"):
                    value = int(value)
                    reservations[value] = meta.get("site", site_dir.name)
            except Exception:
                continue
    except Exception:
        pass
    return reservations


def _host_port_bindable(port):
    """Check non-Docker listeners too, so Docker is not asked to bind an already-used host port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", int(port)))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def port_conflict_reason(port, service, site=None, ignore_container=None):
    try:
        port = int(port)
    except Exception:
        return "invalid port"

    owners = [name for name in _docker_port_owners(port) if name != ignore_container]
    if owners:
        return "Docker: " + ", ".join(sorted(owners))

    reserved = _metadata_port_reservations(service, exclude_site=site).get(port)
    if reserved:
        return f"reserved by {reserved}"

    # If our own running container owns the port, Docker already proved the mapping is valid.
    own_owner = ignore_container and ignore_container in _docker_port_owners(port)
    if not own_owner and not _host_port_bindable(port):
        return "host process/listener"
    return None


def allocate_service_port(service, site=None, preferred=None, ignore_container=None, exclude_ports=None):
    if service == "phpmyadmin":
        start, end = PHPMYADMIN_PORT_START, PHPMYADMIN_PORT_END
    elif service == "sftp":
        start, end = SFTP_PORT_START, SFTP_PORT_END
    else:
        raise ValueError("Unknown management service.")

    excluded = {int(p) for p in (exclude_ports or [])}
    if preferred not in (None, "", "-"):
        try:
            preferred = int(preferred)
            if start <= preferred <= end and preferred not in excluded:
                if not port_conflict_reason(preferred, service, site, ignore_container):
                    return preferred
        except Exception:
            pass

    for port in range(start, end + 1):
        if port in excluded:
            continue
        if not port_conflict_reason(port, service, site, ignore_container):
            return port

    raise RuntimeError(f"No free {service} ports remain in the configured range {start}-{end}.")


def allocate_phpmyadmin_port(site=None, preferred=None, exclude_ports=None):
    return allocate_service_port(
        "phpmyadmin", site=site, preferred=preferred,
        ignore_container=phpmyadmin_container_name(site) if site else None,
        exclude_ports=exclude_ports,
    )


def ufw_status():
    """Return UFW state without changing firewall policy."""
    try:
        proc = subprocess.run(["ufw", "status"], capture_output=True, text=True, timeout=10)
        text = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        active = proc.returncode == 0 and bool(re.search(r"^Status:\s+active\s*$", text, re.I | re.M))
        default_deny = bool(re.search(r"^Default:\s+(?:deny|reject) \(incoming\)", text, re.I | re.M))
        return {"installed": True, "active": active, "default_deny": default_deny, "text": text}
    except FileNotFoundError:
        return {"installed": False, "active": False, "default_deny": False, "text": "ufw is not installed"}
    except Exception as exc:
        return {"installed": True, "active": False, "default_deny": False, "text": str(exc)}


def _valid_source_ip(value):
    raw = (value or "").strip()
    if not raw:
        raise ValueError("A source IP address is required.")
    # X-Forwarded-For can contain a comma-separated chain. The first address is
    # normally the original client when Nginx Proxy Manager is the trusted edge.
    raw = raw.split(",", 1)[0].strip()
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        raise ValueError(f"Invalid source IP address: {raw}")


TRUSTED_PROXY_IPS = {
    ip.strip() for ip in os.environ.get("WP_TRUSTED_PROXY_IPS", "127.0.0.1,::1").split(",") if ip.strip()
}

def detected_client_ip():
    # Only trust X-Forwarded-For when the immediate connecting peer is a
    # known, trusted reverse proxy. Otherwise anyone could set this header
    # themselves to spoof their source IP and bypass IP-restricted features
    # (e.g. temporary phpMyAdmin/SFTP access).
    peer = request.remote_addr or ""
    if peer in TRUSTED_PROXY_IPS:
        forwarded = request.headers.get("X-Forwarded-For", "")
        candidate = forwarded.split(",", 1)[0].strip() if forwarded else peer
    else:
        candidate = peer
    try:
        return _valid_source_ip(candidate)
    except Exception:
        return request.remote_addr or ""


def _load_firewall_sessions():
    try:
        data = json.loads(FIREWALL_SESSIONS_FILE.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_firewall_sessions(items):
    FIREWALL_SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FIREWALL_SESSIONS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, indent=2))
    os.replace(tmp, FIREWALL_SESSIONS_FILE)


def _ufw_require_ready():
    state = ufw_status()
    if not state["installed"]:
        raise RuntimeError("UFW is not installed on this server.")
    if not state["active"]:
        raise RuntimeError("UFW is not active. Configure SSH/80/443 rules and enable UFW before using temporary management access.")
    if not state.get("default_deny"):
        raise RuntimeError("UFW incoming policy is not default-deny/reject. Temporary port access would not provide a security boundary.")
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        raise RuntimeError("wp-host-manager must run as root to manage UFW rules.")
    return state


def _ufw_allow(source_ip, port, site, service):
    comment = f"wp-host:{site}:{service}"
    cmd = ["ufw", "allow", "from", source_ip, "to", "any", "port", str(int(port)), "proto", "tcp", "comment", comment]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "ufw allow failed").strip())


def _ufw_delete(source_ip, port):
    cmd = ["ufw", "--force", "delete", "allow", "from", source_ip, "to", "any", "port", str(int(port)), "proto", "tcp"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    # If the rule was already removed manually, treat that as closed rather than
    # keeping a stale access session forever.
    text = ((proc.stdout or "") + "\n" + (proc.stderr or "")).lower()
    if proc.returncode != 0 and "could not find" not in text and "non-existent" not in text:
        raise RuntimeError((proc.stderr or proc.stdout or "ufw delete failed").strip())


def cleanup_expired_firewall_sessions():
    now = time.time()
    with FIREWALL_LOCK:
        sessions = _load_firewall_sessions()
        keep = []
        changed = False
        for item in sessions:
            expires = item.get("expires_at")
            if expires is not None and float(expires) <= now:
                try:
                    _ufw_delete(item["source_ip"], item["port"])
                except Exception:
                    # Keep no stale logical session. A later UFW audit can expose
                    # manually-created rules independently of this manager state.
                    pass
                changed = True
                try:
                    log_action("management_access_expired", item.get("site", "-"), "success", f"{item.get('service')} port={item.get('port')} source={item.get('source_ip')}")
                except Exception:
                    pass
            else:
                keep.append(item)
        if changed:
            _save_firewall_sessions(keep)
        return keep


def management_access_sessions(site=None, service=None):
    sessions = cleanup_expired_firewall_sessions()
    out = []
    now = time.time()
    for item in sessions:
        if site and item.get("site") != site:
            continue
        if service and item.get("service") != service:
            continue
        row = dict(item)
        exp = row.get("expires_at")
        if exp is None:
            row["remaining"] = "Until closed"
        else:
            seconds = max(0, int(float(exp) - now))
            row["remaining"] = f"{seconds // 60}m {seconds % 60:02d}s"
        row["expires_display"] = "Until closed" if exp is None else format_adelaide(datetime.fromtimestamp(float(exp), timezone.utc), "%d %b %Y, %I:%M:%S %p %Z")
        out.append(row)
    return sorted(out, key=lambda x: (x.get("site", ""), x.get("service", ""), x.get("source_ip", "")))


def service_access_summary(site, service):
    items = management_access_sessions(site, service)
    return {"open": bool(items), "sessions": items, "count": len(items)}


def open_management_access(site, service, source_ip, minutes=FIREWALL_DEFAULT_MINUTES):
    if service not in {"phpmyadmin", "sftp"}:
        raise ValueError("Unknown management service.")
    if not SITE_RE.match(site) or not (SITES / site).exists():
        raise RuntimeError("Site not found.")
    _ufw_require_ready()
    source_ip = _valid_source_ip(source_ip)
    if minutes not in FIREWALL_ALLOWED_MINUTES and minutes != 0:
        raise ValueError("Access duration must be 15, 30, 60 minutes or until closed.")

    if service == "phpmyadmin":
        result = ensure_phpmyadmin(site)
        port = int(result["port"])
    else:
        status = sftp_status(site)
        if not status.get("enabled") or status.get("port") in (None, "", "-"):
            raise RuntimeError("SFTP is disabled for this site. Enable SFTP first, then open firewall access.")
        port = int(status["port"])

    expires_at = None if minutes == 0 else time.time() + (int(minutes) * 60)
    with FIREWALL_LOCK:
        sessions = cleanup_expired_firewall_sessions()
        # One manager-owned rule per site/service/source. Extending access first
        # removes our old exact rule so UFW does not accumulate duplicates.
        retained = []
        for item in sessions:
            if item.get("site") == site and item.get("service") == service and item.get("source_ip") == source_ip:
                try:
                    _ufw_delete(item["source_ip"], item["port"])
                except Exception:
                    pass
            else:
                retained.append(item)
        _ufw_allow(source_ip, port, site, service)
        retained.append({
            "id": secrets.token_urlsafe(12),
            "site": site,
            "service": service,
            "port": port,
            "source_ip": source_ip,
            "opened_by": session.get("username", "-"),
            "opened_at": time.time(),
            "expires_at": expires_at,
        })
        _save_firewall_sessions(retained)
    log_action("management_access_open", site, "success", f"{service} port={port} source={source_ip} minutes={'until-closed' if minutes == 0 else minutes}")
    return {"port": port, "source_ip": source_ip, "expires_at": expires_at}


def close_management_access(site=None, service=None, session_id=None):
    with FIREWALL_LOCK:
        sessions = cleanup_expired_firewall_sessions()
        keep, closed = [], []
        for item in sessions:
            match = True
            if site is not None and item.get("site") != site:
                match = False
            if service is not None and item.get("service") != service:
                match = False
            if session_id is not None and item.get("id") != session_id:
                match = False
            if match:
                try:
                    _ufw_delete(item["source_ip"], item["port"])
                    closed.append(item)
                except Exception:
                    keep.append(item)
                    raise
            else:
                keep.append(item)
        _save_firewall_sessions(keep)
    for item in closed:
        try:
            log_action("management_access_close", item.get("site", "-"), "success", f"{item.get('service')} port={item.get('port')} source={item.get('source_ip')}")
        except Exception:
            pass
    return closed


def _firewall_expiry_worker():
    while True:
        try:
            cleanup_expired_firewall_sessions()
        except Exception:
            pass
        time.sleep(30)


def start_firewall_expiry_worker():
    t = threading.Thread(target=_firewall_expiry_worker, name="wp-host-ufw-expiry", daemon=True)
    t.start()


def ensure_management_network():
    try:
        return docker_client.networks.get("wp-management")
    except Exception:
        return docker_client.networks.create("wp-management", driver="bridge")

def ensure_phpmyadmin(site, requested_port=None):
    site_dir = SITES / site
    if not site_dir.exists():
        raise RuntimeError("Site not found.")

    meta = site_metadata(site_dir)
    container = phpmyadmin_container_name(site)
    preferred = requested_port if requested_port is not None else meta.get("phpmyadmin_port")
    port = allocate_phpmyadmin_port(site=site, preferred=preferred)
    internal_network = f"{site}_internal"
    management_network = "wp-management"

    run(["docker", "compose", "up", "-d", "db"], cwd=site_dir)
    ensure_management_network()

    try:
        existing = docker_client.containers.get(container)
        existing.reload()
        networks = existing.attrs.get("NetworkSettings", {}).get("Networks", {})
        bindings = existing.attrs.get("NetworkSettings", {}).get("Ports", {}).get("80/tcp") or []
        bound_port = None
        if bindings:
            try:
                bound_port = int(bindings[0].get("HostPort"))
            except Exception:
                bound_port = None

        if internal_network in networks and management_network in networks and bound_port == port:
            if existing.status != "running":
                existing.start()
            meta["phpmyadmin_port"] = port
            meta.pop("phpmyadmin_port_conflict", None)
            (site_dir / "site.json").write_text(json.dumps(meta, indent=2))
            return {"port": port, "reassigned": preferred not in (None, "", "-") and str(preferred) != str(port)}

        existing.remove(force=True)
    except docker.errors.NotFound:
        pass
    except Exception:
        # A damaged/partial old container should not block a clean recreate.
        try:
            docker_client.containers.get(container).remove(force=True)
        except Exception:
            pass

    c = docker_client.containers.run(
        "phpmyadmin:latest",
        name=container,
        detach=True,
        restart_policy={"Name": "unless-stopped"},
        network=management_network,
        ports={"80/tcp": ("0.0.0.0", port)},
        environment={
            "PMA_HOST": f"{site}-db",
            "PMA_PORT": "3306",
            "PMA_ARBITRARY": "0",
            "UPLOAD_LIMIT": "512M",
            "MAX_EXECUTION_TIME": "600",
        },
        labels={"wp-host.site": site, "wp-host.role": "phpmyadmin"},
    )

    try:
        docker_client.networks.get(internal_network).connect(c)
    except Exception as exc:
        try: c.remove(force=True)
        except Exception: pass
        raise RuntimeError(f"phpMyAdmin could not join {internal_network}: {exc}")

    c.reload()
    bindings = c.attrs.get("NetworkSettings", {}).get("Ports", {}).get("80/tcp") or []
    if not bindings or str(bindings[0].get("HostPort")) != str(port):
        try: c.remove(force=True)
        except Exception: pass
        raise RuntimeError(f"phpMyAdmin port {port} was not published correctly.")

    meta["phpmyadmin_port"] = port
    meta.pop("phpmyadmin_port_conflict", None)
    (site_dir / "site.json").write_text(json.dumps(meta, indent=2))
    return {"port": port, "reassigned": preferred not in (None, "", "-") and str(preferred) != str(port)}


def remove_phpmyadmin(site):
    try:
        c = docker_client.containers.get(phpmyadmin_container_name(site))
        c.remove(force=True)
    except Exception:
        pass

def release_management_port_reservations(site):
    site_dir = SITES / site
    if not site_dir.exists():
        return
    meta = site_metadata(site_dir)
    meta.pop("phpmyadmin_port", None)
    meta.pop("sftp_port", None)
    meta.pop("sftp_user", None)
    (site_dir / "site.json").write_text(json.dumps(meta, indent=2))

def sftp_container_name(site):
    return f"{site}-sftp"

def sftp_status(site):
    try:
        c = docker_client.containers.get(sftp_container_name(site))
        c.reload()
        port = "-"
        bindings = c.attrs.get("NetworkSettings", {}).get("Ports", {}).get("22/tcp") or []
        if bindings:
            port = bindings[0].get("HostPort", "-")
        return {"enabled": c.status == "running", "status": c.status, "port": port}
    except Exception:
        return {"enabled": False, "status": "off", "port": "-"}

def allocate_sftp_port(site=None, preferred=None, exclude_ports=None):
    return allocate_service_port(
        "sftp", site=site, preferred=preferred,
        ignore_container=sftp_container_name(site) if site else None,
        exclude_ports=exclude_ports,
    )

def generate_sftp_password(length=20):
    # Restricted alphabet avoids delimiters and quoting issues in container startup parsing.
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#%+="
    return "".join(secrets.choice(alphabet) for _ in range(length))

def verify_sftp_account(container):
    container.reload()
    if container.status != "running":
        raise RuntimeError("SFTP container is not running.")
    result = container.exec_run(["passwd", "-S", "wordpress"])
    output = result.output.decode(errors="ignore").strip()
    if result.exit_code != 0 or not output:
        raise RuntimeError("Could not verify the SFTP user.")
    fields = output.split()
    if len(fields) >= 2 and fields[1].upper().startswith("L"):
        raise RuntimeError("SFTP user account is locked.")
    return output

def apply_sftp_password(container_name, password):
    proc = subprocess.run(
        ["docker", "exec", "-i", container_name, "chpasswd"],
        input=f"wordpress:{password}\n".encode("utf-8"),
        capture_output=True,
        timeout=15,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "Could not apply generated SFTP password: "
            + proc.stderr.decode(errors="ignore").strip()
        )

def enable_sftp(site, requested_port=None):
    site_dir = SITES / site
    if not site_dir.exists():
        raise RuntimeError("Site not found.")

    meta = site_metadata(site_dir)
    preferred = requested_port if requested_port is not None else meta.get("sftp_port")
    port = allocate_sftp_port(site=site, preferred=preferred)
    password = generate_sftp_password()
    container_name = sftp_container_name(site)

    try:
        old = docker_client.containers.get(container_name)
        old.remove(force=True)
    except Exception:
        pass

    wordpress_path = str(site_dir / "wordpress")

    # Bootstrap credential creates the account only. The real generated credential
    # is applied explicitly after the container is running.
    c = docker_client.containers.run(
        "atmoz/sftp:alpine",
        name=container_name,
        command="wordpress:BootstrapOnly123:33:33:upload",
        detach=True,
        restart_policy={"Name": "unless-stopped"},
        ports={"22/tcp": ("0.0.0.0", port)},
        volumes={
            wordpress_path: {
                "bind": "/home/wordpress/upload",
                "mode": "rw",
            }
        },
        labels={
            "wp-host.site": site,
            "wp-host.role": "sftp",
        },
    )

    ready = False
    for _ in range(20):
        try:
            c.reload()
            if c.status == "running":
                check = c.exec_run(["getent", "passwd", "wordpress"])
                if check.exit_code == 0:
                    ready = True
                    break
        except Exception:
            pass
        time.sleep(0.5)

    if not ready:
        try:
            c.remove(force=True)
        except Exception:
            pass
        raise RuntimeError("SFTP container did not initialise correctly.")

    try:
        apply_sftp_password(container_name, password)
        verify_sftp_account(c)
    except Exception:
        try:
            c.remove(force=True)
        except Exception:
            pass
        raise

    meta["sftp_enabled"] = True
    meta["sftp_port"] = port
    meta["sftp_user"] = "wordpress"
    (site_dir / "site.json").write_text(json.dumps(meta, indent=2))

    return {
        "host_port": port,
        "username": "wordpress",
        "password": password,
        "path": "/upload",
    }

def reset_sftp_password(site):
    c = docker_client.containers.get(sftp_container_name(site))
    c.reload()
    if c.status != "running":
        raise RuntimeError("SFTP is not currently running for this site.")

    password = generate_sftp_password()
    apply_sftp_password(c.name, password)
    verify_sftp_account(c)

    return {
        "host_port": sftp_status(site).get("port", "-"),
        "username": "wordpress",
        "password": password,
        "path": "/upload",
    }

def disable_sftp(site):
    site_dir = SITES / site

    try:
        c = docker_client.containers.get(sftp_container_name(site))
        c.remove(force=True)
    except Exception:
        pass

    if site_dir.exists():
        meta = site_metadata(site_dir)
        meta["sftp_enabled"] = False
        (site_dir / "site.json").write_text(json.dumps(meta, indent=2))

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
        core = c.exec_run(["php","-r","include '/var/www/html/wp-includes/version.php'; echo $wp_version;"])
        if core.exit_code == 0:
            data["core"] = core.output.decode(errors="ignore").strip() or "-"
        plugins = c.exec_run(["sh","-lc","find /var/www/html/wp-content/plugins -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l"])
        themes = c.exec_run(["sh","-lc","find /var/www/html/wp-content/themes -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l"])
        data["plugins"] = plugins.output.decode().strip() if plugins.exit_code == 0 else "-"
        data["themes"] = themes.output.decode().strip() if themes.exit_code == 0 else "-"
    except Exception:
        pass
    return data

def load_email_settings():
    defaults = {"enabled": False, "smtp_host": "", "smtp_port": 587, "smtp_username": "", "smtp_password": "", "from_email": "", "recipients": "", "security": "starttls", "send_recovery": True, "check_minutes": 5, "failure_threshold": 2}
    try:
        if EMAIL_SETTINGS_FILE.exists():
            defaults.update(json.loads(EMAIL_SETTINGS_FILE.read_text()))
    except Exception:
        pass
    return defaults

def save_email_settings(data):
    EMAIL_SETTINGS_FILE.write_text(json.dumps(data, indent=2))
    os.chmod(EMAIL_SETTINGS_FILE, 0o600)

def send_alert_email(subject, body, settings=None):
    s = settings or load_email_settings()
    if not s.get("enabled"):
        return False, "Email alerts are disabled"
    recipients = [x.strip() for x in re.split(r"[,;]", s.get("recipients", "")) if x.strip()]
    if not recipients:
        return False, "No recipients configured"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = s.get("from_email") or s.get("smtp_username")
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)
    host, port = s.get("smtp_host", ""), int(s.get("smtp_port", 587))
    if not host:
        return False, "SMTP host is blank"
    security = s.get("security", "starttls")
    context = ssl.create_default_context()
    try:
        if security == "ssl":
            smtp = smtplib.SMTP_SSL(host, port, timeout=20, context=context)
        else:
            smtp = smtplib.SMTP(host, port, timeout=20)
            smtp.ehlo()
            if security == "starttls":
                smtp.starttls(context=context); smtp.ehlo()
        if s.get("smtp_username"):
            smtp.login(s.get("smtp_username"), s.get("smtp_password", ""))
        smtp.send_message(msg); smtp.quit()
        return True, "Message sent"
    except Exception as exc:
        return False, str(exc)

def health_email_monitor():
    while True:
        try:
            settings = load_email_settings()
            interval = max(1, min(60, int(settings.get("check_minutes", 5)))) * 60
            threshold = max(1, min(10, int(settings.get("failure_threshold", 2))))

            for d in sorted([p for p in SITES.iterdir() if p.is_dir()]) if SITES.exists() else []:
                meta = site_metadata(d)
                if not meta:
                    continue

                site = meta.get("site", d.name)
                domain = meta.get("domain", "")
                h = site_health(site, domain)
                currently_unhealthy = not h["healthy"]
                message = f"Container={h['container']}, DB={h['db']}, HTTPS={h['http']}"
                now = datetime.now(timezone.utc).isoformat()

                with sqlite3.connect(PLATFORM_DB) as db:
                    row = db.execute(
                        "SELECT unhealthy,last_message,failure_count FROM health_notification_state WHERE site=?",
                        (site,)
                    ).fetchone()

                    if row is None:
                        failure_count = 1 if currently_unhealthy else 0
                        db.execute(
                            "INSERT INTO health_notification_state(site,unhealthy,last_message,last_changed,failure_count) VALUES(?,?,?,?,?)",
                            (site, 0, message, now, failure_count)
                        )
                        db.commit()
                        previous_unhealthy = 0
                    else:
                        previous_unhealthy = int(row[0] or 0)
                        failure_count = int(row[2] or 0)

                    if currently_unhealthy:
                        failure_count += 1 if row is not None else 0
                        db.execute(
                            "UPDATE health_notification_state SET failure_count=?,last_message=? WHERE site=?",
                            (failure_count, message, site)
                        )
                        db.commit()

                        if previous_unhealthy == 0 and failure_count >= threshold:
                            db.execute(
                                "UPDATE health_notification_state SET unhealthy=1,last_changed=?,last_email=? WHERE site=?",
                                (now, now, site)
                            )
                            db.commit()

                            if settings.get("enabled"):
                                ok, detail = send_alert_email(
                                    f"[WP Host] UNHEALTHY: {site}",
                                    (
                                        "WordPress hosting alert\n\n"
                                        f"Site: {site}\n"
                                        f"Domain: {domain}\n"
                                        "State: UNHEALTHY\n"
                                        f"Consecutive failures: {failure_count}\n"
                                        f"{message}\n"
                                        f"Time: {now}\n"
                                    ),
                                    settings
                                )
                                log_action(
                                    "health_alert_email",
                                    site,
                                    "success" if ok else "failed",
                                    f"{detail}; failures={failure_count}"
                                )
                    else:
                        if previous_unhealthy == 1:
                            db.execute(
                                "UPDATE health_notification_state SET unhealthy=0,failure_count=0,last_message=?,last_changed=?,last_email=? WHERE site=?",
                                (message, now, now, site)
                            )
                            db.commit()

                            if settings.get("enabled") and settings.get("send_recovery", True):
                                ok, detail = send_alert_email(
                                    f"[WP Host] RECOVERED: {site}",
                                    (
                                        "WordPress hosting recovery\n\n"
                                        f"Site: {site}\n"
                                        f"Domain: {domain}\n"
                                        "State: HEALTHY\n"
                                        f"Time: {now}\n"
                                    ),
                                    settings
                                )
                                log_action(
                                    "health_recovery_email",
                                    site,
                                    "success" if ok else "failed",
                                    detail
                                )
                        else:
                            db.execute(
                                "UPDATE health_notification_state SET failure_count=0,last_message=? WHERE site=?",
                                (message, site)
                            )
                            db.commit()

            time.sleep(interval)

        except Exception as exc:
            try:
                log_action("health_email_monitor", "system", "failed", str(exc))
            except Exception:
                pass
            time.sleep(60)

def sha256_file(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def security_expected_root_names():
    return {
        "wp-admin", "wp-content", "wp-includes",
        "index.php", "license.txt", "readme.html", "wp-activate.php",
        "wp-blog-header.php", "wp-comments-post.php", "wp-config.php",
        "wp-config-sample.php", "wp-cron.php", "wp-links-opml.php",
        "wp-load.php", "wp-login.php", "wp-mail.php", "wp-settings.php",
        "wp-signup.php", "wp-trackback.php", "xmlrpc.php", ".htaccess",
        ".user.ini", "robots.txt", "favicon.ico"
    }

def get_wp_page_slugs(site):
    try:
        c = docker_client.containers.get(f"{site}-wp")
        if c.status != "running":
            return []
        code = (
            "require '/var/www/html/wp-load.php'; "
            "$q=get_posts(['post_type'=>'page','post_status'=>'publish','numberposts'=>-1]); "
            "foreach($q as $p){echo $p->post_name.\"\\n\";}"
        )
        ex = c.exec_run(["php", "-r", code])
        if ex.exit_code == 0:
            return sorted({x.strip() for x in ex.output.decode(errors="ignore").splitlines() if x.strip()})
    except Exception:
        pass
    return []

def get_wp_admin_users(site):
    try:
        c = docker_client.containers.get(f"{site}-wp")
        if c.status != "running":
            return []
        code = (
            "require '/var/www/html/wp-load.php'; "
            "$u=get_users(['role'=>'administrator']); "
            "foreach($u as $x){echo $x->user_login.\"\\n\";}"
        )
        ex = c.exec_run(["php", "-r", code])
        if ex.exit_code == 0:
            return sorted({x.strip() for x in ex.output.decode(errors="ignore").splitlines() if x.strip()})
    except Exception:
        pass
    return []

def build_security_snapshot(site):
    root = SITES / site / "wordpress"
    if not root.exists():
        return None
    entries = sorted(p.name for p in root.iterdir())
    hashes = {}
    for name in SECURITY_HASH_FILES:
        p = root / name
        if p.exists() and p.is_file():
            try:
                hashes[name] = sha256_file(p)
            except Exception:
                pass
    return {
        "root_entries": entries,
        "file_hashes": hashes,
        "admin_users": get_wp_admin_users(site),
        "page_slugs": get_wp_page_slugs(site),
    }

def set_security_baseline(site, username):
    snap = build_security_snapshot(site)
    if not snap:
        raise RuntimeError("WordPress filesystem not found.")
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(PLATFORM_DB) as db:
        db.execute("""
            INSERT INTO security_baselines(site,created_at,created_by,root_entries,file_hashes,admin_users,page_slugs)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(site) DO UPDATE SET
              created_at=excluded.created_at,
              created_by=excluded.created_by,
              root_entries=excluded.root_entries,
              file_hashes=excluded.file_hashes,
              admin_users=excluded.admin_users,
              page_slugs=excluded.page_slugs
        """, (
            site, now, username,
            json.dumps(snap["root_entries"]),
            json.dumps(snap["file_hashes"]),
            json.dumps(snap["admin_users"]),
            json.dumps(snap["page_slugs"]),
        ))
        db.commit()
    return snap

def load_security_baseline(site):
    with sqlite3.connect(PLATFORM_DB) as db:
        row = db.execute(
            "SELECT created_at,created_by,root_entries,file_hashes,admin_users,page_slugs "
            "FROM security_baselines WHERE site=?",
            (site,)
        ).fetchone()
    if not row:
        return None
    return {
        "created_at": row[0],
        "created_by": row[1],
        "root_entries": json.loads(row[2] or "[]"),
        "file_hashes": json.loads(row[3] or "{}"),
        "admin_users": json.loads(row[4] or "[]"),
        "page_slugs": json.loads(row[5] or "[]"),
    }

def finding_key(finding_type, path, message):
    import hashlib
    raw = f"{finding_type}|{path or ''}|{message}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()

def record_security_finding(site, severity, finding_type, path, message, evidence=""):
    key = finding_key(finding_type, path, message)
    now = datetime.now(timezone.utc).isoformat()
    created = False

    with sqlite3.connect(PLATFORM_DB) as db:
        row = db.execute(
            "SELECT id,active,email_sent FROM security_findings WHERE site=? AND finding_key=?",
            (site, key)
        ).fetchone()

        if row:
            was_active = int(row[1] or 0)
            db.execute(
                "UPDATE security_findings SET severity=?,finding_type=?,path=?,message=?,evidence=?,"
                "last_seen=?,active=1 WHERE id=?",
                (severity, finding_type, path, message, evidence, now, row[0])
            )
            if not was_active:
                db.execute("UPDATE security_findings SET email_sent=0 WHERE id=?", (row[0],))
                created = True
            finding_id = row[0]
        else:
            cur = db.execute("""
                INSERT INTO security_findings(
                    site,severity,finding_key,finding_type,path,message,evidence,
                    first_seen,last_seen,active,email_sent
                ) VALUES(?,?,?,?,?,?,?,?,?,1,0)
            """, (site,severity,key,finding_type,path,message,evidence,now,now))
            finding_id = cur.lastrowid
            created = True
        db.commit()

    if created and severity in SECURITY_EMAIL_SEVERITIES:
        settings = load_email_settings()
        if settings.get("enabled"):
            subject = f"[WP Host Security] {severity}: {site}"
            body = (
                "WordPress Host security alert\n\n"
                f"Site: {site}\n"
                f"Severity: {severity}\n"
                f"Finding: {finding_type}\n"
                f"Path: {path or '-'}\n"
                f"Message: {message}\n"
                f"Evidence: {evidence or '-'}\n"
                f"Time: {now}\n"
            )
            ok, detail = send_alert_email(subject, body, settings)
            with sqlite3.connect(PLATFORM_DB) as db:
                db.execute(
                    "UPDATE security_findings SET email_sent=? WHERE id=?",
                    (1 if ok else 0, finding_id)
                )
                db.commit()
            try:
                log_action("security_alert_email", site, "success" if ok else "failed", detail)
            except Exception:
                pass

    return key

def resolve_missing_findings(site, seen_keys):
    with sqlite3.connect(PLATFORM_DB) as db:
        rows = db.execute(
            "SELECT id,finding_key FROM security_findings WHERE site=? AND active=1",
            (site,)
        ).fetchall()
        for finding_id, key in rows:
            if key not in seen_keys:
                db.execute("UPDATE security_findings SET active=0 WHERE id=?", (finding_id,))
        db.commit()

def suspicious_php_in_uploads(root):
    findings = []
    uploads = root / "wp-content" / "uploads"
    if not uploads.exists():
        return findings
    for p in uploads.rglob("*.php"):
        try:
            rel = str(p.relative_to(root))
            findings.append(("CRITICAL", "php_in_uploads", rel,
                             "PHP file detected inside wp-content/uploads",
                             f"size={p.stat().st_size}"))
        except Exception:
            pass
    return findings

def suspicious_root_entries(root, baseline, page_slugs):
    findings = []
    expected = security_expected_root_names()
    baseline_entries = set((baseline or {}).get("root_entries", []))
    for p in root.iterdir():
        name = p.name
        if name in expected:
            continue
        if name in baseline_entries:
            continue

        rel = name
        severity = "HIGH" if p.is_dir() else "MEDIUM"
        message = "Unexpected root-level directory" if p.is_dir() else "Unexpected root-level file"
        evidence = "New root object not present in trusted baseline."

        if p.is_dir() and name in set(page_slugs):
            severity = "CRITICAL"
            message = "Physical root directory shadows a published WordPress page slug"
            evidence = f"WordPress page slug '{name}' is also a real filesystem directory."

        if p.is_dir():
            try:
                suspicious_names = []
                for child in p.rglob("*"):
                    if child.is_file() and child.suffix.lower() in {".php",".phtml",".phar"}:
                        suspicious_names.append(str(child.relative_to(root)))
                        if len(suspicious_names) >= 10:
                            break
                if suspicious_names:
                    severity = "CRITICAL"
                    evidence += " Executable PHP files: " + ", ".join(suspicious_names)
            except Exception:
                pass

        findings.append((severity, "unexpected_root_entry", rel, message, evidence))
    return findings

def changed_sensitive_files(root, baseline):
    findings = []
    if not baseline:
        return findings
    old = baseline.get("file_hashes", {})
    for name, old_hash in old.items():
        p = root / name
        if not p.exists():
            findings.append(("HIGH", "sensitive_file_missing", name,
                             "Sensitive WordPress file missing since baseline", ""))
            continue
        try:
            new_hash = sha256_file(p)
            if new_hash != old_hash:
                findings.append(("HIGH", "sensitive_file_changed", name,
                                 "Sensitive WordPress file changed since baseline",
                                 f"baseline={old_hash[:12]} current={new_hash[:12]}"))
        except Exception:
            pass
    return findings

def new_admin_users(site, baseline):
    findings = []
    if not baseline:
        return findings
    old = set(baseline.get("admin_users", []))
    current = set(get_wp_admin_users(site))
    for username in sorted(current - old):
        findings.append(("CRITICAL", "new_admin_user", f"user:{username}",
                         "New WordPress administrator detected",
                         f"Administrator '{username}' was not present in baseline."))
    return findings

def external_redirect_probe(site, domain, paths):
    findings = []
    if not domain:
        return findings
    base_host = domain.lower().split(":")[0]
    for path in paths[:20]:
        url = f"https://{domain}/{path.strip('/')}/"
        try:
            r = requests.get(
                url,
                timeout=10,
                allow_redirects=True,
                headers={"User-Agent":"Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/125 Mobile Safari/537.36"}
            )
            final_host = requests.utils.urlparse(r.url).hostname or ""
            if final_host and final_host.lower() not in {base_host, f"www.{base_host}"}:
                findings.append(("CRITICAL", "external_redirect", path,
                                 "Suspicious path redirects to an external domain",
                                 f"{url} -> {r.url}"))
        except Exception:
            pass
    return findings

def scan_site_security(site):
    root = SITES / site / "wordpress"
    if not root.exists():
        return []

    baseline = load_security_baseline(site)
    if baseline is None:
        # First scan establishes a trusted starting point rather than creating noise.
        set_security_baseline(site, "system-auto")
        baseline = load_security_baseline(site)

    page_slugs = get_wp_page_slugs(site)
    findings = []
    findings.extend(suspicious_root_entries(root, baseline, page_slugs))
    findings.extend(suspicious_php_in_uploads(root))
    findings.extend(changed_sensitive_files(root, baseline))
    findings.extend(new_admin_users(site, baseline))
    ok, detail = wordpress_core_checksum(site)
    if not ok:
        findings.append(("CRITICAL","wordpress_core_checksum","/var/www/html","WordPress official core checksum verification failed",detail))
    meta = site_metadata(SITES / site)
    domain = meta.get("domain", "")
    suspicious_paths = [
        f[2] for f in findings
        if f[1] == "unexpected_root_entry" and (root / f[2]).is_dir()
    ]
    findings.extend(external_redirect_probe(site, domain, suspicious_paths))

    seen = set()
    for severity, ftype, path, message, evidence in findings:
        key = record_security_finding(site, severity, ftype, path, message, evidence)
        seen.add(key)

    # Anything not reproduced in this scan is automatically resolved.
    resolve_missing_findings(site, seen)
    return findings

def security_scan_all():
    results = {}
    for d in sorted([p for p in SITES.iterdir() if p.is_dir()]) if SITES.exists() else []:
        if not (d / "site.json").exists():
            continue
        try:
            results[d.name] = scan_site_security(d.name)
        except Exception as exc:
            results[d.name] = [("MEDIUM", "scan_error", "", "Security scan failed", str(exc))]
    return results

def security_scanner_loop():
    while True:
        try:
            security_scan_all()
        except Exception as exc:
            try:
                log_action("security_scan", "system", "failed", str(exc))
            except Exception:
                pass
        time.sleep(max(1, SECURITY_SCAN_INTERVAL_MINUTES) * 60)

def get_site_ops(site):
    with sqlite3.connect(PLATFORM_DB) as db:
        db.execute("INSERT OR IGNORE INTO site_operations(site) VALUES(?)",(site,)); db.commit()
        r=db.execute("SELECT mode,notes,owner_name,owner_email,owner_phone,external_url,last_external_status,last_external_ms,last_external_check FROM site_operations WHERE site=?",(site,)).fetchone()
    return dict(zip(["mode","notes","owner_name","owner_email","owner_phone","external_url","last_external_status","last_external_ms","last_external_check"],r))

def host_policy():
    with sqlite3.connect(PLATFORM_DB) as db:
        r=db.execute("SELECT disk_warn,disk_high,disk_critical,external_check_minutes,restart_alert_delta FROM host_policy WHERE id=1").fetchone()
    return dict(zip(["disk_warn","disk_high","disk_critical","external_check_minutes","restart_alert_delta"],r))

def wordpress_core_checksum(site):
    try:
        c=docker_client.containers.get(f"{site}-wp")
        cmd=["sh","-lc","curl -fsSL -o /tmp/wp-cli.phar https://raw.githubusercontent.com/wp-cli/builds/gh-pages/phar/wp-cli.phar && php /tmp/wp-cli.phar core verify-checksums --allow-root --path=/var/www/html"]
        ex=c.exec_run(cmd); return ex.exit_code==0,ex.output.decode(errors="ignore")[-4000:]
    except Exception as e:return False,str(e)

def plugin_theme_inventory(site):
    try:
        c=docker_client.containers.get(f"{site}-wp")
        code="require '/var/www/html/wp-load.php';require_once ABSPATH.'wp-admin/includes/plugin.php';$p=get_plugins();foreach($p as $k=>$v){echo 'PLUGIN|'.$k.'|'.($v['Version']??'').'\\n';}$ts=wp_get_themes();foreach($ts as $k=>$v){echo 'THEME|'.$k.'|'.$v->get('Version').'\\n';}"
        return c.exec_run(["php","-r",code]).output.decode(errors="ignore")
    except Exception as e:return str(e)

def operational_monitor_loop():
    while True:
        try:
            p=host_policy()
            du=shutil.disk_usage("/"); pct=round(du.used/du.total*100)
            sev="CRITICAL" if pct>=p["disk_critical"] else "HIGH" if pct>=p["disk_high"] else "MEDIUM" if pct>=p["disk_warn"] else None
            if sev: record_security_finding("HOST",sev,"disk_pressure","/",f"Host disk usage is {pct}%",f"Thresholds: {p['disk_warn']}/{p['disk_high']}/{p['disk_critical']}%")
            for d in [x for x in SITES.iterdir() if x.is_dir()] if SITES.exists() else []:
                site=d.name
                for suffix in ("wp","db"):
                    try:
                        c=docker_client.containers.get(f"{site}-{suffix}"); count=int(c.attrs.get("RestartCount",0))
                        with sqlite3.connect(PLATFORM_DB) as db:
                            db.execute("INSERT OR IGNORE INTO site_operations(site) VALUES(?)",(site,))
                            old=db.execute("SELECT last_restart_seen FROM site_operations WHERE site=?",(site,)).fetchone()[0]
                            if count-old>=p["restart_alert_delta"]: record_security_finding(site,"HIGH","container_restart_anomaly",c.name,"Container restart anomaly",f"Restart count {old} -> {count}")
                            db.execute("UPDATE site_operations SET last_restart_seen=?,restart_count=? WHERE site=?",(count,count,site));db.commit()
                    except Exception: pass
                meta=site_metadata(d); ops=get_site_ops(site); url=ops["external_url"] or (f"https://{meta.get('domain')}" if meta.get("domain") else "")
                if url:
                    import time as _time
                    status="DOWN"; ms=None
                    try:
                        st=_time.monotonic(); rr=requests.get(url,timeout=15,allow_redirects=True); ms=int((_time.monotonic()-st)*1000); status="UP" if rr.status_code<500 else f"HTTP {rr.status_code}"
                        if status!="UP": record_security_finding(site,"HIGH","external_uptime",url,"External uptime check failed",status)
                    except Exception as e: record_security_finding(site,"HIGH","external_uptime",url,"External uptime check failed",str(e))
                    with sqlite3.connect(PLATFORM_DB) as db:
                        db.execute("UPDATE site_operations SET external_url=?,last_external_status=?,last_external_ms=?,last_external_check=? WHERE site=?",(url,status,ms,datetime.now(timezone.utc).isoformat(),site));db.commit()
        except Exception as e:
            try: log_action("operational_monitor","system","failed",str(e))
            except Exception: pass
        time.sleep(max(1,int(host_policy()["external_check_minutes"]))*60)

def active_security_findings():
    with sqlite3.connect(PLATFORM_DB) as db:
        rows = db.execute("""
            SELECT id,site,severity,finding_type,path,message,evidence,first_seen,last_seen,email_sent
            FROM security_findings
            WHERE active=1
            ORDER BY CASE severity WHEN 'CRITICAL' THEN 1 WHEN 'HIGH' THEN 2 WHEN 'MEDIUM' THEN 3 ELSE 4 END,
                     last_seen DESC
        """).fetchall()
    return [
        dict(id=r[0],site=r[1],severity=r[2],finding_type=r[3],path=r[4] or "-",
             message=r[5],evidence=r[6] or "-",first_seen=format_adelaide(r[7]),last_seen=format_adelaide(r[8]),
             email_sent=bool(r[9]))
        for r in rows
    ]

def security_alert_count():
    with sqlite3.connect(PLATFORM_DB) as db:
        return db.execute("SELECT COUNT(*) FROM security_findings WHERE active=1").fetchone()[0]

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

LOCAL_BACKUP_DEST_FILE = BASE / "local-backup-destination.json"

def available_backup_destinations():
    """Real mounted filesystems that are reasonable candidates for local backup storage."""
    return [d for d in all_disk_usage() if d["mountpoint"] not in ("/boot", "/boot/efi")]

def load_backup_root_choice():
    if LOCAL_BACKUP_DEST_FILE.exists():
        try:
            data = json.loads(LOCAL_BACKUP_DEST_FILE.read_text())
            mp = data.get("mountpoint")
            if mp:
                return mp
        except Exception:
            pass
    return None

def save_backup_root_choice(mountpoint):
    tmp = LOCAL_BACKUP_DEST_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"mountpoint": mountpoint}, indent=2))
    tmp.replace(LOCAL_BACKUP_DEST_FILE)

def current_backup_mountpoint():
    chosen = load_backup_root_choice()
    mounted = {d["mountpoint"] for d in all_disk_usage()}
    if chosen and chosen in mounted:
        return chosen
    return str(BASE)

def backup_root():
    mp = current_backup_mountpoint()
    if mp == str(BASE):
        return _DEFAULT_BACKUP_ROOT
    return Path(mp) / "wp-host-backups"

BACKUP_STORAGE_FILE = BASE / "backup-storage.json"
BACKUP_CREDENTIALS_DIR = Path("/etc/wp-host/credentials")
BACKUP_MOUNT_ROOT = Path("/mnt/wp-host")

def load_backup_storage():
    defaults = {"type":"smb", "name":"Network NAS", "server":"", "share":"", "username":"", "mount_name":"nas", "retention":30}
    if BACKUP_STORAGE_FILE.exists():
        try:
            data = json.loads(BACKUP_STORAGE_FILE.read_text())
            defaults.update({k:v for k,v in data.items() if k != "password"})
        except Exception:
            pass
    return defaults

def save_backup_storage(data):
    BASE.mkdir(parents=True, exist_ok=True)
    tmp = BACKUP_STORAGE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(BACKUP_STORAGE_FILE)

def backup_mount_path(cfg=None):
    cfg = cfg or load_backup_storage()
    name = str(cfg.get("mount_name") or "nas").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError("Invalid mount name.")
    root = BACKUP_MOUNT_ROOT.resolve()
    target = (BACKUP_MOUNT_ROOT / name).resolve()
    if target.parent != root:
        raise ValueError("NAS mount must be directly beneath /mnt/wp-host/.")
    return target

def backup_credentials_path():
    return BACKUP_CREDENTIALS_DIR / "nas-smb.credentials"

def nas_is_mounted(cfg=None):
    try:
        target = backup_mount_path(cfg)
        return target.is_dir() and os.path.ismount(target)
    except Exception:
        return False

def nas_mount_unit_name(cfg=None):
    target = backup_mount_path(cfg)
    return run(["systemd-escape", "--path", "--suffix=mount", str(target)])

def write_nas_mount_unit(cfg, password=None):
    if cfg.get("type", "smb") != "smb":
        raise ValueError("Only SMB/CIFS is supported in this version.")
    server = str(cfg.get("server", "")).strip()
    share = str(cfg.get("share", "")).strip().strip("/")
    username = str(cfg.get("username", "")).strip()
    if not server or not share or not username:
        raise ValueError("NAS server, share and username are required.")
    if any(ch in server+share for ch in "\n\r"):
        raise ValueError("Invalid NAS server or share.")
    target = backup_mount_path(cfg)
    target.mkdir(parents=True, exist_ok=True)
    BACKUP_CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(BACKUP_CREDENTIALS_DIR, 0o700)
    cred = backup_credentials_path()
    if password:
        cred.write_text(f"username={username}\npassword={password}\n")
        os.chmod(cred, 0o600)
    elif not cred.exists():
        raise ValueError("A NAS password is required for the first configuration.")
    else:
        lines = cred.read_text().splitlines()
        saved_pw = next((x.split("=",1)[1] for x in lines if x.startswith("password=")), "")
        cred.write_text(f"username={username}\npassword={saved_pw}\n")
        os.chmod(cred, 0o600)
    unit = nas_mount_unit_name(cfg)
    unit_path = Path("/etc/systemd/system") / unit
    content = f"""[Unit]\nDescription=WordPress Host NAS Backup Storage\nAfter=network-online.target\nWants=network-online.target\n\n[Mount]\nWhat=//{server}/{share}\nWhere={target}\nType=cifs\nOptions=credentials={cred},rw,vers=3.0,iocharset=utf8,_netdev,nofail,file_mode=0600,dir_mode=0700\nTimeoutSec=30\n\n[Install]\nWantedBy=multi-user.target\n"""
    unit_path.write_text(content)
    os.chmod(unit_path, 0o644)
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", unit])
    return unit

def mount_nas(cfg=None):
    cfg = cfg or load_backup_storage()
    unit = nas_mount_unit_name(cfg)
    run(["systemctl", "restart", unit], timeout=60)
    if not nas_is_mounted(cfg):
        raise RuntimeError("systemd started the mount unit but the NAS is not mounted.")
    return backup_mount_path(cfg)

def unmount_nas(cfg=None):
    cfg = cfg or load_backup_storage()
    unit = nas_mount_unit_name(cfg)
    run(["systemctl", "stop", unit], timeout=60, check=False)
    return not nas_is_mounted(cfg)

def directory_bytes(path):
    path = Path(path)
    if not path.exists(): return 0
    total = 0
    for root, dirs, files in os.walk(path):
        for name in files:
            try: total += (Path(root)/name).stat().st_size
            except OSError: pass
    return total

def backup_sets_for_site(site, root=None):
    root = Path(root or backup_root())
    bdir = root / site
    if not bdir.exists(): return []
    sets = {}
    for p in bdir.iterdir():
        if not p.is_file(): continue
        m = re.match(r"^(\d{8}-\d{6})(-database\.sql(?:\.gz)?|\.tar\.gz)$", p.name)
        if not m: continue
        stamp, suffix = m.groups()
        row = sets.setdefault(stamp, {"stamp":stamp, "archive":None, "database":None})
        if suffix == ".tar.gz": row["archive"] = p
        else: row["database"] = p
    return [sets[k] for k in sorted(sets, reverse=True)]

def enforce_set_retention(site, root, keep):
    sets = backup_sets_for_site(site, root)
    for row in sets[max(1, int(keep)):]:
        for key in ("archive", "database"):
            p = row.get(key)
            if p: p.unlink(missing_ok=True)

def replicate_backup_set_to_nas(site, stamp):
    cfg = load_backup_storage()
    if not cfg.get("server") or not nas_is_mounted(cfg):
        return False, "NAS not configured or mounted"
    source = backup_root() / site
    dest = backup_mount_path(cfg) / site
    dest.mkdir(parents=True, exist_ok=True)
    candidates = [source/f"{stamp}.tar.gz", source/f"{stamp}-database.sql.gz", source/f"{stamp}-database.sql"]
    copied = 0
    for src in candidates:
        if not src.exists(): continue
        dst = dest/src.name
        shutil.copy2(src, dst)
        if dst.stat().st_size != src.stat().st_size:
            raise RuntimeError(f"NAS verification failed for {src.name}")
        copied += 1
    if copied < 2:
        return False, "Local backup set is incomplete"
    enforce_set_retention(site, backup_mount_path(cfg), int(cfg.get("retention",30)))
    return True, "NAS replication complete"

def nas_status():
    cfg = load_backup_storage()
    configured = bool(cfg.get("server") and cfg.get("share"))
    mounted = nas_is_mounted(cfg) if configured else False
    free = "-"
    if mounted:
        try: free = human_bytes(shutil.disk_usage(backup_mount_path(cfg)).free)
        except Exception: pass
    nas = dict(cfg)
    nas.update({"configured":configured, "mounted":mounted, "mount_point":str(backup_mount_path(cfg)), "free":free, "password_saved":backup_credentials_path().exists()})
    return nas


def enforce_retention(site):
    bdir = backup_root() / site
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
                    ex = c.exec_run(["php","-r","include '/var/www/html/wp-includes/version.php'; echo $wp_version;"])
                    if ex.exit_code == 0:
                        wp_version = ex.output.decode(errors="ignore").strip() or "-"
                except Exception:
                    pass
        except Exception:
            status, cpu, memory, size, wp_version, ip = "not-created", "-", "-", "-", "-", "-"

        bdir = backup_root() / site
        latest = "-"
        if bdir.exists():
            items = sorted(bdir.glob("*.tar.gz"), key=lambda p:p.stat().st_mtime, reverse=True)
            if items:
                latest = format_adelaide(items[0].stat().st_mtime)

        health = site_health(site, meta.get("domain","-"))
        versions = wp_versions(site)
        live = container_live_stats(f"{site}-wp")
        sftp = sftp_status(site)
        pma = phpmyadmin_status(site)
        ops = get_site_ops(site)
        result.append(dict(
            site=site,
            domain=meta.get("domain","-"),
            status=status,
            cpu=cpu,
            memory=memory,
            live_cpu=live["cpu"],
            live_memory=live["memory"],
            sftp_enabled=sftp["enabled"],
            sftp_status=sftp["status"],
            sftp_port=sftp["port"],
            phpmyadmin_enabled=pma["enabled"],
            phpmyadmin_status=pma["status"],
            phpmyadmin_port=pma["port"],
            mode=ops.get("mode","live"),
            owner_name=ops.get("owner_name",""),
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

def get_site_summary(site):
    for s in list_sites():
        if s.get("site") == site:
            return s
    return None

def site_backup_history(site, limit=20):
    bdir = backup_root() / site
    if not bdir.exists():
        return []
    rows = []
    for p in sorted(bdir.glob("*.tar.gz"), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
        rows.append({
            "name": p.name,
            "size": human_bytes(p.stat().st_size),
            "created": format_adelaide(p.stat().st_mtime),
        })
    return rows

def site_runtime_details(site):
    data = {
        "php_version":"-","uptime":"-","restart_count":0,
        "db_name":"-","db_user":"-","db_host":"-",
        "plugin_items":[],"theme_items":[],"active_plugins":0,
        "core_checksum":"Unknown",
    }

    try:
        wp = docker_client.containers.get(f"{site}-wp")
        wp.reload()
        started = wp.attrs.get("State",{}).get("StartedAt")
        if started:
            dt = datetime.fromisoformat(started.replace("Z","+00:00"))
            seconds = max(0, int((datetime.now(timezone.utc)-dt.astimezone(timezone.utc)).total_seconds()))
            days, rem = divmod(seconds,86400)
            hours, rem = divmod(rem,3600)
            minutes = rem//60
            data["uptime"] = f"{days}d {hours}h {minutes}m"
        data["restart_count"] = int(wp.attrs.get("RestartCount",0))
        ex = wp.exec_run(["php","-r","echo PHP_VERSION;"])
        if ex.exit_code == 0:
            data["php_version"] = ex.output.decode(errors="ignore").strip()

        env = {}
        for item in wp.attrs.get("Config",{}).get("Env",[]) or []:
            if "=" in item:
                k,v=item.split("=",1); env[k]=v
        data["db_name"] = env.get("WORDPRESS_DB_NAME","-")
        data["db_user"] = env.get("WORDPRESS_DB_USER","-")
        data["db_host"] = env.get("WORDPRESS_DB_HOST","-")

        inv = plugin_theme_inventory(site)
        plugins, themes = [], []
        for line in inv.splitlines():
            parts=line.split("|")
            if len(parts)>=3 and parts[0]=="PLUGIN":
                plugins.append({"name":parts[1],"version":parts[2]})
            elif len(parts)>=3 and parts[0]=="THEME":
                themes.append({"name":parts[1],"version":parts[2]})
        data["plugin_items"] = plugins
        data["theme_items"] = themes

        code = (
            "require '/var/www/html/wp-load.php'; require_once ABSPATH.'wp-admin/includes/plugin.php'; "
            "echo count(get_option('active_plugins',[]));"
        )
        ex = wp.exec_run(["php","-r",code])
        if ex.exit_code == 0:
            try: data["active_plugins"] = int(ex.output.decode(errors="ignore").strip())
            except Exception: pass

        ok, detail = wordpress_core_checksum(site)
        data["core_checksum"] = "Verified" if ok else "FAILED"
        data["core_checksum_detail"] = detail
    except Exception as exc:
        data["runtime_error"] = str(exc)
    return data

def site_security_findings(site):
    return [f for f in active_security_findings() if f.get("site") == site]

def valid_memory(v):
    return re.fullmatch(r"[0-9]+(?:m|g)", v or "") is not None

def ensure_wordpress_proxy_https(site):
    cfg_path = SITES / site / "wordpress" / "wp-config.php"
    if not cfg_path.exists():
        return False
    cfg = cfg_path.read_text()
    if "Reverse proxy HTTPS support" in cfg:
        return True
    block = "\n/* Reverse proxy HTTPS support */\nif (isset($_SERVER['HTTP_X_FORWARDED_PROTO']) && strpos($_SERVER['HTTP_X_FORWARDED_PROTO'], 'https') !== false) {\n    $_SERVER['HTTPS'] = 'on';\n}\n"
    marker = "/* That's all, stop editing! Happy publishing. */"
    cfg = cfg.replace(marker, block + "\n" + marker) if marker in cfg else cfg + block
    cfg_path.write_text(cfg)
    return True

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
    try:
        pma = ensure_phpmyadmin(site)
        meta["phpmyadmin_port"] = pma["port"]
        meta["phpmyadmin_status"] = "provisioned"
    except Exception as exc:
        meta["phpmyadmin_status"] = "failed"
        meta["phpmyadmin_error"] = str(exc)

    try:
        ensure_wordpress_proxy_https(site)
    except Exception as exc:
        meta["proxy_https_warning"] = str(exc)

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
    dest = backup_root() / site
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
    try:
        ok, detail = replicate_backup_set_to_nas(site, stamp)
        if load_backup_storage().get("server"):
            log_action("backup_nas_replication", site, "success" if ok else "warning", detail)
    except Exception as exc:
        log_action("backup_nas_replication", site, "failed", str(exc))
    return archive.name

def restore_latest(site):
    site_dir = SITES / site
    bdir = backup_root() / site
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

def site_summary(sites):
    return {
        "total": len(sites),
        "running": sum(1 for s in sites if s.get("status") == "running" and s.get("health") == "Healthy"),
        "not_created": sum(1 for s in sites if s.get("status") != "running"),
        "backups": sum(1 for s in sites if s.get("backup") not in ("-", "", None)),
    }

def metric_summary(hours=24):
    rows = recent_host_metrics(hours=hours)
    if not rows:
        return {"cpu_avg":"-","cpu_max":"-","ram_avg":"-","ram_max":"-","disk_current":"-","disk_max":"-","samples":0}
    def avg(key):
        return round(sum(float(r[key]) for r in rows)/len(rows), 1)
    def mx(key):
        return round(max(float(r[key]) for r in rows), 1)
    return {
        "cpu_avg": avg("cpu"),
        "cpu_max": mx("cpu"),
        "ram_avg": avg("ram"),
        "ram_max": mx("ram"),
        "disk_current": round(float(rows[-1]["disk"]),1),
        "disk_max": mx("disk"),
        "samples": len(rows),
    }

def list_dashboard_users():
    users = load_users()
    return [
        {
            "username": username,
            "email": entry.get("email", ""),
            "role": entry.get("role", "view"),
            "enabled": entry.get("enabled", True),
            "created": entry.get("created", "-"),
            "mfa_enabled": bool(entry.get("mfa_enabled", False)),
            "mfa_required": bool(entry.get("mfa_required", False)),
            "force_password_change": bool(entry.get("force_password_change", False)),
        }
        for username, entry in sorted(users.items())
    ]

EMAIL_SETTINGS_HTML = open(
    __file__.rsplit("/", 1)[0] + "/templates/email_settings.html",
    encoding="utf-8"
).read()


USERS_PAGE_HTML = r"""
<!doctype html>
<html>

<head>

<meta charset="utf-8">

<meta name="viewport"
      content="width=device-width,initial-scale=1">

<title>
Users · {{ platform_title }}
</title>

<style>

:root {
    color-scheme: dark;

    --bg: #07111f;
    --side: #081322;
    --panel: #0f1b2d;
    --line: #263850;

    --text: #e7eef8;
    --muted: #91a3bb;

    --blue: #2563eb;
    --green: #059669;
    --orange: #d97706;
    --red: #dc2626;
    --purple: #7c3aed;
}

* {
    box-sizing: border-box;
}

body {
    margin: 0;

    background: var(--bg);
    color: var(--text);

    font-family:
        Inter,
        system-ui,
        Segoe UI,
        sans-serif;
}

a {
    color: #82b6ff;
    text-decoration: none;
}

.sidebar {
    position: fixed;

    inset:
        0
        auto
        0
        0;

    width: 224px;

    background: var(--side);

    border-right:
        1px solid
        var(--line);

    padding:
        18px
        14px;
}

.main {
    margin-left: 224px;
}

.brand {
    margin:
        6px
        8px
        22px;

    font-size: 19px;
    font-weight: 850;
}

.brand small {
    display: block;

    color: var(--muted);

    font-size: 12px;
}

.nav a {
    display: block;

    padding:
        11px
        12px;

    margin:
        3px
        0;

    border-radius: 8px;

    color: #c9d5e5;
}

.nav a:hover,
.nav a.active {
    background: #1d4ed8;
    color: white;
}

.top {
    min-height: 66px;

    border-bottom:
        1px solid
        var(--line);

    display: flex;

    align-items: center;
    justify-content: space-between;

    padding:
        0
        22px;
}

.content {
    padding: 22px;

    max-width: 1450px;
}

.card {
    background: var(--panel);

    border:
        1px solid
        var(--line);

    border-radius: 12px;

    padding: 19px;

    margin-bottom: 18px;
}

h1,
h2 {
    margin-top: 0;
}

.muted {
    color: var(--muted);
}

.formgrid {
    display: grid;

    grid-template-columns:
        1.4fr
        2fr
        1fr
        auto;

    gap: 12px;

    align-items: end;
}

label {
    display: block;

    color: #c5d1df;

    font-size: 12px;
    font-weight: 700;
}

input,
select {
    display: block;

    width: 100%;

    margin-top: 6px;

    padding: 10px;

    background: #091522;

    color: white;

    border:
        1px solid
        var(--line);

    border-radius: 8px;
}

.btn {
    display: inline-block;

    padding:
        9px
        12px;

    border: 0;
    border-radius: 7px;

    color: white;

    font-size: 12px;
    font-weight: 800;

    cursor: pointer;

    background: #475569;
}

.blue {
    background: var(--blue);
}

.green {
    background: var(--green);
}

.orange {
    background: var(--orange);
}

.red {
    background: var(--red);
}

.purple {
    background: var(--purple);
}

table {
    width: 100%;

    border-collapse: collapse;

    font-size: 13px;
}

th,
td {
    padding:
        11px
        9px;

    border-bottom:
        1px solid
        var(--line);

    text-align: left;
    vertical-align: middle;
}

th {
    color: #93c5fd;
}

.good {
    color: #4ade80;
    font-weight: 800;
}

.bad {
    color: #fb7185;
    font-weight: 800;
}

.warn {
    color: #fbbf24;
    font-weight: 800;
}

.actions {
    display: flex;

    gap: 6px;

    flex-wrap: wrap;

    align-items: center;
}

.actions form {
    margin: 0;
}

.flash {
    padding:
        10px
        12px;

    margin-bottom: 10px;

    background: #162941;

    border:
        1px solid
        var(--line);

    border-radius: 8px;
}

.pill {
    display: inline-block;

    padding:
        4px
        8px;

    border-radius: 7px;

    background: #17263b;

    font-size: 11px;
}

.smallinput {
    min-width: 180px;
}

@media(max-width:1050px) {

    .sidebar {
        display: none;
    }

    .main {
        margin-left: 0;
    }

    .formgrid {
        grid-template-columns: 1fr;
    }

    .content {
        padding: 12px;
    }

}

</style>

</head>


<body>


<aside class="sidebar">

<div class="brand">

{{ platform_name }}

<small>
Hosting Manager
</small>

</div>


<nav class="nav">

<a href="/">
⌂ Dashboard
</a>

<a href="/sites">
▦ Sites
</a>

<a href="/alerts">
⚠ Alerts
</a>

<a class="active"
   href="/users">
♟ Users
</a>

<a href="/admin/email-settings">
✉ Email Settings
</a>

<a href="/account/security">
🔐 My Security
</a>

<a href="/#system">
⚙ System
</a>

<a href="/#logs">
▤ Logs
</a>

</nav>

</aside>


<div class="main">


<header class="top">

<div>

<h1>
Users
</h1>

<div class="muted">
Platform user and authentication management
</div>

</div>


<div>

<a class="btn blue"
   href="/account/security">

My Security

</a>

&nbsp;

<a class="btn"
   href="/">

Dashboard

</a>

</div>

</header>


<main class="content">


{% with messages = get_flashed_messages() %}

{% for m in messages %}

<div class="flash">
{{ m }}
</div>

{% endfor %}

{% endwith %}


<section class="card">

<h2>
Create User
</h2>

<p class="muted">

A secure temporary password will be generated automatically
and emailed to the user.

The user will be required to change it after first login.

</p>


<form method="post"
      action="/users/create">


<input type="hidden"
       name="csrf_token"
       value="{{ csrf_token() }}">


<div class="formgrid">


<label>

Username

<input
    name="username"
    required
    minlength="3"
    maxlength="40"
    autocomplete="off"
    placeholder="jsmith">

</label>


<label>

Email Address

<input
    name="email"
    type="email"
    required
    placeholder="jsmith@example.com">

</label>


<label>

Role

<select name="role">

<option value="user">
User / Operator
</option>

<option value="view">
View Only
</option>

<option value="admin">
Admin
</option>

</select>

</label>


<button
    class="btn green"
    type="submit">

＋ Create User

</button>


</div>

</form>

</section>



<section class="card">

<h2>
Dashboard Users
</h2>


<table>


<thead>

<tr>

<th>
Username
</th>

<th>
Email
</th>

<th>
Role
</th>

<th>
Status
</th>

<th>
MFA
</th>

<th>
Password
</th>

<th>
Created
</th>

<th>
Actions
</th>

</tr>

</thead>


<tbody>


{% for u in users %}


<tr>


<td>

<strong>
{{ u.username }}
</strong>

{% if u.username == current_user %}

<span class="pill">
You
</span>

{% endif %}

</td>



<td>

{% if u.email %}

{{ u.email }}

{% else %}

<span class="warn">
Email required
</span>

{% endif %}

</td>



<td>

{{ u.role|title }}

</td>



<td>

{% if u.enabled %}

<span class="good">
Enabled
</span>

{% else %}

<span class="bad">
Disabled
</span>

{% endif %}

</td>



<td>

{% if u.mfa_enabled %}

<span class="good">
✓ Enabled
</span>

{% else %}

<span class="warn">
Not Enabled
</span>

{% endif %}

</td>



<td>

{% if u.force_password_change %}

<span class="warn">
Change Required
</span>

{% else %}

<span class="good">
Current
</span>

{% endif %}

</td>



<td>

{{ u.created or "-" }}

</td>



<td>


<div class="actions">


<form method="post"
      action="/users/{{ u.username }}/update">

<input type="hidden"
       name="csrf_token"
       value="{{ csrf_token() }}">

<input
    class="smallinput"
    name="email"
    type="email"
    value="{{ u.email }}"
    placeholder="Email">

<input type="hidden"
       name="role"
       value="{{ u.role }}">

<button
    class="btn blue"
    type="submit">

Save Email

</button>

</form>



<form method="post"
      action="/users/{{ u.username }}/reset-password">

<input type="hidden"
       name="csrf_token"
       value="{{ csrf_token() }}">

<button
    class="btn purple"
    type="submit">

Reset Password

</button>

</form>



{% if u.mfa_enabled %}

<form method="post"
      action="/users/{{ u.username }}/reset-mfa"
      onsubmit="return confirm(
          'Reset MFA for {{ u.username }}?'
      );">

<input type="hidden"
       name="csrf_token"
       value="{{ csrf_token() }}">

<button
    class="btn orange"
    type="submit">

Reset MFA

</button>

</form>

{% endif %}



{% if u.username != current_user %}


<form method="post"
      action="/users/{{ u.username }}/toggle">

<input type="hidden"
       name="csrf_token"
       value="{{ csrf_token() }}">

<button
    class="btn {{ 'orange' if u.enabled else 'green' }}"
    type="submit">

{{ 'Disable' if u.enabled else 'Enable' }}

</button>

</form>



<form method="post"
      action="/users/{{ u.username }}/delete"
      onsubmit="return confirm(
          'Permanently delete {{ u.username }}?'
      );">

<input type="hidden"
       name="csrf_token"
       value="{{ csrf_token() }}">

<button
    class="btn red"
    type="submit">

Delete

</button>

</form>


{% endif %}


</div>


</td>


</tr>


{% else %}


<tr>

<td colspan="8">

No users found.

</td>

</tr>


{% endfor %}


</tbody>


</table>


</section>


<section class="card">

<h2>
Authentication Workflow
</h2>

<p class="muted">

New user → Temporary password emailed →
Password change required →
Authenticator enrolment →
Normal login.

</p>

<p>

<a href="/admin/email-settings">
Configure / test SMTP
</a>

&nbsp; · &nbsp;

<a href="/account/security">
Manage your MFA
</a>

&nbsp; · &nbsp;

<a href="/forgot-password">
Test Forgot Password
</a>

</p>

</section>


</main>


</div>


</body>

</html>
"""


@APP.get("/users")
@admin_required
def users_page():

    return render_template_string(
        USERS_PAGE_HTML,

        users=list_dashboard_users(),

        current_user=
            session.get(
                "username",
                "-"
            ),

        current_role=
            current_role(),
    )


@APP.route("/login", methods=["GET","POST"])
def login():
    if session.get("authenticated"):
        username = session.get("username")
        entry = load_users().get(username, {})
        if session.get("force_password_change") or entry.get("force_password_change", False):
            return redirect(url_for("forced_password_change"))
        if entry.get("mfa_required", False) and not entry.get("mfa_enabled", False):
            return redirect(url_for("mfa_setup_route"))
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username","").strip()
        password = request.form.get("password","")
        if login_locked(username, client_ip()):
            log_auth(username, "login", "rate_limited")
            flash("Too many failed login attempts. Try again in about 15 minutes.")
            return render_template_string(LOGIN_HTML), 429
        users = load_users(); entry = users.get(username)
        if entry and entry.get("enabled", True) and verify_password(entry["password_hash"], password):
            if entry.get("mfa_enabled") and entry.get("mfa_secret"):
                session.clear(); session.permanent = True
                session["mfa_pending_username"] = username
                log_auth(username, "password", "success_mfa_required")
                return redirect(url_for("mfa_verify_route"))
            authenticate_session(username, entry)
            log_auth(username, "login", "success")
            if entry.get("force_password_change"):
                return redirect(url_for("forced_password_change"))
            if entry.get("mfa_required", False) and not entry.get("mfa_enabled", False):
                return redirect(url_for("mfa_setup_route"))
            return redirect(url_for("index"))
        log_auth(username, "login", "failed")
        flash("Invalid username or password.")
    return render_template_string(LOGIN_HTML)

@APP.route("/forgot-password", methods=["GET","POST"])
def forgot_password_route():
    if request.method == "POST":
        email = request.form.get("email","").strip().lower()
        users = load_users(); match = next(((u,e) for u,e in users.items() if e.get("enabled",True) and e.get("email","").strip().lower()==email), None)
        if match:
            username, entry = match
            try:
                token = new_password_reset(username)
                ok, detail = send_account_email(email, f"{PLATFORM_TITLE} password reset", f"A password reset was requested for {username}.\n\nReset your password:\n{account_public_url('/reset-password/'+token)}\n\nThis link expires in 30 minutes. If you did not request it, ignore this email.")
                log_auth(username, "forgot_password", "email_sent" if ok else "email_failed")
            except Exception:
                pass
        flash("If that email belongs to an enabled account, a reset link has been sent.")
    return render_template_string(FORGOT_PASSWORD_HTML)

@APP.route("/reset-password/<token>", methods=["GET","POST"])
def reset_password_route(token):
    username = lookup_password_reset(token)
    if not username:
        flash("This password-reset link is invalid or has expired.")
        return redirect(url_for("forgot_password_route"))
    if request.method == "POST":
        password=request.form.get("password",""); confirm=request.form.get("confirm","")
        if len(password)<12 or password!=confirm:
            flash("Passwords must match and be at least 12 characters.")
            return render_template_string(RESET_PASSWORD_HTML)
        users=load_users(); entry=users.get(username)
        if not entry:
            flash("Account not found."); return redirect(url_for("login"))
        entry["password_hash"]=generate_password_hash(password, method="scrypt")
        entry["force_password_change"]=False; entry["password_changed"]=datetime.now(timezone.utc).isoformat()
        save_users(users); consume_password_resets(username); log_auth(username,"password_reset","success")
        flash("Password changed. You can now sign in."); return redirect(url_for("login"))
    return render_template_string(RESET_PASSWORD_HTML)

@APP.route("/account/change-password", methods=["GET","POST"])
@login_required
def forced_password_change():
    username = session.get("username")
    users = load_users()
    entry = users.get(username)
    if not entry:
        session.clear()
        return redirect(url_for("login"))

    forced = bool(entry.get("force_password_change", False) or session.get("force_password_change"))

    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")

        if not forced:
            current_password = request.form.get("current_password", "")
            if not verify_password(entry["password_hash"], current_password):
                flash("Current password is incorrect.")
                return render_template_string(FORCE_PASSWORD_HTML, forced=False)

        if len(password) < 12 or password != confirm:
            flash("Passwords must match and be at least 12 characters.")
            return render_template_string(FORCE_PASSWORD_HTML, forced=forced)

        entry["password_hash"] = generate_password_hash(password, method="scrypt")
        entry["force_password_change"] = False
        entry["password_changed"] = datetime.now(timezone.utc).isoformat()
        save_users(users)
        session["force_password_change"] = False
        log_auth(username, "forced_password_change" if forced else "password_change", "success")
        log_action("password_change", username, "success", "forced" if forced else "self-service")
        flash("Password changed successfully.")

        if entry.get("mfa_required", False) and not entry.get("mfa_enabled", False):
            return redirect(url_for("mfa_setup_route"))
        return redirect(url_for("account_security"))

    return render_template_string(FORCE_PASSWORD_HTML, forced=forced)


MFA_VERIFY_HTML = r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MFA Verification</title>

<style>
body{
    margin:0;
    background:#07111f;
    color:#e7eef8;
    font-family:Inter,system-ui,Segoe UI,sans-serif
}
.card{
    max-width:480px;
    margin:80px auto;
    background:#0f1b2d;
    border:1px solid #263850;
    border-radius:12px;
    padding:26px
}
input{
    width:100%;
    box-sizing:border-box;
    padding:12px;
    margin:8px 0 15px;
    background:#091522;
    border:1px solid #263850;
    border-radius:8px;
    color:white;
    font-size:18px
}
button{
    width:100%;
    padding:11px;
    border:0;
    border-radius:8px;
    background:#2563eb;
    color:white;
    font-weight:800;
    cursor:pointer
}
.flash{
    background:#162941;
    border:1px solid #263850;
    border-radius:8px;
    padding:10px;
    margin-bottom:12px
}
.muted{color:#91a3bb}
</style>
</head>

<body>

<div class="card">

<h1>Multi-Factor Authentication</h1>

<p class="muted">
Enter the code from your authenticator application
or use one of your recovery codes.
</p>

{% with messages=get_flashed_messages() %}
{% for message in messages %}
<div class="flash">{{ message }}</div>
{% endfor %}
{% endwith %}

<form method="post">

<input type="hidden"
       name="csrf_token"
       value="{{ csrf_token() }}">

<label>
Authenticator or Recovery Code
</label>

<input name="code"
       autocomplete="one-time-code"
       autofocus
       required>

<button type="submit">
Verify
</button>

</form>

</div>

</body>
</html>
"""


MFA_SETUP_HTML = r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">

<title>Set Up MFA</title>

<style>
body{
    margin:0;
    background:#07111f;
    color:#e7eef8;
    font-family:Inter,system-ui,Segoe UI,sans-serif
}
.card{
    max-width:650px;
    margin:50px auto;
    background:#0f1b2d;
    border:1px solid #263850;
    border-radius:12px;
    padding:26px
}
.qr{
    display:block;
    margin:20px auto;
    background:white;
    padding:12px;
    border-radius:10px;
    max-width:280px
}
.secret{
    background:#091522;
    border:1px solid #263850;
    border-radius:8px;
    padding:12px;
    word-break:break-all;
    font-family:monospace
}
input{
    width:100%;
    box-sizing:border-box;
    padding:12px;
    margin:8px 0 15px;
    background:#091522;
    border:1px solid #263850;
    border-radius:8px;
    color:white;
    font-size:18px
}
button{
    width:100%;
    padding:11px;
    border:0;
    border-radius:8px;
    background:#059669;
    color:white;
    font-weight:800;
    cursor:pointer
}
.flash{
    background:#162941;
    border:1px solid #263850;
    border-radius:8px;
    padding:10px;
    margin-bottom:12px
}
.muted{color:#91a3bb}
</style>
</head>

<body>

<div class="card">

<h1>Set Up Multi-Factor Authentication</h1>

<p>
MFA is required before you can access the dashboard.
</p>

<p class="muted">
Scan the QR code using Google Authenticator,
Microsoft Authenticator, Authy or another TOTP-compatible app.
</p>


{% with messages=get_flashed_messages() %}
{% for message in messages %}
<div class="flash">{{ message }}</div>
{% endfor %}
{% endwith %}


<img class="qr"
     src="{{ qr }}"
     alt="Authenticator QR Code">


<p>
If you cannot scan the QR code, enter this key manually:
</p>

<div class="secret">
{{ secret }}
</div>


<form method="post">

<input type="hidden"
       name="csrf_token"
       value="{{ csrf_token() }}">

<label>
Enter the 6-digit code generated by your authenticator
</label>

<input name="code"
       inputmode="numeric"
       autocomplete="one-time-code"
       maxlength="6"
       required>

<button type="submit">
Verify and Enable MFA
</button>

</form>

</div>

</body>
</html>
"""


RECOVERY_CODES_HTML = r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">

<title>MFA Recovery Codes</title>

<style>
body{
    margin:0;
    background:#07111f;
    color:#e7eef8;
    font-family:Inter,system-ui,Segoe UI,sans-serif
}
.card{
    max-width:650px;
    margin:50px auto;
    background:#0f1b2d;
    border:1px solid #263850;
    border-radius:12px;
    padding:26px
}
.codes{
    background:#091522;
    border:1px solid #263850;
    border-radius:8px;
    padding:18px;
    margin:18px 0
}
.code{
    font-family:monospace;
    font-size:16px;
    margin:7px 0
}
.warning{
    background:#422006;
    border:1px solid #92400e;
    padding:12px;
    border-radius:8px
}
a{
    display:inline-block;
    background:#2563eb;
    color:white;
    padding:11px 16px;
    border-radius:8px;
    text-decoration:none;
    font-weight:800
}
</style>

</head>

<body>

<div class="card">

<h1>MFA Enabled</h1>

<p>
Your authenticator has been successfully configured.
</p>

<div class="warning">

<strong>Save these recovery codes now.</strong>

<br><br>

Each code can only be used once.
They will not be shown again.

</div>


<div class="codes">

{% for code in codes %}

<div class="code">
{{ code }}
</div>

{% endfor %}

</div>


<a href="/">
Continue to Dashboard
</a>


</div>

</body>
</html>
"""


@APP.route("/mfa/verify", methods=["GET","POST"])
def mfa_verify_route():
    username=session.get("mfa_pending_username")
    if not username: return redirect(url_for("login"))
    users=load_users(); entry=users.get(username)
    if not entry or not entry.get("enabled",True) or not entry.get("mfa_enabled"):
        session.clear(); return redirect(url_for("login"))
    if request.method=="POST":
        code=request.form.get("code","").strip().upper(); valid=pyotp.TOTP(entry.get("mfa_secret","")).verify(code, valid_window=1) if code.isdigit() else False
        if not valid:
            digest=hashlib.sha256(code.encode()).hexdigest(); hashes=entry.get("mfa_recovery_hashes",[]) or []
            if digest in hashes:
                hashes.remove(digest); entry["mfa_recovery_hashes"]=hashes; save_users(users); valid=True
        if valid:
            authenticate_session(username,entry); log_auth(username,"mfa","success")
            if entry.get("force_password_change"):
                return redirect(url_for("forced_password_change"))
            if entry.get("mfa_required", False) and not entry.get("mfa_enabled", False):
                return redirect(url_for("mfa_setup_route"))
            return redirect(url_for("index"))
        log_auth(username,"mfa","failed"); flash("Invalid authenticator or recovery code.")
    return render_template_string(MFA_VERIFY_HTML)

@APP.route("/mfa/setup", methods=["GET","POST"])
@login_required
def mfa_setup_route():
    username=session.get("username"); users=load_users(); entry=users.get(username)
    if not entry: abort(404)
    secret=session.get("pending_mfa_secret") or pyotp.random_base32(); session["pending_mfa_secret"]=secret
    if request.method=="POST":
        if not pyotp.TOTP(secret).verify(request.form.get("code","").strip(),valid_window=1): flash("Invalid authenticator code.")
        else:
            raw, hashes=new_recovery_codes(); entry["mfa_secret"]=secret; entry["mfa_enabled"]=True; entry["mfa_required"]=False; entry["mfa_enabled_at"]=datetime.now(timezone.utc).isoformat(); entry["mfa_recovery_hashes"]=hashes; save_users(users); session.pop("pending_mfa_secret",None); log_auth(username,"mfa_enable","success"); return render_template_string(RECOVERY_CODES_HTML,codes=raw)
    return render_template_string(MFA_SETUP_HTML,secret=secret,qr=mfa_qr_data(secret,username))


SECURITY_HTML = r"""
<!doctype html>

<html>

<head>

<meta charset="utf-8">

<meta name="viewport"
      content="width=device-width,initial-scale=1">

<title>
Account Security
</title>

<style>

:root {
    color-scheme: dark;

    --bg: #07111f;
    --panel: #0f1b2d;
    --line: #263850;
    --text: #e7eef8;
    --muted: #91a3bb;

    --blue: #2563eb;
    --green: #059669;
    --red: #dc2626;
}

* {
    box-sizing: border-box;
}

body {
    margin: 0;

    background: var(--bg);
    color: var(--text);

    font-family:
        Inter,
        system-ui,
        Segoe UI,
        sans-serif;
}

.wrap {
    max-width: 850px;

    margin: 42px auto;

    padding: 0 18px;
}

.card {
    background: var(--panel);

    border:
        1px solid
        var(--line);

    border-radius: 12px;

    padding: 22px;

    margin-bottom: 18px;
}

h1,
h2 {
    margin-top: 0;
}

.muted {
    color: var(--muted);
}

a {
    color: #82b6ff;

    text-decoration: none;
}

.status {
    display: inline-block;

    padding:
        5px
        10px;

    border-radius: 8px;

    font-size: 12px;
    font-weight: 800;
}

.enabled {
    background: #064e3b;
    color: #6ee7b7;
}

.disabled {
    background: #78350f;
    color: #fcd34d;
}

.btn {
    display: inline-block;

    padding:
        10px
        14px;

    border: 0;
    border-radius: 7px;

    background: var(--blue);

    color: white;

    font-weight: 800;

    cursor: pointer;

    text-decoration: none;
}

.green {
    background: var(--green);
}

.red {
    background: var(--red);
}

.flash {
    padding:
        10px
        12px;

    margin-bottom: 10px;

    background: #162941;

    border:
        1px solid
        var(--line);

    border-radius: 8px;
}

.detail {
    display: grid;

    grid-template-columns:
        150px
        1fr;

    gap: 10px;

    margin:
        18px
        0;
}

.detail div {
    padding:
        8px
        0;

    border-bottom:
        1px solid
        var(--line);
}

</style>

</head>


<body>


<div class="wrap">


<p>

<a href="/">
← Dashboard
</a>

&nbsp; · &nbsp;

<a href="/users">
Users
</a>

</p>


<h1>
Account Security
</h1>


{% with messages = get_flashed_messages() %}

{% for message in messages %}

<div class="flash">
{{ message }}
</div>

{% endfor %}

{% endwith %}


<section class="card">


<h2>
Your Account
</h2>


<div class="detail">

<div class="muted">
Username
</div>

<div>
<strong>{{ username }}</strong>
</div>


<div class="muted">
Email
</div>

<div>

{% if email %}

{{ email }}

{% else %}

<span class="muted">
No email configured
</span>

{% endif %}

</div>


<div class="muted">
MFA Status
</div>

<div>

{% if mfa_enabled %}

<span class="status enabled">
✓ Enabled
</span>

{% else %}

<span class="status disabled">
Not Enabled
</span>

{% endif %}

</div>

</div>


</section>



<section class="card">


<h2>
Multi-Factor Authentication
</h2>


{% if mfa_enabled %}


<p>

Multi-factor authentication is currently
<strong>enabled</strong> for your account.

</p>


<p class="muted">

Your authenticator code will be required
when signing in.

</p>


<form method="post"
      action="/account/mfa-disable"

      onsubmit="return confirm(
          'Reset MFA and enrol your authenticator again?'
      );">


<input type="hidden"
       name="csrf_token"
       value="{{ csrf_token() }}">

<label style="display:block;margin:14px 0 5px">Current Password</label>
<input type="password" name="password" autocomplete="current-password" required style="width:100%;padding:10px;background:#091522;border:1px solid var(--line);border-radius:8px;color:#fff;margin-bottom:12px">

<button
    class="btn red"
    type="submit">

Reset MFA / Re-enrol

</button>


</form>


{% else %}


<p>

Protect your dashboard account with an
authenticator application.

</p>


<p class="muted">

Compatible applications include Google Authenticator,
Microsoft Authenticator, Authy and other standard
TOTP authenticator applications.

</p>


<a class="btn green"
   href="/mfa/setup">

Enable MFA

</a>


{% endif %}


</section>



<section class="card">


<h2>
Password Security
</h2>


<p class="muted">

Change your dashboard password if you believe it
has been exposed or simply want to rotate it.

</p>


<a class="btn"
   href="/account/change-password">

Change Password

</a>


</section>



<section class="card">


<h2>
Recovery
</h2>


<p class="muted">

When MFA is enabled, recovery codes are generated
during enrolment.

Store those codes somewhere secure.

Each recovery code can only be used once.

</p>


</section>


</div>


</body>

</html>
"""


@APP.get("/account/security")
@login_required
def account_security():
    username=session.get("username"); entry=load_users().get(username,{})
    return render_template_string(SECURITY_HTML,username=username,email=entry.get("email",""),mfa_enabled=bool(entry.get("mfa_enabled",False)))

@APP.post("/account/mfa-disable")
@login_required
def account_mfa_disable():
    username=session.get("username"); password=request.form.get("password",""); users=load_users(); entry=users.get(username)
    if not entry or not verify_password(entry["password_hash"],password): flash("Current password is incorrect.")
    else:
        entry["mfa_enabled"]=False; entry["mfa_required"]=True; entry.pop("mfa_secret",None); entry["mfa_recovery_hashes"]=[]; save_users(users); log_auth(username,"mfa_disable","success"); flash("MFA reset. Enrol your authenticator again to continue.")
    return redirect(url_for("account_security"))


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
    sites = list_sites()
    return render_template_string(
        HTML,
        sites=sites,
        site_summary=site_summary(sites),
        auth_events=recent_auth_events(),
        network=PROXY_NETWORK,
        host=request.host,
        host_only=host_only,
        current_user=session.get("username","-"),
        current_theme=current_user_theme(),
        current_role=current_role(),
        users=list_dashboard_users() if current_role() == "admin" else [],
        email_settings=load_email_settings() if current_role() == "admin" else {},
        security_alert_count=security_alert_count() if current_role() in {"admin","user"} else 0,
        recent_security=active_security_findings()[:8] if current_role() in {"admin","user"} else [],
        recent_activity=recent_audit_events()[:10],
        host_stats=host_stats(),
        alerts=active_alerts(),
        audit_events=recent_audit_events(),
        metric_summary=metric_summary(24),
    )

@APP.get("/sites")
@login_required
def sites_page():
    return render_template_string(
        SITES_HTML,
        sites=list_sites(),
        current_user=session.get("username","-"),
        current_role=current_role(),
        security_alert_count=security_alert_count() if current_role() in {"admin","user"} else 0,
    )

@APP.get("/site/<site>")
@login_required
def site_dashboard(site):
    if not SITE_RE.match(site):
        abort(404)
    summary = get_site_summary(site)
    if summary is None:
        abort(404)

    audits = [x for x in recent_audit_events(300) if x.get("target") == site]
    return render_template_string(
        SITE_HTML,
        site=summary,
        runtime=site_runtime_details(site),
        ops=get_site_ops(site),
        backups=site_backup_history(site),
        findings=site_security_findings(site),
        migration_files=migration_engine.source_files(site),
        migration_report=migration_engine.load_report(site),
        audits=audits[:100],
        host_only=request.host.split(":")[0],
        manager_public_host=request.host.split(":")[0],
        current_user=session.get("username","-"),
        current_role=current_role(),
        security_count=len(site_security_findings(site)) if current_role() in {"admin","user"} else 0,
        pma_access=service_access_summary(site, "phpmyadmin"),
        sftp_access=service_access_summary(site, "sftp"),
        client_ip=detected_client_ip(),
        ufw=ufw_status(),
    )

@APP.post("/site/<site>/migrate")
@operator_required
def migrate_site_route(site):
    if not SITE_RE.match(site) or not (SITES / site).exists():
        abort(404)
    try:
        report = migration_engine.run_migration(site, request.form.get("old_url", "").strip())
        try:
            findings = scan_site_security(site)
            report["security_findings"] = len(findings)
            report.setdefault("steps", []).append({"name":"Security scan","ok":True,"detail":f"{len(findings)} finding(s)"})
            migration_engine.save_report(site, report)
        except Exception as sec_exc:
            report.setdefault("steps", []).append({"name":"Security scan","ok":False,"detail":str(sec_exc)})
            migration_engine.save_report(site, report)
        log_action("site_migration", site, report.get("status", "success"), f"{report.get('zip')} + {report.get('sql')}")
        if report.get("status") == "success":
            flash(f"{site}: migration completed and all primary validation checks passed.")
        else:
            flash(f"{site}: migration completed but validation needs attention. Review the Migration tab.")
    except Exception as exc:
        log_action("site_migration", site, "failed", str(exc))
        flash(f"{site}: migration failed: {exc}")
    return redirect(url_for("site_dashboard", site=site) + "#migration")

@APP.post("/site/<site>/migration-rollback")
@operator_required
def migration_rollback_route(site):
    if not SITE_RE.match(site) or not (SITES / site).exists():
        abort(404)
    try:
        used = migration_engine.rollback_migration(site)
        log_action("site_migration_rollback", site, "success", str(used))
        flash(f"{site}: migration rollback completed.")
    except Exception as exc:
        log_action("site_migration_rollback", site, "failed", str(exc))
        flash(f"{site}: rollback failed: {exc}")
    return redirect(url_for("site_dashboard", site=site) + "#migration")



@APP.post("/access/<site>/<service>/open/<int:minutes>")
@operator_required
def open_management_access_route(site, service, minutes):
    if service not in {"phpmyadmin", "sftp"}:
        abort(400)
    try:
        source_ip = request.form.get("source_ip", "").strip() or detected_client_ip()
        result = open_management_access(site, service, source_ip, minutes)
        flash(f"{site}: {service} firewall access opened from {result['source_ip']} on port {result['port']} for {minutes} minutes.")
        if service == "phpmyadmin" and request.form.get("launch") == "1":
            return redirect(f"http://{request.host.split(':')[0]}:{result['port']}")
    except Exception as exc:
        log_action("management_access_open", site, "failed", str(exc))
        flash(f"{site}: could not open {service} firewall access: {exc}")
    return redirect(request.referrer or url_for("site_dashboard", site=site))


@APP.post("/access/<site>/<service>/close")
@operator_required
def close_management_access_route(site, service):
    if service not in {"phpmyadmin", "sftp"}:
        abort(400)
    try:
        closed = close_management_access(site=site, service=service)
        flash(f"{site}: {service} firewall access closed ({len(closed)} session(s)).")
    except Exception as exc:
        flash(f"{site}: could not close {service} firewall access: {exc}")
    return redirect(request.referrer or url_for("site_dashboard", site=site))


@APP.post("/admin/access/<session_id>/close")
@admin_required
def close_management_access_session_route(session_id):
    try:
        closed = close_management_access(session_id=session_id)
        flash(f"Closed {len(closed)} management access session(s).")
    except Exception as exc:
        flash(f"Could not close management access session: {exc}")
    return redirect(url_for("port_manager"))


@APP.post("/admin/access/close-all")
@admin_required
def close_all_management_access_route():
    try:
        closed = close_management_access()
        flash(f"Closed all manager-owned UFW access ({len(closed)} session(s)).")
    except Exception as exc:
        flash(f"Could not close all management access: {exc}")
    return redirect(url_for("port_manager"))


def management_port_rows():
    rows = []
    SITES.mkdir(parents=True, exist_ok=True)
    for site_dir in sorted((p for p in SITES.iterdir() if p.is_dir()), key=lambda p: p.name):
        meta = site_metadata(site_dir)
        if not meta:
            continue
        site = meta.get("site", site_dir.name)
        for service, key, container_name, container_port in (
            ("SFTP", "sftp_port", sftp_container_name(site), "22/tcp"),
            ("phpMyAdmin", "phpmyadmin_port", phpmyadmin_container_name(site), "80/tcp"),
        ):
            configured = meta.get(key, "-") or "-"
            runtime = "-"
            container_state = "missing"
            try:
                c = docker_client.containers.get(container_name)
                c.reload()
                container_state = c.status
                runtime = _container_published_ports(c)
                runtime = next((str(p) for p, cp in runtime.items() if cp == container_port), "-")
            except Exception:
                pass

            reason = None
            if configured not in ("-", "", None):
                reason = port_conflict_reason(configured, service.lower(), site, container_name)
                if runtime not in ("-", "", None) and str(runtime) != str(configured):
                    reason = f"container is published on {runtime}, metadata says {configured}"
            status = "CONFLICT" if reason else ("RUNNING" if container_state == "running" else ("RESERVED" if configured != "-" else "UNASSIGNED"))
            rows.append({
                "site": site,
                "service": service,
                "service_id": "sftp" if service == "SFTP" else "phpmyadmin",
                "configured": configured,
                "runtime": runtime,
                "container": container_name,
                "container_state": container_state,
                "status": status,
                "reason": reason or "",
            })
    return rows


@APP.get("/admin/ports")
@admin_required
def port_manager():
    rows = management_port_rows()
    return render_template_string(r"""
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Port Manager · {{ platform_title }}</title>
<style>:root{color-scheme:dark;--bg:#07111f;--panel:#0f1b2d;--line:#263850;--text:#e7eef8;--muted:#91a3bb;--blue:#2563eb;--green:#059669;--red:#dc2626;--amber:#d97706}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,system-ui,Segoe UI,sans-serif}.wrap{max-width:1180px;margin:40px auto;padding:0 18px}.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:20px;margin-bottom:18px}.muted{color:var(--muted)}a{color:#82b6ff;text-decoration:none}.table{width:100%;border-collapse:collapse}.table th,.table td{text-align:left;padding:11px;border-bottom:1px solid var(--line);vertical-align:middle}.badge{display:inline-block;padding:4px 8px;border-radius:999px;font-size:12px;font-weight:800}.RUNNING{background:#064e3b}.RESERVED{background:#374151}.UNASSIGNED{background:#374151}.CONFLICT{background:#7f1d1d}.btn{border:0;border-radius:7px;padding:8px 12px;color:#fff;font-weight:800;cursor:pointer;background:var(--blue)}.flash{background:#162941;border:1px solid var(--line);padding:10px;border-radius:8px;margin-bottom:10px}.ranges{display:grid;grid-template-columns:1fr 1fr;gap:12px}.range{background:#091522;border:1px solid var(--line);border-radius:10px;padding:14px}@media(max-width:800px){.table{font-size:13px}.ranges{grid-template-columns:1fr}.table th:nth-child(5),.table td:nth-child(5){display:none}}</style></head>
<body><div class="wrap"><p><a href="/">← Dashboard</a> · <a href="/sites">Sites</a></p><h1>Management Port Manager</h1><p class="muted">Each site keeps its own reserved SFTP and phpMyAdmin host ports. Reservations are not reused by another site until that site is deleted.</p>
{% with messages=get_flashed_messages() %}{% for m in messages %}<div class="flash">{{m}}</div>{% endfor %}{% endwith %}
<div class="card"><div class="ranges"><div class="range"><b>SFTP range</b><br>{{ sftp_start }}–{{ sftp_end }}</div><div class="range"><b>phpMyAdmin range</b><br>{{ pma_start }}–{{ pma_end }}</div></div></div>
<div class="card"><div class="ranges"><div class="range"><b>UFW status</b><br><span class="badge {{ 'RUNNING' if ufw.active and ufw.default_deny else 'CONFLICT' }}">{{ 'ACTIVE · DEFAULT DENY' if ufw.active and ufw.default_deny else ('ACTIVE · POLICY CHECK' if ufw.active else 'NOT ACTIVE') }}</span></div><div class="range"><b>Active management access</b><br>{{ access_sessions|length }} session(s) <form method="post" action="/admin/access/close-all" style="display:inline;margin-left:10px"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><button class="btn" {% if not access_sessions %}disabled{% endif %}>Close All</button></form></div></div></div>
<div class="card"><h3>Temporary Firewall Access</h3><table class="table"><thead><tr><th>Site</th><th>Service</th><th>Port</th><th>Source IP</th><th>Opened By</th><th>Expires</th><th>Action</th></tr></thead><tbody>{% for a in access_sessions %}<tr><td><b>{{a.site}}</b></td><td>{{a.service}}</td><td>{{a.port}}</td><td>{{a.source_ip}}</td><td>{{a.opened_by}}</td><td>{{a.remaining}}<br><span class="muted">{{a.expires_display}}</span></td><td><form method="post" action="/admin/access/{{a.id}}/close"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><button class="btn">Close</button></form></td></tr>{% endfor %}{% if not access_sessions %}<tr><td colspan="7">No temporary management ports are open.</td></tr>{% endif %}</tbody></table></div>
<div class="card"><h3>Port Reservations</h3><table class="table"><thead><tr><th>Site</th><th>Service</th><th>Reserved Port</th><th>Runtime Port</th><th>Container</th><th>Status</th><th>Action</th></tr></thead><tbody>
{% for r in rows %}<tr><td><b>{{r.site}}</b></td><td>{{r.service}}</td><td>{{r.configured}}</td><td>{{r.runtime}}</td><td>{{r.container}}<br><span class="muted">{{r.container_state}}</span></td><td><span class="badge {{r.status}}">{{r.status}}</span>{% if r.reason %}<br><span class="muted">{{r.reason}}</span>{% endif %}</td><td><form method="post" action="/admin/ports/{{r.site}}/{{r.service_id}}/reassign" onsubmit="return confirm('Allocate a different port for {{r.site}} {{r.service}}?');"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><button class="btn">Reassign</button></form></td></tr>{% endfor %}
{% if not rows %}<tr><td colspan="7">No sites found.</td></tr>{% endif %}</tbody></table></div>
<p class="muted">Only TCP 80/443 need to be publicly forwarded for normal websites. Keep management ranges closed or restricted at the firewall/router unless remote administration is intentionally required.</p></div></body></html>
""", rows=rows, sftp_start=SFTP_PORT_START, sftp_end=SFTP_PORT_END, pma_start=PHPMYADMIN_PORT_START, pma_end=PHPMYADMIN_PORT_END, access_sessions=management_access_sessions(), ufw=ufw_status())


@APP.post("/admin/ports/<site>/<service>/reassign")
@admin_required
def reassign_management_port(site, service):
    if not SITE_RE.match(site) or not (SITES / site).exists():
        abort(404)
    if service not in {"sftp", "phpmyadmin"}:
        abort(400)

    site_dir = SITES / site
    meta = site_metadata(site_dir)
    key = "sftp_port" if service == "sftp" else "phpmyadmin_port"
    old = meta.get(key)
    container_name = sftp_container_name(site) if service == "sftp" else phpmyadmin_container_name(site)
    new_port = allocate_service_port(service, site=site, ignore_container=container_name, exclude_ports=[old] if old not in (None, "", "-") else [])

    close_management_access(site=site, service=service)
    if service == "phpmyadmin":
        result = ensure_phpmyadmin(site, requested_port=new_port)
        log_action("port_reassign", site, "success", f"phpmyadmin {old or '-'}->{result['port']}")
        flash(f"{site}: phpMyAdmin reassigned to port {result['port']}.")
    else:
        running = sftp_status(site).get("enabled", False)
        if running:
            result = enable_sftp(site, requested_port=new_port)
            log_action("port_reassign", site, "success", f"sftp {old or '-'}->{result['host_port']}")
            flash(f"{site}: SFTP reassigned to port {result['host_port']}. New password: {result['password']} — save it now; it is not stored.")
        else:
            meta["sftp_port"] = new_port
            (site_dir / "site.json").write_text(json.dumps(meta, indent=2))
            log_action("port_reassign", site, "success", f"sftp-reservation {old or '-'}->{new_port}")
            flash(f"{site}: SFTP port {new_port} reserved. It will be used next time SFTP is enabled.")
    return redirect(url_for("port_manager"))


@APP.route("/settings/branding", methods=["GET", "POST"])
@admin_required
def branding_settings():
    env_path = MANAGER / "manager.env"

    def write_env_values(values):
        lines = env_path.read_text().splitlines() if env_path.exists() else []
        out, seen = [], set()
        for line in lines:
            key = line.split("=", 1)[0] if "=" in line else ""
            if key in values:
                escaped = values[key].replace("\\", "\\\\").replace('"', '\\"')
                out.append(f'{key}="{escaped}"')
                seen.add(key)
            else:
                out.append(line)
        for key, value in values.items():
            if key not in seen:
                escaped = value.replace("\\", "\\\\").replace('"', '\\"')
                out.append(f'{key}="{escaped}"')
        env_path.write_text("\n".join(out) + "\n")

    if request.method == "POST":
        name = request.form.get("platform_name", "").strip() or "WP Host"
        title = request.form.get("platform_title", "").strip() or f"{name} WordPress Hosting"
        domain = request.form.get("manager_domain", "").strip().lower()
        domain = re.sub(r"^https?://", "", domain).strip("/")

        write_env_values({
            "PLATFORM_NAME": name,
            "PLATFORM_TITLE": title,
            "MANAGER_DOMAIN": domain,
        })
        log_action("branding_update", "platform", "success", f"{name} | {title} | {domain}")
        flash("Branding saved. The manager will restart to apply it.")
        subprocess.Popen(["systemctl", "restart", "wp-host-manager"])
        return redirect(url_for("branding_settings"))

    return render_template_string(r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Branding · {{ platform_title }}</title>
<style>
body{margin:0;background:#07111f;color:#e7eef8;font-family:Inter,system-ui,Segoe UI,sans-serif}
.wrap{max-width:820px;margin:44px auto;padding:0 18px}
.card{background:#0f1b2d;border:1px solid #263850;border-radius:14px;padding:24px}
h1{margin-top:0}.muted{color:#91a3bb}
label{display:block;font-weight:750;margin-top:18px}
input{width:100%;box-sizing:border-box;margin-top:6px;padding:11px;background:#091522;border:1px solid #263850;border-radius:8px;color:#fff}
.btn{display:inline-block;margin-top:22px;border:0;border-radius:8px;padding:10px 16px;background:#2563eb;color:#fff;font-weight:800;cursor:pointer;text-decoration:none}
.back{color:#82b6ff;text-decoration:none;display:inline-block;margin-bottom:14px}
.preview{margin-top:22px;background:#091522;border:1px solid #263850;border-radius:10px;padding:16px}
</style>
</head>
<body><div class="wrap">
<a class="back" href="/">← Dashboard</a>
<div class="card">
<h1>Platform Branding</h1>
<p class="muted">Controls the management interface only. Client WordPress site branding is not changed.</p>
<form method="post">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<label>Company / Platform Name
<input name="platform_name" value="{{ platform_name }}" placeholder="WP Host"></label>
<label>Dashboard Title
<input name="platform_title" value="{{ platform_title }}" placeholder="WP Host WordPress Hosting"></label>
<label>Management Domain
<input name="manager_domain" value="{{ manager_domain }}" placeholder="hosting.example.com"></label>
<button class="btn" type="submit">Save Branding</button>
</form>
<div class="preview">
<b>Preview</b><br>
{{ platform_title }}<br>
<span class="muted">{{ manager_domain or "No management domain configured" }}</span>
</div>
</div></div></body>
</html>
""")




@APP.get("/alerts")
@alerts_required
def alerts_page():
    return render_template_string(
        ALERTS_HTML,
        findings=active_security_findings(),
        sites=list_sites(),
        site_ops={s["site"]:get_site_ops(s["site"]) for s in list_sites()},
        current_user=session.get("username","-"),
        current_role=current_role(),
    )

@APP.post("/alerts/scan")
@alerts_required
def alerts_scan_now():
    results = security_scan_all()
    total = sum(len(v) for v in results.values())
    log_action("security_scan_manual", "all-sites", "success", f"findings={total}")
    flash(f"Security scan completed. {total} finding(s) detected in this scan.")
    return redirect(url_for("alerts_page"))

@APP.post("/alerts/baseline/<site>")
@alerts_required
def alerts_set_baseline(site):
    if not SITE_RE.match(site) or not (SITES / site).exists():
        abort(404)
    set_security_baseline(site, session.get("username","-"))
    # Run immediately; anything now trusted will clear if no longer considered suspicious.
    scan_site_security(site)
    log_action("security_baseline_set", site, "success", "")
    flash(f"{site}: trusted security baseline updated.")
    return redirect(url_for("alerts_page"))

def set_site_mode_runtime(site, mode):
    """Apply the requested operational mode to the running site."""
    site_dir = SITES / site
    if not site_dir.exists():
        raise RuntimeError("Site not found.")

    wp = docker_client.containers.get(f"{site}-wp")
    proxy_net = docker_client.networks.get(PROXY_NETWORK)

    if mode == "quarantine":
        try:
            proxy_net.disconnect(wp)
        except Exception:
            pass
        return

    # Live and maintenance both require proxy connectivity.
    try:
        wp.reload()
        nets = wp.attrs.get("NetworkSettings", {}).get("Networks", {})
        if PROXY_NETWORK not in nets:
            proxy_net.connect(wp)
    except Exception:
        try:
            proxy_net.connect(wp)
        except Exception:
            pass

    maintenance_file = site_dir / "wordpress" / ".maintenance-platform.html"
    htaccess = site_dir / "wordpress" / ".htaccess"
    marker_start = "# BEGIN WP-HOST-MAINTENANCE"
    marker_end = "# END WP-HOST-MAINTENANCE"

    if mode == "maintenance":
        maintenance_file.write_text("""<!doctype html><html><head><meta charset="utf-8"><title>Maintenance</title>
<style>body{font-family:system-ui;background:#07111f;color:#fff;display:grid;place-items:center;min-height:100vh;margin:0}
main{max-width:620px;padding:42px;background:#0f1b2d;border-radius:16px;text-align:center}p{color:#a7b4c7}</style></head>
<body><main><h1>Website Maintenance</h1><p>This website is temporarily unavailable while maintenance is being performed.</p></main></body></html>""")
        existing = htaccess.read_text() if htaccess.exists() else ""
        # Remove any old platform block first.
        pattern = re.compile(r"# BEGIN WP-HOST-MAINTENANCE.*?# END WP-HOST-MAINTENANCE\\n?", re.S)
        existing = pattern.sub("", existing)
        block = """# BEGIN WP-HOST-MAINTENANCE
RewriteEngine On
RewriteCond %{REQUEST_URI} !^/\\.maintenance-platform\\.html$
RewriteRule ^.*$ /.maintenance-platform.html [R=302,L]
# END WP-HOST-MAINTENANCE
"""
        htaccess.write_text(block + existing)
    else:
        if htaccess.exists():
            existing = htaccess.read_text()
            pattern = re.compile(r"# BEGIN WP-HOST-MAINTENANCE.*?# END WP-HOST-MAINTENANCE\\n?", re.S)
            htaccess.write_text(pattern.sub("", existing))
        maintenance_file.unlink(missing_ok=True)

def clone_site_to_staging(site):
    src_dir = SITES / site
    if not src_dir.exists():
        raise RuntimeError("Source site not found.")

    src_meta = site_metadata(src_dir)
    src_domain = src_meta.get("domain", "")
    if not src_domain:
        raise RuntimeError("Source site has no domain.")

    staging_site = f"{site}-staging"
    staging_domain = f"staging.{src_domain}"

    if (SITES / staging_site).exists():
        raise RuntimeError(f"{staging_site} already exists.")

    # Create fully separate WordPress + MariaDB stack first.
    password, meta = create_site(
        staging_site,
        staging_domain,
        src_meta.get("memory", "1g"),
        src_meta.get("cpus", "1.00"),
        src_meta.get("db_memory", "512m"),
        "admin",
        "staging@example.invalid",
        "",
        False,
    )

    staging_dir = SITES / staging_site

    # Preserve target wp-config.php because it points at the staging DB environment.
    target_config = staging_dir / "wordpress" / "wp-config.php"
    saved_config = target_config.read_text() if target_config.exists() else None

    # Copy full production filesystem over staging, except the staging DB config.
    run([
        "rsync","-a","--delete",
        "--exclude=wp-config.php",
        str(src_dir / "wordpress") + "/",
        str(staging_dir / "wordpress") + "/"
    ], timeout=1800)

    if saved_config is not None:
        target_config.write_text(saved_config)

    # Dump production DB.
    src_env = {}
    for line in (src_dir / ".env").read_text().splitlines():
        if "=" in line:
            k,v=line.split("=",1); src_env[k]=v

    dst_env = {}
    for line in (staging_dir / ".env").read_text().splitlines():
        if "=" in line:
            k,v=line.split("=",1); dst_env[k]=v

    dump_path = staging_dir / "source-production.sql"
    dump_cmd = (
        f"docker exec {site}-db mariadb-dump "
        f"-u{src_env['DB_USER']} -p'{src_env['DB_PASSWORD']}' "
        f"{src_env['DB_NAME']} > '{dump_path}'"
    )
    run(["bash","-lc",dump_cmd], timeout=1800)

    # Import into separate staging database.
    import_cmd = (
        f"cat '{dump_path}' | docker exec -i {staging_site}-db mariadb "
        f"-u{dst_env['DB_USER']} -p'{dst_env['DB_PASSWORD']}' {dst_env['DB_NAME']}"
    )
    run(["bash","-lc",import_cmd], timeout=1800)
    dump_path.unlink(missing_ok=True)

    # Rewrite WordPress URLs and discourage indexing.
    code = (
        "require '/var/www/html/wp-load.php';"
        f"update_option('siteurl','https://{staging_domain}');"
        f"update_option('home','https://{staging_domain}');"
        "update_option('blog_public','0');"
        "echo 'staging-updated';"
    )
    c = docker_client.containers.get(f"{staging_site}-wp")
    ex = c.exec_run(["php","-r",code])
    if ex.exit_code != 0:
        raise RuntimeError("Staging URL rewrite failed: " + ex.output.decode(errors="ignore")[-1000:])

    ensure_wordpress_proxy_https(staging_site)

    # Mark staging and keep it isolated from the public proxy network.
    with sqlite3.connect(PLATFORM_DB) as db:
        db.execute("INSERT OR IGNORE INTO site_operations(site) VALUES(?)",(staging_site,))
        db.execute(
            "UPDATE site_operations SET mode='maintenance',notes=?,owner_name=?,owner_email=?,owner_phone=? WHERE site=?",
            (
                f"Staging clone of {site}. Not publicly provisioned.",
                get_site_ops(site).get("owner_name",""),
                get_site_ops(site).get("owner_email",""),
                get_site_ops(site).get("owner_phone",""),
                staging_site,
            )
        )
        db.commit()

    # Do not publicly expose it yet.
    try:
        proxy_net = docker_client.networks.get(PROXY_NETWORK)
        swp = docker_client.containers.get(f"{staging_site}-wp")
        proxy_net.disconnect(swp)
    except Exception:
        pass

    # Create a clean security baseline for the new staging clone.
    try:
        set_security_baseline(staging_site, session.get("username","system"))
    except Exception:
        pass

    return staging_site, staging_domain

@APP.post("/site-ops/<site>")
@alerts_required
def site_ops_save(site):
    if not SITE_RE.match(site) or not (SITES/site).exists(): abort(404)
    old=get_site_ops(site)
    with sqlite3.connect(PLATFORM_DB) as db:
        db.execute("UPDATE site_operations SET notes=?,owner_name=?,owner_email=?,owner_phone=?,external_url=? WHERE site=?",(request.form.get("notes","").strip(),request.form.get("owner_name","").strip(),request.form.get("owner_email","").strip(),request.form.get("owner_phone","").strip(),request.form.get("external_url","").strip(),site));db.commit()
    log_action("site_details_update",site,"success",""); flash(f"{site}: details saved."); return redirect(request.referrer or url_for("site_dashboard", site=site))

@APP.post("/site-mode/<site>/<mode>")
@alerts_required
def site_mode(site,mode):
    if mode not in {"live","maintenance","quarantine"}:
        abort(400)
    if not SITE_RE.match(site) or not (SITES/site).exists():
        abort(404)

    get_site_ops(site)
    set_site_mode_runtime(site, mode)

    with sqlite3.connect(PLATFORM_DB) as db:
        db.execute("UPDATE site_operations SET mode=? WHERE site=?",(mode,site))
        db.commit()

    log_action("site_mode",site,"success",mode)
    flash(f"{site}: {mode} mode applied.")
    return redirect(request.referrer or url_for("index"))

@APP.post("/staging-clone/<site>")
@alerts_required
def staging_clone(site):
    if not SITE_RE.match(site) or not (SITES/site).exists():
        abort(404)
    try:
        staging_site, staging_domain = clone_site_to_staging(site)
        log_action("staging_clone",site,"success",f"{staging_site} / {staging_domain}")
        flash(
            f"{site}: staging clone created as {staging_site}. "
            f"Planned hostname: {staging_domain}. It remains isolated until you provision it."
        )
    except Exception as exc:
        log_action("staging_clone",site,"failed",str(exc))
        flash(f"{site}: staging clone failed: {exc}")
    return redirect(request.referrer or url_for("index"))

@APP.post("/change-password")
@operator_required
def change_password():
    username = session.get("username")
    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    users = load_users()
    entry = users.get(username)
    if not entry or not verify_password(entry["password_hash"], current_password):
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

BACKUP_DESTINATION_HTML = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Backup Destination · {{ platform_title }}</title>
<style>:root{color-scheme:dark;--bg:#07111f;--panel:#0f1b2d;--line:#263850;--text:#e7eef8;--muted:#91a3bb;--blue:#2563eb;--green:#059669}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,system-ui,Segoe UI,sans-serif}.wrap{max-width:900px;margin:42px auto;padding:0 18px}.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:20px;margin-bottom:18px}.muted{color:var(--muted)}a{color:#82b6ff;text-decoration:none}table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:9px;border-bottom:1px solid var(--line);text-align:left}.btn{border:0;border-radius:7px;padding:10px 14px;color:white;font-weight:800;cursor:pointer;background:var(--green);margin-top:16px}.tag{display:inline-block;padding:3px 7px;border-radius:6px;background:#17334b;font-size:11px}@media(max-width:700px){table{font-size:12px}}</style></head><body><div class="wrap"><p><a href="/">← Dashboard</a> · <a href="/users">Users</a></p><h1>Backup Destination</h1>
{% with messages = get_flashed_messages() %}{% for m in messages %}<div class="card">{{ m }}</div>{% endfor %}{% endwith %}
<section class="card"><h2>Local Storage Location</h2><p class="muted">Choose which mounted disk stores local backups — your primary array, a second RAID array once built, or any other mounted volume.</p><form method="post" action="/admin/backup-destination"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><table><tr><th></th><th>Mountpoint</th><th>Device</th><th>Filesystem</th><th>Free space</th></tr>{% for d in destinations %}<tr><td><input type="radio" name="mountpoint" value="{{ d.mountpoint }}" {{ 'checked' if d.mountpoint == current_mountpoint else '' }} required style="width:auto"></td><td>{{ d.mountpoint }}{% if d.mountpoint == current_mountpoint %} <span class="tag">current</span>{% endif %}</td><td>{{ d.device }}</td><td>{{ d.fstype }}</td><td>{{ d.free }}</td></tr>{% else %}<tr><td colspan="5">No eligible mounted disks detected.</td></tr>{% endfor %}</table>
<section class="card"><h2>Network NAS (secondary copy)</h2><p class="muted">Every local backup is copied here automatically once it's created. A NAS outage never blocks or breaks the local backup — it just skips replication until the NAS is back.</p>{% if nas.configured %}<p><b>Status:</b> {% if nas.mounted %}<span class="tag" style="background:#0d3b23;color:#4ade80">CONNECTED</span>{% else %}<span class="tag" style="background:#3b0d0d;color:#fb7185">DISCONNECTED</span>{% endif %} &nbsp; //{{ nas.server }}/{{ nas.share }} → {{ nas.mount_point }} &nbsp; free: {{ nas.free }}</p>{% endif %}<form method="post" action="/admin/backup-destination/nas/save"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><label>Name<input name="name" value="{{ nas.name or 'Network NAS' }}" maxlength="60"></label><label>NAS address<input name="server" value="{{ nas.server }}" placeholder="192.168.1.20" required></label><label>Share<input name="share" value="{{ nas.share }}" placeholder="WordPressBackups" required></label><label>Username<input name="username" value="{{ nas.username }}" autocomplete="off" required></label><label>Password<input name="password" type="password" placeholder="{{ 'Saved — leave blank to keep current password' if nas.password_saved else 'SMB password' }}" autocomplete="new-password"></label><label>Mount name<input name="mount_name" value="{{ nas.mount_name or 'nas' }}" pattern="[A-Za-z0-9_-]+" required></label><label>NAS retention (backup sets per site)<input name="retention" type="number" min="1" max="365" value="{{ nas.retention or 30 }}"></label><button class="btn" name="mode" value="save_mount">Save &amp; Mount</button> <button class="btn" name="mode" value="save" style="background:var(--blue)">Save Only</button></form>{% if nas.configured %}<div style="margin-top:12px;display:flex;gap:8px"><form method="post" action="/admin/backup-destination/nas/test"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="btn" style="background:var(--blue)">Test</button></form><form method="post" action="/admin/backup-destination/nas/mount"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="btn">Mount / Remount</button></form><form method="post" action="/admin/backup-destination/nas/unmount"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="btn" style="background:#d97706">Unmount</button></form></div>{% endif %}</section>
<label style="display:flex;align-items:center;gap:8px;margin-top:12px;font-size:13px"><input type="checkbox" name="migrate" style="width:auto"> Move existing backups from the current location to the new one</label><button class="btn">Save Destination</button></form></section>
</div></body></html>
"""

@APP.get("/admin/email-settings")
@admin_required
def email_settings_page_v911():
    data = load_email_settings()
    password_saved = bool(data.get("smtp_password"))
    view = dict(data); view["smtp_password"] = ""
    return render_template_string(EMAIL_SETTINGS_V911_HTML, e=view, password_saved=password_saved)

@APP.get("/admin/backup-destination")
@admin_required
def backup_destination_page():
    return render_template_string(
        BACKUP_DESTINATION_HTML,
        destinations=available_backup_destinations(),
        current_mountpoint=current_backup_mountpoint(),
        current_role=current_role(),
        nas=nas_status(),
    )

@APP.post("/admin/backup-destination/nas/save")
@admin_required
def backup_destination_nas_save():
    try:
        cfg = load_backup_storage()
        cfg.update({
            "type":"smb",
            "name":request.form.get("name","Network NAS").strip()[:60] or "Network NAS",
            "server":request.form.get("server","").strip(),
            "share":request.form.get("share","").strip().strip("/"),
            "username":request.form.get("username","").strip(),
            "mount_name":request.form.get("mount_name","nas").strip(),
            "retention":max(1,min(365,int(request.form.get("retention","30"))))
        })
        backup_mount_path(cfg)
        password = request.form.get("password","")
        old_cfg = load_backup_storage()
        if nas_is_mounted(old_cfg):
            try: unmount_nas(old_cfg)
            except Exception: pass
        write_nas_mount_unit(cfg, password=password or None)
        save_backup_storage(cfg)
        if request.form.get("mode") == "save_mount":
            mount_nas(cfg)
            flash("NAS configuration saved and mounted successfully.")
        else:
            flash("NAS configuration saved.")
        log_action("backup_nas_config", cfg.get("name","NAS"), "success", f"//{cfg['server']}/{cfg['share']} -> {backup_mount_path(cfg)}")
    except Exception as exc:
        log_action("backup_nas_config", "NAS", "failed", str(exc))
        flash(f"NAS configuration failed: {exc}")
    return redirect(url_for("backup_destination_page"))

@APP.post("/admin/backup-destination/nas/test")
@admin_required
def backup_destination_nas_test():
    cfg = load_backup_storage()
    try:
        if nas_is_mounted(cfg):
            flash("NAS is currently mounted and reachable.")
        else:
            flash("NAS is not currently mounted.")
    except Exception as exc:
        flash(f"NAS test failed: {exc}")
    return redirect(url_for("backup_destination_page"))

@APP.post("/admin/backup-destination/nas/mount")
@admin_required
def backup_destination_nas_mount():
    try:
        mount_nas()
        flash("NAS mounted successfully.")
        log_action("backup_nas_mount", "NAS", "success", "mounted")
    except Exception as exc:
        flash(f"Could not mount NAS: {exc}")
        log_action("backup_nas_mount", "NAS", "failed", str(exc))
    return redirect(url_for("backup_destination_page"))

@APP.post("/admin/backup-destination/nas/unmount")
@admin_required
def backup_destination_nas_unmount():
    try:
        unmount_nas()
        flash("NAS unmounted.")
        log_action("backup_nas_unmount", "NAS", "success", "unmounted")
    except Exception as exc:
        flash(f"Could not unmount NAS: {exc}")
        log_action("backup_nas_unmount", "NAS", "failed", str(exc))
    return redirect(url_for("backup_destination_page"))

@APP.post("/admin/backup-destination")
@admin_required
def backup_destination_save():
    try:
        new_mp = request.form.get("mountpoint", "").strip()
        migrate = request.form.get("migrate") == "on"
        valid_mounts = {d["mountpoint"] for d in available_backup_destinations()}
        if new_mp not in valid_mounts:
            raise ValueError("Selected location is not currently a mounted filesystem.")
        old_root = backup_root()
        save_backup_root_choice(new_mp)
        new_root = backup_root()
        new_root.mkdir(parents=True, exist_ok=True)
        if migrate and old_root.resolve() != new_root.resolve() and old_root.exists():
            moved = 0
            for item in old_root.iterdir():
                target = new_root / item.name
                if target.exists():
                    continue
                shutil.move(str(item), str(target))
                moved += 1
            flash(f"Backup destination updated to {new_mp}. Moved {moved} existing site backup folder(s).")
            log_action("backup_destination_migrate", "system", "success", f"{moved} moved {old_root} -> {new_root}")
        else:
            flash(f"Backup destination updated to {new_mp}.")
        log_action("backup_destination_config", "system", "success", f"local backup root -> {new_root}")
    except Exception as exc:
        log_action("backup_destination_config", "system", "failed", str(exc))
        flash(f"Could not update backup destination: {exc}")
    return redirect(url_for("backup_destination_page"))

@APP.post("/admin/email-settings")
@admin_required
def admin_email_settings():
    old = load_email_settings()
    password = request.form.get("smtp_password", "") or old.get("smtp_password", "")
    try: port = int(request.form.get("smtp_port", "587"))
    except ValueError: port = 587
    try: check_minutes = max(1, min(60, int(request.form.get("check_minutes", "5"))))
    except ValueError: check_minutes = 5
    try: failure_threshold = max(1, min(10, int(request.form.get("failure_threshold", "2"))))
    except ValueError: failure_threshold = 2
    data = {"enabled": request.form.get("enabled") == "on", "smtp_host": request.form.get("smtp_host", "").strip(), "smtp_port": port, "smtp_username": request.form.get("smtp_username", "").strip(), "smtp_password": password, "from_email": request.form.get("from_email", "").strip(), "recipients": request.form.get("recipients", "").strip(), "security": request.form.get("security", "starttls"), "send_recovery": request.form.get("send_recovery") == "on", "check_minutes": check_minutes, "failure_threshold": failure_threshold}
    if data["security"] not in {"starttls","ssl","none"}: data["security"] = "starttls"
    save_email_settings(data)
    log_action("email_settings_update", "system", "success", f"enabled={data['enabled']}; recipients={data['recipients']}")
    if request.form.get("mode") == "test":
        test_data = dict(data); test_data["enabled"] = True
        ok, detail = send_alert_email("[WP Host] Test email", "WordPress Host Manager email alerts are configured correctly.", test_data)
        log_action("email_test", "system", "success" if ok else "failed", detail)
        flash("Test email sent successfully." if ok else f"Test email failed: {detail}")
    else:
        flash("Email alert settings saved.")
    return redirect(url_for("email_settings_page_v911"))

@APP.post("/users/create")
@admin_required
def user_create():
    username=request.form.get("username","").strip(); email=request.form.get("email","").strip().lower(); role=request.form.get("role","user")
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,40}",username): flash("Username must be 3-40 characters using letters, numbers, dot, underscore or hyphen."); return redirect(url_for("users_page"))
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+",email): flash("Enter a valid email address."); return redirect(url_for("users_page"))
    if role not in {"admin","user","view"}: flash("Invalid role."); return redirect(url_for("users_page"))
    users=load_users()
    if username in users: flash("That username already exists."); return redirect(url_for("users_page"))
    if any(e.get("email","").lower()==email for e in users.values()): flash("That email address is already assigned to another user."); return redirect(url_for("users_page"))
    temp=generate_temporary_password(); users[username]={"password_hash":generate_password_hash(temp,method="scrypt"),"email":email,"enabled":True,"role":role,"force_password_change":True,"mfa_enabled":False,"mfa_required":True,"mfa_recovery_hashes":[],"created":datetime.now(timezone.utc).isoformat(),"created_by":session.get("username")}; save_users(users)
    ok,detail=send_account_email(email,f"{PLATFORM_TITLE} account created",f"Your account has been created.\n\nUsername: {username}\nTemporary password: {temp}\nLogin: {account_public_url('/login')}\n\nYou must change this temporary password at first login. After changing it, you will be required to configure multi-factor authentication before accessing the dashboard.")
    if not ok:
        users=load_users(); users.pop(username,None); save_users(users); log_action("user_create",username,"failed",f"email: {detail}"); flash(f"User was not created because the temporary-password email failed: {detail}"); return redirect(url_for("users_page"))
    log_auth(session.get("username"),f"user_create:{username}:{role}","success"); log_action("user_create",username,"success",f"role={role}; email={email}"); flash(f"User {username} created and temporary password emailed to {email}."); return redirect(url_for("users_page"))

@APP.post("/users/<username>/update")
@admin_required
def user_update(username):
    users=load_users(); entry=users.get(username)
    if not entry: flash("User not found."); return redirect(url_for("users_page"))
    email=request.form.get("email","").strip().lower(); role=request.form.get("role",entry.get("role","view"))
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+",email): flash("Invalid email address."); return redirect(url_for("users_page"))
    if any(u!=username and e.get("email","").lower()==email for u,e in users.items()): flash("That email is already in use."); return redirect(url_for("users_page"))
    if role not in {"admin","user","view"}: flash("Invalid role."); return redirect(url_for("users_page"))
    entry["email"]=email; entry["role"]=role; save_users(users); log_action("user_update",username,"success",f"role={role}; email={email}"); flash(f"{username} updated."); return redirect(url_for("users_page"))

@APP.post("/users/<username>/reset-password")
@admin_required
def admin_reset_user_password(username):
    users=load_users(); entry=users.get(username)
    if not entry: flash("User not found."); return redirect(url_for("users_page"))
    email=entry.get("email","").strip()
    if not email: flash("Set an email address before resetting this user's password."); return redirect(url_for("users_page"))
    temp=generate_temporary_password(); old_hash=entry.get("password_hash"); old_force=entry.get("force_password_change",False); entry["password_hash"]=generate_password_hash(temp,method="scrypt"); entry["force_password_change"]=True; entry.setdefault("mfa_required", not entry.get("mfa_enabled",False)); save_users(users)
    ok,detail=send_account_email(email,f"{PLATFORM_TITLE} temporary password",f"An administrator reset your password.\n\nUsername: {username}\nTemporary password: {temp}\nLogin: {account_public_url('/login')}\n\nYou must change this password at first login.")
    if not ok:
        users=load_users(); entry=users.get(username,{}); entry["password_hash"]=old_hash; entry["force_password_change"]=old_force; save_users(users); flash(f"Password was not changed because email delivery failed: {detail}"); return redirect(url_for("users_page"))
    consume_password_resets(username); log_action("user_password_reset",username,"success",email); flash(f"Temporary password emailed to {email}."); return redirect(url_for("users_page"))

@APP.post("/users/<username>/reset-mfa")
@admin_required
def admin_reset_user_mfa(username):
    users=load_users(); entry=users.get(username)
    if not entry: flash("User not found."); return redirect(url_for("users_page"))
    entry["mfa_enabled"]=False; entry["mfa_required"]=True; entry.pop("mfa_secret",None); entry["mfa_recovery_hashes"]=[]; save_users(users); log_action("user_mfa_reset",username,"success"); flash(f"MFA reset for {username}. They can enrol again after signing in."); return redirect(url_for("users_page"))

@APP.post("/users/<username>/toggle")
@admin_required
def user_toggle(username):
    users=load_users()
    if username==session.get("username"): flash("You cannot disable your own account."); return redirect(url_for("users_page"))
    if username not in users: flash("User not found."); return redirect(url_for("users_page"))
    users[username]["enabled"]=not users[username].get("enabled",True); save_users(users); state="enabled" if users[username]["enabled"] else "disabled"; log_action(f"user_{state}",username,"success"); flash(f"User {username} {state}."); return redirect(url_for("users_page"))

@APP.post("/users/<username>/delete")
@admin_required
def user_delete(username):
    users=load_users()
    if username==session.get("username"): flash("You cannot delete your own account."); return redirect(url_for("users_page"))
    if username not in users: flash("User not found."); return redirect(url_for("users_page"))
    del users[username]; save_users(users); consume_password_resets(username); log_action("user_delete",username,"success"); flash(f"User {username} deleted."); return redirect(url_for("users_page"))

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
        elif action == "phpmyadmin-enable":
            pma = ensure_phpmyadmin(site)
            log_action("site_phpmyadmin_enable", site, "success", f"port={pma['port']}")
            flash(f"{site}: phpMyAdmin available on port {pma['port']}.")
            return redirect(request.referrer or url_for("site_dashboard", site=site))
        elif action == "sftp-enable":
            creds = enable_sftp(site)
            log_action("site_sftp_enable", site, "success", f"port={creds['host_port']}")
            flash(
                f"{site}: SFTP enabled — port {creds['host_port']}, "
                f"username {creds['username']}, password {creds['password']}, "
                f"path {creds['path']}. Save the password now; it is not stored."
            )
            return redirect(request.referrer or url_for("site_dashboard", site=site))
        elif action == "sftp-reset-password":
            creds = reset_sftp_password(site)
            log_action("site_sftp_reset_password", site, "success", f"port={creds['host_port']}")
            flash(
                f"SFTP password reset for {site}. "
                f"Host: {request.host.split(':')[0]} | Port: {creds['host_port']} | "
                f"User: {creds['username']} | Password: {creds['password']} | "
                "SAVE THIS PASSWORD NOW - it will not be shown again."
            )
            return redirect(request.referrer or url_for("site_dashboard", site=site))
        elif action == "sftp-disable":
            close_management_access(site=site, service="sftp")
            disable_sftp(site)
            log_action("site_sftp_disable", site, "success", "")
            flash(f"{site}: SFTP disabled and its SFTP container removed.")
            return redirect(request.referrer or url_for("site_dashboard", site=site))
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
            close_management_access(site=site)
            remove_phpmyadmin(site)
            disable_sftp(site)
            release_management_port_reservations(site)
            run(["docker","compose","down"], cwd=site_dir)
            flash("Containers removed and management-port reservations released. Site directory and persistent database volume were retained.")
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
start_firewall_expiry_worker()

if os.environ.get("WP_SCHEDULER_STARTED") != "1":
    os.environ["WP_SCHEDULER_STARTED"] = "1"
    threading.Thread(target=scheduled_backup_all, daemon=True).start()
    threading.Thread(target=metrics_collector, daemon=True).start()
    threading.Thread(target=health_email_monitor, daemon=True).start()
    threading.Thread(target=security_scanner_loop, daemon=True).start()
    threading.Thread(target=operational_monitor_loop, daemon=True).start()

if __name__ == "__main__":
    APP.run(host="0.0.0.0", port=int(os.environ.get("WP_DASHBOARD_PORT","8088")))
