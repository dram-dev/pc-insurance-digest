"""Telegram Bot API transport — pure send/poll, no domain content.

Mirrors `digest_core.sinks.databricks`: the client lives here, each domain
constructs the singleton from its own settings. Everything a digest says
(message formatting, what counts as a signal, quiet hours) stays domain-side.

Messages are sent with parse_mode=HTML, which only needs `< > &` escaped —
far safer than MarkdownV2's dozen special characters. Nothing here raises into
a pipeline: failures are logged and swallowed so a push problem can't break a
run.
"""
from __future__ import annotations

import html
import logging

import requests

logger = logging.getLogger(__name__)

_TIMEOUT = 10


def esc(s: str | None) -> str:
    """Escape the three chars Telegram HTML mode cares about (< > &)."""
    return html.escape(s or "", quote=False)


def href(val) -> str | None:
    """An http(s) URL safe to interpolate into an href, or None.

    Telegram only accepts http/https/tg in a link and 400s on anything else, and
    an unescaped quote would terminate the attribute — either way it rejects the
    whole message, which `send` can only log. Non-web links (obsidian://) must be
    rendered as plain text instead.
    """
    s = str(val or "").strip()
    if not s.lower().startswith(("http://", "https://", "tg://")):
        return None
    return html.escape(s, quote=True)


def join_within(parts: list[str], limit: int) -> str:
    """Join parts with newlines, dropping whole trailing parts that don't fit.

    Slicing assembled HTML instead cuts mid-tag or mid-entity, and Telegram
    rejects the entire message rather than just the broken tail — so the user
    gets silence instead of a slightly short message.
    """
    out: list[str] = []
    used = 0
    for p in parts:
        cost = len(p) + (1 if out else 0)
        if used + cost > limit:
            break
        out.append(p)
        used += cost
    return "\n".join(out)


class TelegramNotifier:
    """Telegram Bot API client. Disabled (no-op) unless token + chat id set.

    Send-only by default; `get_updates` enables an interactive bot listener.
    """

    def __init__(self, token: str, chat_id: str, enabled: bool) -> None:
        self.token = token
        self.chat_id = chat_id
        self.enabled = enabled and bool(token) and bool(chat_id)

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def send(self, text: str) -> bool:
        """POST one HTML message. True on success; False on no-op or any failure."""
        if not self.enabled:
            logger.debug("notify: disabled or unconfigured; skipping send")
            return False
        try:
            resp = requests.post(
                self._url("sendMessage"),
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            logger.warning("notify: send failed: %s", exc)
            return False
        return True

    def send_chat_action(self, action: str = "typing") -> None:
        """Best-effort 'typing…' indicator while a reply is being prepared."""
        if not self.enabled:
            return
        try:
            requests.post(
                self._url("sendChatAction"),
                json={"chat_id": self.chat_id, "action": action},
                timeout=_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("notify: chat action failed: %s", exc)

    def get_updates(self, offset: int | None = None, timeout: int = 30) -> list[dict] | None:
        """Long-poll for incoming updates.

        Returns a list on success (possibly empty) and None on failure, so the
        caller can tell "no new messages" from "the request failed" — a failure
        returns instantly rather than after the long poll, so treating the two
        alike turns a persistent 401/409 into a ~1 req/s hammer.
        """
        if not self.enabled:
            return None
        params: dict = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        try:
            resp = requests.get(
                self._url("getUpdates"), params=params, timeout=timeout + 10
            )
            resp.raise_for_status()
            return resp.json().get("result", [])
        except Exception as exc:  # noqa: BLE001
            logger.warning("notify: get_updates failed: %s", exc)
            return None
