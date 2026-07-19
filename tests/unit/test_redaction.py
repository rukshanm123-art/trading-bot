"""Secret redaction: registered values and credential-shaped strings never
reach log output, including exception tracebacks."""

import io
import logging

from trading_bot.logging_setup import HumanFormatter, JsonFormatter
from trading_bot.security.redaction import GLOBAL_REDACTOR, REDACTED, RedactionFilter
from trading_bot.security.secrets import StaticSecretProvider

SECRET = "sk-verysecretapikey1234567890"


def capture_logger(formatter) -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)
    handler.addFilter(RedactionFilter())
    logger = logging.getLogger(f"test_redaction_{id(formatter)}")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return logger, stream


def test_secrets_never_appear_in_logs():
    GLOBAL_REDACTOR.register(SECRET)
    logger, stream = capture_logger(HumanFormatter())
    logger.info("connecting with key %s", SECRET)
    try:
        raise RuntimeError(f"auth failed for {SECRET}")
    except RuntimeError:
        logger.exception("boom")
    output = stream.getvalue()
    assert SECRET not in output
    assert REDACTED in output


def test_json_formatter_redacts_too():
    GLOBAL_REDACTOR.register(SECRET)
    logger, stream = capture_logger(JsonFormatter())
    logger.error("token=%s", SECRET)
    assert SECRET not in stream.getvalue()


def test_pattern_redaction_without_registration():
    # never registered — must still be masked by shape
    text = GLOBAL_REDACTOR.redact("X-MBX-APIKEY: AbCdEf0123456789XyZ and more")
    assert "AbCdEf0123456789XyZ" not in text
    text2 = GLOBAL_REDACTOR.redact("https://x/api?symbol=BTC&signature=" + "a1" * 20)
    assert "a1a1a1" not in text2


def test_secret_provider_registers_automatically():
    provider = StaticSecretProvider({"MY_KEY": "abcdefgh12345678"})
    assert provider.get("MY_KEY") == "abcdefgh12345678"
    assert "abcdefgh12345678" not in GLOBAL_REDACTOR.redact("value abcdefgh12345678 here")


def test_redact_mapping():
    GLOBAL_REDACTOR.register(SECRET)
    clean = GLOBAL_REDACTOR.redact_mapping({"a": SECRET, "b": {"c": SECRET}, "d": 5})
    assert clean["a"] == REDACTED
    assert clean["b"]["c"] == REDACTED
    assert clean["d"] == 5


def test_short_values_not_registered():
    GLOBAL_REDACTOR.register("abc")  # too short to be a real secret
    assert GLOBAL_REDACTOR.redact("abc def") == "abc def"
