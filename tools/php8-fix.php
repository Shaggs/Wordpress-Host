<?php
/*
 * PHP 8 compatibility fixer for one WordPress site (runs inside a wordpress:cli container
 * that has the site's files mounted, as root).
 *
 *   php php8-fix.php update <slug> [<slug> ...]   replace free plugins with the latest wordpress.org release
 *   php php8-fix.php fix                          patch PHP-8-fatal syntax in wp-content (plugins, themes, mu-plugins)
 *   php php8-fix.php check                        only list files PHP 8 refuses
 *
 * Every changed file is copied to <file>.bak-php8 first; replaced plugins are moved to
 * wp-content/php8-old/<slug>/ (outside plugins/, so they are not loaded).
 */
define('WPC', getenv('WPC') ?: '/var/www/html/wp-content');
$cmd = $argv[1] ?? 'check';

function lint($file) {
    $out = shell_exec('php -l ' . escapeshellarg($file) . ' 2>&1');
    if (strpos($out, 'No syntax errors') !== false) return null;
    if (preg_match('/(?:Parse|Fatal) error:\s*(.*?) in .*? on line (\d+)/s', $out, $m)) return [trim($m[1]), (int)$m[2]];
    return [trim($out), 0];
}

function php_files() {
    $files = [];
    foreach (['plugins', 'themes', 'mu-plugins'] as $d) {
        if (!is_dir(WPC . "/$d")) continue;
        $it = new RecursiveIteratorIterator(new RecursiveDirectoryIterator(WPC . "/$d", FilesystemIterator::SKIP_DOTS));
        foreach ($it as $f) if (substr($f, -4) === '.php') $files[] = (string)$f;
    }
    foreach (glob(WPC . '/*.php') as $f) $files[] = $f;  // drop-ins: advanced-cache.php, object-cache.php, db.php
    sort($files);
    return $files;
}

/* $str{0} / $this->buf{$i} / $a['x']{1}  ->  [ ]  (only on the line PHP complained about) */
function fix_curly($line) {
    $re = '/(\$[A-Za-z_]\w*(?:(?:->|::)\$?[A-Za-z_]\w*|\[[^\[\]\n]*\])*)\{([^{}\n]*)\}/';
    return preg_replace($re, '$1[$2]', $line);
}

/* a ? b : c ? d : e  ->  (a ? b : c) ? d : e   which is exactly how PHP 7 ran it (left-associative).
   Done with PHP's own tokenizer so strings and nested brackets are handled correctly. */
function fix_ternary($src, $lineNo) {
    $toks = token_get_all($src);
    $n = count($toks);
    $line = 1; $pos = []; $text = [];
    for ($i = 0; $i < $n; $i++) {
        $t = $toks[$i];
        $text[$i] = is_array($t) ? $t[1] : $t;
        $pos[$i] = $line;
        $line += substr_count($text[$i], "\n");
    }
    // find statements that contain two top-level '?' ... ':' pairs on/around the reported line
    for ($i = 0; $i < $n; $i++) {
        if ($text[$i] !== '?' || abs($pos[$i] - $lineNo) > 3) continue;
        // walk back to the start of the expression (depth 0: after ; { } ( , = return echo .= etc.)
        $depth = 0; $start = $i;
        for ($j = $i - 1; $j >= 0; $j--) {
            $s = $text[$j];
            if ($s === ')' || $s === ']') { $depth++; continue; }
            if ($s === '(' || $s === '[') { if ($depth === 0) break; $depth--; continue; }
            if ($depth === 0 && (in_array($s, [';', '{', '}', ',', '=', '.=', '+=', '-=', '=>', 'return', 'echo', 'print'], true)
                                 || (is_array($toks[$j]) && in_array($toks[$j][0], [T_RETURN, T_ECHO, T_PRINT, T_OPEN_TAG, T_CONCAT_EQUAL, T_DOUBLE_ARROW], true)))) break;
            $start = $j;
        }
        // walk forward: first ':' at depth 0 after this '?', then a second '?' at depth 0
        $depth = 0; $colon = null; $q2 = null;
        for ($j = $i + 1; $j < $n; $j++) {
            $s = $text[$j];
            if ($s === '(' || $s === '[' || $s === '{') { $depth++; continue; }
            if ($s === ')' || $s === ']' || $s === '}') { if ($depth === 0) break; $depth--; continue; }
            if ($depth > 0) continue;
            if ($s === ';' || $s === ',') break;
            if ($s === '?' && $colon === null) { $colon = -1; break; } // nested in the middle: not our pattern
            if ($s === ':' && $colon === null) { $colon = $j; continue; }
            if ($s === '?' && $colon !== null) { $q2 = $j; break; }
        }
        if ($colon > 0 && $q2) {
            while ($start < $i && is_array($toks[$start]) && $toks[$start][0] === T_WHITESPACE) $start++;
            $end = $q2 - 1;
            while ($end > $colon && is_array($toks[$end]) && $toks[$end][0] === T_WHITESPACE) $end--;
            $text[$start] = '(' . $text[$start];
            $text[$end] = $text[$end] . ')';
            return implode('', $text);
        }
    }
    return null;
}

function fix_file($file) {
    $orig = file_get_contents($file);
    $src = $orig;
    $tmp = $file . '.php8tmp';
    $changes = 0;
    for ($round = 0; $round < 300; $round++) {
        file_put_contents($tmp, $src);
        $err = lint($tmp);
        if ($err === null) break;
        [$msg, $ln] = $err;
        $new = null;
        if (stripos($msg, 'curly braces') !== false && $ln > 0) {
            $lines = explode("\n", $src);
            $fixed = fix_curly($lines[$ln - 1]);
            if ($fixed !== $lines[$ln - 1]) { $lines[$ln - 1] = $fixed; $new = implode("\n", $lines); }
        } elseif (stripos($msg, 'Unparenthesized') !== false && $ln > 0) {
            $new = fix_ternary($src, $ln);
        }
        if ($new === null || $new === $src) {
            @unlink($tmp);
            $code = $ln > 0 ? trim(explode("\n", $src)[$ln - 1]) : '';
            return [false, $changes, "$msg (line $ln: $code)"];
        }
        $src = $new; $changes++;
    }
    @unlink($tmp);
    if ($src === $orig) return [true, 0, ''];
    if (!file_exists($file . '.bak-php8')) copy($file, $file . '.bak-php8');
    file_put_contents($file, $src);           // writes in place: owner and permissions stay the same
    return [true, $changes, ''];
}

function rel($f) { return substr($f, strlen(WPC) + 1); }

if ($cmd === 'check' || $cmd === 'fix') {
    $bad = 0; $fixed = 0; $left = [];
    foreach (php_files() as $f) {
        if (lint($f) === null) continue;
        $bad++;
        if ($cmd === 'check') { $e = lint($f); echo "  " . rel($f) . "  line {$e[1]}: {$e[0]}\n"; continue; }
        [$ok, $n, $why] = fix_file($f);
        if ($ok) { $fixed++; echo "  fixed  " . rel($f) . "  ($n change" . ($n == 1 ? '' : 's') . ")\n"; }
        else { $left[] = rel($f) . ": $why"; echo "  LEFT   " . rel($f) . "\n         $why\n"; }
    }
    echo $cmd === 'check' ? "$bad file(s) PHP 8 refuses\n" : "$fixed of $bad file(s) fixed" . ($left ? ", " . count($left) . " need a hand fix" : '') . "\n";
    exit($left ? 1 : 0);
}

if ($cmd === 'update') {
    $old = WPC . '/php8-old';
    @mkdir($old, 0755, true);
    file_put_contents("$old/.htaccess", "Require all denied\n");   // old plugin code must not be reachable from the web
    foreach (array_slice($argv, 2) as $slug) {
        if (!preg_match('/^[a-z0-9-]+$/', $slug)) { echo "  skip   $slug (not a wordpress.org slug)\n"; continue; }
        $url = (getenv("WPORG") ?: "https://downloads.wordpress.org/plugin") . "/$slug.latest-stable.zip";
        $zip = "$old/.$slug.zip";
        $data = @file_get_contents($url);
        if ($data === false || strlen($data) < 1000 || substr($data, 0, 2) !== 'PK') {
            echo "  KEPT   $slug - no download (closed on wordpress.org, or no internet)\n"; continue;
        }
        file_put_contents($zip, $data);
        $z = new ZipArchive();
        if ($z->open($zip) !== true) { echo "  KEPT   $slug - bad zip\n"; continue; }
        $stage = "$old/.stage-$slug";   // same disk as plugins/, so the move below is a plain rename
        shell_exec('rm -rf ' . escapeshellarg($stage));
        $z->extractTo($stage); $z->close();
        if (!is_dir("$stage/$slug")) { echo "  KEPT   $slug - unexpected zip layout\n"; continue; }
        $dst = WPC . "/plugins/$slug";
        $oldVer = '';
        foreach (glob("$dst/*.php") ?: [] as $pf) if (preg_match('/^\s*\*?\s*Version:\s*(\S+)/mi', file_get_contents($pf, false, null, 0, 8192), $m)) { $oldVer = $m[1]; break; }
        if (is_dir($dst)) {
            $bk = "$old/$slug";
            shell_exec('rm -rf ' . escapeshellarg($bk));
            rename($dst, $bk);
        }
        rename("$stage/$slug", $dst);
        shell_exec('rm -rf ' . escapeshellarg($stage) . ' ' . escapeshellarg($zip));
        $newVer = '';
        foreach (glob("$dst/*.php") as $pf) if (preg_match('/^\s*\*?\s*Version:\s*(\S+)/mi', file_get_contents($pf, false, null, 0, 8192), $m)) { $newVer = $m[1]; break; }
        echo "  updated $slug  " . ($oldVer ?: '?') . " -> $newVer\n";
    }
    exit(0);
}
echo "usage: php8-fix.php check|fix|update <slugs>\n"; exit(2);
