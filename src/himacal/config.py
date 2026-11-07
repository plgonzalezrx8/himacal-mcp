"""Deployment settings and provider account configuration."""

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field

from himacal.errors import ProviderError


class CalDAVConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str
    username: str = Field(min_length=1)
    password_file: Path
    timezone: str = "UTC"


class AccountConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: dict[str, Any] | None = None
    caldav: CalDAVConfig | None = None


@dataclass(frozen=True)
class Settings:
    accounts: dict[str, AccountConfig] = field(repr=False)
    mcp_password: str = field(repr=False)
    public_url: str = "http://localhost:8000"
    himalaya_binary: str = "himalaya"
    command_timeout: float = 60.0

    def require_account(self, account_id: str, capability: Literal["email", "caldav"]):
        account = self.accounts.get(account_id)
        if account is None:
            raise ProviderError("account_not_found", "Unknown account identifier.")
        if getattr(account, capability) is None:
            raise ProviderError(
                "unsupported", "This account does not have that provider configured."
            )
        return account


def load_settings() -> Settings:
    config_path = Path(os.environ.get("HIMACAL_CONFIG", "himacal.toml")).resolve()
    secret_path = os.environ.get("HIMACAL_MCP_PASSWORD_FILE")
    try:
        if not secret_path:
            raise ValueError
        password = Path(secret_path).read_text(encoding="utf-8").strip()
        if len(password) < 32 or any(ord(char) < 33 or ord(char) > 126 for char in password):
            raise ValueError
    except (OSError, UnicodeError, ValueError):
        raise ValueError(
            "MCP password must be a readable, single-line secret of at least 32 characters."
        ) from None

    public_url = os.environ.get("HIMACAL_PUBLIC_URL", "http://localhost:8000").rstrip("/")
    try:
        parsed = urlsplit(public_url)
        _ = parsed.port
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or (
                parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            )
        ):
            raise ValueError
    except ValueError:
        raise ValueError(
            "HIMACAL_PUBLIC_URL must be an HTTPS origin, or HTTP on localhost, without a path or credentials."
        ) from None

    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
        if set(raw) != {"accounts"} or not isinstance(raw["accounts"], dict) or not raw["accounts"]:
            raise ValueError
        accounts = {}
        for account_id, value in raw["accounts"].items():
            if (
                not account_id
                or len(account_id) > 100
                or not all(
                    char.isascii() and (char.isalnum() or char in "_-") for char in account_id
                )
            ):
                raise ValueError
            account = AccountConfig.model_validate(value)
            if account.email is None and account.caldav is None:
                raise ValueError
            if account.email is not None:
                backends = {
                    "imap",
                    "smtp",
                    "jmap",
                    "gmail",
                    "msgraph",
                    "maildir",
                    "m2dir",
                    "mbox",
                    "pimdir",
                    "sieve",
                }
                if not backends.intersection(account.email):
                    raise ValueError
            if account.caldav is not None:
                endpoint = urlsplit(account.caldav.url)
                if (
                    endpoint.scheme not in {"http", "https"}
                    or not endpoint.hostname
                    or endpoint.username is not None
                ):
                    raise ValueError
                ZoneInfo(account.caldav.timezone)
                path = account.caldav.password_file
                if not path.is_absolute():
                    path = config_path.parent / path
                account.caldav.password_file = path.resolve()
                if not path.is_file() or not path.read_text(encoding="utf-8").strip():
                    raise ValueError
            accounts[account_id] = account
    except (OSError, ValueError, TypeError, KeyError, ZoneInfoNotFoundError):
        # Parsing libraries include input values in their errors; none are safe to log here.
        raise ValueError(
            "Invalid account configuration: check account fields, endpoints, timezones and secret files."
        ) from None
    return Settings(
        accounts=accounts,
        mcp_password=password,
        public_url=public_url,
        himalaya_binary=os.environ.get("HIMACAL_HIMALAYA_BINARY", "himalaya"),
    )
