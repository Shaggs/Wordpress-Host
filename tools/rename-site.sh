#!/bin/bash
# Finish renaming a site's ID after its folder has been moved.
#   sudo bash rename-site.sh ardeners-treasures gardeners-treasures          (check only - changes nothing)
#   sudo bash rename-site.sh ardeners-treasures gardeners-treasures --go     (do it)
# What --go does (the old database volume is KEPT until you remove it yourself):
#   1. dumps the database to /opt/wp-host/backups/rename-<old>-<date>.sql.gz
#   2. stops and removes the old containers and network (volumes kept)
#   3. copies each old volume <old>_x to <new>_x
#   4. updates the site ID inside the site's settings files (not the domain)
#   5. starts the site under the new ID
#   6. moves dashboard history (alerts, uptime, baselines...), user site access and backup folders to the new ID
set -u
OLD=${1:?old id}; NEW=${2:?new id}; GO=${3:-}
BASE=/opt/wp-host; D=$BASE/sites/$NEW
ok() { echo "  [ok] $*"; }
bad() { echo "  [STOP] $*"; exit 1; }
echo "== Checks"
if [ -d "$BASE/sites/$OLD" ]; then
  rmdir "$BASE/sites/$OLD/migration-source" 2>/dev/null; rmdir "$BASE/sites/$OLD" 2>/dev/null \
    && ok "removed empty leftover folder $BASE/sites/$OLD" \
    || bad "$BASE/sites/$OLD exists and is not empty - check what is in it first"
fi
[ -d "$D" ] || bad "$D not found"
ok "folder is $D"
CF=$(cd "$D" && ls compose.yml docker-compose.yml compose.yaml docker-compose.yaml 2>/dev/null | head -1)
[ -n "$CF" ] && ok "compose file: $CF" || bad "no compose file in $D"
CONTAINERS=$( { docker ps -a --filter "label=com.docker.compose.project=$OLD" --format '{{.Names}}'; docker ps -a --format '{{.Names}}' | grep -E "^${OLD}-"; } | sort -u)
VOLUMES=$(docker volume ls -q | grep -E "^${OLD}_" || true)
echo "  old containers: ${CONTAINERS:-none}"
echo "  old volumes:    ${VOLUMES:-none}"
for v in $VOLUMES; do docker volume inspect "${NEW}${v#$OLD}" >/dev/null 2>&1 && bad "volume ${NEW}${v#$OLD} already exists"; done
FILES=$(find "$D" -maxdepth 3 -path "$D/wordpress" -prune -o -type f \( -name '*.yml' -o -name '*.yaml' -o -name '*.json' -o -name '.env*' -o -name '*.conf' -o -name '*.tpl' \) -print; ls "$D/wordpress/wp-config.php" 2>/dev/null)
echo "  lines mentioning the old ID (these will change):"
for f in $FILES; do perl -ne 'print "    '"${f#$D/}"': $_" if /(?<![A-Za-z0-9.-])'"$OLD"'(?![A-Za-z0-9.])/' "$f"; done
echo "  lines that mention it as part of a domain/other word (left alone):"
for f in $FILES; do perl -ne 'print "    '"${f#$D/}"': $_" if /'"$OLD"'/ && !/(?<![A-Za-z0-9.-])'"$OLD"'(?![A-Za-z0-9.])/' "$f"; done
PDB=$BASE/platform.db
echo "  dashboard records under the old ID:"
python3 - "$PDB" "$OLD" <<'EOF'
import sqlite3, sys
db = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
for (t,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
    cols = [r[1] for r in db.execute(f'PRAGMA table_info("{t}")')]
    for c in ("site", "target"):
        if c in cols:
            n = db.execute(f'SELECT COUNT(*) FROM "{t}" WHERE "{c}"=?', (sys.argv[2],)).fetchone()[0]
            if n: print(f"    {t}.{c}: {n}")
EOF
BK=$(grep -h '^WP_BACKUP_DIR=' $BASE/manager/manager.env 2>/dev/null | cut -d= -f2-)
BDIRS=$(ls -d "$BASE/backups/$OLD" ${BK:+"$BK/$OLD"} 2>/dev/null | sort -u)
echo "  backup folders: ${BDIRS:-none}"
[ "$GO" = "--go" ] || { echo; echo "Check only. Nothing changed. Re-run with --go to do it."; exit 0; }

echo; echo "== 1. Database dump"
mkdir -p $BASE/backups
DUMP=$BASE/backups/rename-$OLD-$(date +%Y%m%d-%H%M).sql.gz
RP=$(grep -h '^DB_ROOT_PASSWORD=' "$D/.env" | cut -d= -f2-)
if docker exec "$OLD-db" sh -c "command -v mariadb-dump >/dev/null" 2>/dev/null; then
  docker exec "$OLD-db" mariadb-dump -uroot -p"$RP" --all-databases --single-transaction 2>/dev/null | gzip > "$DUMP"
  [ $(gzip -dc "$DUMP" | head -c 20000 | grep -c 'CREATE TABLE') -gt 0 ] && ok "dump saved: $DUMP ($(du -h "$DUMP" | cut -f1))" || bad "dump looks empty - nothing else changed"
else
  bad "$OLD-db container not running - start it or tell Claude; nothing changed"
fi
echo "== 2. Stop old containers"
for c in $CONTAINERS; do docker rm -f "$c" >/dev/null && ok "removed container $c"; done
for n in $(docker network ls -q --filter "label=com.docker.compose.project=$OLD"); do docker network rm "$n" >/dev/null && ok "removed network $n"; done
echo "== 3. Copy volumes"
for v in $VOLUMES; do
  nv="${NEW}${v#$OLD}"; short="${v#${OLD}_}"
  docker volume create --label com.docker.compose.project="$NEW" --label com.docker.compose.volume="$short" "$nv" >/dev/null
  docker run --rm -v "$v":/from:ro -v "$nv":/to alpine sh -c 'cp -a /from/. /to/' && ok "$v -> $nv ($(docker run --rm -v "$nv":/v alpine du -sh /v | cut -f1))" || bad "copy of $v failed - old volume untouched"
done
echo "== 4. Update settings files"
for f in $FILES; do cp -p "$f" "$f.bak-rename"; perl -pi -e 's/(?<![A-Za-z0-9.-])'"$OLD"'(?![A-Za-z0-9.])/'"$NEW"'/g' "$f"; done
ok "updated (originals kept as *.bak-rename)"
echo "== 5. Start under the new ID"
(cd "$D" && docker compose up -d 2>&1 | tail -5)
for i in $(seq 1 30); do s=$(docker inspect -f '{{.State.Health.Status}}' "$NEW-db" 2>/dev/null); [ "$s" = healthy ] && break; sleep 3; done
docker ps --filter "label=com.docker.compose.project=$NEW" --format '  {{.Names}}: {{.Status}}'
echo "== 6. Dashboard records, user access, backups"
cp -p $PDB $BASE/platform.db.bak-rename-$OLD
python3 - "$PDB" "$OLD" "$NEW" <<'EOF'
import sqlite3, sys
db = sqlite3.connect(sys.argv[1]); o, n = sys.argv[2], sys.argv[3]
for (t,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
    cols = [r[1] for r in db.execute(f'PRAGMA table_info("{t}")')]
    for c in ("site", "target"):
        if c in cols:
            try:
                k = db.execute(f'UPDATE "{t}" SET "{c}"=? WHERE "{c}"=?', (n, o)).rowcount
                if k: print(f"  [ok] {t}.{c}: {k} row(s)")
            except sqlite3.IntegrityError as e:
                print(f"  [skip] {t}.{c}: {e}")
db.commit()
EOF
if grep -q "\"$OLD\"" $BASE/users.json 2>/dev/null; then cp -p $BASE/users.json $BASE/users.json.bak-rename; perl -pi -e 's/"'"$OLD"'"/"'"$NEW"'"/g' $BASE/users.json; ok "users.json site access updated"; fi
for b in $BDIRS; do mv "$b" "$(dirname "$b")/$NEW" && ok "backups: $b -> $(dirname "$b")/$NEW"; done
systemctl restart wp-host-manager && ok "dashboard restarted"
echo
echo "== Next (by hand)"
echo "  * Nginx Proxy Manager: edit this site's proxy host, set Forward Hostname to  $NEW-wp  and save."
echo "  * If the site had phpMyAdmin or SFTP switched on, switch it on again from the site page in the dashboard."
echo "  * Check the site loads, then the dashboard (Sites list, Live Usage)."
echo "  * When happy, remove the old volume(s):"
for v in $VOLUMES; do echo "      sudo docker volume rm $v"; done
