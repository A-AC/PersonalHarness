"""First-run setup: validate values, probe the endpoint, apply the wizard.

The wizard, ``harness init``, and the Docker entrypoint all funnel through
these helpers so a value accepted in one place is accepted everywhere. The
functions do not touch the database; applying setup only updates the config
file (the process picks up the new values immediately because it shares the
same ``Config`` object).
"""

from __future__ import annotations

import os
import zoneinfo
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import util
from .config import (
    DEFAULTS,
    ENV_OVERRIDES,
    Config,
    is_system_timezone_name,
    save_config,
)

MAX_BASE_URL_CHARS = 500
MAX_TOKEN_CHARS = 512
MAX_MODEL_NAME_CHARS = 200
MAX_API_KEY_CHARS = 2000


class SetupError(ValueError):
    """A user-correctable setup error with the HTTP status to report."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = int(status)


def normalize_base_url(value: str) -> str:
    """Return a clean ``scheme://host`` base URL without ``/v1``.

    The stored value is the root the model client appends its paths to, so
    ``http://host/v1/`` and ``http://host`` must mean the same thing.
    """
    if not isinstance(value, str):
        raise SetupError("model base URL must be a string")
    text = value.strip()
    if not text:
        raise SetupError("model base URL is required")
    if len(text) > MAX_BASE_URL_CHARS:
        raise SetupError(
            f"model base URL must be at most {MAX_BASE_URL_CHARS} characters"
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise SetupError("model base URL contains control characters")
    parts = urlsplit(text)
    if parts.scheme.lower() not in ("http", "https"):
        raise SetupError(
            "model base URL must start with http:// or https://"
        )
    if not parts.hostname:
        raise SetupError("model base URL must include a host")
    normalized = text.rstrip("/")
    if normalized.lower().endswith("/v1"):
        normalized = normalized[:-3].rstrip("/")
    if not normalized:
        raise SetupError("model base URL must include a host")
    return normalized


def validate_setup_values(
    *,
    token: Any,
    model_base_url: Any,
    model_name: Any,
    timezone: Any = "system",
    api_key: Any = "",
) -> dict[str, str]:
    """Validate and normalise wizard/init values without mutating anything."""
    if not isinstance(token, str):
        raise SetupError("token is required")
    clean_token = token.strip()
    if not clean_token:
        raise SetupError("token is required")
    if len(clean_token) > MAX_TOKEN_CHARS:
        raise SetupError(
            f"token must be at most {MAX_TOKEN_CHARS} characters"
        )

    base_url = normalize_base_url(model_base_url)

    if not isinstance(model_name, str):
        raise SetupError("model name is required")
    clean_model = model_name.strip()
    if not clean_model:
        raise SetupError("model name is required")
    if len(clean_model) > MAX_MODEL_NAME_CHARS:
        raise SetupError(
            f"model name must be at most {MAX_MODEL_NAME_CHARS} characters"
        )

    if timezone is None:
        zone_name = "system"
    elif not isinstance(timezone, str):
        raise SetupError("timezone must be a string")
    else:
        zone_name = timezone.strip()
    if not zone_name or is_system_timezone_name(zone_name):
        zone_name = "system"
    else:
        try:
            zoneinfo.ZoneInfo(zone_name)
        except Exception as exc:
            raise SetupError(f"unknown timezone: {zone_name}") from exc

    if api_key is None:
        clean_key = ""
    elif not isinstance(api_key, str):
        raise SetupError("api key must be a string")
    else:
        clean_key = api_key
    if len(clean_key) > MAX_API_KEY_CHARS:
        raise SetupError(
            f"api key must be at most {MAX_API_KEY_CHARS} characters"
        )

    return {
        "token": clean_token,
        "model_base_url": base_url,
        "model_name": clean_model,
        "timezone": zone_name,
        "api_key": clean_key,
    }


def probe_model_endpoint(
    base_url: Any,
    *,
    api_key: str = "",
    model: str = "",
    timeout_s: float = 4.0,
) -> dict[str, Any]:
    """GET ``<base>/v1/models`` and report what happened. Never raises."""
    result: dict[str, Any] = {
        "base_url": str(base_url or ""),
        "reachable": False,
        "models": [],
        "model_listed": None,
        "error": "",
    }
    try:
        normalized = normalize_base_url(base_url)
    except SetupError as exc:
        result["error"] = str(exc)
        return result
    result["base_url"] = normalized

    headers = {}
    if isinstance(api_key, str) and api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    timeout = httpx.Timeout(timeout_s, connect=min(2.0, timeout_s))
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(f"{normalized}/v1/models", headers=headers)
    except Exception as exc:  # connection errors, invalid URLs, timeouts
        result["error"] = f"models probe failed: {exc}"
        return result

    result["reachable"] = response.status_code < 500
    if response.status_code != 200:
        result["error"] = (
            f"endpoint returned {response.status_code}: "
            f"{response.text[:200]}"
        )
        return result
    try:
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise ValueError("expected an object with a data list")
    except Exception as exc:
        result["error"] = f"malformed models response: {exc}"
        return result

    names = [
        str(item["id"])
        for item in data["data"]
        if isinstance(item, dict) and item.get("id")
    ]
    result["models"] = names[:200]
    if isinstance(model, str) and model:
        result["model_listed"] = model in names
    return result


def apply_setup(
    config: Config, values: dict[str, Any], *, save: bool = True
) -> dict[str, str]:
    """Validate and persist the core setup values on ``config``."""
    normalized = validate_setup_values(
        token=values.get("token"),
        model_base_url=values.get("model_base_url"),
        model_name=values.get("model_name"),
        timezone=values.get("timezone") or "system",
        api_key=values.get("api_key") or "",
    )
    config.set("server.token", normalized["token"])
    config.set("model.base_url", normalized["model_base_url"])
    config.set("model.name", normalized["model_name"])
    config.set("model.api_key", normalized["api_key"])
    config.set("timezone", normalized["timezone"])
    config.set("setup.complete", True)
    config.set("setup.completed_at", util.iso())
    if save and config.path is not None:
        save_config(config)
    return normalized


def setup_state(config: Config) -> dict[str, Any]:
    """Safe wizard state; never includes the token or api key value."""
    env = os.environ
    return {
        "required": not bool(config.get("setup.complete")),
        "model_base_url": str(config.get("model.base_url") or ""),
        "model_name": str(config.get("model.name") or ""),
        "timezone": str(config.get("timezone") or "system"),
        "system_timezone": util.system_tz_name(),
        "env_overrides": [
            name for name in ENV_OVERRIDES if env.get(name)
        ],
    }


def values_from_env(environ: dict[str, str]) -> dict[str, str]:
    """Non-interactive bootstrap values from the container environment."""
    env = environ or {}
    token = str(env.get("HARNESS_TOKEN") or "").strip()
    if not token:
        raise SetupError("HARNESS_TOKEN is required")
    base_url = str(env.get("HARNESS_MODEL_BASE_URL") or "").strip()
    if not base_url:
        raise SetupError("HARNESS_MODEL_BASE_URL is required")
    model_name = str(
        env.get("HARNESS_MODEL_NAME") or DEFAULTS["model"]["name"]
    )
    timezone = str(env.get("HARNESS_TIMEZONE") or "system")
    api_key = str(env.get("HARNESS_MODEL_API_KEY") or "")
    return validate_setup_values(
        token=token,
        model_base_url=base_url,
        model_name=model_name,
        timezone=timezone,
        api_key=api_key,
    )
