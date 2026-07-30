"""Scripted end-to-end OIDC flow against the compose dex (owner's Mac).

NOT part of CI (the browser flow is not CI-suitable; the token layer is
proven with mocks in the unit suite). Run on the owner's machine against the
full container stack started in oidc mode:

    AUTH_BACKEND=oidc, OIDC_ISSUER_URL=http://localhost:5556/dex,
    OIDC_INTERNAL_ISSUER_URL=http://dex:5556/dex, demo bridge map set
    (see .env.example), then: uv run pytest -m dex --no-header -q

The script walks the real authorization-code flow: /auth/login redirect →
dex local-password form → callback → session cookie → /auth/session.
"""

from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.dex

API = "http://localhost:8000"


def _login(email: str, password: str) -> httpx.Client:
    client = httpx.Client(follow_redirects=False, timeout=30.0)
    # 1. BFF starts the flow: 302 to dex's PUBLIC authorization endpoint.
    r = client.get(f"{API}/auth/login")
    assert r.status_code == 302, r.text
    auth_url = r.headers["location"]
    assert auth_url.startswith("http://localhost:5556/dex/auth")
    # 2. dex with a single local connector redirects to the password form.
    r = client.get(auth_url)
    while r.status_code in (302, 303):
        r = client.get(r.headers["location"])
    assert r.status_code == 200
    # 3. Post the demo credentials to the form URL (dex keeps the req id in it).
    r = client.post(str(r.url), data={"login": email, "password": password})
    # 4. Follow dex-side redirects (approval is skipped) until the callback.
    while r.status_code in (302, 303) and "/auth/callback" not in r.headers["location"]:
        r = client.get(r.headers["location"])
    assert r.status_code in (302, 303), r.text
    r = client.get(r.headers["location"])  # the API callback
    assert r.status_code == 302 and r.headers["location"] == "/"
    assert client.cookies.get("rag_session")
    return client


@pytest.mark.parametrize(
    ("email", "password", "department"),
    [
        ("anna@nordfels.example", "nordfels-anna", "it"),
        ("ben@nordfels.example", "nordfels-ben", "hr"),
    ],
)
def test_full_dex_login_yields_a_session_with_the_bridged_department(email, password, department):
    client = _login(email, password)
    session = client.get(f"{API}/auth/session")
    assert session.status_code == 200
    body = session.json()
    assert body["department"] == department  # demo bridge: email → department
    assert body["user_id"]  # pseudonymous sub, never the email
    assert email not in body["user_id"]
    # RLS scoping is identical to the key mode: the documents list works and
    # is department-scoped (counts verified visually in the UI by the owner).
    docs = client.get(f"{API}/documents")
    assert docs.status_code == 200
    # Logout kills the session immediately.
    csrf = body["csrf"]
    out = client.post(f"{API}/auth/logout", headers={"X-CSRF-Token": csrf})
    assert out.status_code == 204
    assert client.get(f"{API}/auth/session").status_code == 401
