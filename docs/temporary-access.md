# Temporary Management Access

V9.4 can temporarily expose a site's phpMyAdmin or SFTP service through Ubuntu UFW. The feature is designed for environments where the upstream router/cloud firewall permits the management ranges to reach the host, while UFW remains closed by default.

## Port ranges

- SFTP: `21000-21999`
- phpMyAdmin: `22000-22999`

Every site owns an independent reserved port. Multiple sites may therefore have active management sessions at the same time.

## Security model

The platform does **not** enable UFW or change its default policy. Before using temporary access, the administrator must safely configure the host firewall. UFW must be active and incoming traffic must default to deny/reject. Ensure the actual SSH port and TCP 80/443 are allowed before enabling UFW remotely.

When a technician opens management access, the manager creates an exact UFW rule restricted to the detected source IP and the site's assigned service port. For example:

```text
source 203.0.113.10 -> TCP 22001 only
```

The rule is tagged with a `wp-host:<site>:<service>` comment. Manager-created sessions are stored in `/opt/wp-host/firewall-sessions.json`.

## Session behaviour

- Site UI defaults to 30-minute access.
- Backend supports 15, 30 and 60 minute access.
- Sessions expire independently.
- Multiple sites/services can be open simultaneously.
- `Close Now` removes one session.
- `Close All` removes all manager-owned management sessions.
- Expired rules are cleaned by the manager background worker.
- Sessions persist across manager restarts.

## Port Manager

Open **Admin -> Port Manager** (`/admin/ports`) to view reserved/runtime ports, conflicts, UFW state and active temporary management sessions.

## Recommended baseline

Review the SSH rule first, then a typical standard-SSH baseline is:

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw enable
sudo ufw status verbose
```

Do not add a permanent UFW allow for `21000-22999`; doing so bypasses the temporary-access security model.
