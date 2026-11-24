#!/usr/bin/env python3
"""Stop crawlers and WP-Cron from overloading WordPress sites.

  sudo python3 site-calm.py bots                 block junk crawlers, slow down heavy ones (all sites)
  sudo python3 site-calm.py cron <site> [...]    run WP-Cron on a schedule instead of on visits
  sudo python3 site-calm.py cron --all           ... for every WordPress site
  sudo python3 site-calm.py status               what is in place
  sudo python3 site-calm.py undo-bots | undo-cron <site>|--all

bots: at the proxy (Nginx Proxy Manager), for every site:
  - blocked (403):  ShapBot, Bytespider, MJ12bot, DotBot, BLEXBot, Barkrowler, serpstatbot,
                    MegaIndex, ImagesiftBot, DataForSeoBot - crawlers that bring no visitors
  - slowed to 30 requests a minute per site (extra ones get "try later"):
                    Facebook/Meta crawlers, Ahrefs, Semrush, Petal, GPTBot, ClaudeBot, CCBot,
                    Amazonbot, Yandex, Baidu, Seznam, Awario
  Google and Bing are not touched.
cron: sets DISABLE_WP_CRON in wp-config.php (backup kept) and runs the site's scheduled jobs from
  the server every 5 minutes (/etc/cron.d/wp-site-cron) - same jobs, just not piggy-backed on visits.
"""
import os, re, shutil, subprocess, sys, tempfile

NPM = os.environ.get("NPM_CONTAINER", "wp-npm")
CUSTOM = os.environ.get("NPM_CUSTOM", "/opt/wp-host/nginx-proxy-manager/data/nginx/custom")
CRONF = "/etc/cron.d/wp-site-cron"
A, B = "# >>> site-calm bots", "# <<< site-calm bots"

BLOCK = r"ShapBot|Bytespider|MJ12bot|DotBot|BLEXBot|Barkrowler|serpstatbot|MegaIndex|ImagesiftBot|DataForSeoBot"
SLOW = (r"meta-webindexer|meta-externalagent|facebookcatalog|AhrefsBot|SemrushBot|PetalBot|GPTBot|"
        r"ClaudeBot|CCBot|Amazonbot|YandexBot|Baiduspider|SeznamBot|AwarioBot")
HTTP_TOP = f"""{A} (crawler rules)
map $http_user_agent $calm_block {{
    default 0;
    "~*({BLOCK})" 1;
}}
map $http_user_agent $calm_slow_key {{
    default "";
    "~*({SLOW})" $host;
}}
limit_req_zone $calm_slow_key zone=calm_bots:10m rate=30r/m;
{B}
"""
SERVER = f"""{A}
if ($calm_block) {{ return 403; }}
limit_req zone=calm_bots burst=10 nodelay;
{B}
"""


def sh(args, timeout=120):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def strip(t):
    return re.sub(re.escape(A) + r".*?" + re.escape(B) + r"\n?", "", t, flags=re.S)


def set_bots(on):
    files = {"http_top.conf": HTTP_TOP, "server_proxy.conf": SERVER}
    backup = {}
    for fn, block in files.items():
        p = os.path.join(CUSTOM, fn)
        old = open(p).read() if os.path.exists(p) else None
        backup[p] = old
        new = strip(old or "").rstrip("\n")
        if on:
            new = (new + "\n\n" if new.strip() else "") + block
        if old is not None and not os.path.exists(p + ".bak-calm"):
            shutil.copy2(p, p + ".bak-calm")
        if new.strip():
            open(p, "w").write(new if new.endswith("\n") else new + "\n")
        elif old is not None:
            os.unlink(p)
    rc, out, err = sh(["docker", "exec", NPM, "nginx", "-t"])
    if rc != 0:
        for p, old in backup.items():
            if old is None:
                os.path.exists(p) and os.unlink(p)
            else:
                open(p, "w").write(old)
        sys.exit("nginx rejected the change - files put back:\n" + (err or out)[-500:])
    sh(["docker", "exec", NPM, "nginx", "-s", "reload"])
    print("crawler rules", "ON: junk crawlers blocked, heavy crawlers limited to 30/min per site" if on else "removed")


def wp_sites():
    rc, out, _ = sh(["docker", "ps", "--format", "{{.Names}}"])
    names = set(out.split())
    return sorted(n[:-3] for n in names if n.endswith("-wp") and n[:-3] + "-db" in names)


def wp(site, *args):
    rc, out, _ = sh(["docker", "inspect", "-f", "{{range .Config.Env}}{{println .}}{{end}}", site + "-wp"])
    fd, envf = tempfile.mkstemp()
    try:
        with os.fdopen(fd, "w") as f:
            f.write("".join(l + "\n" for l in out.splitlines() if l.startswith("WORDPRESS_")))
        return sh(["docker", "run", "--rm", "--user", "33:33", "--env-file", envf, "--network", site + "_internal",
                   "--volumes-from", site + "-wp", "wordpress:cli", "wp", "--skip-plugins", "--skip-themes", *args])
    finally:
        os.unlink(envf)


def cron_lines():
    try:
        return [l for l in open(CRONF).read().splitlines() if l.strip() and not l.startswith(("#", "SHELL", "PATH"))]
    except OSError:
        return []


def write_cron(lines):
    if not lines:
        os.path.exists(CRONF) and os.unlink(CRONF)
        return
    with open(CRONF, "w") as f:
        f.write("# WordPress scheduled jobs, run by the server instead of on visits (site-calm.py)\n"
                "SHELL=/bin/bash\nPATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n"
                + "\n".join(sorted(set(lines))) + "\n")
    os.chmod(CRONF, 0o644)


def cron_line(site, i):
    m = (i * 7) % 5  # spread sites over the 5-minute slot
    return (f"{m}-59/5 * * * * root timeout 600 docker exec -u www-data {site}-wp "
            f"php /var/www/html/wp-cron.php >/dev/null 2>&1  # {site}")


def set_cron(sites, on):
    lines = [l for l in cron_lines() if not any(l.endswith("# " + s) for s in sites)]
    for i, s in enumerate(sites):
        if on:
            sh(["docker", "exec", s + "-wp", "sh", "-c",
                "cd /var/www/html && [ -f wp-config.php.bak-calm ] || cp -p wp-config.php wp-config.php.bak-calm"])
            rc, out, err = wp(s, "config", "set", "DISABLE_WP_CRON", "true", "--raw", "--type=constant")
            if rc != 0:
                print(f"  {s}: could not edit wp-config.php ({(err or out).strip()[-160:]}) - skipped")
                continue
            lines.append(cron_line(s, i))
            print(f"  {s}: WP-Cron now runs every 5 minutes from the server, not on visits")
        else:
            wp(s, "config", "delete", "DISABLE_WP_CRON", "--type=constant")
            print(f"  {s}: back to WP-Cron on visits")
    write_cron(lines)


def status():
    t = open(os.path.join(CUSTOM, "server_proxy.conf")).read() if os.path.exists(os.path.join(CUSTOM, "server_proxy.conf")) else ""
    print("crawler rules:", "ON" if A in t else "off")
    on = [l.rsplit("# ", 1)[-1] for l in cron_lines()]
    for s in wp_sites():
        print(f"  {s:28} WP-Cron: {'server schedule' if s in on else 'on visits'}")


if __name__ == "__main__":
    if os.geteuid() != 0:
        sys.exit("run with sudo")
    a = sys.argv[1:] or ["status"]
    cmd, rest = a[0], a[1:]
    sites = wp_sites() if rest == ["--all"] else rest
    if cmd == "bots":
        set_bots(True)
    elif cmd == "undo-bots":
        set_bots(False)
    elif cmd in ("cron", "undo-cron"):
        unknown = [s for s in sites if s not in wp_sites()]
        if not sites or unknown:
            sys.exit(f"name WordPress site(s) or --all (unknown: {', '.join(unknown)})" if unknown else __doc__)
        set_cron(sites, cmd == "cron")
    elif cmd == "status":
        status()
    else:
        sys.exit(__doc__)
