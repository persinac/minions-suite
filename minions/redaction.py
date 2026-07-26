"""Scrub credentials out of anything on its way to a log.

Two credentials had to be rotated in one afternoon. One was a human dumping a
Kubernetes Secret's `.data`; the other was `preflight.py` logging a Redis URL
with the password inline. Both were fixed at the call site, which fixes exactly
those two call sites.

That does not scale. `nats_client.py` logs the server string, `repos.py` logs the
clone URL — both harmless today only because the credential happens to live
somewhere else. The day someone puts a token in a clone URL, or points NATS at
`nats://user:pass@host`, it lands in a transcript and needs rotating, and nobody
notices until it already has.

So this runs as a logging filter over every record, rather than as a helper
people must remember to call. A missed call site is the failure mode; removing
the need to remember is the fix.

Deliberately pattern-based rather than value-based. A filter that only knows the
secrets it was handed at startup misses anything minted later — GitHub App
installation tokens are reminted hourly, so a value-based scrubber would go stale
within the hour.
"""

import logging
import re

# scheme://user:password@host — the shape every connection string uses, and the
# one that made both of this session's rotations necessary.
_URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<user>[^:/@\s]+):(?P<secret>[^@/\s]+)@")

# Token shapes worth catching on sight. Anchored on the vendor prefix, because
# the suffix is high-entropy and unmatchable.
_TOKEN_PATTERNS = [
    re.compile(r"\bghs_[A-Za-z0-9]{20,}"),  # GitHub App installation
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),  # GitHub personal
    re.compile(r"\bgho_[A-Za-z0-9]{20,}"),  # GitHub OAuth
    re.compile(r"\bghr_[A-Za-z0-9]{20,}"),  # GitHub refresh
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}"),  # GitLab personal
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),  # Anthropic
    re.compile(r"\bsk-[A-Za-z0-9]{32,}"),  # OpenAI and lookalikes
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),  # Slack
    re.compile(r"\bdp\.pt\.[A-Za-z0-9]{20,}"),  # Doppler personal
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
]

# PEM bodies. The header alone is not sensitive; everything between is.
_PEM_BLOCK = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)

REDACTED = "[REDACTED]"


def redact(value: str) -> str:
    """Return `value` with any recognised credential replaced."""
    if not value:
        return value

    text = _PEM_BLOCK.sub("-----BEGIN PRIVATE KEY-----[REDACTED]-----END PRIVATE KEY-----", value)

    # Keep the scheme, host and username: those are what make a log line useful
    # for debugging. Only the secret goes.
    text = _URL_CREDENTIALS.sub(lambda m: f"{m.group('scheme')}{m.group('user')}:{REDACTED}@", text)

    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub(REDACTED, text)

    return text


class RedactingFilter(logging.Filter):
    """Scrub credentials from every log record.

    Applied to handlers rather than loggers: a logger-level filter does not run
    for records propagated from child loggers, which would silently exempt every
    `minions.*` and third-party logger — exactly the ones that emit connection
    strings.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)

        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: redact(v) if isinstance(v, str) else v for k, v in record.args.items()}
            else:
                record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)

        # Tracebacks carry the same strings — a connection error renders the DSN.
        if record.exc_text:
            record.exc_text = redact(record.exc_text)

        return True


def install(logger: logging.Logger | None = None) -> None:
    """Attach the filter to every handler on the root logger.

    Call after logging is configured. Idempotent.
    """
    target = logger or logging.getLogger()
    for handler in target.handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())
