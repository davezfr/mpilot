from __future__ import annotations

import json
from hmac import compare_digest

from fastapi import HTTPException, Request

from mpilot.acquisition.env import env_first


class ApiAuthConfigurationError(RuntimeError):
    pass


def authenticate_api_key(provided_api_key: str) -> tuple[bool, str | None]:
    """Return (authenticated, requester principal).

    A global API key is an operator/admin principal and therefore returns None.
    Requester-specific keys return their bound requester ID.
    """
    configured_requester_keys = requester_api_keys()
    global_key = (env_first("QBITLARR_API_KEY", default="") or "").strip()
    if global_key and compare_digest(provided_api_key, global_key):
        return True, None

    for requester_id, api_key in configured_requester_keys.items():
        if compare_digest(provided_api_key, api_key):
            return True, requester_id
    return False, None


def requester_api_keys() -> dict[str, str]:
    raw = (env_first("QBITLARR_REQUESTER_API_KEYS", default="") or "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiAuthConfigurationError("MPILOT_ACQUISITION_REQUESTER_API_KEYS must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise ApiAuthConfigurationError("MPILOT_ACQUISITION_REQUESTER_API_KEYS must be a JSON object")

    normalized: dict[str, str] = {}
    global_key = (env_first("QBITLARR_API_KEY", default="") or "").strip()
    for requester_id, api_key in payload.items():
        requester = str(requester_id).strip()
        if not isinstance(api_key, str):
            raise ApiAuthConfigurationError("requester API keys must be strings")
        secret = api_key.strip()
        if not requester or not secret:
            raise ApiAuthConfigurationError("requester API key entries must have non-empty requester IDs and keys")
        if secret in normalized.values():
            raise ApiAuthConfigurationError("requester API keys must be unique")
        if global_key and compare_digest(secret, global_key):
            raise ApiAuthConfigurationError("requester API keys must differ from the administrator API key")
        normalized[requester] = secret
    return normalized


def bind_requester(request: Request, requested_id: str | None) -> str | None:
    """Bind a caller-supplied requester ID to its authenticated principal."""
    principal = getattr(request.state, "auth_requester_id", None)
    if getattr(request.state, "auth_is_admin", False):
        return requested_id
    if not principal:
        raise HTTPException(status_code=403, detail="Requester identity is not authenticated")
    if requested_id is not None and requested_id != principal:
        raise HTTPException(status_code=403, detail="Requester identity does not match API key")
    return principal


def require_snapshot_owner(request: Request, owner_id: str | None) -> None:
    principal = getattr(request.state, "auth_requester_id", None)
    if getattr(request.state, "auth_is_admin", False):
        return
    if not principal or owner_id != principal:
        raise HTTPException(status_code=404, detail="Query snapshot not found")
