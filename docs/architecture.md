# Architecture

The platform uses a shared management layer and isolated per-site WordPress stacks.

```text
Internet
  |
Cloudflare (optional)
  |
Nginx Proxy Manager
  +-- WordPress site containers
  +-- Management application
```

Each site normally contains a WordPress container and a MariaDB container on an
internal Docker network. WordPress also joins the shared reverse-proxy network.

The repository deliberately keeps Docker Compose, Python application source,
templates, systemd configuration and helper scripts as normal files so changes
are easy to review through Git.
