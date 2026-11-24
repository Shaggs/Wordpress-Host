#!/bin/bash
# Make an old WordPress site run on PHP 8 (used after migrating very old sites).
#   sudo bash php8-fix.sh <site>            back up, update free plugins, patch the rest, test
#   sudo bash php8-fix.sh <site> --check    only list files PHP 8 refuses
#
# Keep php8-fix.php next to this script.
#  - wp-content (without uploads) is saved first to ~/<site>-wp-content-before-php8-<time>.tgz
#  - free plugins are replaced by the latest wordpress.org release; old copies go to
#    wp-content/php8-old/ (blocked from the web). Plugins closed on wordpress.org are kept.
#  - remaining PHP-8-fatal lines (paid plugins, themes) are patched; each changed file
#    keeps a <file>.bak-php8 copy
set -u
SITE=${1:?usage: sudo bash php8-fix.sh <site> [--check]}
HERE=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
PHPF="$HERE/php8-fix.php"
[ -f "$PHPF" ] || { echo "php8-fix.php must be next to this script"; exit 1; }
docker inspect "${SITE}-wp" >/dev/null 2>&1 || { echo "no container ${SITE}-wp"; exit 1; }
CLI="docker run --rm -i --user 0:0 --volumes-from ${SITE}-wp wordpress:cli php --"

if [ "${2:-}" = "--check" ]; then $CLI check < "$PHPF"; exit $?; fi

# free plugins: everything in plugins/ that exists on wordpress.org (others are reported as KEPT)
PAID_OR_CUSTOM='^(index\.php|js_composer|Ultimate_VC_Addons|revslider|go_pricing|dt-dummy|.*-core)$'
SLUGS=$(docker exec ${SITE}-wp sh -c 'ls /var/www/html/wp-content/plugins' | grep -vE "$PAID_OR_CUSTOM" | tr '\n' ' ')

BK=~"${SUDO_USER:-$USER}"; BK=$(eval echo "$BK")/${SITE}-wp-content-before-php8-$(date +%Y%m%d-%H%M).tgz
echo "== 1. backup of wp-content (without uploads) -> $BK"
docker exec ${SITE}-wp tar czf - -C /var/www/html/wp-content --exclude=./uploads --exclude=./cache --exclude=./php8-old . > "$BK" \
  && ls -lh "$BK" | awk '{print "   " $5}' || { echo "backup failed - nothing changed"; exit 1; }
[ -n "${SUDO_USER:-}" ] && chown "$SUDO_USER" "$BK"

echo "== 2. update free plugins from wordpress.org: $SLUGS"
$CLI update $SLUGS < "$PHPF"

echo "== 3. patch code PHP 8 refuses"
$CLI fix < "$PHPF"; FIXRC=$?

echo "== 4. file ownership for the web server"
docker exec ${SITE}-wp chown -R 33:33 /var/www/html/wp-content/plugins /var/www/html/wp-content/themes

echo "== 5. does WordPress load with all plugins and the theme?"
ENVF=$(mktemp); docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' ${SITE}-wp | grep -E '^WORDPRESS_' > "$ENVF"
W="docker run --rm -i --user 33:33 --env-file $ENVF --network ${SITE}_internal --volumes-from ${SITE}-wp wordpress:cli wp"
$W eval 'echo "WordPress " . get_bloginfo("version") . " loaded OK, theme: " . wp_get_theme()->get("Name") . "\n";' 2>&1 | grep -v '^PHP \(Deprecated\|Warning\|Notice\)' | tail -15
$W plugin list --fields=name,status,version 2>/dev/null
rm -f "$ENVF"
[ $FIXRC -ne 0 ] && echo "Some files still need a hand fix - see LEFT above."
echo "Undo: restore $BK into wp-content (keep uploads)."
