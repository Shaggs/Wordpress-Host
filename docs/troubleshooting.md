# Troubleshooting

## Database connection

Check WordPress Docker database settings and confirm `DB_HOST` resolves to the
database service rather than `localhost`.

## Redirect loops

```bash
curl -IL --max-redirs 10 https://example.com/
```

Imported `.htaccess`, proxy HTTPS detection, WordPress `home/siteurl` and edge
SSL settings are common causes.

## Unstyled WordPress admin

Inspect the generated login assets. HTTP assets on an HTTPS page indicate that
WordPress is not correctly recognising the original HTTPS request.

## Homepage works, pages return 404

Check standard WordPress rewrite rules and pretty permalinks.

## MU-plugin fatal error

Move the failing item out of `wp-content/mu-plugins` and retest the base site.
