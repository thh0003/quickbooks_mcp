from pathlib import Path

import pytest

from qbo_mcp.config import ConfigError, Settings

ALL_KEYS = ["QBO_CLIENT_ID", "QBO_CLIENT_SECRET", "QBO_ENVIRONMENT", "QBO_REDIRECT_URI", "QBO_TOKEN_PATH"]


@pytest.fixture
def clean_env(monkeypatch):
    for key in ALL_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_missing_required_variables_are_all_named(clean_env, tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        Settings.load(dotenv_path=tmp_path / "absent.env")
    message = str(excinfo.value)
    assert "QBO_CLIENT_ID" in message
    assert "QBO_CLIENT_SECRET" in message


def test_invalid_environment_names_valid_values(clean_env, tmp_path, monkeypatch):
    monkeypatch.setenv("QBO_CLIENT_ID", "id")
    monkeypatch.setenv("QBO_CLIENT_SECRET", "secret")
    monkeypatch.setenv("QBO_ENVIRONMENT", "prod")
    with pytest.raises(ConfigError) as excinfo:
        Settings.load(dotenv_path=tmp_path / "absent.env")
    assert "sandbox" in str(excinfo.value) and "production" in str(excinfo.value)


def test_defaults_applied(clean_env, tmp_path, monkeypatch):
    monkeypatch.setenv("QBO_CLIENT_ID", "id")
    monkeypatch.setenv("QBO_CLIENT_SECRET", "secret")
    settings = Settings.load(dotenv_path=tmp_path / "absent.env")
    assert settings.environment == "sandbox"
    assert settings.redirect_uri == "http://localhost:8765/callback"
    assert settings.token_path == Path.home() / ".qbo_mcp" / "tokens-sandbox.json"


def test_values_from_dotenv_file_and_process_env_wins(clean_env, tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "QBO_CLIENT_ID=file-id\nQBO_CLIENT_SECRET=file-secret\nQBO_ENVIRONMENT=production\n"
        f"QBO_TOKEN_PATH={tmp_path / 'tok.json'}\n"
    )
    monkeypatch.setenv("QBO_CLIENT_ID", "env-id")
    settings = Settings.load(dotenv_path=env_file)
    assert settings.client_id == "env-id"
    assert settings.client_secret == "file-secret"
    assert settings.environment == "production"
    assert settings.token_path == tmp_path / "tok.json"
