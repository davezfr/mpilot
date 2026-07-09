from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Awaitable, Callable, Optional

import httpx


TELEGRAM_API_BASE = "https://api.telegram.org"

SyncTelegramApiPost = Callable[[str, str, dict[str, Any]], dict[str, Any]]
AsyncTelegramApiPost = Callable[[httpx.AsyncClient, str, str, dict[str, Any]], Awaitable[dict[str, Any]]]


def coerce_telegram_int(value: str) -> int | str:
    stripped = str(value).strip()
    try:
        return int(stripped)
    except ValueError:
        return stripped


def telegram_message_id(data: dict[str, Any]) -> Optional[str]:
    result = data.get("result")
    if isinstance(result, dict):
        value = result.get("message_id")
        if value is not None:
            return str(value)
    return None


def telegram_edit_error_allows_replacement(error: BaseException | str, *, include_message_id_invalid: bool = True) -> bool:
    message = str(error).casefold()
    markers = [
        "message to edit not found",
        "message can't be edited",
        "message cannot be edited",
        "message identifier is not specified",
    ]
    if include_message_id_invalid:
        markers.append("message_id_invalid")
    return any(marker in message for marker in markers)


def telegram_api_post(token: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        "%s/bot%s/%s" % (TELEGRAM_API_BASE, token, method),
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20.0) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or "Telegram request failed") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise RuntimeError("Telegram request failed: %s" % error) from error
    return _checked_telegram_response(data)


async def async_telegram_api_post(
    client: httpx.AsyncClient,
    token: str,
    method: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    response = await client.post("%s/bot%s/%s" % (TELEGRAM_API_BASE, token, method), json=payload)
    response.raise_for_status()
    return _checked_telegram_response(response.json())


def send_or_edit_telegram_status_message(
    *,
    token: str,
    chat_id: str,
    thread_id: Optional[str],
    message: str,
    message_id: Optional[str],
    api_post: SyncTelegramApiPost = telegram_api_post,
    include_message_id_invalid: bool = False,
) -> Optional[str]:
    if message_id:
        try:
            data = api_post(
                token,
                "editMessageText",
                {
                    "chat_id": coerce_telegram_int(chat_id),
                    "message_id": coerce_telegram_int(message_id),
                    "text": message,
                    "disable_web_page_preview": True,
                },
            )
            return telegram_message_id(data) or message_id
        except RuntimeError as error:
            if "message is not modified" in str(error).casefold():
                return message_id
            if not telegram_edit_error_allows_replacement(
                error,
                include_message_id_invalid=include_message_id_invalid,
            ):
                raise

    payload: dict[str, Any] = {
        "chat_id": coerce_telegram_int(chat_id),
        "text": message,
        "disable_web_page_preview": True,
    }
    if thread_id:
        payload["message_thread_id"] = coerce_telegram_int(thread_id)
    data = api_post(token, "sendMessage", payload)
    return telegram_message_id(data)


async def async_send_or_edit_telegram_status_message(
    *,
    token: str,
    chat_id: str,
    thread_id: Optional[str],
    message: str,
    message_id: Optional[str],
    buttons: list[dict[str, str]] | None = None,
    reply_markup: dict[str, Any] | None = None,
    api_post: AsyncTelegramApiPost = async_telegram_api_post,
    include_message_id_invalid: bool = True,
    return_existing_on_unreplaceable_edit_error: bool = True,
) -> Optional[str]:
    async with httpx.AsyncClient(timeout=20.0) as client:
        if message_id:
            try:
                payload: dict[str, Any] = {
                    "chat_id": chat_id,
                    "message_id": coerce_telegram_int(message_id),
                    "text": message,
                    "disable_web_page_preview": True,
                }
                if buttons is not None:
                    payload["reply_markup"] = reply_markup or {"inline_keyboard": []}
                data = await api_post(client, token, "editMessageText", payload)
                return telegram_message_id(data) or message_id
            except RuntimeError as error:
                if "message is not modified" in str(error).casefold():
                    return message_id
                if not telegram_edit_error_allows_replacement(
                    error,
                    include_message_id_invalid=include_message_id_invalid,
                ):
                    if return_existing_on_unreplaceable_edit_error:
                        return message_id
                    raise

        payload = {
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        if thread_id:
            payload["message_thread_id"] = coerce_telegram_int(thread_id)
        data = await api_post(client, token, "sendMessage", payload)
        return telegram_message_id(data)


def _checked_telegram_response(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise RuntimeError("Telegram returned an unexpected response")
    if data.get("ok") is not True:
        description = str(data.get("description") or "Telegram request failed")
        raise RuntimeError(description)
    return data
