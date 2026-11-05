"""The frontend is served only from /app/; the root redirects there.

Served at /, the page's relative ``assets/…`` URLs resolved to /assets/…, which the auth
middleware blocks (an <img> cannot send the bearer token), so the scene and orb never loaded.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.conftest import client


@pytest.fixture
def locked(monkeypatch):
    monkeypatch.setenv("FOUNDER_OS_PASSWORD", "correct horse")
    monkeypatch.delenv("FOUNDER_OS_SECRET", raising=False)
    yield


class TestRootRedirect:

    @pytest.mark.parametrize("path", ["/", "/index.html"])
    def test_root_redirects_to_app(self, locked, path):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/app/"

    def test_head_root_redirects(self, locked):
        r = client.head("/", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/app/"

    def test_query_string_is_kept(self, locked):
        r = client.get("/?view=syllabus", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/app/?view=syllabus"

    def test_redirect_lands_on_the_frontend(self, locked):
        r = client.get("/")
        assert r.status_code == 200
        assert str(r.url).endswith("/app/")
        assert "<title>Founder OS" in r.text


class TestStaticAssets:

    @pytest.mark.parametrize("asset", ["orb.png", "scene.png"])
    def test_app_assets_load_without_token(self, locked, asset):
        r = client.get(f"/app/assets/{asset}")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert len(r.content) > 1000

    def test_root_assets_stay_locked(self, locked):
        # The exemption was not widened: nothing outside /app/ is served without a token.
        assert client.get("/assets/orb.png").status_code == 401


class TestApiStillLocked:

    @pytest.mark.parametrize("path", ["/todos", "/briefing/today", "/syllabus", "/kpis", "/radar"])
    def test_api_routes_401_without_token(self, locked, path):
        r = client.get(path)
        assert r.status_code == 401, path
        assert r.headers.get("www-authenticate") == "Bearer"
