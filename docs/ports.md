# Management ports

Each site owns independent host-side management ports.

- SFTP: TCP 21000-21999
- phpMyAdmin: TCP 22000-22999
- Public WordPress traffic: TCP 80/443 through Nginx Proxy Manager

The manager reserves a site's assigned management ports in `site.json`. Other sites will not reuse those reservations, even when SFTP is temporarily disabled. Reservations are released when the platform delete action removes the site containers; the retained site files/database can later be brought back with a newly allocated management port.

The allocator checks saved site reservations, Docker port mappings (including stopped containers), and non-Docker host listeners before choosing a port.

Admins can inspect and reassign allocations from **Admin → Port Manager** (`/admin/ports`). Reassigning an active SFTP service recreates its SFTP container and rotates the SFTP password.


## V9.4 temporary access

Reserved ports remain assigned to each site even while closed. UFW controls whether a management port is externally reachable. Temporary rules are limited to the technician source IP and expire independently; multiple site ports may be open at once. Do not create permanent UFW allows for the whole management range.
