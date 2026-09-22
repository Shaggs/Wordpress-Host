# Changelog

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
