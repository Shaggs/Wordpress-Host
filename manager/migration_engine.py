\
import os, re, json, time, shutil, zipfile, subprocess, shlex, gzip, posixpath
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

# --- wp-config.php handling: comment-aware, prefix from the database dump, config audit -------------------------------

def php_comment_spans(text):
    """(start, end) of every PHP comment, found with a small scanner that knows about quoted strings, so
    a '//' inside 'https://...' is not mistaken for a comment and a commented-out line is never treated as code."""
    spans, i, n = [], 0, len(text)
    while i < n:
        c, two = text[i], text[i:i + 2]
        if two == "/*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            spans.append((i, j))
            i = j
        elif two == "//" or (c == "#" and text[i + 1:i + 2] != "["):
            j = i
            while j < n and text[j] not in "\r\n" and text[j:j + 2] != "?>":
                j += 1
            spans.append((i, j))
            i = j
        elif c in "'\"":
            i += 1
            while i < n and text[i] != c:
                i += 2 if text[i] == "\\" else 1
            i += 1
        elif text.startswith("<<<", i):
            m = re.match(r"<<<[ \t]*(['\"]?)([A-Za-z_]\w*)\1[ \t]*\r?\n", text[i:i + 200])
            if m:
                end = re.search(r"\r?\n[ \t]*" + re.escape(m.group(2)) + r"\b", text[i + m.end():])
                i = n if not end else i + m.end() + end.end()
            else:
                i += 3
        else:
            i += 1
    return spans

def in_spans(spans, pos):
    return any(a <= pos < b for a, b in spans)

def active_matches(pattern, text, flags=0):
    spans = php_comment_spans(text)
    return [m for m in re.finditer(pattern, text, flags) if not in_spans(spans, m.start())]

def php_call_end(text, open_idx):
    """Index just after the ')' that closes the '(' at open_idx, skipping strings and comments; -1 if unbalanced."""
    depth, i, n = 0, open_idx, len(text)
    while i < n:
        c, two = text[i], text[i:i + 2]
        if two == "/*":
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if two == "//" or c == "#":
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if c in "'\"":
            i += 1
            while i < n and text[i] != c:
                i += 2 if text[i] == "\\" else 1
            i += 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1

PREFIX_ASSIGN = r"\$table_prefix\s*=\s*(?:'[^'\n]*'|\"[^\"\n]*\")\s*;"
WPSETTINGS_REQUIRE = r"(?:require_once|require)\b[^;\n]*wp-settings\.php[^;\n]*;"

def active_table_prefix(text):
    """The $table_prefix actually set by the code (never one inside a comment), or None."""
    for m in active_matches(r"\$table_prefix\s*=\s*(['\"])([A-Za-z0-9_]*)\1\s*;", text):
        return m.group(2) or None
    return None

def dump_prefix_info(path):
    """(prefixes that have options+users+posts tables, prefixes proven by a '<prefix>user_roles' option row) from an SQL dump."""
    opener = gzip.open if str(path).lower().endswith(".gz") else open
    tables, roles, in_options_insert = set(), set(), False
    with opener(path, "rt", errors="ignore") as fh:
        for line in fh:
            if line.startswith("CREATE TABLE"):
                m = re.match(r"CREATE TABLE (?:IF NOT EXISTS )?[`\"]([^`\"]+)[`\"]", line)
                if m:
                    tables.add(m.group(1))
            elif line.startswith(("INSERT", "REPLACE")):
                m = re.match(r"(?:INSERT|REPLACE)(?:\s+IGNORE)?\s+INTO\s+[`\"]([^`\"]+)[`\"]", line)
                in_options_insert = bool(m and m.group(1).endswith("options"))
            if in_options_insert and "user_roles'" in line:
                for k in re.finditer(r"'([A-Za-z0-9_]*)user_roles'", line):
                    roles.add(k.group(1))
            if in_options_insert and line.rstrip().endswith(";"):
                in_options_insert = False
    cands = {t[:-7] for t in tables if t.endswith("options") and (t[:-7] + "users") in tables and (t[:-7] + "posts") in tables}
    return cands, roles

def resolve_prefix(config_text, dump_path):
    """(prefix, note). The database dump is the authority: WordPress's own user_roles option proves the prefix.
    The imported wp-config.php may not set one at all (some hosts keep it in a separate file) or may disagree."""
    cfg = active_table_prefix(config_text) if config_text else None
    cands, roles = dump_prefix_info(dump_path)
    proven = sorted(roles & cands) or sorted(roles)
    if len(proven) == 1:
        p = proven[0]
    elif len(proven) > 1:
        if cfg in proven:
            return cfg, ""
        raise RuntimeError("the SQL dump contains more than one WordPress install (%s) and wp-config.php does not say which to use" % ", ".join(proven))
    elif len(cands) == 1:
        p = next(iter(cands))
    elif len(cands) > 1:
        if cfg in cands:
            return cfg, ""
        raise RuntimeError("the SQL dump contains more than one set of WordPress tables (%s) and wp-config.php does not say which to use" % ", ".join(sorted(cands)))
    else:
        return (cfg, "no WordPress tables were recognised in the dump") if cfg else ("wp_", "no table prefix found in wp-config.php or the dump - assuming wp_")
    if cfg == p:
        return p, ""
    if cfg is None:
        return p, "the imported wp-config.php sets no table prefix, so the one in the database dump is used"
    return p, "wp-config.php said '%s' but the database dump uses '%s' - using the database's" % (cfg, p)

def zip_config_text(zpath):
    with zipfile.ZipFile(zpath) as zf:
        names = [n for n in zf.namelist() if n.endswith("wp-config.php") and "/wp-content/" not in "/" + n]
        if not names:
            return ""
        return zf.read(min(names, key=lambda n: n.count("/"))).decode("utf-8", "ignore")

_PATH_TOKEN = r"(?:__DIR__|dirname\(\s*__FILE__\s*\)|dirname\(\s*ABSPATH\s*\)|ABSPATH|'[^'$]*'|\"[^\"$]*\")"

def eval_path_expr(arg):
    """Resolve a simple PHP path expression (__DIR__, dirname(__FILE__), ABSPATH, string literals joined with '.') to the
    path it will have inside the container, or None if it is not a simple expression."""
    a = arg.strip()
    while a.startswith("(") and a.endswith(")") and php_call_end(a, 0) == len(a):
        a = a[1:-1].strip()
    if not re.fullmatch(_PATH_TOKEN + r"(?:\s*\.\s*" + _PATH_TOKEN + r")*", a):
        return None
    out = ""
    for tok in re.findall(_PATH_TOKEN, a):
        if tok in ("__DIR__",) or tok.startswith("dirname(") and "__FILE__" in tok:
            out += "/var/www/html"
        elif tok == "ABSPATH":
            out += "/var/www/html/"
        elif tok.startswith("dirname(") and "ABSPATH" in tok:
            out += "/var/www"
        else:
            out += tok[1:-1]
    if not out.startswith("/"):
        out = "/var/www/html/" + out
    return posixpath.normpath(out)

def disable_missing_requires(txt, live):
    """Comment out any require/require_once whose file will not exist on this server - typically a file the old host kept
    outside the web folder (e.g. ../configs/wp-config-hosting.php). The original line is kept in the comment.
    Returns (new text, descriptions)."""
    notes, pieces, last = [], [], 0
    spans = php_comment_spans(txt)
    for m in re.finditer(r"(?m)^([ \t]*)(?:@[ \t]*)?(require_once|require)\b([^\n;]*);", txt):
        if in_spans(spans, m.start()):
            continue
        path = eval_path_expr(m.group(3))
        if path is None or path.endswith(("/wp-settings.php", "/wp-load.php")):
            continue
        if path.startswith("/var/www/html/"):
            if (live / path[len("/var/www/html/"):]).exists():
                continue
        elif path.startswith(("/usr/", "/etc/", "/opt/", "/tmp/", "/var/lib/")):
            continue
        original = m.group(0).strip().replace("?>", "? >")
        pieces += [txt[last:m.start()],
                   m.group(1) + "// WP Host: disabled - the file it loads does not exist on this server (it was outside the website folder or missing from the import). Original line: " + original]
        last = m.end()
        notes.append("disabled a require of a missing file (%s)" % path)
    if not notes:
        return txt, []
    return "".join(pieces) + txt[last:], notes

STANDARD_WP_CONFIG = """<?php
/* WP Host standard configuration (the import contained no wp-config.php) */
define('DB_CHARSET', 'utf8mb4');
define('DB_COLLATE', '');
$table_prefix = 'wp_';
define('WP_DEBUG', false);
/* That's all, stop editing! Happy publishing. */
if ( ! defined( 'ABSPATH' ) ) {
    define( 'ABSPATH', __DIR__ . '/' );
}
require_once ABSPATH . 'wp-settings.php';
"""

DB_DEFINE_KEYS = ("DB_NAME", "DB_USER", "DB_PASSWORD", "DB_HOST")

def audit_config(txt, live):
    """Static checks that catch the faults that made migrated sites return HTTP 500. Returns a list of problems."""
    problems = []
    for key in DB_DEFINE_KEYS:
        if not active_matches(r"define\s*\(\s*['\"]%s['\"]\s*," % key, txt):
            problems.append("%s is not defined" % key)
    call = next(iter(active_matches(r"(?<!function )getenv_docker\s*\(", txt)), None)
    defn = next(iter(active_matches(r"function\s+getenv_docker", txt)), None)
    if call and (defn is None or call.start() < defn.start()):
        problems.append("getenv_docker() is used before it is defined")
    req = next(iter(active_matches(WPSETTINGS_REQUIRE, txt)), None)
    if req is None:
        problems.append("wp-config.php never loads wp-settings.php")
    elif not [m for m in active_matches(PREFIX_ASSIGN, txt) if m.start() < req.start()]:
        problems.append("$table_prefix is not set before WordPress loads")
    if disable_missing_requires(txt, live)[1]:
        problems.append("it still requires a file that does not exist")
    return problems

def normalise_config(site, prefix):
    live = SITES / site / "wordpress"
    cfg = live / "wp-config.php"
    imported = live / "wp-config.imported.php"
    notes = []

    if cfg.exists():
        txt = cfg.read_text(errors="ignore")
        shutil.copy2(cfg, imported)
    else:
        sample = live / "wp-config-sample.php"
        txt = sample.read_text(errors="ignore") if sample.exists() else STANDARD_WP_CONFIG
        notes.append("the import had no wp-config.php (some hosts keep it above the web folder) - built one from "
                     + ("wp-config-sample.php" if sample.exists() else "a standard template"))

    helper = """
/* WP Host Docker environment helper */
if (!function_exists('getenv_docker')) {
 function getenv_docker($env,$default) {
  if ($fileEnv=getenv($env.'_FILE')) return rtrim(file_get_contents($fileEnv), "\\r\\n");
  $val=getenv($env); return ($val!==false)?$val:$default;
 }
}
"""
    defs = {
        "DB_NAME": "define('DB_NAME', getenv_docker('WORDPRESS_DB_NAME', 'wordpress'));",
        "DB_USER": "define('DB_USER', getenv_docker('WORDPRESS_DB_USER', 'wordpress'));",
        "DB_PASSWORD": "define('DB_PASSWORD', getenv_docker('WORDPRESS_DB_PASSWORD', ''));",
        "DB_HOST": "define('DB_HOST', getenv_docker('WORDPRESS_DB_HOST', 'db:3306'));",
    }
    for key, line in defs.items():
        m = next(iter(active_matches(r"define\s*\(\s*['\"]%s['\"]\s*," % key, txt)), None)
        if m:
            end = php_call_end(txt, txt.index("(", m.start()))
            semi = re.compile(r"\s*;").match(txt, end) if end > 0 else None
            if not semi:
                raise RuntimeError("could not read the %s line in wp-config.php" % key)
            txt = txt[:m.start()] + line + txt[semi.end():]
        else:
            txt = txt.replace("<?php", "<?php\n" + line, 1)
            notes.append("%s was not defined in wp-config.php (taken from the container settings instead)" % key)

    # The helper must be defined ABOVE every getenv_docker() call, so it goes in after the defines (which puts it above them).
    call = next(iter(active_matches(r"(?<!function )getenv_docker\s*\(", txt)), None)
    defn = next(iter(active_matches(r"function\s+getenv_docker", txt)), None)
    if defn is None or (call is not None and call.start() < defn.start()):
        txt = re.sub(r"<\?php\s*", lambda m: "<?php\n" + helper + "\n", txt, count=1)

    # $table_prefix: correct every active assignment, and make sure one is set BEFORE wp-settings.php loads WordPress.
    pline = "$table_prefix = '%s';" % prefix
    for m in reversed(active_matches(PREFIX_ASSIGN, txt)):
        txt = txt[:m.start()] + pline + txt[m.end():]
    req = next(iter(active_matches(WPSETTINGS_REQUIRE, txt)), None)
    if req is None:
        if not active_matches(PREFIX_ASSIGN, txt):
            txt += "\n" + pline + "\n"
        notes.append("no line loading wp-settings.php was found in wp-config.php")
    elif not [m for m in active_matches(PREFIX_ASSIGN, txt) if m.start() < req.start()]:
        marker = None
        for mm in re.finditer(r"(?m)^[ \t]*/\*[^\n]*stop editing[^\n]*", txt[:req.start()]):
            marker = mm
        at = marker.start() if marker else txt.rfind("\n", 0, req.start()) + 1
        txt = txt[:at] + pline + "\n" + txt[at:]
        notes.append("the table prefix was not set before WordPress loads - added")

    # Files that were outside the old web folder (or missing) cannot be loaded here.
    txt, disabled = disable_missing_requires(txt, live)
    notes += disabled

    # Remove WP Host blocks left by an earlier migration so this import is idempotent, then add the HTTPS block.
    txt = re.sub(r"/\* WP Host reverse proxy HTTPS handling \*/.*?(?=\n\S)", "", txt, flags=re.S)
    txt = re.sub(r"/\* WP Host force HTTPS behind reverse proxy \*/\r?\n\$_SERVER\['HTTPS'\] = 'on';\r?\n\$_SERVER\['SERVER_PORT'\] = 443;\r?\n"
                 r"\$_SERVER\['HTTP_X_FORWARDED_PROTO'\] = 'https';\r?\nif \(!defined\('FORCE_SSL_ADMIN'\)\) define\('FORCE_SSL_ADMIN', true\);\r?\n?", "", txt)

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
    txt = re.sub(r"<\?php\s*", lambda m: "<?php\n" + proxy + "\n", txt, count=1)
    cfg.write_text(txt)

    problems = audit_config(txt, live)
    if problems:
        raise RuntimeError("wp-config.php could not be made safe to load: " + "; ".join(problems))
    return "; ".join(notes) if notes else "OK (table prefix '%s')" % prefix


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

def quarantine_dir(site):
    """Quarantined imports live next to the web root, never inside it (so they cannot be requested)."""
    q = SITES / site / "quarantine"
    q.mkdir(parents=True, exist_ok=True)
    os.chmod(q, 0o700)
    return q

def quarantine_risky_components(site):
    live = SITES / site / "wordpress"
    content = live / "wp-content"
    qroot = quarantine_dir(site)
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
    qmu = quarantine_dir(site) / "mu-plugins"
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

VIEW_RE = re.compile(r'^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:ALGORITHM=\w+\s+)?(?:DEFINER=\S+\s+)?'
                     r'(?:SQL\s+SECURITY\s+\w+\s+)?VIEW\s+`([^`]+)`', re.I | re.S)
DEFINER_RE = re.compile(r'DEFINER=`[^`]+`@`[^`]+`\s*')


def prepare_sql(path, out):
    """Make dumps from any backup tool import cleanly:
    - foreign-key checks off during the import (tables can come in any order)
    - views moved to the end, ordered by dependency, DEFINER removed
    Everything else is copied unchanged. Returns the number of views moved."""
    opener = gzip.open if str(path).lower().endswith(".gz") else open
    views, order, buf = {}, [], []
    with opener(path, "rt", encoding="utf-8", errors="surrogateescape") as fi, \
         open(out, "w", encoding="utf-8", errors="surrogateescape") as fo:
        fo.write("SET FOREIGN_KEY_CHECKS=0;\nSET UNIQUE_CHECKS=0;\n")
        for line in fi:
            if not buf and (line.startswith("--") or not line.strip()):
                fo.write(line)
                continue
            buf.append(line)
            if line.rstrip().endswith(";"):
                stmt = "".join(buf)
                buf = []
                m = VIEW_RE.match(stmt)
                if m:
                    name = m.group(1)
                    if name not in views:
                        order.append(name)
                    views[name] = DEFINER_RE.sub("", stmt)
                else:
                    fo.write(stmt)
        if buf:
            fo.write("".join(buf))
        done, seen = [], set()
        def visit(n, stack=()):
            if n in seen or n in stack:
                return
            for d in order:
                if d != n and f"`{d}`" in views[n]:
                    visit(d, stack + (n,))
            seen.add(n)
            done.append(n)
        for n in order:
            visit(n)
        if done:
            fo.write("\n-- Views, moved here and ordered by dependency\n")
        for n in done:
            fo.write(f"DROP VIEW IF EXISTS `{n}`;\n" + views[n].rstrip() + "\n")
        fo.write("\nSET UNIQUE_CHECKS=1;\nSET FOREIGN_KEY_CHECKS=1;\n")
    return len(done)


def import_sql(site, path):
    e = envs(site)
    cmd = ["docker","exec","-i",f"{site}-db","mariadb","-uroot",f"-p{e['root']}",e["name"]]
    prepared = Path(str(path) + ".import.sql")
    try:
        moved = prepare_sql(path, prepared)
        with open(prepared, "rb") as fh:
            p = subprocess.run(cmd, stdin=fh, capture_output=True, timeout=3600)
    finally:
        prepared.unlink(missing_ok=True)
    if p.returncode:
        raise RuntimeError("SQL import failed: " + p.stderr.decode(errors="ignore")[-4000:])
    return "imported" + (f" ({moved} views reordered)" if moved else "")


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
        # WP 5+/7.x login pages may load separate stylesheets instead of load-styles.php: check every one.
        urls = re.findall(r"""<link[^>]+rel=['"]stylesheet['"][^>]*href=['"]([^'"]+)""", r.text, re.I)
        urls += re.findall(r"""href=['"]([^'"]*load-styles\.php[^'"]*)""", r.text, re.I)
        urls = list(dict.fromkeys(urls))
        assets_ok = bool(urls) and all(u.startswith("https://") or (u.startswith("/") and not u.startswith("//")) for u in urls)
        checks.append({
            "check":"HTTPS assets",
            "ok":assets_ok,
            "detail":(f"{len(urls)} stylesheet(s): " + ", ".join(urls[:3])) if urls else "no stylesheets found on wp-login.php"
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

# --- legacy-php8-readiness ---
REMOVED_PHP8 = (r"(?<![\w>:$.\\])(create_function|each|money_format|ereg|eregi|ereg_replace|eregi_replace"
                r"|split|spliti|mysql_[a-z_]+|get_magic_quotes_gpc|get_magic_quotes_runtime|set_magic_quotes_runtime)\s*\(")
PHP8_MIN_CORE = (5, 6)   # first core release that boots on PHP 8

def wp_core_version(live):
    try:
        t = (Path(live) / "wp-includes" / "version.php").read_text(errors="ignore")
    except OSError:
        return ""
    m = re.search(r"\$wp_version\s*=\s*'([^']+)'", t)
    return m.group(1) if m else ""

def php8_scan(live):
    """Removed-function calls outside comments (function definitions such as 'function each(' are skipped)."""
    hits = []
    content = Path(live) / "wp-content"
    for sub in ("plugins", "themes"):
        for f in (content / sub).rglob("*.php"):
            if "php8-old" in f.parts or not f.is_file():   # e.g. a directory named jmespath.php
                continue
            try:
                t = f.read_text(errors="ignore")
            except OSError:
                continue
            for m in active_matches(REMOVED_PHP8, t):
                if re.search(r"function\s*&?\s*$", t[max(0, m.start() - 20):m.start()]):
                    continue
                hits.append(f"{f.relative_to(content)}:{t.count(chr(10), 0, m.start()) + 1} {m.group(1)}()")
    return hits

def php8_readiness(site):
    live = SITES / site / "wordpress"
    res = {"core": "", "syntax_errors": [], "removed_calls": [], "updated": [], "deactivated": [], "load": ""}

    ver = wp_core_version(live)
    major = tuple(int(x) for x in re.findall(r"\d+", ver)[:2]) or (0, 0)
    if major < PHP8_MIN_CORE:
        bk = SITES / site / "migration-backups" / f"core-{ver or 'unknown'}-{time.strftime('%Y%m%d-%H%M%S')}.tgz"
        bk.parent.mkdir(parents=True, exist_ok=True)
        run_cmd(["tar", "czf", str(bk), "-C", str(live), "wp-admin", "wp-includes",
                 *sorted(p.name for p in live.glob("*.php") if p.name != "wp-config.php")])
        wpcli(site, ["core", "download", "--skip-content", "--force"], timeout=600, safe=False)  # does not boot WP
        run_cmd(["chown", "-R", "33:33", str(live / "wp-admin"), str(live / "wp-includes")])
        wpcli(site, ["core", "update-db"], timeout=600, safe=True)
        res["core"] = f"{ver} -> {wp_core_version(live)} (old core: {bk.name})"
    else:
        res["core"] = f"{ver} (no upgrade needed)"

    ex = DOCKER.containers.get(f"{site}-wp").exec_run(["sh", "-c",
        "find /var/www/html/wp-content/plugins /var/www/html/wp-content/themes -name '*.php' "
        "-not -path '*/php8-old/*' -print0 | xargs -0 -n1 -P4 php -l 2>&1 | grep -E 'Parse error|Fatal error' || true"])
    res["syntax_errors"] = [l.strip() for l in ex.output.decode(errors="ignore").splitlines() if l.strip()][:50]
    res["removed_calls"] = php8_scan(live)[:200]

    tried = set()
    for _ in range(20):
        try:
            wpcli(site, ["eval", "echo 'php8-load-ok';"], timeout=300, safe=False)
            res["load"] = "WordPress loads with all active plugins and the theme"
            break
        except RuntimeError as exc:
            msg = str(exc)
            m = re.search(r"wp-content/plugins/([^/\s'\"]+)/", msg.split("Fatal error", 1)[-1])
            if not m or m.group(1) in tried:
                res["load"] = "still failing (theme/core, or a plugin that could not be isolated): " + msg[-600:]
                break
            slug = m.group(1)
            tried.add(slug)
            old = live / "wp-content" / "php8-old"
            old.mkdir(exist_ok=True)
            (old / ".htaccess").write_text("Require all denied\n")
            dest = old / slug
            if not dest.exists():
                shutil.copytree(live / "wp-content" / "plugins" / slug, dest, symlinks=True)
            try:
                wpcli(site, ["plugin", "update", slug], timeout=300, safe=True)
                run_cmd(["chown", "-R", "33:33", str(live / "wp-content" / "plugins" / slug)])
                wpcli(site, ["eval", "echo 'ok';"], timeout=300, safe=False)
                res["updated"].append(slug)
                continue
            except RuntimeError:
                pass
            wpcli(site, ["plugin", "deactivate", slug], timeout=120, safe=True)
            res["deactivated"].append(slug)
    return res

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

    resolved = {}

    def check_prefix():
        p, note = resolve_prefix(zip_config_text(z), q)
        resolved["prefix"] = p
        return f"table prefix '{p}'" + (f" - {note}" if note else "")
    step("Check database dump and table prefix", check_prefix)

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
    prefix = resolved.get("prefix") or table_prefix(root / "wp-config.php")
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
        # Backup tools (e.g. ManageWP's mwp_db/) put the database dump inside the
        # site files. Never publish it: anyone could download the whole database.
        run_cmd(["rsync","-a",
                 "--exclude=/mwp_db/",
                 "--exclude=/*.sql", "--exclude=/*.sql.gz",
                 "--exclude=/wp-content/*.sql", "--exclude=/wp-content/*.sql.gz",
                 str(root)+"/", str(live)+"/"])
        return f"table prefix {prefix} (database dumps not copied to the website)"
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

    def _php8():
        try:
            return php8_readiness(site)
        except Exception as exc:   # report, don't abort: the search-replace below is the real gate
            return {"core": "", "syntax_errors": [], "removed_calls": [], "updated": [], "deactivated": [],
                    "load": f"readiness step error: {exc}"[:600]}
    php8 = step("Legacy core and PHP 8 readiness", _php8)
    report["php8"] = php8
    save_report(site, report)

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

    def lint_config():
        ex = DOCKER.containers.get(f"{site}-wp").exec_run(["php", "-l", "/var/www/html/wp-config.php"])
        if ex.exit_code != 0:
            raise RuntimeError("wp-config.php has a syntax error: " + ex.output.decode(errors="ignore")[-400:])
        return "no syntax errors"
    step("Check wp-config.php", lint_config)

    # Test quarantined MU plugins one at a time after base WordPress works.
    if "wp-content/mu-plugins" in quarantined:
        component_results = step("Test imported MU plugins", lambda: restore_compatible_mu_plugins(site))
        report["component_checks"] = component_results
        save_report(site, report)

    checks = step("Validate migrated website", lambda: verify(site,domain,prefix))
    report["checks"] = checks
    php8_bad = php8["deactivated"] or php8["syntax_errors"] or not php8["load"].startswith("WordPress loads")
    checks.append({
        "check": "PHP 8 readiness",
        "ok": not php8_bad,
        "detail": "; ".join(x for x in (
            "core " + php8["core"],
            php8["load"][:300],
            ("updated: " + ", ".join(php8["updated"])) if php8["updated"] else "",
            ("DEACTIVATED (fatal on PHP 8): " + ", ".join(php8["deactivated"])) if php8["deactivated"] else "",
            (f"{len(php8['syntax_errors'])} file(s) with PHP 8 syntax errors") if php8["syntax_errors"] else "",
            (f"{len(php8['removed_calls'])} call(s) to functions PHP 8 removed") if php8["removed_calls"] else "",
        ) if x),
    })

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
