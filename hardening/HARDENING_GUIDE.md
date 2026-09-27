# Host Hardening Guide — WordPress Hosting Platform

This guide addresses the gaps found during the security audit: an inactive
firewall, SSH password authentication left enabled with no brute-force
protection, and no automated banning of repeated failed login attempts.

**Read this whole guide before running anything.** The steps must be done
in this order. Doing the SSH password step before confirming key-based
login works is the single most common way to permanently lock yourself
out of a remote server — there is no "undo" if you get that step wrong
and don't have console/physical access to the machine.

---

## Step 1 — Set up SSH key-based login (on YOUR machine, not the server)

If you already log in with a key (not a password), skip to Step 2.

**1a. Generate a key pair**, on your own laptop/desktop — not on the server:

```bash
ssh-keygen -t ed25519 -C "your-email@example.com"
```

Press Enter to accept the default file location. Set a passphrase if you
want an extra layer of protection on the key itself (recommended, but not
required).

**1b. Copy the public key to the server:**

```bash
ssh-copy-id -p <port> your-username@your-server-address
```

(Replace `<port>` with 22, or whatever port SSH runs on if you've changed
it. Replace the rest with your actual login details.)

**1c. Verify it actually works**, in a genuinely new terminal window
(keep your current session open as a safety net — don't close it yet):

```bash
ssh -o PubkeyAuthentication=yes -o PasswordAuthentication=no \
    -p <port> your-username@your-server-address
```

If this logs you in **without asking for a password**, you're ready for
Step 3 (SSH hardening) later. If it fails, stop — fix this before going
any further, and do not proceed to disabling password authentication.

---

## Step 2 — Firewall + fail2ban

This is the safe-to-run-first part: it doesn't touch SSH password
authentication at all, so there's no lockout risk from this step alone.

Copy `harden_firewall.sh` to the server, then run:

```bash
chmod +x harden_firewall.sh

# First, see exactly what it would do without changing anything:
sudo ./harden_firewall.sh --dry-run

# If that looks right, apply it for real:
sudo ./harden_firewall.sh
```

**What this does:**
- Installs UFW if it isn't already
- Allows SSH, HTTP/HTTPS (80/443, for the reverse proxy), and the manager
  dashboard port — allowed *before* the firewall is enabled, so your
  current session is never at risk
- Sets the firewall to default-deny incoming / allow outgoing, then
  enables it
- Installs and enables fail2ban with an SSH jail (5 failed attempts within
  15 minutes bans that IP for 1 hour)

**What this deliberately leaves closed:** the SFTP (21000–21999) and
phpMyAdmin (22000–22999) port ranges. Those are managed dynamically by the
platform itself — a specific IP gets temporary access only when you
explicitly grant it from the dashboard, and the firewall rule disappears
again automatically. Statically opening those ranges here would defeat
that feature entirely, leaving every site's SFTP/phpMyAdmin permanently
reachable from anywhere regardless of whether access was ever granted.

**After running it, before doing anything else:** open a **separate** new
terminal window and confirm you can establish a fresh SSH connection
successfully. Don't skip this — it's the checkpoint that confirms the
firewall didn't accidentally cut off your own access.

---

## Step 3 — Disable SSH password authentication

**Only do this once Step 1's verification actually succeeded.** This is
the step that can lock you out permanently if done first.

Copy `harden_ssh_keys_only.sh` to the server, then run:

```bash
chmod +x harden_ssh_keys_only.sh

# This will refuse to run without this exact flag, on purpose:
sudo ./harden_ssh_keys_only.sh --i-have-verified-key-login
```

The script itself also checks that at least one `authorized_keys` file
with actual content exists anywhere on the system before proceeding, as a
second layer of protection against the worst-case mistake. It validates
the new SSH config before applying it, and automatically rolls back if
anything looks wrong.

**After running it, before closing your terminal:** open a **new**
terminal window and confirm you can still log in. If something's wrong at
this point, you'll need console or physical access to the machine to fix
it — there's no remote path back in once password auth is off and key
login isn't working.

---

## What's intentionally not covered here

- **HTTPS / Secure cookie flag**: tracked separately — production
  already has HTTPS via Cloudflare, but worth double-checking the SSL/TLS
  mode is set to "Full" or "Full (Strict)" rather than "Flexible" (the
  latter only encrypts browser-to-Cloudflare, not Cloudflare-to-origin).
- **Backup encryption at rest**: a separate, larger piece of work — the
  audit found backups are currently stored in plaintext everywhere
  (local, second array, and NAS). This needs a real design decision about
  key management (an encryption key stored on the same server as the
  backups protects against nothing), not a quick script.
- **Audit log tamper-resistance**: currently, anyone with host/shell
  access can rewrite the audit trail directly at the database level.
  Meaningful improvement here means shipping logs to a separate system in
  near-real-time, which is a bigger architectural change than a
  hardening script covers.

---

## Quick reference — do this, in this order

1. Generate an SSH key on your own machine, copy it to the server,
   **verify key-only login works in a separate terminal**
2. `sudo ./harden_firewall.sh --dry-run` → review → `sudo ./harden_firewall.sh`
3. Verify SSH still works in a separate terminal
4. `sudo ./harden_ssh_keys_only.sh --i-have-verified-key-login`
5. Verify SSH still works in a separate terminal, one more time
