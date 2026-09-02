#!/usr/bin/env python3
import importlib.util
import os
import sys
from pathlib import Path

MANAGER = Path(os.environ.get('WP_PREFLIGHT_MANAGER', '/opt/wp-host/manager'))
CANDIDATE = Path(os.environ.get('WP_PREFLIGHT_APP', str(MANAGER / 'app.py')))

if not CANDIDATE.exists():
    raise SystemExit(f'Candidate not found: {CANDIDATE}')

os.environ['WP_SCHEDULER_STARTED'] = '1'
sys.path.insert(0, str(MANAGER))

spec = importlib.util.spec_from_file_location('app_candidate', CANDIDATE)
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

# Prevent test POSTs from requiring CSRF tokens and isolate all user mutations.
app.APP.config['TESTING'] = True
app.APP.config['WTF_CSRF_ENABLED'] = False

print('Imported candidate: PASS')

required_templates = [
    'LOGIN_HTML','FORGOT_PASSWORD_HTML','RESET_PASSWORD_HTML','FORCE_PASSWORD_HTML',
    'MFA_VERIFY_HTML','MFA_SETUP_HTML','RECOVERY_CODES_HTML','SECURITY_HTML',
    'USERS_PAGE_HTML','EMAIL_SETTINGS_V911_HTML'
]
for name in required_templates:
    if not getattr(app, name, None):
        raise AssertionError(f'Missing template: {name}')
print('Auth templates present: PASS')

routes = {}
for rule in app.APP.url_map.iter_rules():
    routes.setdefault(rule.rule, set()).update(rule.methods)
required_routes = {
    '/login': {'GET','POST'},
    '/forgot-password': {'GET','POST'},
    '/reset-password/<token>': {'GET','POST'},
    '/account/change-password': {'GET','POST'},
    '/account/security': {'GET'},
    '/account/mfa-disable': {'POST'},
    '/mfa/setup': {'GET','POST'},
    '/mfa/verify': {'GET','POST'},
    '/users': {'GET'},
    '/users/create': {'POST'},
    '/users/<username>/reset-password': {'POST'},
    '/users/<username>/reset-mfa': {'POST'},
    '/admin/email-settings': {'GET','POST'},
}
for route, methods in required_routes.items():
    have = routes.get(route, set())
    if not methods <= have:
        raise AssertionError(f'{route}: missing methods {methods-have}')
print('Auth routes: PASS')

# Synthetic isolated users.
admin_hash = app.generate_password_hash('AdminPass!12345', method='scrypt')
temp_hash = app.generate_password_hash('TempPass!12345', method='scrypt')
users = {
    'AdminTest': {
        'password_hash': admin_hash, 'email':'admin@example.test', 'enabled':True,
        'role':'admin','force_password_change':False,'mfa_enabled':False,'mfa_required':False,
        'mfa_recovery_hashes':[],'created':'test'
    },
    'NewUser': {
        'password_hash': temp_hash, 'email':'user@example.test', 'enabled':True,
        'role':'user','force_password_change':True,'mfa_enabled':False,'mfa_required':True,
        'mfa_recovery_hashes':[],'created':'test'
    },
    'Viewer': {
        'password_hash': app.generate_password_hash('ViewPass!12345', method='scrypt'),
        'email':'view@example.test','enabled':True,'role':'view','force_password_change':False,
        'mfa_enabled':False,'mfa_required':False,'mfa_recovery_hashes':[],'created':'test'
    }
}

def load_users():
    return users

def save_users(new_users):
    # The application commonly calls save_users(users) with the same dict object.
    # Take a detached copy before replacing our in-memory synthetic store.
    snapshot = {username: dict(entry) for username, entry in new_users.items()}
    users.clear()
    users.update(snapshot)

app.load_users = load_users
app.save_users = save_users
app.log_auth = lambda *a, **k: None
app.log_action = lambda *a, **k: None
app.login_locked = lambda *a, **k: False
app.send_account_email = lambda *a, **k: (True, 'Message sent (preflight mock)')
app.load_email_settings = lambda: {
    'enabled':True,'smtp_host':'smtp.example.test','smtp_port':587,'smtp_username':'test',
    'smtp_password':'secret','from_email':'host@example.test','recipients':'admin@example.test',
    'security':'starttls','send_recovery':True,'check_minutes':5,'failure_threshold':2
}
app.save_email_settings = lambda data: None
app.send_alert_email = lambda *a, **k: (True, 'Message sent (preflight mock)')

client = app.APP.test_client()

def set_session(username, force=None):
    entry = users[username]
    with client.session_transaction() as sess:
        sess.clear()
        sess['authenticated'] = True
        sess['username'] = username
        sess['role'] = entry['role']
        sess['force_password_change'] = entry.get('force_password_change', False) if force is None else force

# Anonymous/public render checks.
assert client.get('/login').status_code == 200
assert client.get('/forgot-password').status_code == 200
print('Login + Forgot Password render: PASS')

# Admin pages render.
set_session('AdminTest', False)
assert client.get('/users').status_code == 200
assert client.get('/admin/email-settings').status_code == 200
assert client.get('/account/security').status_code == 200
assert client.get('/account/change-password').status_code == 200
print('Admin Users + Email + Security + Password pages: PASS')

# Permission check.
set_session('Viewer', False)
assert client.get('/users').status_code == 403
assert client.get('/account/security').status_code == 200
print('View-only permissions + self-service security: PASS')

# First-login gate: authenticated temp user cannot reach dashboard.
set_session('NewUser', True)
r = client.get('/', follow_redirects=False)
assert r.status_code in (301,302,303,307,308)
assert '/account/change-password' in r.headers.get('Location','')
print('First login -> forced password change: PASS')

# Forced password change -> MFA setup.
r = client.post('/account/change-password', data={
    'password':'NewStrong!12345', 'confirm':'NewStrong!12345'
}, follow_redirects=False)
assert r.status_code in (301,302,303,307,308)
assert '/mfa/setup' in r.headers.get('Location','')
assert users['NewUser']['force_password_change'] is False
print('Forced password change -> mandatory MFA: PASS')

# MFA setup page and QR.
r = client.get('/mfa/setup')
assert r.status_code == 200
body = r.get_data(as_text=True)
assert 'data:image/png;base64,' in body
with client.session_transaction() as sess:
    secret = sess.get('pending_mfa_secret')
assert secret
print('MFA setup + QR generation: PASS')

# Complete MFA enrolment using real TOTP.
code = app.pyotp.TOTP(secret).now()
r = client.post('/mfa/setup', data={'code':code}, follow_redirects=False)
assert r.status_code == 200
body = r.get_data(as_text=True)
assert 'MFA Enabled' in body and 'recovery' in body.lower()
assert users['NewUser']['mfa_enabled'] is True
assert users['NewUser']['mfa_required'] is False
assert len(users['NewUser']['mfa_recovery_hashes']) == 10
print('MFA enrolment + recovery codes: PASS')

# Future login: password -> MFA verify -> dashboard.
client.get('/logout')
r = client.post('/login', data={'username':'NewUser','password':'NewStrong!12345'}, follow_redirects=False)
assert '/mfa/verify' in r.headers.get('Location','')
code = app.pyotp.TOTP(users['NewUser']['mfa_secret']).now()
r = client.post('/mfa/verify', data={'code':code}, follow_redirects=False)
assert r.status_code in (301,302,303,307,308)
assert r.headers.get('Location','').endswith('/')
print('Future login password + MFA -> dashboard: PASS')

# Self-service password change requires current password when not forced.
set_session('NewUser', False)
r = client.post('/account/change-password', data={
    'current_password':'WRONG','password':'Another!12345','confirm':'Another!12345'
})
assert r.status_code == 200
assert app.verify_password(users['NewUser']['password_hash'], 'NewStrong!12345')
r = client.post('/account/change-password', data={
    'current_password':'NewStrong!12345','password':'Another!12345','confirm':'Another!12345'
}, follow_redirects=False)
assert r.status_code in (301,302,303,307,308)
assert app.verify_password(users['NewUser']['password_hash'], 'Another!12345')
print('Self-service password change: PASS')

# User creation path requires no admin-selected password and sends invite via mocked account mail.
set_session('AdminTest', False)
r = client.post('/users/create', data={'username':'CreatedUser','email':'created@example.test','role':'user'}, follow_redirects=False)
assert 'CreatedUser' in users
assert users['CreatedUser']['force_password_change'] is True
assert users['CreatedUser']['mfa_required'] is True
assert users['CreatedUser']['mfa_enabled'] is False
print('Create user temp-password + first-login flags: PASS')

# Admin MFA reset forces re-enrolment.
users['NewUser']['mfa_enabled'] = True
users['NewUser']['mfa_required'] = False
users['NewUser']['mfa_secret'] = app.pyotp.random_base32()
set_session('AdminTest', False)
r = client.post('/users/NewUser/reset-mfa', follow_redirects=False)
assert users['NewUser']['mfa_enabled'] is False
assert users['NewUser']['mfa_required'] is True
assert 'mfa_secret' not in users['NewUser']
print('Admin MFA reset -> mandatory re-enrolment: PASS')

print('\n============================================================')
print('V9.2.0 AUTH PREFLIGHT: ALL TESTS PASSED')
print('============================================================')
