#!/usr/bin/env python3
"""Add 'Made & maintained by Tel5' footer credit to cafe + restaurant sites and templates.
Idempotent (marker: tel5-credit). Backs up each changed file to <file>.bak-credit-<stamp>."""
import io, shutil, sys, time, zipfile
from pathlib import Path

STAMP = time.strftime("%Y%m%d-%H%M%S")
MARK = "tel5-credit"
LINK = '<a class="tel5-credit" href="https://tel5.com.au" target="_blank" rel="noopener">Tel5</a>'

CAFE = {
    "layouts/layout-1.php": [(
        "<p>&copy; <?= date('Y') ?> <?= $e($c['name']) ?></p>",
        "<p>&copy; <?= date('Y') ?> <?= $e($c['name']) ?> &middot; Made &amp; maintained by " + LINK + "</p>")],
    "layouts/layout-2.php": [(
        "<span>&copy; <?= date('Y') ?> <?= $e($c['name']) ?></span>",
        "<span>&copy; <?= date('Y') ?> <?= $e($c['name']) ?> &middot; Made &amp; maintained by " + LINK + "</span>")],
    "layouts/layout-3.php": [(
        "<span>&copy; <?= date('Y') ?> <?= $e($c['name']) ?></span>",
        "<span>&copy; <?= date('Y') ?> <?= $e($c['name']) ?> &middot; Made &amp; maintained by " + LINK + "</span>")],
}
REST = {
    "public/index.html": [(
        "esc(b.phone) + '</a></div>';",
        "esc(b.phone) + '</a>' +\n      '<span>Made &amp; maintained by "
        + LINK.replace("'", "\\'") + "</span></div>';")],
}

def apply(text, rules, label):
    if MARK in text:
        print(f"  skip (already patched): {label}"); return None
    for old, new in rules:
        n = text.count(old)
        if n != 1:
            sys.exit(f"ABORT: anchor found {n}x in {label}: {old!r}")
        text = text.replace(old, new)
    return text

def patch_dir(root, mapping):
    for rel, rules in mapping.items():
        p = Path(root) / rel
        new = apply(p.read_text(), rules, str(p))
        if new is None: continue
        shutil.copy2(p, f"{p}.bak-credit-{STAMP}")
        with open(p, "w") as f: f.write(new)   # in-place: keeps owner/mode
        print(f"  patched {p}")

def patch_zip(zpath, mapping):
    zpath = Path(zpath)
    with zipfile.ZipFile(zpath) as z:
        infos = z.infolist(); data = {i.filename: z.read(i) for i in infos}
    changed = False
    for rel, rules in mapping.items():
        new = apply(data[rel].decode(), rules, f"{zpath}:{rel}")
        if new is not None: data[rel] = new.encode(); changed = True
    if not changed: return
    shutil.copy2(zpath, f"{zpath}.bak-credit-{STAMP}")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        for i in infos: out.writestr(i, data[i.filename])
    with open(zpath, "wb") as f: f.write(buf.getvalue())
    print(f"  patched {zpath}")

print("cafe site: coffee-factory");  patch_dir("/opt/wp-host/sites/coffee-factory/site", CAFE)
print("restaurant site: res");       patch_dir("/opt/wp-host/sites/res/site", REST)
for z in ["/home/tel5/Wordpress-Host/manager/site_templates/cafe.zip", "/opt/wp-host/manager/site_templates/cafe.zip"]:
    print("template", z); patch_zip(z, CAFE)
for z in ["/home/tel5/Wordpress-Host/manager/site_templates/restaurant.zip", "/opt/wp-host/manager/site_templates/restaurant.zip"]:
    print("template", z); patch_zip(z, REST)
