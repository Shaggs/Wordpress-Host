#!/usr/bin/env python3
"""Add the www. name (or the bare name) to existing sites' proxy hosts and SSL certificates.

  sudo python3 add-www-existing.py          dry run: show what would change
  sudo python3 add-www-existing.py --go     do it

Only proxy hosts that forward to a site container (<site>-wp / -web / -app) are touched, and only
when the missing name resolves in DNS. For each: a new Let's Encrypt certificate covering both
names is requested; only if that succeeds is it attached to the proxy host with both names.
If it fails, the host is left exactly as it was. Old certificates are listed, not deleted.
Reads WP_NPM_URL / WP_NPM_EMAIL / WP_NPM_PASSWORD from /opt/wp-host/manager/manager.env.
"""
import json, socket, sys, urllib.request, urllib.error

ENV = "/opt/wp-host/manager/manager.env"
GO = "--go" in sys.argv
SITE_SUFFIXES = ("-wp", "-web", "-app")


def env():
    vals = {}
    try:
        for line in open(ENV):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        sys.exit(f"cannot read {ENV} (run with sudo)")
    return vals


E = env()
URL = E.get("WP_NPM_URL", "http://127.0.0.1:81").rstrip("/")
TOKEN = None


def api(method, path, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if TOKEN:
        req.add_header("Authorization", "Bearer " + TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read()[:300].decode(errors='replace')}")


def resolves(name):
    try:
        return bool(socket.getaddrinfo(name, 80, type=socket.SOCK_STREAM))
    except OSError:
        return False


def twin(d):
    d = d.lower().rstrip(".")
    return d[4:] if d.startswith("www.") else "www." + d


TOKEN = api("POST", "/api/tokens", {"identity": E.get("WP_NPM_EMAIL", ""), "secret": E.get("WP_NPM_PASSWORD", "")}).get("token")
if not TOKEN:
    sys.exit("Nginx Proxy Manager login failed - check WP_NPM_* in manager.env")

hosts = api("GET", "/api/nginx/proxy-hosts")
todo = []
for h in hosts:
    fh = h.get("forward_host", "")
    names = [n.lower() for n in h.get("domain_names", [])]
    if not fh.endswith(SITE_SUFFIXES) or not names or not h.get("enabled", True):
        continue
    main = names[0]
    alt = twin(main)
    if alt in names:
        print(f"  ok       {', '.join(names)}")
        continue
    if not resolves(alt):
        print(f"  skip     {main}: {alt} has no DNS record (add one, then re-run)")
        continue
    todo.append((h, names + [alt], alt))
    print(f"  {'adding' if GO else 'would add'} {alt} to {main}  (proxy #{h['id']}, current certificate #{h.get('certificate_id') or 'none'})")

if not GO:
    print(f"\n{len(todo)} site(s) to update. Run again with --go to apply.")
    sys.exit(0)

done, failed, old_certs = 0, 0, []
for h, names, alt in todo:
    try:
        cert = api("POST", "/api/nginx/certificates",
                   {"provider": "letsencrypt", "nice_name": names[0], "domain_names": names,
                    "meta": {"dns_challenge": False}}, timeout=180)
        cid = cert.get("id")
        if not cid:
            raise RuntimeError("certificate created without an id")
        payload = {k: h.get(k) for k in (
            "forward_scheme", "forward_host", "forward_port", "access_list_id", "caching_enabled",
            "block_exploits", "advanced_config", "meta", "allow_websocket_upgrade", "http2_support",
            "hsts_enabled", "hsts_subdomains", "enabled", "locations")}
        payload.update({"domain_names": names, "certificate_id": cid, "ssl_forced": True,
                        "access_list_id": h.get("access_list_id") or 0})
        payload = {k: v for k, v in payload.items() if v is not None}
        api("PUT", f"/api/nginx/proxy-hosts/{h['id']}", payload)
        done += 1
        print(f"  done     {', '.join(names)}  (certificate #{cid})")
        if h.get("certificate_id"):
            old_certs.append(h["certificate_id"])
    except Exception as e:
        failed += 1
        print(f"  FAILED   {names[0]}: {e}  - host left unchanged")

print(f"\n{done} updated, {failed} failed.")
if old_certs:
    used = {x.get("certificate_id") for x in api("GET", "/api/nginx/proxy-hosts")}
    unused = [c for c in old_certs if c not in used]
    if unused:
        print("Old certificates no longer used (safe to delete in NPM > SSL Certificates): "
              + ", ".join(f"#{c}" for c in unused))
