# Operations scripts

One-off and maintenance scripts used on a WP Host server. Run them as root on the host (most default to check-only; read each script's header first).

| Script | What it does |
|---|---|
| `site-calm.py` | Crawler limits at the proxy (blocks junk bots, rate-limits heavy ones) and moves WP-Cron from page visits to a server cron job. |
| `local-hairpin.sh` | Lets the server reach its own sites by their public address locally (needed when the firewall no longer hairpins translated traffic). Set `PUBLIC_IP` and `LAN_IP`. |
| `php8-fix.sh` + `php8-fix.php` | Gets an old WordPress site running on PHP 8: backs up, updates free plugins, patches lines PHP 8 rejects. Keep both files together. |
| `add-www-existing.py` | Adds `www.` to existing sites' NPM proxy host and certificate (new sites get it from the dashboard). |
| `cap-workers-all.sh` | Gives every existing WordPress site an Apache worker cap sized to its memory limit (new sites get it automatically). |
| `rename-site.sh` | Finishes renaming a site's ID after its folder was moved: DB dump, volume copy, settings and dashboard history. |
| `shared-ip-guard.py` | **Retired.** Kept Wordfence per-address blocking off while every visitor arrived from one NAT address. Not needed since the firewall fix; kept for reference only. |
