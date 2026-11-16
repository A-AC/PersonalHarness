"""Provider resolution for Phase 9A.

One OpenAI-compatible endpoint per provider, with no silent fallback.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from .config import (
    Config,
    ConfigError,
    validate_allowed_models,
    validate_providers,
)


DEFAULT_PROVIDER_ID = "default"


class ModelSelectionError(ValueError):
    """Invalid or unverifiable model/provider selection."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = int(status)


@dataclass(frozen=True)
class Provider:
    id: str
    base_url: str
    api_key_env: str = ""
    models: tuple[str, ...] = ()
    api_key: str = ""
    builtin: bool = False


def provider_records(
    config: Config, environ: dict[str, str] | None = None,
) -> list[Provider]:
    """Resolve configured providers and the built-in default endpoint."""
    configured = validate_providers(config.get("providers", []) or [])
    env = os.environ if environ is None else environ
    records: list[Provider] = []
    has_default = False
    for entry in configured:
        api_key_env = entry["api_key_env"]
        api_key = (
            str(env.get(api_key_env, "") or "") if api_key_env else ""
        )
        records.append(Provider(
            id=entry["id"],
            base_url=entry["base_url"],
            api_key_env=api_key_env,
            models=tuple(entry["models"]),
            api_key=api_key,
            builtin=False,
        ))
        if entry["id"] == DEFAULT_PROVIDER_ID:
            has_default = True

    if not has_default:
        builtin = Provider(
            id=DEFAULT_PROVIDER_ID,
            base_url=str(config.get("model.base_url", "")),
            api_key=str(config.get("model.api_key", "") or ""),
            builtin=True,
            models=(),
        )
        records.insert(0, builtin)
    return records


def get_provider(
    config: Config,
    provider_id: str | None = None,
    environ: dict[str, str] | None = None,
) -> Provider:
    """Resolve one explicitly selected provider without fallback."""
    wanted = str(provider_id or "").strip() or str(
        config.get("model.provider", "") or ""
    ).strip()
    if wanted:
        for provider in provider_records(config, environ):
            if provider.id == wanted:
                return provider
        raise ModelSelectionError(f"unknown model provider: {wanted}")
    for provider in provider_records(config, environ):
        if provider.id == DEFAULT_PROVIDER_ID:
            return provider
    raise ModelSelectionError(f"unknown model provider: {DEFAULT_PROVIDER_ID}")


def current_provider(
    config: Config, environ: dict[str, str] | None = None,
) -> Provider:
    """Return the provider selected by the current configuration."""
    return get_provider(config, None, environ)


def configured_provider_id(config: Config) -> str:
    """Return the intended provider id without resolving or raising."""
    return str(config.get("model.provider", "") or "").strip() or (
        DEFAULT_PROVIDER_ID
    )


def allowed_models(config: Config) -> tuple[str, ...]:
    """Return the configured model allowlist, or empty when invalid."""
    try:
        return tuple(validate_allowed_models(
            config.get("model.allowed", []) or []
        ))
    except ConfigError:
        return ()


def selectable_models(
    config: Config,
    provider: Provider,
    advertised: list[str] | None = None,
) -> list[str]:
    """Intersect advertised/provider models with the optional allowlist."""
    if advertised is None:
        base = list(provider.models) or list(allowed_models(config))
    else:
        base = list(advertised)
        if provider.models:
            base = [name for name in base if name in provider.models]

    allowed = allowed_models(config)
    if allowed:
        base = [name for name in base if name in allowed]

    unique: list[str] = []
    seen: set[str] = set()
    for name in base:
        if name not in seen:
            seen.add(name)
            unique.append(name)
    return unique


def provider_catalogue(
    config: Config,
    lister: Any = None,
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build the provider/model catalogue used by the model picker."""
    providers = provider_records(config, environ)
    selection_error = ""
    try:
        current = get_provider(config, None, environ)
    except ModelSelectionError as exc:
        current = None
        selection_error = str(exc)
    configured_id = configured_provider_id(config)
    current_id = current.id if current is not None else configured_id
    current_base_url = current.base_url if current is not None else ""
    configured_name = str(config.get("model.name", ""))
    context_window = int(config.get("model.context_window", 0) or 0)
    provider_payloads: list[dict[str, Any]] = []
    flattened: list[dict[str, Any]] = []

    for provider in providers:
        if callable(lister):
            try:
                raw_info = lister(provider.id)
                info = raw_info if isinstance(raw_info, dict) else {}
            except Exception as exc:
                info = {
                    "reachable": False,
                    "models": [],
                    "error": str(exc),
                }
        else:
            info = {
                "reachable": False,
                "models": [],
                "error": "no model client available",
            }

        reachable = bool(info.get("reachable", False))
        error = str(info.get("error") or "")
        advertised = list(info.get("models") or []) if reachable else []
        names = selectable_models(
            config,
            provider,
            advertised if reachable else None,
        )

        if provider.id == current_id and configured_name not in names:
            names.append(configured_name)

        model_entries: list[dict[str, Any]] = []
        for name in names:
            source = (
                "advertised"
                if reachable and name in advertised
                else "configured"
            )
            entry = {
                "name": name,
                "provider": provider.id,
                "context_window": context_window,
                "reachable": reachable,
                "current": (
                    provider.id == current_id and name == configured_name
                ),
                "source": source,
            }
            model_entries.append(entry)
            flattened.append(entry)

        provider_payloads.append({
            "id": provider.id,
            "base_url": provider.base_url,
            "builtin": provider.builtin,
            "reachable": reachable,
            "error": error,
            "advertised": advertised,
            "models": model_entries,
        })

    if current is None and configured_name:
        flattened.append({
            "name": configured_name,
            "provider": configured_id,
            "context_window": context_window,
            "reachable": False,
            "current": True,
            "source": "configured",
        })

    return {
        "current": {
            "provider": current_id,
            "name": configured_name,
            "context_window": context_window,
            "base_url": current_base_url,
            "error": selection_error,
        },
        "allowed": list(allowed_models(config)),
        "context_window": context_window,
        "restart_required": False,
        "providers": provider_payloads,
        "models": flattened,
    }


def verify_model_selection(
    config: Config,
    name: str,
    provider_id: str | None = None,
    lister: Any = None,
    environ: dict[str, str] | None = None,
) -> tuple[Provider, bool, dict[str, Any]]:
    """Verify that a model is available from one explicitly selected provider."""
    if not isinstance(name, str) or not name.strip():
        raise ModelSelectionError("model name is required")
    selected_name = name.strip()
    provider = get_provider(config, provider_id, environ)

    if callable(lister):
        try:
            raw_info = lister(provider.id)
            info = raw_info if isinstance(raw_info, dict) else {}
        except Exception as exc:
            info = {
                "reachable": False,
                "models": [],
                "error": str(exc),
            }
    else:
        info = {
            "reachable": False,
            "models": [],
            "error": "no model client available",
        }

    if bool(info.get("reachable", False)):
        choices = selectable_models(
            config, provider, list(info.get("models") or [])
        )
        if selected_name not in choices:
            choice_text = ", ".join(choices) or "none"
            raise ModelSelectionError(
                f"model {selected_name!r} is not available from provider "
                f"{provider.id}; choose one of: {choice_text}"
            )
        return provider, True, info

    static = selectable_models(config, provider, None)
    if selected_name in static:
        return provider, False, info
    detail = info.get("error") or "no response"
    raise ModelSelectionError(
        f"model provider {provider.id} is unreachable ({detail}); cannot "
        f"verify model {selected_name!r}",
        status=503,
    )
