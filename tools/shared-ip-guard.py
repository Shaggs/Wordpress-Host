#!/usr/bin/env python3
"""shared-ip-guard - stop Wordfence from taking WordPress sites down while every visitor
arrives from the same address (the router's source NAT turns every visitor into one address, set in SHARED_IPS).

  sudo python3 shared-ip-guard.py status    read-only: each site, how it is reached, its Wordfence
                                            settings and any block on the shared address
  sudo python3 shared-ip-guard.py run       fix anything that needs it (what the cron job runs)
  sudo python3 shared-ip-guard.py install   install to /usr/local/sbin, add the proxy's login rate
                                            limit, run every 2 minutes from cron, run once now
  sudo python3 shared-ip-guard.py restore   put back every Wordfence setting this changed
                                            (use once the router passes real addresses)
  sudo python3 shared-ip-guard.py uninstall restore, remove the cron job and the rate limit

Per site:
  * Behind Cloudflare (real visitor address arrives in X-Forwarded-For):
      Wordfence keeps full protection; it is told to read the real address
      (howGetIPs = HTTP_X_FORWARDED_FOR, trusted proxy = the shared address).
  * Not behind Cloudflare (every visitor looks like the shared address):
      Wordfence's per-address blocking is switched off, because one block there blocks everyone:
        loginSecurityEnabled = 0  (brute-force lockouts)
        firewallEnabled      = 0  (rate limiting / advanced blocking)
        other_WFNet          = 0  (Wordfence network blocks)
      The Wordfence firewall rules (SQL injection, XSS, malicious uploads ...) stay ON.
      Brute force is limited at the proxy instead: at most 10 login / xmlrpc POSTs a minute per
      site (burst 10), answered with 429 - the rest of the site is unaffected.
      The original values are saved and put back automatically if the site moves to Cloudflare,
      or by "restore".
  * Every site: any Wordfence block on the shared address is removed (backstop).
"""
import fcntl, ipaddress, json, os, re, shutil, socket, subprocess, sys, syslog, tempfile, time

SHARED = [s.strip() for s in os.environ.get("SHARED_IPS", "").split(",") if s.strip()]
for _ip in SHARED:
    ipaddress.ip_address(_ip)  # refuse anything that is not an IP (values go into SQL)
NPM = os.environ.get("NPM_CONTAINER", "wp-npm")
NPM_DATA = os.environ.get("NPM_DATA", "/opt/wp-host/nginx-proxy-manager/data")
STATE = "/opt/wp-host/shared-ip-guard.json"
LOCK = "/run/shared-ip-guard.lock"
CRON = "/etc/cron.d/shared-ip-guard"
TARGET = "/usr/local/sbin/shared-ip-guard"
MARK_A, MARK_B = "# >>> shared-ip-guard", "# <<< shared-ip-guard"
LOGIN_RATE = os.environ.get("LOGIN_RATE", "10r/m")

SHARED_MODE_OFF = {"loginSecurityEnabled": "0", "firewallEnabled": "0", "other_WFNet": "0"}
REAL_IP = {"howGetIPs": "HTTP_X_FORWARDED_FOR", "howGetIPs_trusted_proxies": "\n".join(SHARED)}

CF_NETS = [ipaddress.ip_network(n) for n in (
    "173.245.48.0/20 103.21.244.0/22 103.22.200.0/22 103.31.4.0/22 141.101.64.0/18 "
    "108.162.192.0/18 190.93.240.0/20 188.114.96.0/20 197.234.240.0/22 198.41.128.0/17 "
    "162.158.0.0/15 104.16.0.0/13 104.24.0.0/14 172.64.0.0/13 131.0.72.0/22 "
    "2400:cb00::/32 2606:4700::/32 2803:f800::/32 2405:b500::/32 2405:8100::/32 "
    "2a06:98c0::/29 2c0f:f248::/32").split()]

VERBOSE = True


def log(msg, quiet=False):
    if VERBOSE or not quiet:
        print(msg, flush=True)
    try:
        syslog.openlog("shared-ip-guard")
        syslog.syslog(msg)
    except Exception:
        pass


def sh(args, inp=None, timeout=180):
    try:
        p = subprocess.run(args, input=inp, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"


def ip_sql_list():
    vals = []
    for ip in SHARED:
        a = ipaddress.ip_address(ip)
        vals.append(f"INET6_ATON('{ip}')")
        if a.version == 4:
            vals.append(f"INET6_ATON('::ffff:{ip}')")
    return ",".join(vals)


# ---------------------------------------------------------------- state
def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {"sites": {}}


def save_state(st):
    st["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1, sort_keys=True)
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE)


# ---------------------------------------------------------------- sites
def wp_sites():
    rc, out, _ = sh(["docker", "ps", "--format", "{{.Names}}"])
    names = set(out.split())
    return sorted(n[:-3] for n in names if n.endswith("-wp") and n[:-3] + "-db" in names)


def site_domain(site):
    pat = re.compile(r'set\s+\$server\s+"?' + re.escape(site) + r'-wp"?\s*;')
    d = os.path.join(NPM_DATA, "nginx", "proxy_host")
    try:
        files = sorted(os.listdir(d))
    except OSError:
        return None
    for fn in files:
        if not fn.endswith(".conf"):
            continue
        try:
            txt = open(os.path.join(d, fn)).read()
        except OSError:
            continue
        if pat.search(txt):
            m = re.search(r"server_name\s+([^;\s]+)", txt)
            return m.group(1) if m else None
    return None


def behind_cloudflare(domain):
    if not domain:
        return False
    try:
        addrs = {ai[4][0] for ai in socket.getaddrinfo(domain, 443, proto=socket.IPPROTO_TCP)}
    except OSError:
        return False  # can't tell: treat as shared (the safe choice - it cannot cause an outage)
    return bool(addrs) and all(any(ipaddress.ip_address(a) in n for n in CF_NETS) for a in addrs)


class Site:
    def __init__(self, name):
        self.name = name
        rc, out, _ = sh(["docker", "inspect", "-f", "{{json .Config.Env}}", name + "-wp"])
        env = {}
        for kv in (json.loads(out) if rc == 0 and out.strip() else []) or []:
            k, _, v = kv.partition("=")
            env[k] = v
        self.env = {k: v for k, v in env.items() if k.startswith("WORDPRESS_")}
        self.client = None

    def sql(self, q):
        """Run SQL in the site's own database container. Returns rows (lists of str)."""
        if self.client is None:
            rc, out, _ = sh(["docker", "exec", self.name + "-db", "sh", "-c",
                             "command -v mariadb || command -v mysql"])
            self.client = out.strip().splitlines()[0] if rc == 0 and out.strip() else ""
        if not self.client:
            raise RuntimeError("no mysql client in " + self.name + "-db")
        user = self.env.get("WORDPRESS_DB_USER", "")
        db = self.env.get("WORDPRESS_DB_NAME", "")
        # password from the db container's own environment, so it never shows on the host's ps
        script = ('P="${MARIADB_PASSWORD:-$MYSQL_PASSWORD}"; [ -n "$P" ] || P="$GUARD_PW"; '
                  'MYSQL_PWD="$P" exec "$0" -h 127.0.0.1 -u "$1" -N -B "$2"')
        args = ["docker", "exec", "-i", self.name + "-db", "sh", "-c", script, self.client, user, db]
        rc, out, err = sh(args, inp=q, timeout=60)
        if rc != 0 and "denied" in err.lower() and self.env.get("WORDPRESS_DB_PASSWORD"):
            args[3:3] = ["-e", "GUARD_PW=" + self.env["WORDPRESS_DB_PASSWORD"]]
            args[args.index("sh", 3) + 2] = script.replace('P="${MARIADB_PASSWORD:-$MYSQL_PASSWORD}"; [ -n "$P" ] || ', "")
            rc, out, err = sh(args, inp=q, timeout=60)
        if rc != 0:
            raise RuntimeError(err.strip().splitlines()[-1] if err.strip() else "sql failed")
        return [line.split("\t") for line in out.splitlines() if line.strip()]

    def wf_tables(self):
        """[(prefix, config_table, blocks_table)] for every Wordfence install in this database
        (live site and staging copies), whatever the letter case of the table names."""
        tables = [r[0] for r in self.sql("SHOW TABLES;")]
        low = {t.lower(): t for t in tables}
        found = []
        for t in tables:
            m = re.match(r"(?i)^(.*)wfconfig$", t)
            if m:
                pre = m.group(1)
                blk = low.get((pre + "wfblocks7").lower())
                found.append((pre, t, blk))
        return found

    def main_prefix(self):
        return self.env.get("WORDPRESS_TABLE_PREFIX", "wp_")

    def get_opts(self, cfg, keys):
        names = ",".join("'%s'" % k for k in keys)
        rows = self.sql(f"SELECT name, val FROM `{cfg}` WHERE name IN ({names});")
        return {r[0]: (r[1] if len(r) > 1 else "") for r in rows}

    def shared_blocks(self, blk):
        if not blk:
            return 0
        rows = self.sql(f"SELECT COUNT(*) FROM `{blk}` WHERE IP IN ({ip_sql_list()}) "
                        f"AND (expiration = 0 OR expiration > UNIX_TIMESTAMP());")
        return int(rows[0][0]) if rows else 0

    # -- changing things: through Wordfence's own code when possible (keeps its firewall cache in
    #    step), straight in the database otherwise (plugin folder renamed, staging copy, errors)
    def wp_eval(self, sets, unblock):
        fd, envf = tempfile.mkstemp(prefix="sig-env-")
        try:
            with os.fdopen(fd, "w") as f:
                for k, v in self.env.items():
                    f.write(f"{k}={v}\n")
            os.chmod(envf, 0o600)
            skip = ""
            try:
                row = self.sql(f"SELECT option_value FROM `{self.main_prefix()}options` "
                               f"WHERE option_name='active_plugins';")
                ser = row[0][0] if row else ""
                slugs = set(re.findall(r's:\d+:"([^"/]+)/', ser))
                slugs |= {s[:-4] for s in re.findall(r's:\d+:"([^"/]+\.php)"', ser)}
                slugs.discard("wordfence")
                skip = ",".join(sorted(slugs))
            except Exception:
                pass
            php = ("if(!class_exists('wfConfig')){echo 'NOWF';return;}"
                   "try{"
                   "foreach(json_decode(base64_decode('%s'),true) as $k=>$v){wfConfig::set($k,$v);}"
                   "if(class_exists('wfBlock')){foreach(json_decode(base64_decode('%s'),true) as $ip){"
                   "if(method_exists('wfBlock','unlockOutIP'))wfBlock::unlockOutIP($ip);"
                   "if(method_exists('wfBlock','unblockIP'))wfBlock::unblockIP($ip);}}"
                   "if(class_exists('wfWAFIPBlocksController')&&method_exists('wfWAFIPBlocksController','synchronizeConfigSettings'))"
                   "wfWAFIPBlocksController::synchronizeConfigSettings();"
                   "echo 'OK';}catch(Throwable $e){echo 'ERR '.$e->getMessage();}") % (
                __import__("base64").b64encode(json.dumps(sets).encode()).decode(),
                __import__("base64").b64encode(json.dumps(unblock).encode()).decode())
            args = ["docker", "run", "--rm", "--user", "33:33", "--env-file", envf,
                    "--network", self.name + "_internal", "--volumes-from", self.name + "-wp",
                    "wordpress:cli", "wp", "--skip-themes"]
            if skip:
                args.append("--skip-plugins=" + skip)
            args += ["eval", php]
            rc, out, err = sh(args, timeout=150)
            return out.strip().splitlines()[-1] if out.strip() else ("ERR " + err.strip()[-200:])
        finally:
            try:
                os.unlink(envf)
            except OSError:
                pass

    def sql_set(self, cfg, sets):
        for k, v in sets.items():
            v = v.replace("\\", "\\\\").replace("'", "\\'")
            self.sql(f"INSERT INTO `{cfg}` (name, val, autoload) VALUES ('{k}', '{v}', 'yes') "
                     f"ON DUPLICATE KEY UPDATE val = VALUES(val);")

    def sql_unblock(self, blk):
        if blk:
            self.sql(f"DELETE FROM `{blk}` WHERE IP IN ({ip_sql_list()});")


# ---------------------------------------------------------------- actions
def handle_site(name, st, mode_override=None, apply=True):
    s = Site(name)
    dom = site_domain(name)
    cf = behind_cloudflare(dom)
    mode = mode_override or ("cloudflare" if cf else "shared")
    sst = st["sites"].setdefault(name, {})
    try:
        tabs = s.wf_tables()
    except Exception as e:
        log(f"{name}: database not reachable ({e})")
        return
    if not tabs:
        if not apply:
            print(f"{name:28} {dom or '-':32} no Wordfence")
        return
    if apply and sst.get("_resync"):
        # settings were written straight to the database while Wordfence was switched off;
        # once it is back, let it reload them into its firewall
        if s.wp_eval({}, SHARED) == "OK":
            sst.pop("_resync", None)
            save_state(st)
            log(f"{name}: Wordfence is back - firewall settings re-synchronised")
    for pre, cfg, blk in tabs:
        keys = list(SHARED_MODE_OFF) + list(REAL_IP)
        cur = s.get_opts(cfg, keys)
        nblk = s.shared_blocks(blk)
        saved = sst.get(pre, {}).get("orig")
        want = dict(REAL_IP)
        if mode == "shared":
            want.update(SHARED_MODE_OFF)
        elif saved:
            want.update(saved)  # back on Cloudflare (or restore): put the originals back
        change = {k: v for k, v in want.items() if cur.get(k, None) != v}
        label = f"{name}{'' if pre == s.main_prefix() else ' [' + pre + ']'}"
        if not apply:
            prot = "OFF" if cur.get("loginSecurityEnabled") == "0" else "on"
            print(f"{label:28} {dom or '-':32} {mode:10} lockouts {prot:3}  "
                  f"rate-limit {'OFF' if cur.get('firewallEnabled') == '0' else 'on':3}  "
                  f"blocks on shared IP: {nblk}" + (f"   needs: {', '.join(change)}" if change else ""))
            continue
        if not change and not nblk:
            continue
        if mode == "shared" and not saved:
            sst[pre] = {"orig": {k: cur.get(k, "1") for k in SHARED_MODE_OFF},
                        "since": time.strftime("%Y-%m-%d %H:%M")}
            save_state(st)
        how = "db"
        if pre == s.main_prefix():
            res = s.wp_eval(change, SHARED if nblk else [])
            how = "wordfence" if res == "OK" else "db"
            if res != "OK":
                sst["_resync"] = True
                save_state(st)
            if res != "OK" and res != "NOWF":
                log(f"{label}: Wordfence API said '{res[:120]}', using the database instead", quiet=True)
        if how == "db":
            s.sql_set(cfg, change)
        if s.shared_blocks(blk):
            s.sql_unblock(blk)
        if nblk:
            log(f"{label}: removed {nblk} Wordfence block(s) on the shared address ({how})", quiet=False)
        if change:
            log(f"{label}: {mode} mode - set {', '.join(f'{k}={v!r}' for k, v in change.items())} ({how})",
                quiet=False)
        if mode != "shared" and saved:
            sst.pop(pre, None)
            save_state(st)
            log(f"{label}: original Wordfence settings restored")


def with_lock(fn):
    f = open(LOCK, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another run is in progress")
        return
    fn()


def cmd_status():
    st = load_state()
    print(f"shared address(es): {', '.join(SHARED)}")
    print(f"{'site':28} {'domain':32} {'reached via':10}")
    for site in wp_sites():
        handle_site(site, st, apply=False)
    print("\nproxy login rate limit:", "installed" if nginx_installed() else "NOT installed")
    print("cron job:", "installed" if os.path.exists(CRON) else "NOT installed")


def cmd_run():
    def go():
        st = load_state()
        for site in wp_sites():
            try:
                handle_site(site, st)
            except Exception as e:
                log(f"{site}: error {e}")
    with_lock(go)


def cmd_restore():
    def go():
        st = load_state()
        for site in list(st["sites"]):
            if site in wp_sites() and st["sites"][site]:
                handle_site(site, st, mode_override="cloudflare")  # cloudflare mode = restore originals
        log("restore finished: Wordfence settings are back to what they were before shared-ip-guard")
    with_lock(go)


# ---------------------------------------------------------------- proxy rate limit
HTTP_TOP = f"""{MARK_A} (login rate limit; every visitor shares one address, so limit per site)
map "$request_method:$uri" $sig_login_key {{
    default "";
    "~^POST:/(wp-login\\.php|xmlrpc\\.php)$" $host;
}}
limit_req_zone $sig_login_key zone=sig_wplogin:10m rate={LOGIN_RATE};
{MARK_B}
"""
SERVER_PROXY = f"""{MARK_A}
limit_req zone=sig_wplogin burst=10 nodelay;
limit_req_status 429;
{MARK_B}
"""


def custom(fn):
    return os.path.join(NPM_DATA, "nginx", "custom", fn)


def strip_block(txt):
    return re.sub(re.escape(MARK_A) + r".*?" + re.escape(MARK_B) + r"\n?", "", txt, flags=re.S)


def nginx_installed():
    try:
        return MARK_A in open(custom("server_proxy.conf")).read()
    except OSError:
        return False


def nginx_reload_or_revert(backups):
    rc, out, err = sh(["docker", "exec", NPM, "nginx", "-t"])
    if rc != 0:
        for path, old in backups.items():
            if old is None:
                os.unlink(path)
            else:
                open(path, "w").write(old)
        print("nginx rejected the change, put the files back:\n" + (err or out)[-600:])
        return False
    sh(["docker", "exec", NPM, "nginx", "-s", "reload"])
    return True


def set_nginx(on):
    os.makedirs(os.path.dirname(custom("x")), exist_ok=True)
    backups = {}
    for fn, block in (("http_top.conf", HTTP_TOP), ("server_proxy.conf", SERVER_PROXY)):
        p = custom(fn)
        old = open(p).read() if os.path.exists(p) else None
        backups[p] = old
        new = strip_block(old or "")
        if on:
            new = (new.rstrip("\n") + "\n\n" if new.strip() else "") + block
        if old is not None:
            shutil.copy2(p, p + ".bak-shared-ip-guard")
        if new.strip():
            open(p, "w").write(new)
        elif old is not None:
            os.unlink(p)
    if nginx_reload_or_revert(backups):
        print("proxy login rate limit:", f"ON ({LOGIN_RATE} per site, burst 10)" if on else "removed")
        if on:
            rc, out, _ = sh(["docker", "exec", NPM, "sh", "-c",
                             "grep -L 'custom/server_proxy' /data/nginx/proxy_host/*.conf 2>/dev/null"])
            if out.strip():
                print("  note: these proxy hosts do not include server_proxy.conf, so are not limited:\n  "
                      + out.strip().replace("\n", "\n  "))


def cmd_install():
    me = os.path.realpath(__file__)
    if me != TARGET:
        shutil.copy2(me, TARGET)
    os.chmod(TARGET, 0o755)
    with open(CRON, "w") as f:
        f.write("# keep Wordfence from blocking the shared NAT address (see " + TARGET + ")\n"
                "SHELL=/bin/bash\nPATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n"
                "*/2 * * * * root " + TARGET + " run --quiet >/dev/null 2>&1\n")
    os.chmod(CRON, 0o644)
    print("installed", TARGET, "and", CRON, "(every 2 minutes)")
    set_nginx(True)
    cmd_run()
    print()
    cmd_status()
    print("\nfollow it with:  journalctl -t shared-ip-guard -f")


def cmd_uninstall():
    cmd_restore()
    for p in (CRON,):
        if os.path.exists(p):
            os.unlink(p)
    set_nginx(False)
    print("removed cron job and rate limit; script left at", TARGET)


if __name__ == "__main__":
    if os.geteuid() != 0:
        sys.exit("run with sudo")
    if "--quiet" in sys.argv:
        VERBOSE = False
    cmd = next((a for a in sys.argv[1:] if not a.startswith("-")), "status")
    {"status": cmd_status, "run": cmd_run, "install": cmd_install,
     "restore": cmd_restore, "uninstall": cmd_uninstall}.get(
        cmd, lambda: sys.exit(__doc__))()
