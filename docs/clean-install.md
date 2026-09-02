# Clean installation policy

The GitHub release is a clean installer, not an in-place upgrade utility.

`install.sh` refuses to overwrite an existing `/opt/wp-host` deployment when it
detects a manager application, user store or platform database.

This repository intentionally does **not** contain historical patch scripts,
hotfix scripts, update scripts, rollback scripts, or patch-generated backup
scripts.

The `scripts/wordpress-backup` command is retained because it is a normal
operator command for backing up hosted WordPress sites. It is unrelated to
software patching.
