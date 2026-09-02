# Authentication

Version 9.2.0 uses a single dashboard user store with three roles:

- **Admin** — full platform administration and user management.
- **User / Operator** — operational access to hosted sites.
- **View Only** — read-only platform access.

All roles can manage their own password and MFA.

## First login

New users are created with a generated temporary password that is emailed to
the address supplied by the administrator.

The required first-login sequence is:

1. Sign in with the temporary password.
2. Choose a new password (minimum 12 characters).
3. Enrol a TOTP authenticator.
4. Verify the authenticator code.
5. Save the one-time recovery codes.
6. Continue to the dashboard.

Dashboard access is blocked until required password and MFA setup is complete.

## MFA

The platform uses standard TOTP and works with Google Authenticator, Microsoft
Authenticator, Authy and other compatible applications.

Recovery codes are stored only as SHA-256 hashes and are consumed after use.

## Password recovery

The Forgot Password workflow sends a single-use reset link to the account email.
Reset links expire after 30 minutes and the public form does not disclose
whether an email address exists.

## SMTP

SMTP is configured in **Admin → Email Settings** and is shared by:

- new-user temporary-password emails;
- password-reset emails;
- platform health/security notifications.

Configure and test SMTP before creating additional users.
