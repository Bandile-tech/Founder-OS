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


class TestCompleteTodo:

    def test_complete_is_logged_and_undoable(self):
        import models
        from tests.conftest import TestSessionLocal
        db = TestSessionLocal()
        t = models.Todo(text="Finalise Bel Baby delivery", priority=1, category="business", due="2026-11-07", source="manual")
        db.add(t); db.commit(); tid = t.id; db.close()
        r = client.post(f"/todos/{tid}/complete")
        assert r.status_code == 200 and r.json()["done"] is True
        change = r.json()["change"]
        assert change["table"] == "todos" and change["action"] == "update"
        again = client.post(f"/todos/{tid}/complete").json()
        assert again["done"] is True and again["change"] is None
        client.post(f"/changes/{change['id']}/undo")
        db = TestSessionLocal()
        assert db.query(models.Todo).filter(models.Todo.id == tid).first().done is False
        db.close()
        assert client.post("/todos/99999/complete").status_code == 404


class TestExemptions:

    def test_app_prefix_is_tight(self, locked):
        assert auth.is_exempt("/app/", "GET") and auth.is_exempt("/app", "GET")
        assert not auth.is_exempt("/application", "GET")
        assert not auth.is_exempt("/apples", "GET")
        assert auth.is_exempt("/anything", "OPTIONS")
        assert not auth.is_exempt("/docs", "GET")


class TestLoginRateLimit:

    def test_lock_after_repeated_failures(self, locked, monkeypatch):
        auth.reset_rate_limits()
        monkeypatch.setattr(auth, "LOGIN_MAX_FAILURES", 3)
        for _ in range(2):
            assert _login("nope").status_code == 401
        r = _login("nope")
        assert r.status_code == 429 and "Retry-After" in r.headers
        # locked even with the right password
        assert _login().status_code == 429
        auth.reset_rate_limits()
        assert _login().status_code == 200

    def test_success_clears_failures(self, locked, monkeypatch):
        auth.reset_rate_limits()
        monkeypatch.setattr(auth, "LOGIN_MAX_FAILURES", 3)
        _login("nope"); _login("nope")
        assert _login().status_code == 200
        _login("nope"); _login("nope")
        assert _login("nope").status_code == 429
        auth.reset_rate_limits()

    def test_forwarded_for_is_used(self, locked, monkeypatch):
        auth.reset_rate_limits()
        monkeypatch.setattr(auth, "LOGIN_MAX_FAILURES", 2)
        h = {"X-Forwarded-For": "203.0.113.9, 10.0.0.1"}
        client.post("/auth/login", json={"password": "x"}, headers=h)
        assert client.post("/auth/login", json={"password": "x"}, headers=h).status_code == 429
        assert _login("x").status_code == 401        # a different address is not locked
        auth.reset_rate_limits()
