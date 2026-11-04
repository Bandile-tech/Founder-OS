"""
/vault/status must never expose the vault repo URL or any credentials in it.

In production VAULT_REPO_URL carries a GitHub token
(https://x-access-token:<token>@github.com/...), so echoing it back leaks
the token to anyone who can reach the endpoint.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi.testclient import TestClient

import vault_sync
from main import app

client = TestClient(app)

FAKE_TOKEN = "ghp_FAKE0000TEST"
CREDENTIALED_URL = f"https://x-access-token:{FAKE_TOKEN}@github.com/example/vault.git"


def test_vault_status_contains_no_repo_url_or_credentials(monkeypatch):
    monkeypatch.setattr(vault_sync, "VAULT_REPO_URL", CREDENTIALED_URL)

    r = client.get("/vault/status")

    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"last_synced", "chunk_count", "file_count"}
    assert FAKE_TOKEN not in r.text
    assert "x-access-token" not in r.text
    assert "github.com" not in r.text
    # No URL with embedded user:password@ anywhere in the payload.
    assert not re.search(r"://[^/\s\"]+@", r.text)
