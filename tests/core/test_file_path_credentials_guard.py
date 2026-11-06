"""validate_file_path must block the credential store wherever it actually lives."""

import pytest

from core.utils import validate_file_path


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALLOWED_FILE_DIRS", str(tmp_path))
    monkeypatch.delenv("WORKSPACE_MCP_CREDENTIALS_DIR", raising=False)
    monkeypatch.delenv("GOOGLE_MCP_CREDENTIALS_DIR", raising=False)
    return tmp_path


def test_default_credentials_dir_is_blocked_without_env_var(fake_home):
    creds = fake_home / ".google_workspace_mcp" / "credentials"
    creds.mkdir(parents=True)
    token_file = creds / "someone@example.com.json"
    token_file.write_text('{"refresh_token": "x"}')
    with pytest.raises(ValueError, match="credential storage directory"):
        validate_file_path(str(token_file))


def test_env_configured_credentials_dir_is_still_blocked(fake_home, monkeypatch):
    creds = fake_home / "store_creds"
    creds.mkdir()
    token_file = creds / "user.json"
    token_file.write_text("{}")
    monkeypatch.setenv("WORKSPACE_MCP_CREDENTIALS_DIR", str(creds))
    with pytest.raises(ValueError, match="credential storage directory"):
        validate_file_path(str(token_file))


def test_ordinary_file_in_home_is_allowed(fake_home):
    doc = fake_home / "report.pdf"
    doc.write_bytes(b"%PDF")
    assert validate_file_path(str(doc)) == doc.resolve()
