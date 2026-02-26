# Security Review

## OWASP Top 10

**Injection**
- SQL: all queries use parameterized statements or ORM — no string concatenation into SQL
- Command injection: no unsanitized user input passed to subprocess, exec, or eval
- LDAP/NoSQL injection: query construction uses safe builders, not string templates

**Broken Authentication**
- Passwords are hashed with bcrypt, argon2, or scrypt — never MD5/SHA1
- Session tokens have sufficient entropy (128+ bits)
- Login endpoints are rate-limited to prevent brute force
- Password reset tokens expire and are single-use

**Broken Access Control**
- Every endpoint enforces authorization (not just authentication)
- Horizontal privilege escalation is prevented — users cannot access other users' resources by changing IDs
- Admin functionality is protected by role checks, not just hidden UI
- Direct object references are validated against the authenticated user's permissions

**Security Misconfiguration**
- Debug mode is disabled in production configurations
- Default credentials are not present in code or config
- Error messages do not expose stack traces or internal paths
- CORS is configured restrictively (not `Access-Control-Allow-Origin: *` on authenticated endpoints)

**XSS**
- User-generated content is escaped before rendering in HTML
- No `dangerouslySetInnerHTML`, `innerHTML`, or `v-html` without sanitization
- Content-Security-Policy headers are set where applicable
- URL parameters are validated before use in redirects (open redirect prevention)

## Secrets Management
- No secrets, API keys, passwords, or tokens in source code
- No secrets in CI pipeline files — use CI variables or a secrets manager
- `.env` files are in `.gitignore`
- Secrets rotation is possible without code changes

## Data Protection
- PII is not logged (names, emails, phone numbers, IP addresses in plain text)
- Sensitive data at rest is encrypted
- Data in transit uses TLS — no HTTP endpoints for sensitive operations
- Backup and retention policies exist for sensitive data

## Dependency Security
- Dependencies are pinned to specific versions (not `latest` or `*`)
- Known vulnerable dependencies should be flagged
- Lock files are committed and not manually edited
