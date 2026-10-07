# Changelog

## 9.18.0
- Added background tasks with live progress. Long operations no longer hold the page: creating a site (WordPress, HTML/PHP, Payload), migration and rollback, backup, restore (latest or a specific backup), update, start/stop/restart, delete, HTTPS setup, installing login reporting, staging clone, NAS mount, security scan and setting a baseline now run in the background. A progress card (bottom-right of every page) shows the current step, a progress bar (animated when the duration is unknown) and elapsed time; it follows you between pages, and the page refreshes itself when the task finishes. The original route handlers run unchanged (same form data, same signed-in user, so the audit log still names the right person) and their success/failure messages - including one-time secrets such as a new site's admin password - are handed back to the user's own session on completion and wiped from the task store. The security scan shows a real percentage; Payload builds are tracked end to end. One operation per site at a time.
- Added resource guards so heavy work can't starve or crash live sites: at most 2 heavy tasks run at once (the rest show as Queued); each heavy task only starts when free memory (estimate plus a 1.5 GB reserve) and free disk (estimate plus a 5 GB reserve, disk under 92% full) allow - disk shortfalls fail immediately with the numbers, memory shortfalls wait up to 3 minutes. All tunable via WP_BG_* settings; bad values fall back to defaults.
- The Payload build container is now capped (memory, swap and CPU; default 30% of host RAM between 2 and 4 GB) so a runaway build can only kill itself; an out-of-memory build now says so plainly. Existing Payload sites get the cap too.
- Backups (manual and the nightly job) now check the destination has room for the backup plus a 2 GB reserve before writing, and nightly backup failures are logged and emailed instead of being silently swallowed (previously `except Exception: pass`).
- Fixed a missing `jsonify` import: the WordPress admin login-reporting callback (9.14.0) crashed with a NameError on every call - it recorded the login first, so it appeared to work - and its error replies (bad site / unknown site / bad secret) crashed instead of returning 400/404/403. The new /api/tasks endpoint needed the same import.
- Known issue found, not yet fixed: /settings/branding refers to an undefined MANAGER variable (present in the original code).
- Process: patches are now also checked for undefined names, which py_compile cannot catch (that is how the missing import slipped through).
- Not verified against a real out-of-memory event or real Docker memory caps (unit tests only). Background tasks and progress cards confirmed working on a live box.

## 9.17.0
- Added Payload CMS as a third site type (Next.js-based, Payload 3.x) with its own MongoDB container. A fresh blank Payload starter is scaffolded automatically; creation returns immediately and a background job pulls images, installs, builds, starts the app, provisions proxy/SSL (port 3000) and registers the first admin. Status shows BUILDING, then RUNNING, or BUILD-FAILED with an alert email.
- Payload's first admin is registered by the platform itself (Admin Email/Password from the form) via Payload's first-register endpoint, closing the window where whoever opened /admin first would become the administrator.
- Payload backups archive the project without node_modules/.next plus a MongoDB dump; restore puts both back and rebuilds in the background. New Rebuild & Restart button in the More menu. phpMyAdmin and Maintenance mode are not available for Payload sites and now say so clearly.
- Restore is now site-type aware (HTML/PHP webroot and service names, database only when the site has one, newest complete backup set rather than independently newest archive and dump).
- Wordfence vulnerability feed matching is now enabled by default (the API key remains optional).
- Fixed shared bugs found along the way: list_sites() never exposed the site type to templates (so earlier WordPress-only menu rules never worked); health, uptime and quarantine hard-coded the -wp container; HTML/PHP sites without a database were always reported UNHEALTHY; SFTP always mounted the wordpress folder (wrong for HTML/PHP); the Create form made the optional admin password required after switching site types.
- Payload sites are not covered by the WordPress plugin/vulnerability scanner (as with HTML/PHP sites).
- Not yet verified end-to-end with live containers (MongoDB, proxy/SSL, SFTP, restore) - test on a non-production box before relying on it.

## 9.16.0
- Added a staff-approved backup cleanup suggestion: when host disk usage crosses 80%, the platform identifies the site whose backups are using the most disk space (measured by total size, not file count, since that's what actually frees space) and raises an alert naming it. A "Remove Oldest Backup" button lets staff trigger cleanup themselves, one backup set at a time - nothing is deleted automatically, matching how two servers recently hit 100% disk usage with no earlier warning.
- This reuses the existing Alerts page "Dismiss for 24h" snooze mechanism - the same auto-resolve-if-it-clears-on-its-own behavior applies here too.

## 9.15.0
- Added the ability to restore a site from a specific backup, not just the most recent one - a "Restore" button now appears on each row of the Backup History table. The existing quick "restore latest" action is unchanged. The backup identifier is validated against the exact timestamp format our own backups use before being used to build a file path (tested against path-traversal and injection attempts).
- Stopped exposing the WordPress admin password in plain text after site creation. It's now flashed under its own category and rendered as a masked input with Show/Hide and Copy buttons, shown once. The Copy button reads the password back from the input's own DOM value rather than re-embedding it in a JS string literal, which would otherwise be a real bug (and a minor XSS risk) for a password containing a quote or backslash.
- Changed what "Delete" actually does to a site. It previously only stopped and removed the running containers - the site's directory and database volume were both explicitly retained, and backups were untouched. It now runs `docker compose down -v`, deletes the site's directory entirely, and clears its backup folder down to just the single most recent backup set. The confirmation and description text were updated to match - the old copy said nothing was destroyed, which would have been badly misleading left as-is.
- Replaced the plain browser confirm() dialog on site deletion with a modal requiring the word "delete" to be typed before the Confirm Delete button becomes clickable.
- Fixed a real, pre-existing gap: every other action in the site action dispatcher (backup, restore, sftp-enable, etc.) explicitly logs itself to the audit trail before its own early return - "delete" never did, so site deletions were not being recorded at all. It now logs the acting username explicitly, and the flash message names them too.
- Process note: caught and fixed a real bug in an earlier patch this cycle where a new route's decorator was inserted between an existing route's decorator and the function it wrapped, silently reattaching that decorator to the wrong function - Flask refused to start at all (duplicate endpoint), but even if it hadn't, the original route would have stopped being registered entirely. py_compile could not have caught this, since the broken code was syntactically valid; an AST-based check confirming no two route decorators resolve to the same function name is now used for any patch that inserts code near an existing route.

## 9.14.0
- Added a "Dismiss for 24h" button to each finding on the Alerts page, implemented as a snooze rather than a permanent dismiss - a naive "mark inactive" approach would have been immediately undone, since record_security_finding() unconditionally re-activates a finding every time it's re-detected (e.g. the next periodic disk-pressure check). If the underlying issue clears during the snooze window, the existing auto-resolve logic (unchanged) marks it genuinely inactive regardless of the timer, and it stays gone permanently; if it's still a real problem after 24 hours, the snooze expires and it reappears.
- Added a Login Logs section to the Settings page, showing every dashboard sign-in attempt (successful or not) with timestamp, username, event, result, and IP. The underlying data was already being captured via log_auth() - it just was never actually displayed anywhere.
- Added WordPress admin login tracking: a small must-use plugin (auto-activated, no manual steps) reports administrator-role logins back to the platform, authenticated by a per-site shared secret. New sites get this automatically; existing sites get an "Install / Reinstall" button under the site's "More" menu (WordPress sites only).
- Added IP-to-country geolocation for both platform dashboard logins and WordPress admin logins (ip-api.com, no key required). Private/loopback addresses show as "LAN" with no network call; any lookup failure degrades to "-" rather than risking a slow or broken login.
- Fixed a real bug that silently broke the WordPress admin login tracking entirely: the new callback endpoint is authenticated by its own per-site secret header (appropriate for a non-browser, server-to-server call), but Flask-WTF's CSRFProtect(APP) applies globally to every POST route with zero exemptions. WordPress's server-side HTTP call has no session/cookie and therefore no way to supply a CSRF token, so every callback was being rejected with "400: CSRF token is missing" before the route's own secret-validation logic ever ran. Confirmed via a live-instrumented test against a real site, and verified the fix doesn't weaken CSRF protection anywhere else in the app.

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
