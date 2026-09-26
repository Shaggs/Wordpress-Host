\
import os, re, json, time, shutil, zipfile, subprocess, shlex
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import docker, requests

SITES = Path(os.environ.get("WP_SITES_DIR", "/opt/wp-host/sites"))
TZ = ZoneInfo("Australia/Adelaide")
DOCKER = docker.from_env()

def adelaide(value=None):
    dt = value or datetime.now(timezone.utc)
    if isinstance(dt, (int, float)):
        dt = datetime.fromtimestamp(dt, timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ).strftime("%d %b %Y, %I:%M %p %Z")

def human(n):
    n = float(n)
    for unit in ("B","KB","MB","GB","TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024

def site_meta(site):
    return json.loads((SITES / site / "site.json").read_text())

def source_dir(site):
    p = SITES / site / "migration-source"
    p.mkdir(parents=True, exist_ok=True)
    return p

def sftp_source_dir(site):
    return SITES / site / "wordpress" / "migration-source"

def report_path(site):
    return SITES / site / "migration-report.json"

def load_report(site):
    try:
        return json.loads(report_path(site).read_text())
    except Exception:
        return {}

def save_report(site, report):
    report_path(site).write_text(json.dumps(report, indent=2))

def source_files(site):
    # V8.1 recognises the protected source folder AND the natural SFTP path:
    # /upload/migration-source -> /opt/wp-host/sites/SITE/wordpress/migration-source
    roots = [source_dir(site), sftp_source_dir(site)]
    items, seen = [], set()
    for root in roots:
        if not root.exists():
            continue
        for p in root.iterdir():
            if not p.is_file():
                continue
            low = p.name.lower()
            if not (low.endswith(".zip") or low.endswith(".sql") or low.endswith(".sql.gz")):
                continue
            key = (p.name, p.stat().st_size)
            if key in seen:
                continue
            seen.add(key)
            items.append({
                "name": p.name,
                "path": str(p),
                "size": human(p.stat().st_size),
                "kind": "WordPress ZIP" if low.endswith(".zip") else "SQL",
                "modified": adelaide(p.stat().st_mtime),
            })
    return sorted(items, key=lambda x: Path(x["path"]).stat().st_mtime, reverse=True)

def find_inputs(site):
    files = source_files(site)
    z = next((Path(x["path"]) for x in files if x["name"].lower().endswith(".zip")), None)
    q = next((Path(x["path"]) for x in files if x["name"].lower().endswith(".sql") or x["name"].lower().endswith(".sql.gz")), None)
    return z, q

def run_cmd(cmd, cwd=None, timeout=1800, input_data=None):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, input=input_data, timeout=timeout)
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout).decode(errors="ignore")[-4000:])
    return p.stdout.decode(errors="ignore")

def safe_extract(zpath, dest):
    dest = dest.resolve()
    with zipfile.ZipFile(zpath, "r") as z:
        for m in z.infolist():
            # Reject symlink-type ZIP entries outright (defense-in-depth;
            # see module docstring for why this matters even though the
            # default extraction path doesn't currently materialize them).
            if (m.external_attr >> 16) & 0o170000 == 0o120000:
                raise RuntimeError(f"Symlink in ZIP rejected: {m.filename}")
            target = (dest / m.filename).resolve()
            if target != dest and dest not in target.parents:
                raise RuntimeError(f"Unsafe ZIP path rejected: {m.filename}")
        z.extractall(dest)

def wp_root(root):
    candidates = [root] + [p for p in root.rglob("*") if p.is_dir()]
    for p in candidates:
        if all((p / x).is_dir() for x in ("wp-admin","wp-content","wp-includes")):
            return p
    return None

def table_prefix(config):
    try:
        m = re.search(r"\$table_prefix\s*=\s*['\"]([^'\"]+)['\"]\s*;", config.read_text(errors="ignore"))
        if m and re.fullmatch(r"[A-Za-z0-9_]+", m.group(1)):
            return m.group(1)
    except Exception:
        pass
    return "wp_"

def envs(site):
    wp = DOCKER.containers.get(f"{site}-wp")
    db = DOCKER.containers.get(f"{site}-db")
    we, de = {}, {}
    for x in wp.attrs.get("Config",{}).get("Env",[]) or []:
        if "=" in x:
            k,v = x.split("=",1); we[k] = v
    for x in db.attrs.get("Config",{}).get("Env",[]) or []:
        if "=" in x:
            k,v = x.split("=",1); de[k] = v
    return {
        "name": we.get("WORDPRESS_DB_NAME") or de.get("MARIADB_DATABASE") or "wordpress",
        "user": we.get("WORDPRESS_DB_USER") or de.get("MARIADB_USER") or "wordpress",
        "password": we.get("WORDPRESS_DB_PASSWORD") or de.get("MARIADB_PASSWORD") or "",
        "host": we.get("WORDPRESS_DB_HOST") or "db:3306",
        "root": de.get("MARIADB_ROOT_PASSWORD") or de.get("MYSQL_ROOT_PASSWORD") or "",
    }

def preflight_package(zpath, qpath):
    result = {
        "zip_size": human(zpath.stat().st_size),
        "sql_size": human(qpath.stat().st_size),
        "warnings": [],
    }
    if not zipfile.is_zipfile(zpath):
        raise RuntimeError("WordPress source is not a valid ZIP archive.")
    with zipfile.ZipFile(zpath, "r") as z:
        names = z.namelist()
        result["zip_entries"] = len(names)
        result["has_wp_admin"] = any("/wp-admin/" in f"/{n}" or n.startswith("wp-admin/") for n in names)
        result["has_wp_content"] = any("/wp-content/" in f"/{n}" or n.startswith("wp-content/") for n in names)
        result["has_wp_includes"] = any("/wp-includes/" in f"/{n}" or n.startswith("wp-includes/") for n in names)
        if not all((result["has_wp_admin"], result["has_wp_content"], result["has_wp_includes"])):
            raise RuntimeError("ZIP does not appear to contain a complete WordPress installation.")
        risky = [
            n for n in names
            if n.endswith(".htaccess")
            or "/mu-plugins/" in f"/{n}"
            or n.endswith("/object-cache.php")
            or n.endswith("/advanced-cache.php")
            or n.endswith("/db.php")
        ]
        result["risky_items"] = len(risky)
        if risky:
            result["warnings"].append(f"{len(risky)} imported redirect/MU/drop-in item(s) will be isolated during first boot.")
    if qpath.stat().st_size == 0:
        raise RuntimeError("SQL dump is empty.")
    return result

def normalise_config(site, prefix):
    live = SITES / site / "wordpress"
    cfg = live / "wp-config.php"
    imported = live / "wp-config.imported.php"
    txt = cfg.read_text(errors="ignore") if cfg.exists() else "<?php\n"

    if cfg.exists():
        shutil.copy2(cfg, imported)

    helper = """
/* WP Host Docker environment helper */
if (!function_exists('getenv_docker')) {
 function getenv_docker($env,$default) {
  if ($fileEnv=getenv($env.'_FILE')) return rtrim(file_get_contents($fileEnv), "\\r\\n");
  $val=getenv($env); return ($val!==false)?$val:$default;
 }
}
"""
    if "function getenv_docker" not in txt:
        txt = re.sub(r"<\?php\s*", "<?php\n" + helper + "\n", txt, count=1)

    defs = {
        "DB_NAME": "define('DB_NAME', getenv_docker('WORDPRESS_DB_NAME', 'wordpress'));",
        "DB_USER": "define('DB_USER', getenv_docker('WORDPRESS_DB_USER', 'wordpress'));",
        "DB_PASSWORD": "define('DB_PASSWORD', getenv_docker('WORDPRESS_DB_PASSWORD', ''));",
        "DB_HOST": "define('DB_HOST', getenv_docker('WORDPRESS_DB_HOST', 'db:3306'));",
    }
    for key,line in defs.items():
        pat = rf"define\s*\(\s*['\"]{key}['\"]\s*,.*?\)\s*;"
        if re.search(pat, txt, re.S):
            txt = re.sub(pat, line, txt, count=1, flags=re.S)
        else:
            txt = txt.replace("<?php", "<?php\n" + line, 1)

    pline = f"$table_prefix = '{prefix}';"
    if re.search(r"\$table_prefix\s*=\s*['\"][^'\"]+['\"]\s*;", txt):
        txt = re.sub(r"\$table_prefix\s*=\s*['\"][^'\"]+['\"]\s*;", pline, txt, count=1)
    else:
        txt += "\n" + pline + "\n"

    # Remove prior WP Host blocks so the import is idempotent.
    txt = re.sub(r"/\* WP Host reverse proxy HTTPS handling \*/.*?(?=\n\S)", "", txt, flags=re.S)
    txt = re.sub(r"/\* WP Host force HTTPS behind reverse proxy \*/.*?(?=\n\S)", "", txt, flags=re.S)

    # The hosting platform terminates TLS at NPM. Force HTTPS before any WordPress
    # bootstrap code executes so login/admin assets cannot fall back to http://.
    proxy = """
/* WP Host force HTTPS behind reverse proxy */
$_SERVER['HTTPS'] = 'on';
$_SERVER['SERVER_PORT'] = 443;
$_SERVER['HTTP_X_FORWARDED_PROTO'] = 'https';
if (!defined('FORCE_SSL_ADMIN')) define('FORCE_SSL_ADMIN', true);
"""

    # Insert immediately after PHP open tag, not near the bottom.
    txt = re.sub(r"<\?php\s*", "<?php\n" + proxy + "\n", txt, count=1)
    cfg.write_text(txt)
    return imported

def clean_htaccess(site):
    live = SITES / site / "wordpress"
    ht = live / ".htaccess"
    imported = live / ".htaccess.imported"
    if ht.exists():
        shutil.copy2(ht, imported)

    standard = """# BEGIN WordPress
<IfModule mod_rewrite.c>
RewriteEngine On
RewriteRule .* - [E=HTTP_AUTHORIZATION:%{HTTP:Authorization}]
RewriteBase /
RewriteRule ^index\\.php$ - [L]
RewriteCond %{REQUEST_FILENAME} !-f
RewriteCond %{REQUEST_FILENAME} !-d
RewriteRule . /index.php [L]
</IfModule>
# END WordPress
"""
    ht.write_text(standard)
    os.chown(ht, 33, 33)
    os.chmod(ht, 0o644)
    return "Imported .htaccess preserved; clean WordPress rewrite rules installed"

def quarantine_risky_components(site):
    live = SITES / site / "wordpress"
    content = live / "wp-content"
    qroot = live / ".platform-quarantine"
    qroot.mkdir(parents=True, exist_ok=True)
    moved = []

    mu = content / "mu-plugins"
    if mu.exists():
        dest = qroot / "mu-plugins"
        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(mu), str(dest))
        mu.mkdir(parents=True, exist_ok=True)
        moved.append("wp-content/mu-plugins")

    for name in ("object-cache.php","advanced-cache.php","db.php"):
        src = content / name
        if src.exists():
            dest = qroot / name
            shutil.move(str(src), str(dest))
            moved.append(f"wp-content/{name}")

    return moved

def restore_compatible_mu_plugins(site):
    live = SITES / site / "wordpress"
    qmu = live / ".platform-quarantine" / "mu-plugins"
    active = live / "wp-content" / "mu-plugins"
    active.mkdir(parents=True, exist_ok=True)
    results = []
    if not qmu.exists():
        return results

    for item in sorted(qmu.iterdir()):
        target = active / item.name
        try:
            shutil.move(str(item), str(target))
            # MU plugins always load; this is a real compatibility test.
            wpcli(site, ["option","get","home"], timeout=120, safe=False)
            results.append({"item":item.name,"ok":True,"detail":"Re-enabled successfully"})
        except Exception as exc:
            try:
                if target.exists():
                    shutil.move(str(target), str(qmu / item.name))
            except Exception:
                pass
            results.append({"item":item.name,"ok":False,"detail":str(exc)[-1200:]})
    return results

def backup(site):
    dest = SITES / site / "migration-backups" / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest.mkdir(parents=True, exist_ok=True)
    live = SITES / site / "wordpress"
    if live.exists():
        (dest / "wordpress").mkdir()
        run_cmd(["rsync","-a",str(live)+"/",str(dest/"wordpress")+"/"])
    e = envs(site)
    if e["root"]:
        with (dest / "database.sql").open("wb") as fh:
            p = subprocess.run(
                ["docker","exec",f"{site}-db","mariadb-dump","-uroot",f"-p{e['root']}",e["name"]],
                stdout=fh, stderr=subprocess.PIPE, timeout=1800
            )
            if p.returncode:
                (dest / "database.sql").unlink(missing_ok=True)
    return dest

def rollback_migration(site, backup_path=None):
    report = load_report(site)
    chosen = Path(backup_path or report.get("rollback_path",""))
    if not chosen.exists():
        backups = SITES / site / "migration-backups"
        options = sorted([p for p in backups.iterdir() if p.is_dir()], reverse=True) if backups.exists() else []
        if not options:
            raise RuntimeError("No migration rollback backup is available.")
        chosen = options[0]

    live = SITES / site / "wordpress"
    source_files = chosen / "wordpress"
    if source_files.exists():
        for p in list(live.iterdir()):
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
        run_cmd(["rsync","-a",str(source_files)+"/",str(live)+"/"])

    dump = chosen / "database.sql"
    if dump.exists():
        reset_db(site)
        import_sql(site, dump)

    DOCKER.containers.get(f"{site}-wp").restart()
    report["rollback_status"] = "completed"
    report["rollback_time"] = adelaide()
    report["rollback_used"] = str(chosen)
    save_report(site, report)
    return chosen

def reset_db(site):
    e = envs(site)
    if not e["root"]:
        raise RuntimeError("MariaDB root password unavailable")
    sql = (
        f"DROP DATABASE IF EXISTS `{e['name']}`;"
        f"CREATE DATABASE `{e['name']}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
        f"CREATE USER IF NOT EXISTS '{e['user']}'@'%' IDENTIFIED BY '{e['password']}';"
        f"ALTER USER '{e['user']}'@'%' IDENTIFIED BY '{e['password']}';"
        f"GRANT ALL PRIVILEGES ON `{e['name']}`.* TO '{e['user']}'@'%';"
        "FLUSH PRIVILEGES;"
    )
    run_cmd(["docker","exec",f"{site}-db","mariadb","-uroot",f"-p{e['root']}","-e",sql], timeout=120)

def import_sql(site, path):
    e = envs(site)
    cmd = ["docker","exec","-i",f"{site}-db","mariadb","-uroot",f"-p{e['root']}",e["name"]]
    if str(path).lower().endswith(".gz"):
        rd = subprocess.Popen(["gzip","-dc",str(path)], stdout=subprocess.PIPE)
        try:
            p = subprocess.run(cmd, stdin=rd.stdout, capture_output=True, timeout=3600)
        finally:
            rd.terminate()
    else:
        with open(path,"rb") as fh:
            p = subprocess.run(cmd, stdin=fh, capture_output=True, timeout=3600)
    if p.returncode:
        raise RuntimeError("SQL import failed: " + p.stderr.decode(errors="ignore")[-4000:])

def scalar(site, sql):
    e = envs(site)
    return run_cmd(
        ["docker","exec",f"{site}-db","mariadb","-N","-B","-uroot",f"-p{e['root']}",e["name"],"-e",sql],
        timeout=60
    ).strip()

def wpcli(site, args, timeout=1800, safe=True):
    c = DOCKER.containers.get(f"{site}-wp")
    q = " ".join(shlex.quote(str(x)) for x in args)
    safe_flags = " --skip-plugins --skip-themes" if safe else ""
    cmd = (
        "test -f /tmp/wp-cli.phar || "
        "curl -fsSL -o /tmp/wp-cli.phar https://raw.githubusercontent.com/wp-cli/builds/gh-pages/phar/wp-cli.phar; "
        "php /tmp/wp-cli.phar " + q + safe_flags + " --allow-root --path=/var/www/html"
    )
    ex = c.exec_run(["sh","-lc",cmd])
    out = ex.output.decode(errors="ignore")
    if ex.exit_code:
        raise RuntimeError("WP-CLI failed: " + out[-3500:])
    return out

def redirect_probe(url, max_hops=10):
    history = []
    current = url
    for _ in range(max_hops):
        r = requests.get(current, timeout=12, allow_redirects=False)
        history.append((r.status_code, current, r.headers.get("Location","")))
        if r.status_code not in (301,302,303,307,308):
            return True, history
        location = r.headers.get("Location")
        if not location:
            return True, history
        from urllib.parse import urljoin
        nxt = urljoin(current, location)
        if nxt == current:
            return False, history
        current = nxt
    return False, history

def verify(site, domain, prefix):
    checks = []
    c = DOCKER.containers.get(f"{site}-wp")

    php = (
        '$m=new mysqli(getenv("WORDPRESS_DB_HOST"),getenv("WORDPRESS_DB_USER"),'
        'getenv("WORDPRESS_DB_PASSWORD"),getenv("WORDPRESS_DB_NAME"));'
        'if($m->connect_errno){fwrite(STDERR,$m->connect_error);exit(1);}echo "OK";'
    )
    ex = c.exec_run(["php","-r",php])
    checks.append({
        "check":"Database connection",
        "ok":ex.exit_code == 0,
        "detail":ex.output.decode(errors="ignore")[-500:]
    })

    try:
        home = wpcli(site, ["option","get","home"], 120, safe=True).strip()
        checks.append({
            "check":"WordPress bootstrap",
            "ok":home.rstrip("/") == f"https://{domain}".rstrip("/"),
            "detail":home
        })
    except Exception as exc:
        checks.append({"check":"WordPress bootstrap","ok":False,"detail":str(exc)})

    # Detect self-redirects and long chains before requests follows them.
    try:
        ok, hist = redirect_probe(f"https://{domain}/", 10)
        detail = " | ".join(f"{s} {u} -> {loc}" for s,u,loc in hist[:6])
        checks.append({"check":"Redirect loop","ok":ok,"detail":detail or "No redirects"})
    except Exception as exc:
        checks.append({"check":"Redirect loop","ok":False,"detail":str(exc)})

    try:
        r = requests.get(f"https://{domain}/", timeout=15, allow_redirects=True)
        checks.append({"check":"Homepage","ok":r.status_code < 400,"detail":f"HTTP {r.status_code} -> {r.url}"})
    except Exception as exc:
        checks.append({"check":"Homepage","ok":False,"detail":str(exc)})

    try:
        r = requests.get(f"https://{domain}/wp-login.php", timeout=15, allow_redirects=True)
        checks.append({"check":"WP login","ok":r.status_code < 500,"detail":f"HTTP {r.status_code} -> {r.url}"})
        urls = re.findall(r"""href=['"]([^'"]*load-styles\.php[^'"]*)""", r.text, re.I)
        assets_ok = bool(urls) and all(u.startswith("https://") or u.startswith("/") for u in urls)
        checks.append({
            "check":"HTTPS assets",
            "ok":assets_ok,
            "detail":", ".join(urls[:3]) if urls else "load-styles.php not found"
        })
    except Exception as exc:
        checks.append({"check":"WP login","ok":False,"detail":str(exc)})
        checks.append({"check":"HTTPS assets","ok":False,"detail":"Login request failed"})

    try:
        r = requests.get(f"https://{domain}/wp-admin/", timeout=15, allow_redirects=True)
        login_ok = r.status_code < 500 and ("/wp-login.php" in r.url or "/wp-admin" in r.url)
        checks.append({"check":"WP admin","ok":login_ok,"detail":f"HTTP {r.status_code} -> {r.url}"})
    except Exception as exc:
        checks.append({"check":"WP admin","ok":False,"detail":str(exc)})

    # Verify up to three real published page permalinks.
    try:
        pages = scalar(
            site,
            f"SELECT post_name FROM `{prefix}posts` "
            "WHERE post_type='page' AND post_status='publish' AND post_name<>'' LIMIT 3;"
        ).splitlines()
        if not pages:
            checks.append({"check":"Pretty permalinks","ok":True,"detail":"No published pages available for test"})
        else:
            failures = []
            details = []
            for slug in pages:
                rr = requests.get(f"https://{domain}/{slug}/", timeout=15, allow_redirects=True)
                details.append(f"/{slug}/={rr.status_code}")
                if rr.status_code >= 400:
                    failures.append(slug)
            checks.append({
                "check":"Pretty permalinks",
                "ok":not failures,
                "detail":", ".join(details)
            })
    except Exception as exc:
        checks.append({"check":"Pretty permalinks","ok":False,"detail":str(exc)})

    try:
        r = requests.get(f"https://{domain}/wp-json/", timeout=15, allow_redirects=True)
        checks.append({"check":"REST API","ok":r.status_code < 400,"detail":f"HTTP {r.status_code}"})
    except Exception as exc:
        checks.append({"check":"REST API","ok":False,"detail":str(exc)})

    try:
        out = wpcli(site, ["core","verify-checksums"], 300, safe=True)
        checks.append({"check":"WordPress core checksum","ok":True,"detail":out[-800:]})
    except Exception as exc:
        checks.append({"check":"WordPress core checksum","ok":False,"detail":str(exc)})

    return checks

def run_migration(site, old_override=""):
    meta = site_meta(site)
    domain = meta.get("domain","").strip()
    if not domain:
        raise RuntimeError("Site domain missing")

    z,q = find_inputs(site)
    if not z or not q:
        raise RuntimeError(
            "One WordPress ZIP and one SQL dump are required. "
            "SFTP path /upload/migration-source/ is supported."
        )

    report = {
        "site":site,"domain":domain,"started":adelaide(),"finished":"",
        "status":"running","zip":z.name,"sql":q.name,"steps":[],"checks":[],
        "component_checks":[]
    }
    save_report(site, report)

    def step(name, fn):
        try:
            detail = fn()
            report["steps"].append({"name":name,"ok":True,"detail":str(detail or "OK")})
            save_report(site, report)
            return detail
        except Exception as exc:
            report["steps"].append({"name":name,"ok":False,"detail":str(exc)})
            report["status"] = "failed"
            report["finished"] = adelaide()
            save_report(site, report)
            raise

    step("Pre-flight package scan", lambda: preflight_package(z,q))

    src = source_dir(site)
    def preserve():
        names = []
        for p in (z,q):
            target = src / p.name
            if p.resolve() != target.resolve():
                shutil.copy2(p,target)
            names.append(target.name)
        return ", ".join(names)
    step("Preserve source package", preserve)
    z, q = src / z.name, src / q.name

    rollback = Path(step("Create rollback backup", lambda: backup(site)))
    report["rollback_path"] = str(rollback)
    save_report(site, report)

    try:
        DOCKER.containers.get(f"{site}-wp").stop(timeout=30)
    except Exception:
        pass

    work = SITES / site / ".migration-work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()

    def extract():
        safe_extract(z, work)
        root = wp_root(work)
        if not root:
            raise RuntimeError("ZIP does not contain wp-admin, wp-content and wp-includes")
        return root
    root = Path(step("Validate and extract WordPress ZIP", extract))
    prefix = table_prefix(root / "wp-config.php")
    report["table_prefix"] = prefix
    save_report(site, report)

    live = SITES / site / "wordpress"
    def deploy():
        live.mkdir(exist_ok=True)
        for p in list(live.iterdir()):
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
        run_cmd(["rsync","-a",str(root)+"/",str(live)+"/"])
        return f"table prefix {prefix}"
    step("Deploy imported WordPress files", deploy)

    step("Normalise Docker and HTTPS wp-config", lambda: normalise_config(site,prefix))
    step("Replace imported .htaccess", lambda: clean_htaccess(site))
    quarantined = step("Quarantine risky imported components", lambda: quarantine_risky_components(site))

    try:
        DOCKER.containers.get(f"{site}-wp").start()
    except Exception:
        run_cmd(["docker","compose","up","-d","wordpress"], cwd=SITES/site)
    time.sleep(3)

    step("Recreate clean target database", lambda: reset_db(site))
    step("Import SQL database", lambda: import_sql(site,q))

    table = f"{prefix}options"
    old_home = step(
        "Detect imported site URL",
        lambda: scalar(site, f"SELECT option_value FROM `{table}` WHERE option_name='home' LIMIT 1;")
    )
    old_site = scalar(site, f"SELECT option_value FROM `{table}` WHERE option_name='siteurl' LIMIT 1;")
    old = (old_override or old_home or old_site or "").strip().rstrip("/")
    new = f"https://{domain}"
    report["old_url"] = old
    report["new_url"] = new
    save_report(site, report)

    e = envs(site)
    def seturls():
        sql = f"UPDATE `{table}` SET option_value='{new}' WHERE option_name IN ('home','siteurl');"
        run_cmd(
            ["docker","exec",f"{site}-db","mariadb","-uroot",f"-p{e['root']}",e["name"],"-e",sql],
            timeout=60
        )
        return new
    step("Set WordPress home/siteurl", seturls)

    if old and old != new:
        step(
            "Serialized-safe domain replacement",
            lambda: wpcli(
                site,
                ["search-replace",old,new,"--all-tables-with-prefix","--skip-columns=guid","--precise"],
                safe=True
            )
        )
        alt = ("https://" + old[7:]) if old.startswith("http://") else ("http://" + old[8:]) if old.startswith("https://") else ""
        if alt and alt != new:
            try:
                wpcli(
                    site,
                    ["search-replace",alt,new,"--all-tables-with-prefix","--skip-columns=guid","--precise"],
                    safe=True
                )
            except Exception:
                pass

    step(
        "Repair WordPress permissions",
        lambda: (
            run_cmd(["chown","-R","33:33",str(live)]),
            run_cmd(["find",str(live),"-type","d","-exec","chmod","755","{}","+"]),
            run_cmd(["find",str(live),"-type","f","-exec","chmod","644","{}","+"]),
            "www-data / 755 directories / 644 files"
        )[-1]
    )

    step("Restart WordPress", lambda: DOCKER.containers.get(f"{site}-wp").restart())
    time.sleep(3)

    # Test quarantined MU plugins one at a time after base WordPress works.
    if "wp-content/mu-plugins" in quarantined:
        component_results = step("Test imported MU plugins", lambda: restore_compatible_mu_plugins(site))
        report["component_checks"] = component_results
        save_report(site, report)

    checks = step("Validate migrated website", lambda: verify(site,domain,prefix))
    report["checks"] = checks

    critical_names = {
        "Database connection","WordPress bootstrap","Redirect loop","Homepage",
        "WP login","HTTPS assets","WP admin","Pretty permalinks"
    }
    critical_failed = any(not c["ok"] and c["check"] in critical_names for c in checks)
    any_failed = any(not c["ok"] for c in checks)
    report["status"] = "blocked" if critical_failed else "attention" if any_failed else "success"
    report["finished"] = adelaide()
    save_report(site, report)
    shutil.rmtree(work, ignore_errors=True)
    return report
