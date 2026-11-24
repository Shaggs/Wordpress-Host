#!/bin/bash
# Give every existing WordPress site an Apache worker cap sized to its memory limit
# (about 100 MB per worker, min 5, max 32), so traffic spikes queue instead of crashing the site.
#   sudo bash cap-workers-all.sh          check only - shows what each site would get
#   sudo bash cap-workers-all.sh --go     apply (each site restarts its web container: a few seconds' blip)
# Re-run after changing a site's memory allowance to resize its cap. compose.yml is backed up first.
GO=${1:-}
for D in /opt/wp-host/sites/*/; do
  SITE=$(basename "$D"); C="$SITE-wp"
  CF=$(cd "$D" && ls compose.yml docker-compose.yml 2>/dev/null | head -1)
  [ -n "$CF" ] || continue
  grep -q -- '- ./wordpress:/var/www/html$' "$D$CF" || { printf "%-24s skipped (not a WordPress site)\n" "$SITE"; continue; }
  IMG=$(docker inspect -f '{{.Config.Image}}' "$C" 2>/dev/null)
  case "$IMG" in *fpm*) printf "%-24s skipped (php-fpm image)\n" "$SITE"; continue;; esac
  MEM=$(docker inspect -f '{{.HostConfig.Memory}}' "$C" 2>/dev/null); MEM=${MEM:-0}
  MB=$(( MEM / 1048576 )); [ "$MB" -gt 0 ] || MB=1024
  W=$(( MB / 100 )); [ $W -lt 5 ] && W=5; [ $W -gt 32 ] && W=32
  S=$(( W < 3 ? W : 3 )); X=$(( W < 8 ? W : 8 ))
  HAS=$(grep -c 'apache-workers.conf' "$D$CF")
  CUR=$(grep -oE 'MaxRequestWorkers +[0-9]+' "$D/apache-workers.conf" 2>/dev/null | awk '{print $2}')
  printf "%-24s memory %5s MB -> %2s workers   (now: %s)\n" "$SITE" "$MB" "$W" "${CUR:-no cap}"
  [ "$GO" = "--go" ] || continue
  cat > "$D/apache-workers.conf" <<EOF
# WP Host: worker cap sized to the site's memory limit (${MB} MB)
<IfModule mpm_prefork_module>
    StartServers             $S
    MinSpareServers          $S
    MaxSpareServers          $X
    ServerLimit             $W
    MaxRequestWorkers       $W
    MaxConnectionsPerChild 500
</IfModule>
EOF
  if [ "$HAS" = 0 ]; then
    cp -p "$D$CF" "$D$CF.bak-workers"
    sed -i 's|^\(\s*\)- ./wordpress:/var/www/html$|&\n\1- ./apache-workers.conf:/etc/apache2/conf-enabled/zz-workers.conf:ro|' "$D$CF"
    (cd "$D" && docker compose up -d wordpress >/dev/null 2>&1)
  else
    docker exec "$C" apachectl -k graceful >/dev/null 2>&1
  fi
  sleep 5
  if timeout 10 docker exec "$C" php -r 'echo "ok";' >/dev/null 2>&1; then
    echo "      applied, site answering"
  else
    echo "      APPLIED BUT SITE NOT ANSWERING - restore with: cd $D && cp -p $CF.bak-workers $CF && docker compose up -d wordpress"
  fi
done
[ "$GO" = "--go" ] || echo; [ "$GO" = "--go" ] || echo "Check only. Re-run with --go to apply."
