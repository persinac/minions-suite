"""Credentials must not survive the trip to a log.

Two rotations in one afternoon: a Kubernetes Secret dumped by hand, and
preflight.py logging a Redis URL with the password inline. Both were fixed at the
call site, which protects those two call sites and nothing else.

These tests exercise the filter that runs over every record, so a future
`logger.info(url)` cannot leak regardless of who writes it.
"""

import logging

import pytest

from minions.redaction import REDACTED, RedactingFilter, redact


class TestConnectionStrings:
    @pytest.mark.parametrize(
        "url",
        [
            "postgresql://minions:hunter2@db-postgresql-nyc1.ondigitalocean.com:25060/minions",
            "redis://default:s3cr3tpw@redis-stack.minion-suite.svc.cluster.local:6379",
            "nats://natsuser:natspass@18.191.42.210:4222",
            "amqp://guest:guest@localhost:5672",
        ],
    )
    def test_the_password_is_removed(self, url):
        out = redact(url)

        assert REDACTED in out
        for secret in ("hunter2", "s3cr3tpw", "natspass", "guest:guest"):
            assert secret not in out or (secret == "guest:guest" and "guest:[REDACTED]" in out)

    def test_scheme_host_and_user_survive(self):
        """A fully-masked URL is useless for debugging — keep what is not secret."""
        out = redact("postgresql://minions:hunter2@db.example.com:25060/minions")

        assert out.startswith("postgresql://minions:")
        assert "db.example.com:25060/minions" in out
        assert "hunter2" not in out

    def test_a_url_without_credentials_is_untouched(self):
        url = "https://github.com/flippin-balls/wallet-api.git"

        assert redact(url) == url

    def test_credentials_embedded_in_a_sentence(self):
        out = redact("Redis ping failed for redis://default:letmein@host:6379 after 3 tries")

        assert "letmein" not in out
        assert "after 3 tries" in out


class TestTokens:
    @pytest.mark.parametrize(
        "token",
        [
            "ghs_16C7e42F292c6912E7710c838347Ae178B4a",
            "ghp_16C7e42F292c6912E7710c838347Ae178B4a",
            "github_pat_11ABCDEFG0abcdefghijkl_ABCDEFGHIJKLMNOPQRSTUVWXYZ",
            "glpat-ABCDEFGHIJKLMNOPQRST",
            "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789",
            "xoxb-1234567890-abcdefghijkl",
            "dp.pt.abcdefghijklmnopqrstuvwxyz0123",
            "AKIAIOSFODNN7EXAMPLE",
        ],
    )
    def test_known_token_shapes_are_scrubbed(self, token):
        out = redact(f"authenticated with {token} ok")

        assert token not in out
        assert REDACTED in out

    def test_surrounding_text_survives(self):
        out = redact("minted ghs_16C7e42F292c6912E7710c838347Ae178B4a for installation 148993220")

        assert "installation 148993220" in out
        assert "ghs_16C7" not in out

    def test_ordinary_words_are_not_mangled(self):
        text = "skipped the task because status was success"

        assert redact(text) == text


class TestPrivateKeys:
    def test_pem_body_is_removed(self):
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA1234\nsecretmaterial\n-----END RSA PRIVATE KEY-----"

        out = redact(f"loaded key: {pem}")

        assert "secretmaterial" not in out
        assert "MIIEowIBAAKCAQEA1234" not in out
        assert "BEGIN PRIVATE KEY" in out, "the shape should remain so the log still makes sense"


class TestFilterIntegration:
    """The filter must catch records however they were formatted."""

    @staticmethod
    def _emit(msg, *args):
        record = logging.LogRecord("t", logging.INFO, __file__, 1, msg, args or None, None)
        RedactingFilter().filter(record)
        return record.getMessage()

    def test_message_string(self):
        out = self._emit("connecting to redis://default:pw123456@host:6379")

        assert "pw123456" not in out

    def test_lazy_percent_args(self):
        """The idiomatic form — the secret is in args, not msg."""
        out = self._emit("Memory system enabled (redis=%s)", "redis://default:pw123456@host:6379")

        assert "pw123456" not in out
        assert "Memory system enabled" in out

    def test_dict_args(self):
        # LogRecord unwraps a single Mapping passed inside a tuple; handing it
        # the bare dict makes LogRecord itself raise on args[0].
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "url=%(url)s", ({"url": "redis://u:pw123456@h:6379"},), None)
        RedactingFilter().filter(record)

        assert "pw123456" not in record.getMessage()

    def test_non_string_args_are_left_alone(self):
        out = self._emit("job %s took %d turns and cost $%.2f", "abc123", 64, 20.57)

        assert "64" in out and "20.57" in out

    def test_exception_text_is_scrubbed(self):
        """A connection error renders the DSN into the traceback."""
        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "boom", None, None)
        record.exc_text = 'OperationalError: could not connect to postgresql://u:pw123456@h:5432/db'

        RedactingFilter().filter(record)

        assert "pw123456" not in record.exc_text

    def test_filter_never_drops_a_record(self):
        """Redaction must not double as suppression."""
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "anything", None, None)

        assert RedactingFilter().filter(record) is True


class TestInstall:
    def test_install_is_idempotent(self):
        from minions.redaction import install

        logger = logging.getLogger("redaction-test")
        logger.addHandler(logging.NullHandler())

        install(logger)
        install(logger)

        filters = [f for h in logger.handlers for f in h.filters if isinstance(f, RedactingFilter)]
        assert len(filters) == 1

    def test_installed_on_handlers_not_the_logger(self):
        """A logger-level filter does not run for records from child loggers."""
        from minions.redaction import install

        logger = logging.getLogger("redaction-test-2")
        handler = logging.NullHandler()
        logger.addHandler(handler)

        install(logger)

        assert any(isinstance(f, RedactingFilter) for f in handler.filters)
