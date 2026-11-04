"""
A token embedded in VAULT_REPO_URL must never reach the logs (stdout) or an
API response, whatever git does: fail on clone, fail on pull, or time out.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi.testclient import TestClient

import main
import vault_sync
from main import app

client = TestClient(app)

FAKE_TOKEN = "ghp_FAKE0000TEST"
REPO_URL = f"https://x-access-token:{FAKE_TOKEN}@github.com/example/vault.git"


# ── The shared helper ────────────────────────────────────────

def test_redact_strips_user_and_token_from_url_but_keeps_host_and_path():
    out = vault_sync.redact_credentials(f"Cloning {REPO_URL}")
    assert FAKE_TOKEN not in out
    assert "x-access-token" not in out
    assert "github.com/example/vault.git" in out


def test_redact_strips_token_used_as_url_username():
    out = vault_sync.redact_credentials(f"https://{FAKE_TOKEN}@github.com/example/vault.git")
    assert FAKE_TOKEN not in out
    assert "github.com/example/vault.git" in out


def test_redact_strips_bare_github_tokens():
    for token in (FAKE_TOKEN, "github_pat_FAKE_0000_TEST", "gho_FAKE0000TEST"):
        assert token not in vault_sync.redact_credentials(f"remote: bad credentials {token}")


def test_redact_strips_configured_secret_even_outside_a_url(monkeypatch):
    secret = "plainS3cretNoPrefix"
    monkeypatch.setattr(vault_sync, "VAULT_REPO_URL", f"https://user:{secret}@git.example.com/v.git")
    assert secret not in vault_sync.redact_credentials(f"error near {secret} here")


def test_redact_leaves_clean_text_alone():
    msg = "fatal: repository 'https://github.com/example/vault.git/' not found"
    assert vault_sync.redact_credentials(msg) == msg


# ── Every git failure path, through logs and the API ─────────

def _clone_fails(cmd, **kw):
    return subprocess.CompletedProcess(
        cmd, 128, stdout="",
        stderr=(
            f"Cloning into '{cmd[-1]}'...\n"
            f"fatal: unable to access '{REPO_URL}/': Could not resolve host: github.com"
        ),
    )


def _pull_fails(cmd, **kw):
    return subprocess.CompletedProcess(
        cmd, 1, stdout="",
        stderr=f"fatal: unable to access '{REPO_URL}/': The requested URL returned error: 403",
    )


def _git_times_out(cmd, **kw):
    raise subprocess.TimeoutExpired(cmd, 60)


@pytest.fixture
def credentialed_repo(monkeypatch):
    monkeypatch.setattr(vault_sync, "VAULT_REPO_URL", REPO_URL)


@pytest.mark.parametrize(
    "fake_git, local_exists",
    [(_clone_fails, False), (_pull_fails, True), (_git_times_out, False)],
    ids=["clone-error", "pull-error", "timeout"],
)
def test_post_vault_sync_never_exposes_token(
    credentialed_repo, monkeypatch, tmp_path, capsys, fake_git, local_exists
):
    local = tmp_path if local_exists else tmp_path / "vault"
    monkeypatch.setattr(vault_sync, "VAULT_LOCAL_PATH", str(local))
    monkeypatch.setattr(vault_sync.subprocess, "run", fake_git)

    r = client.post("/vault/sync")

    assert r.status_code == 200
    assert r.json()["errors"], "the git failure should still be reported"
    assert FAKE_TOKEN not in r.text
    assert FAKE_TOKEN not in capsys.readouterr().out


def test_scheduled_sync_log_never_exposes_token(credentialed_repo, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(vault_sync, "VAULT_LOCAL_PATH", str(tmp_path / "vault"))
    monkeypatch.setattr(vault_sync.subprocess, "run", _clone_fails)

    main._vault_sync_job()

    out = capsys.readouterr().out
    assert "[vault_scheduler] sync complete" in out
    assert FAKE_TOKEN not in out
