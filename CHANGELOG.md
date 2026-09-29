# Changelog

## 9.13.0
- Added a new "HTML / PHP" site type as a full parallel path alongside WordPress, selectable via a dropdown on the Create Site form. Uses php:8.3-apache as the web container, with an optional MariaDB database (a checkbox toggle) reusing the exact same setup WordPress sites already use, so phpMyAdmin and NPM/SSL provisioning work identically for both types.
- Fixed a real bug found while building this: `npm_create_proxy()` hardcoded the forward host as `{site}-wp`, which would have made the new site type provision successfully but never actually receive any traffic. Fixed with a backward-compatible optional parameter - the WordPress path is unaffected.
- Fixed two more latent bugs the same hardcoded assumption had spread to: `list_sites()` would have shown every HTML/PHP site as permanently "not-created", and `backup_site()` hardcoded the "wordpress" folder name for its archive, which would have silently produced backups missing the actual site files for the new type. `backup_site()` also now correctly skips the database dump for HTML/PHP sites created without one.
- Added visible labels to every field on the Create Site form. It never had any - relying entirely on placeholder text, which doesn't work on `<select>` dropdowns at all (they just show whatever's currently selected with no indication of what it represents). This went from a minor rough edge to a real usability problem once the site-type dropdown and database toggle were added alongside the existing memory/CPU/DB-memory dropdowns.
- Fixed a real JS syntax error in this same area: the site-type toggle script had literal backslash-quotes instead of plain quotes, silently breaking the toggle entirely - the WordPress-only admin fields never actually hid themselves when HTML/PHP was selected, despite the dropdown itself working. Confirmed working end-to-end by creating a real HTML/PHP site through the dashboard.
- Known remaining gap: `restore_latest()` still assumes a WordPress site's folder name and always attempts a database restore. Not yet safe for restoring an HTML/PHP site.

## 9.12.0
- Fixed a real bug in `ufw_status()` that permanently blocked the SFTP and phpMyAdmin temporary-access features on every box, regardless of how correctly UFW was actually configured: it called `ufw status` instead of `ufw status verbose`, but the "Default: deny (incoming)" line its own detection regex looks for only appears in verbose output. `default_deny` was always False, so `_ufw_require_ready()` always refused to open any port.
- `harden_firewall.sh` now runs automatically at the end of install.sh, right after the health check confirms the platform is up. Previously this was a separate, easy-to-forget manual step - and since the platform's own temporary-access features require UFW to be active and default-deny to function at all, skipping it silently broke those buttons on every fresh install. If hardening fails, the install still reports the core platform as successfully installed, with a clear warning to re-run it separately.

## 9.11.0
- Fixed install.sh crashing during its own preflight step: generated secrets (scrypt password hash, Flask secret, NPM password) are now single-quoted when written to manager.env, so bash's own `source` doesn't try to expand the `$` characters in the scrypt hash format as variable references.
- Fixed a real functional bug on the Backup Destination page: the Network NAS section's form was nested inside the Local Storage Location form (invalid HTML). Verified in a real browser that this left the "Save Destination" button completely orphaned (not part of any form - clicking it did nothing) and wired the NAS "Save & Mount"/"Save Only" buttons to the wrong endpoint. Both sections are now properly independent.
- Cleaned up the Network NAS form layout: added missing label/input CSS (this page had none at all) and arranged the fields into a responsive grid instead of running together on one line.
- Fixed the "current" mountpoint tag being unreadable in light theme (hardcoded dark background with no explicit text color).
- Fixed the "Create WordPress Site" panel on the Sites page staying visually stuck dark regardless of the selected theme (hardcoded inline background/border colors bypassing the theme system entirely).
- Fixed the same class of hardcoded, non-theme-aware color bug across the rest of the app, found via a systematic search rather than one screenshot at a time: the Sites page's site cards and stat boxes, the Site detail page's card background and "More" menu dropdown, Port Manager, and Branding Settings all now correctly follow the selected theme instead of staying stuck dark.
- Hardened install.sh against two real problems hit standing up a fresh box: `unzip` was missing from the OS dependency list entirely, and Docker's data-root relocation (previously always done manually, one time, outside the script) is now automated - along with relocating containerd's own separate data directory, which was never relocated anywhere before and was the direct cause of a production disk-full incident today. Both are now configured before either service starts for the first time on any future install.
- Retains all V9.10 Wordfence CVE integration, security audit findings, and host hardening guide/scripts, plus all earlier versioned functionality.

## 9.10.0
- Renamed the "Email Settings" page to "Settings" (nav labels updated on Dashboard and Users pages; underlying route unchanged).
- Added a Vulnerability Scanning section to Settings: a toggle to enable Wordfence vulnerability-feed matching, plus an optional API key (masked once saved, matching the SMTP/NAS password pattern).
- Wired the actual Wordfence feed integration: when enabled, downloads and caches the feed globally (once per 24h, not per-site), matches installed core/plugin/theme versions against known CVE ranges, and shows matched CVEs (with severity and fixed-in version) ahead of the plain "Outdated" label in the per-site Version & Vulnerability Scan section.
- Added a full security audit covering route authorization/CSRF, SQL injection/XSS surface, login rate-limiting, Docker isolation, host hardening, dependency CVEs, backup encryption, and audit log tamper-resistance.
- Added hardening/ directory: a guided, safety-ordered host hardening process (SSH key setup verification, firewall + fail2ban via harden_firewall.sh, and an explicitly-gated SSH password-auth-disable step via harden_ssh_keys_only.sh) addressing the gaps the audit found. Deliberately does not statically open the SFTP/phpMyAdmin port ranges, since the platform already manages those dynamically per-session via its own temporary-access feature.
- Retains all V9.9 post-migration vulnerability scanning/migration upload, V9.8 maintenance-mode admin bypass and action button toggles, V9.7 shared-theme-stylesheet, V9.6 theming/KPI-fix, V9.5 backup-destination/NAS, V9.4 temporary access, V9.3 port allocation and V9.2 authentication/MFA/SMTP functionality.

## 9.9.0
- Added automatic version/vulnerability scanning after every migration: installed WordPress core, plugin and theme versions are checked against the latest available on wordpress.org, with results shown in a dedicated section on the site page's Security tab (separate from the ongoing baseline-based findings). Correctly distinguishes outdated components, plugins closed on wordpress.org, and custom/premium components not hosted there.
- Added a web-based upload button for migration source files (.zip, .sql, .sql.gz) on the site page's Migration tab, since the SFTP jail is scoped to the wordpress/ folder and cannot reach the protected migration-source directory. Filenames are sanitised to prevent path traversal.
- Retains all V9.8 maintenance-mode admin bypass and site action button toggles, V9.7 shared-theme-stylesheet, V9.6 theming/KPI-fix, V9.5 backup-destination/NAS, V9.4 temporary access, V9.3 port allocation and V9.2 authentication/MFA/SMTP functionality.

## 9.8.0
- Fixed maintenance mode blocking wp-admin for everyone, including admins: the .htaccess rules now exclude /wp-admin, /wp-login.php, /wp-cron.php, and the /wp-includes and /wp-content asset directories wp-admin depends on, so visitors still see the maintenance page while admin access keeps working normally.
- Fixed the site page's "Maintenance", "Stop", and "Quarantine" buttons getting stuck on their original label with no way to reverse the action from the same panel. Each now toggles to its opposite action once applied: Maintenance -> Return Live, Stop Site -> Start Site, Quarantine -> Restore Access.
- Retains all V9.7 shared-theme-stylesheet, V9.6 theming/KPI-fix, V9.5 backup-destination/NAS, V9.4 temporary access, V9.3 port allocation and V9.2 authentication/MFA/SMTP functionality.

## 9.7.0
- Extracted the four colour themes (Midnight/Light/Forest/Sunset) into a single shared stylesheet (manager/static/theme.css) instead of duplicating the variable blocks inside every page's inline <style> tag.
- Extended the per-user colour theme to every authenticated page: Sites, Site detail, Alerts, Users, Account Security, Email Settings, and Backup Destination now all follow the selected theme, not just the Dashboard.
- Fixed hardcoded dark-background/white-text elements on these pages (search boxes, sidebar nav links, input/textarea fields) that previously stayed fixed regardless of the selected theme.
- Retains all V9.6 theming/KPI-fix, V9.5 backup-destination/NAS, V9.4 temporary access, V9.3 port allocation and V9.2 authentication/MFA/SMTP functionality.

## 9.6.0
- Added a per-disk usage table to the dashboard's System section, listing every mounted filesystem (device, mountpoint, filesystem type, used/total/free, percentage).
- Fixed KPI card spacing on the dashboard (label and description text were running together on one line).
- Added a per-user colour scheme selector (Midnight/Light/Forest/Sunset) next to the search box; the choice is stored on the user's own account and persists across devices/sessions.
- Fixed Light/Forest/Sunset themes not correctly recolouring the header bar, search box, and sidebar navigation links (these were hardcoded rather than tied to the theme's CSS variables).
- Security: bootstrap admin password is now hashed with scrypt (via Werkzeug) at install time instead of a single SHA-256 pass; existing installs continue to authenticate via the legacy verification path unchanged.
- Security: detected_client_ip() no longer trusts X-Forwarded-For from arbitrary peers, only from a configurable trusted-proxy allowlist (WP_TRUSTED_PROXY_IPS, default 127.0.0.1/::1) — closes a spoofing hole that could bypass the temporary phpMyAdmin/SFTP source-IP restriction.
- Security: ZIP extraction now rejects symlink-type archive entries as defense-in-depth against a "zip slip via symlink" pattern.
- Retains all V9.5 backup-destination/NAS, V9.4 temporary access, V9.3 port allocation and V9.2 authentication/MFA/SMTP functionality.

## 9.5.0
- Added a per-disk usage table to the dashboard's System section, listing every mounted filesystem (device, mountpoint, filesystem type, used/total/free, percentage).
- The dashboard's headline Storage metric now reports the platform's actual data disk rather than the OS root filesystem.
- Added a selectable local backup destination: admins can choose which mounted disk stores local backups (primary array, a second RAID array, or any other mounted volume), with automatic safe fallback if the chosen disk is ever unmounted, and an optional one-click migration of existing backups.
- Added NAS backup replication as a secondary destination (SMB/CIFS): every local backup is copied and verified against the NAS automatically once configured and mounted, with independent retention. A NAS outage never blocks or breaks the local backup.
- New admin page: /admin/backup-destination, covering both local destination and NAS configuration (Test / Mount / Unmount controls, persistent systemd mount unit, root-only stored credentials).
- Retains all V9.4 temporary access, V9.3 port allocation and V9.2 authentication/MFA/SMTP functionality.

## 9.4.0
- Added temporary per-site UFW management access for phpMyAdmin and SFTP.
- Temporary access is restricted to the technician source IP rather than opening the port globally.
- Added automatic 15, 30 and 60 minute expiry support; the site UI defaults to 30 minutes.
- Multiple phpMyAdmin/SFTP access sessions can be active simultaneously because every site retains its own management port.
- Added Close Now per session and Close All for manager-owned management access.
- Added persistent firewall-session state so expiry continues after manager restarts.
- Added UFW readiness checks: UFW must be active and use default-deny/reject incoming before temporary access can be created.
- The manager does not enable UFW or change the host's default firewall policy automatically.
- Added active-session, source-IP and expiry visibility to Admin → Port Manager.
- Fresh installs now install the `ufw` package but leave it disabled until the administrator configures the safe baseline.
- Retains all V9.3 collision-safe port allocation and V9.2 authentication/MFA/SMTP functionality.

## 9.3.0
- Added an admin Management Port Manager at `/admin/ports`.
- SFTP ports remain in the dedicated 21000-21999 range and phpMyAdmin ports in 22000-22999.
- Port allocation now checks Docker bindings, host listeners and every site's saved reservation before provisioning.
- Stored/reserved ports are never reused by another site until the owning site is deleted.
- Stale or conflicting saved ports are automatically replaced with the next safe free port when a service is enabled/repaired.
- Admins can explicitly reassign a site's SFTP or phpMyAdmin port from the Port Manager.
- Reassigning active SFTP rotates the SFTP password and displays the new password once.
- Existing clean-install, MFA, recovery, SMTP, migration, backup and security behaviour from 9.2.0 is retained.

# Changelog

## 9.2.0

Clean GitHub release built from the completed authentication implementation.

### Authentication
- Added email-based user onboarding with generated temporary passwords.
- Added mandatory first-login password change.
- Added mandatory first-login TOTP MFA for all dashboard roles.
- Added authenticator QR enrolment and one-time recovery codes.
- Added self-service account security and password management.
- Added Forgot Password with 30-minute single-use reset links.
- Added administrator password reset and MFA reset/re-enrolment controls.

### Email
- Added dashboard SMTP administration.
- Added SMTP test workflow.
- Unified account email and platform alert SMTP configuration.

### Distribution
- Converted to a clean-install GitHub release.
- Removed all historical fix/update/hotfix/rollback/patch-backup scripts.
- Added authentication preflight test suite.
- Removed organisation/server-specific branding and state.
- Installer refuses to overwrite an existing deployment.
