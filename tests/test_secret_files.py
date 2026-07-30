"""Unit tests for the *_FILE secrets convention and JSON logging (A5.2).

Precedence contract: a directly set variable ALWAYS wins over NAME_FILE; the
file source only fills what would otherwise stay at its default. A configured
but unreadable file aborts startup (fail-closed) — never a silent ignore.
"""

from __future__ import annotations

import json
import logging

import pytest

from rag_assistant.config import SECRET_FILE_FIELDS, Settings
from rag_assistant.obs import JsonLogFormatter, setup_logging


def _clear_secret_env(monkeypatch):
    for name in SECRET_FILE_FIELDS:
        monkeypatch.delenv(name.upper(), raising=False)
        monkeypatch.delenv(f"{name.upper()}_FILE", raising=False)


# ── *_FILE loading ────────────────────────────────────────────────────────────
def test_whitelist_is_pinned():
    assert SECRET_FILE_FIELDS == (
        "openai_api_key",
        "anthropic_api_key",
        "azure_openai_api_key",
        "api_key_anna",
        "api_key_ben",
        "database_url",
        "redis_url",
        "oidc_client_secret",
    )


def test_file_fills_an_unset_field(monkeypatch, tmp_path):
    _clear_secret_env(monkeypatch)
    secret = tmp_path / "openai_api_key"
    secret.write_text("file-key-123\n", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY_FILE", str(secret))

    s = Settings(_env_file=None)

    assert s.openai_api_key == "file-key-123"  # trailing newline stripped


def test_directly_set_env_wins_over_file(monkeypatch, tmp_path):
    _clear_secret_env(monkeypatch)
    secret = tmp_path / "openai_api_key"
    secret.write_text("file-key-123", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY_FILE", str(secret))
    monkeypatch.setenv("OPENAI_API_KEY", "env-key-456")

    assert Settings(_env_file=None).openai_api_key == "env-key-456"


def test_unreadable_file_aborts_startup(monkeypatch, tmp_path):
    _clear_secret_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL_FILE", str(tmp_path / "does-not-exist"))

    with pytest.raises(ValueError, match="DATABASE_URL_FILE is set but not readable"):
        Settings(_env_file=None)


def test_without_file_variables_behavior_is_unchanged(monkeypatch):
    _clear_secret_env(monkeypatch)
    s = Settings(_env_file=None)
    assert s.openai_api_key == ""
    assert s.api_key_anna == "demo-anna-it"  # plain defaults, untouched


def test_non_whitelisted_file_variable_is_ignored(monkeypatch, tmp_path):
    """Only the whitelist is file-loadable — OLLAMA_MODEL_FILE does nothing."""
    _clear_secret_env(monkeypatch)
    rogue = tmp_path / "model"
    rogue.write_text("evil-model", encoding="utf-8")
    monkeypatch.setenv("OLLAMA_MODEL_FILE", str(rogue))

    assert Settings(_env_file=None).ollama_model == "qwen3:8b"


# ── JSON logging ──────────────────────────────────────────────────────────────
def _format(record: logging.LogRecord) -> dict:
    return json.loads(JsonLogFormatter().format(record))


def test_formatter_emits_parsable_json_with_required_fields():
    record = logging.LogRecord(
        name="rag_assistant.api",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="api ready (profile=%s)",
        args=("default",),
        exc_info=None,
    )
    payload = _format(record)
    assert set(payload) == {"ts", "level", "logger", "message"}
    assert payload["level"] == "INFO"
    assert payload["logger"] == "rag_assistant.api"
    assert payload["message"] == "api ready (profile=default)"


def test_exceptions_contribute_only_the_class_name():
    try:
        raise RuntimeError("secret detail that must not appear")
    except RuntimeError:
        import sys

        record = logging.LogRecord(
            name="x",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="audit write failed",
            args=(),
            exc_info=sys.exc_info(),
        )
    payload = _format(record)
    assert payload["exc_type"] == "RuntimeError"
    assert "secret detail" not in json.dumps(payload)  # no text, no traceback


def test_setup_logging_false_is_a_noop():
    root = logging.getLogger()
    before = list(root.handlers)
    setup_logging(False)
    assert root.handlers == before


def test_setup_logging_true_installs_the_json_formatter():
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        setup_logging(True)
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, JsonLogFormatter)
    finally:
        root.handlers[:] = before
