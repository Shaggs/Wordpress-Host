# WordPress Hosting Platform

A vendor-neutral, Docker-based WordPress hosting and management platform for
running multiple isolated WordPress sites on a single Linux host.

**Current release: 9.4.0**

This repository is a **clean-source release**. It contains the current
application, Docker definitions, installer, service definition, operational
commands, tests and documentation. It does not contain historical patch/update
scripts or server-specific data.

## Features

- Isolated WordPress + MariaDB Docker stacks per site
- Nginx Proxy Manager reverse proxy and TLS integration
- Web management dashboard
- Admin, User/Operator and View-only roles
- User creation by email with generated temporary passwords
- Forced first-login password change
- Mandatory first-login TOTP MFA
- Google Authenticator / Microsoft Authenticator compatible MFA
- One-time recovery codes
- Forgot Password with 30-minute single-use reset links
- Dashboard-managed SMTP configuration and test email
- Host CPU, RAM, storage and Docker monitoring
- Site health and security monitoring
- Backup and restore workflows
- Per-site SFTP
- Per-site phpMyAdmin
- Management Port Manager with collision-safe SFTP/phpMyAdmin reservations
- Temporary UFW management-access sessions restricted to the technician source IP
- Automatic 15/30/60-minute firewall expiry with Close Now / Close All controls
- Maintenance and quarantine modes
- Staging clone workflow
- WordPress ZIP + SQL migration engine
- HTTPS/reverse-proxy normalisation
- Migration validation and rollback
- Configurable platform branding

## Clean install

Supported target: Ubuntu/Debian with `apt`.

```bash
git clone <your-repository-url>
cd wordpress-hosting-platform
chmod +x install.sh
sudo ./install.sh
```

The installer asks for the initial administrator, admin email, password,
management domain and platform branding.

The initial administrator must enrol MFA on first login.

> `install.sh` intentionally refuses to overwrite an existing installation.
> This repository is not an in-place patch system.

## Repository layout

```text
.
├── install.sh
├── VERSION
├── manager/
│   ├── app.py
│   ├── migration_engine.py
│   ├── requirements.txt
│   ├── site-compose.yml.tpl
│   ├── templates/
│   └── static/
├── docker/
│   ├── npm/
│   └── site-template/
├── systemd/
│   └── wp-host-manager.service
├── scripts/
│   ├── wordpress-backup
│   ├── wordpress-list
│   ├── wordpress-restart
│   └── wordpress-update
├── tests/
│   └── auth_preflight.py
├── config/
├── docs/
├── LICENSE
└── CHANGELOG.md
```

## Authentication

New account workflow:

```text
Admin creates account
        ↓
Temporary password emailed
        ↓
Mandatory password change
        ↓
Mandatory MFA enrolment
        ↓
Recovery codes
        ↓
Dashboard
```

Normal subsequent login:

```text
Password → MFA code → Dashboard
```

See [docs/authentication.md](docs/authentication.md).

## SMTP

After installation, open **Admin → Email Settings** and configure SMTP before
creating additional dashboard users.

SMTP is used for user onboarding, Forgot Password, password resets and platform
alerting.

## WordPress migration

Upload a complete WordPress ZIP and SQL dump into the site's migration source
folder and use the Migration tab.

See [docs/migration.md](docs/migration.md).

## Patching / upgrades

Historical `fix-*`, `update-*`, hotfix and patch-backup scripts are intentionally
excluded from this repository.

See [docs/clean-install.md](docs/clean-install.md).

## Security

Before exposing the service publicly, review:

- cloud/firewall rules;
- HTTPS configuration;
- administrator MFA;
- SMTP sender configuration;
- backup retention;
- Nginx Proxy Manager security;
- access to phpMyAdmin and SFTP.

See [docs/security.md](docs/security.md).

## License

MIT License.

Copyright © 2026 Shane Rees.

Third-party components retain their own licences.

## Management Port Manager and Temporary Access (9.4)

Each site receives independent SFTP (`21000-21999`) and phpMyAdmin (`22000-22999`) host ports. The allocator checks Docker, host listeners and saved site reservations to prevent collisions. Admins can view or reassign ports at `/admin/ports`.

V9.4 adds optional temporary UFW access sessions. The manager never enables UFW or changes the default policy itself. Once the administrator has safely configured UFW with SSH/80/443 allowed and default-deny incoming, a technician can temporarily expose a site's SFTP or phpMyAdmin port to their source IP for 15, 30 or 60 minutes. Multiple sites can be open simultaneously and each rule expires independently. See `docs/temporary-access.md`.
