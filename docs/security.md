# Security Notes

- Publish the management interface through HTTPS.
- Restrict Nginx Proxy Manager administration on TCP 81.
- Do not expose MariaDB TCP 3306 publicly.
- Restrict SFTP ports and disable SFTP when it is not needed.
- Avoid persistent direct public exposure of phpMyAdmin.
- Store generated secrets outside Git.
- Keep Ubuntu, Docker images, WordPress core, plugins and themes maintained.
- Test backups and restores.


## Temporary UFW Management Access

V9.4 can create source-IP-restricted, temporary UFW rules for per-site SFTP and phpMyAdmin ports. UFW must already be active with a default-deny/reject incoming policy. The platform deliberately does not enable UFW automatically because remote firewall changes can lock out SSH administrators. See `temporary-access.md`.
