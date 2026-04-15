"""Configuration loading and validation from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Raised when the configuration fails validation."""


@dataclass(frozen=True)
class Config:
    """Process-wide configuration.

    Fields:
        trueconf_server: TrueConf Server address without scheme (`video.example.com`).
        trueconf_token: ready JWT, or None if login/password is used instead.
        trueconf_username: bot account login for `from_credentials`.
        trueconf_password: bot account password.
        trueconf_verify_ssl: whether to verify the TrueConf server TLS certificate.
        trueconf_web_port: WebSocket port.
        webhook_public_url: public base URL used in generated links (no trailing `/`).
        webhook_http_host: interface for the incoming-webhook HTTP server.
        webhook_http_port: HTTP server port.
        webhook_storage_path: absolute path to the JSON state file.
        webhook_admin_only: whether only OWNER/ADMIN can manage hooks.
        webhook_rate_limit_per_minute: per-token incoming POST limit, per minute.
        webhook_rate_limit_per_ip_per_minute: per-IP incoming POST limit, per minute.
        webhook_max_upload_mb: maximum incoming POST body size in megabytes.
        webhook_max_attachments: maximum number of attachments per request.
    """

    trueconf_server: str
    trueconf_token: str | None
    trueconf_username: str | None
    trueconf_password: str | None
    trueconf_verify_ssl: bool
    trueconf_web_port: int
    webhook_public_url: str
    webhook_http_host: str
    webhook_http_port: int
    webhook_storage_path: Path
    webhook_admin_only: bool
    webhook_rate_limit_per_minute: int
    webhook_rate_limit_per_ip_per_minute: int
    webhook_max_upload_mb: int
    webhook_max_attachments: int

    @property
    def uses_credentials(self) -> bool:
        """True when authentication goes through login/password (supervisor refresh is active)."""
        return self.trueconf_token is None


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"Invalid boolean value: {value!r}")


def _parse_int(value: str | None, default: int, name: str) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"Invalid integer value for {name}: {value!r}") from exc


def _require(value: str | None, name: str) -> str:
    if value is None or value.strip() == "":
        raise ConfigError(f"Required environment variable {name} is not set")
    return value.strip()


def load_config(env_path: Path | None = None) -> Config:
    """Load configuration from `.env` and the environment.

    Priority rule: `TRUECONF_TOKEN` -> fixed token; otherwise the pair
    `TRUECONF_USERNAME` + `TRUECONF_PASSWORD` is required. Violations raise
    `ConfigError` with a descriptive message.

    Args:
        env_path: optional path to a `.env` file. When omitted, dotenv discovery
                  uses its usual rules.

    Returns: a populated `Config`.

    Raises: `ConfigError` on missing required fields or invalid values.
    """
    if env_path is not None:
        load_dotenv(dotenv_path=env_path, override=False)
    else:
        load_dotenv(override=False)

    server = _require(os.getenv("TRUECONF_SERVER"), "TRUECONF_SERVER")

    token = os.getenv("TRUECONF_TOKEN")
    token = token.strip() if token else None
    if token == "":
        token = None

    username = os.getenv("TRUECONF_USERNAME")
    password = os.getenv("TRUECONF_PASSWORD")
    username = username.strip() if username else None
    password = password.strip() if password else None
    if username == "":
        username = None
    if password == "":
        password = None

    if token is None:
        if not username or not password:
            raise ConfigError(
                "No authentication configured: set either TRUECONF_TOKEN or "
                "the pair TRUECONF_USERNAME + TRUECONF_PASSWORD."
            )

    public_url = _require(os.getenv("WEBHOOK_PUBLIC_URL"), "WEBHOOK_PUBLIC_URL").rstrip("/")

    storage_raw = os.getenv("WEBHOOK_STORAGE_PATH") or "data/webhooks.json"
    storage_path = Path(storage_raw).expanduser().resolve()

    return Config(
        trueconf_server=server,
        trueconf_token=token,
        trueconf_username=username,
        trueconf_password=password,
        trueconf_verify_ssl=_parse_bool(os.getenv("TRUECONF_VERIFY_SSL"), default=True),
        trueconf_web_port=_parse_int(os.getenv("TRUECONF_WEB_PORT"), 443, "TRUECONF_WEB_PORT"),
        webhook_public_url=public_url,
        webhook_http_host=os.getenv("WEBHOOK_HTTP_HOST") or "0.0.0.0",
        webhook_http_port=_parse_int(os.getenv("WEBHOOK_HTTP_PORT"), 8080, "WEBHOOK_HTTP_PORT"),
        webhook_storage_path=storage_path,
        webhook_admin_only=_parse_bool(os.getenv("WEBHOOK_ADMIN_ONLY"), default=True),
        webhook_rate_limit_per_minute=_parse_int(
            os.getenv("WEBHOOK_RATE_LIMIT_PER_MINUTE"), 60, "WEBHOOK_RATE_LIMIT_PER_MINUTE"
        ),
        webhook_rate_limit_per_ip_per_minute=_parse_int(
            os.getenv("WEBHOOK_RATE_LIMIT_PER_IP_PER_MINUTE"),
            120, "WEBHOOK_RATE_LIMIT_PER_IP_PER_MINUTE",
        ),
        webhook_max_upload_mb=_parse_int(
            os.getenv("WEBHOOK_MAX_UPLOAD_MB"), 25, "WEBHOOK_MAX_UPLOAD_MB"
        ),
        webhook_max_attachments=_parse_int(
            os.getenv("WEBHOOK_MAX_ATTACHMENTS"), 10, "WEBHOOK_MAX_ATTACHMENTS"
        ),
    )
