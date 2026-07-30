"""Lifespan startup gate (A1): the refusal happens BEFORE any connection.

These are unit tests on purpose — the gate sits in front of PgBackend/Redis
construction, so proving the refusal needs no services. A PgBackend stand-in
that fails on construction proves the gate position, not just the outcome.
"""

from __future__ import annotations

import logging

import pytest

import rag_assistant.api as api_mod
from conftest import make_registry, make_settings


class ForbiddenBackend:
    """Trips the test if the app reaches connection setup despite the gate."""

    def __init__(self, *args, **kwargs):
        raise AssertionError("PgBackend must not be constructed before the gate passes")


@pytest.fixture
def gated_app(monkeypatch):
    monkeypatch.setattr(api_mod, "get_registry", make_registry)
    monkeypatch.setattr(api_mod, "PgBackend", ForbiddenBackend)

    def _with(settings):
        monkeypatch.setattr(api_mod, "get_settings", lambda: settings)
        return api_mod.lifespan(api_mod.app)

    return _with


async def test_production_with_demo_config_aborts_before_any_connection(gated_app, caplog):
    demo = make_settings(api_key_anna="demo-anna-it", api_key_ben="demo-ben-hr")
    with (
        caplog.at_level(logging.ERROR, logger="rag_assistant.api"),
        pytest.raises(RuntimeError, match=r"production readiness failed: \d+ finding"),
    ):
        async with gated_app(demo):
            pass
    # every finding is logged individually with rule + message + remedy
    logged = [r.message for r in caplog.records if r.message.startswith("readiness R")]
    assert any("readiness R1" in m for m in logged)
    assert all("remedy:" in m for m in logged)


async def test_demo_mode_skips_readiness_but_not_value_validation(gated_app):
    # demo mode passes the gate; ForbiddenBackend then proves the code path
    # reached connection setup — i.e. no readiness check blocked it.
    with pytest.raises(AssertionError, match="PgBackend must not be constructed"):
        async with gated_app(make_settings(deployment_mode="demo")):
            pass


@pytest.mark.parametrize(
    "overrides",
    [
        {"deployment_mode": "prod"},
        {"deployment_mode": "demo", "auth_backend": "sso"},
        {"deployment_mode": "production", "auth_backend": "sso"},
    ],
)
async def test_unknown_field_values_abort_startup_in_every_mode(gated_app, overrides):
    with pytest.raises(ValueError, match="unknown"):
        async with gated_app(make_settings(**overrides)):
            pass


async def test_oidc_without_issuer_aborts_startup(gated_app):
    # A6 adjustment: 'not implemented yet' is gone; the fail-closed value
    # check now demands an issuer for the oidc backend in every mode.
    with pytest.raises(ValueError, match="requires OIDC_ISSUER_URL"):
        async with gated_app(make_settings(deployment_mode="demo", auth_backend="oidc")):
            pass


async def test_oidc_with_issuer_passes_the_value_check(gated_app):
    # ForbiddenBackend trips = the lifespan reached connection setup, i.e.
    # nothing refused the now-implemented oidc backend before it.
    with pytest.raises(AssertionError, match="PgBackend must not be constructed"):
        async with gated_app(
            make_settings(
                deployment_mode="demo",
                auth_backend="oidc",
                oidc_issuer_url="http://localhost:5556/dex",
            )
        ):
            pass
