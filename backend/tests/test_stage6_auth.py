"""Rebuild Stage 6 item 1 — login, session tokens, route protection, CORS."""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import auth
from tests.conftest import client


@pytest.fixture
def locked(monkeypatch):
    monkeypatch.setenv("FOUNDER_OS_PASSWORD", "correct horse")
    monkeypatch.delenv("FOUNDER_OS_SECRET", raising=False)
    yield


def _login(pw="correct horse"):
    return client.post("/auth/login", json={"password": pw})


class TestOpenByDefault:

    def test_no_password_means_no_auth(self, monkeypatch):
        monkeypatch.delenv("FOUNDER_OS_PASSWORD", raising=False)
        assert client.get("/todos").status_code == 200
        s = client.get("/auth/status").json()
        assert s == {"auth_required": False, "authenticated": True}
        assert _login("anything").json()["auth_required"] is False


class TestLocked:

    def test_routes_require_token(self, locked):
        for path in ("/todos", "/briefing/today", "/syllabus", "/changes", "/usage/summary", "/docs", "/openapi.json"):
            r = client.get(path)
            assert r.status_code == 401, path
            assert r.headers.get("www-authenticate") == "Bearer"
        assert client.post("/parse", json={"text": "x"}).status_code == 401
        assert client.post("/orchestrator", json={"text": "x", "session_id": "s"}).status_code == 401

    def test_health_ping_and_auth_open(self, locked):
        assert client.get("/health").status_code == 200
        assert client.get("/ping").status_code == 200
        assert client.get("/auth/status").json() == {"auth_required": True, "authenticated": False}

    def test_wrong_password(self, locked):
        r = _login("nope")
        assert r.status_code == 401 and "Wrong" in r.json()["detail"]

    def test_login_then_access(self, locked):
        r = _login()
        assert r.status_code == 200
        body = r.json()
        token = body["token"]
        assert token and body["expires_at"] > time.time() + 29 * 86400
        h = {"Authorization": f"Bearer {token}"}
        assert client.get("/todos", headers=h).status_code == 200
        assert client.get("/auth/status", headers=h).json()["authenticated"] is True
        assert client.get("/todos", headers={"X-Founder-Token": token}).status_code == 200
        # SSE route works with the token too
        r = client.post("/orchestrator", json={"text": "hi", "session_id": "s"}, headers=h)
        assert r.status_code == 200

    def test_tampered_and_expired_tokens_rejected(self, locked):
        token = _login().json()["token"]
        p, s = token.split(".")
        assert client.get("/todos", headers={"Authorization": f"Bearer {p}.{s[:-2]}xx"}).status_code == 401
        assert client.get("/todos", headers={"Authorization": "Bearer garbage"}).status_code == 401
        old, _ = auth.issue_token(now=time.time() - 400 * 86400)
        assert client.get("/todos", headers={"Authorization": f"Bearer {old}"}).status_code == 401

    def test_token_bound_to_secret(self, locked, monkeypatch):
        token = _login().json()["token"]
        monkeypatch.setenv("FOUNDER_OS_PASSWORD", "changed")
        assert client.get("/todos", headers={"Authorization": f"Bearer {token}"}).status_code == 401

    def test_preflight_passes_without_token(self, locked):
        r = client.options("/todos", headers={"Origin": "http://localhost:8000",
                                              "Access-Control-Request-Method": "GET"})
        assert r.status_code == 200


class TestCORS:

    def test_default_origins_are_local_only(self):
        assert auth.allowed_origins() == ["http://127.0.0.1:8000", "http://localhost:8000"]
        r = client.get("/health", headers={"Origin": "http://localhost:8000"})
        assert r.headers.get("access-control-allow-origin") == "http://localhost:8000"
        r = client.get("/health", headers={"Origin": "https://evil.example"})
        assert r.headers.get("access-control-allow-origin") is None

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("FOUNDER_OS_ORIGINS", "https://founder-os.netlify.app, https://founderos.app")
        assert auth.allowed_origins() == ["https://founder-os.netlify.app", "https://founderos.app"]
