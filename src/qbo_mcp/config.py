"""Settings loaded from the environment and an optional ``.env`` file."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

VALID_ENVIRONMENTS = ("sandbox", "production")
DEFAULT_REDIRECT_URI = "http://localhost:8765/callback"
REQUIRED_KEYS = ("QBO_CLIENT_ID", "QBO_CLIENT_SECRET")


class ConfigError(ValueError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    client_id: str
    client_secret: str
    environment: str
    redirect_uri: str
    token_path: Path

    @classmethod
    def load(cls, dotenv_path: str | os.PathLike[str] | None = None, env: Mapping[str, str] | None = None) -> "Settings":
        """Build settings from ``env`` (default: the process environment after loading ``.env``).

        Process environment values win over the ``.env`` file. When ``env`` is supplied
        explicitly no ``.env`` file is read, which keeps tests hermetic.
        """
        if env is None:
            load_dotenv(dotenv_path=dotenv_path, override=False)
            env = os.environ

        missing = [key for key in REQUIRED_KEYS if not env.get(key)]
        if missing:
            raise ConfigError(
                "Missing required environment variable(s): "
                + ", ".join(missing)
                + ". Set them in .env or the process environment."
            )

        environment = env.get("QBO_ENVIRONMENT", "sandbox").strip().lower()
        if environment not in VALID_ENVIRONMENTS:
            raise ConfigError(
                f"QBO_ENVIRONMENT must be one of {', '.join(VALID_ENVIRONMENTS)}; got {environment!r}."
            )

        token_path_raw = env.get("QBO_TOKEN_PATH")
        if token_path_raw:
            token_path = Path(token_path_raw).expanduser()
        else:
            token_path = Path.home() / ".qbo_mcp" / f"tokens-{environment}.json"

        return cls(
            client_id=env["QBO_CLIENT_ID"],
            client_secret=env["QBO_CLIENT_SECRET"],
            environment=environment,
            redirect_uri=env.get("QBO_REDIRECT_URI") or DEFAULT_REDIRECT_URI,
            token_path=token_path,
        )
