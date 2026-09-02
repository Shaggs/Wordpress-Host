# WordPress Migration

Upload one complete WordPress ZIP and one `.sql` or `.sql.gz` database dump to:

```text
/upload/migration-source/
```

The migration engine preserves the source, takes a rollback backup, extracts the
site, normalises Docker database settings and reverse-proxy HTTPS handling,
replaces unsafe imported `.htaccess` rules, quarantines risky MU plugins/drop-ins,
imports the SQL database, performs serialized-safe URL replacement, repairs
permissions and validates the resulting site.

A homepage alone is not sufficient migration validation. Review `/wp-admin`,
HTTPS assets, internal pages, pretty permalinks, redirects, REST API and any
business-critical forms/integrations.
