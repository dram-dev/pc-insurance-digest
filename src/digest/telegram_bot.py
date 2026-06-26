"""Interactive Telegram ask-bot — query the P&C archive from your phone.

A long-polling listener (no public endpoint needed): it reads incoming messages
via getUpdates, runs each through the RAG `semantic.ask`, and replies in the
same chat. Built to run as a launchd daemon alongside the MLX server.

Security: it answers ONLY the configured TELEGRAM_CHAT_ID. Messages from any
other chat (someone who stumbled onto the bot) are logged and ignored.
"""
from __future__ import annotations

import html
import logging
import time

from digest_core.summarize.backends import BackendError

from digest import capture, semantic
from digest.config import settings
from digest.sinks.notify import notifier

logger = logging.getLogger(__name__)

_MAX_MSG = 4000  # Telegram hard limit is 4096; leave headroom
_HELP = (
    "🔎 <b>Ask the P&C digest archive</b>\n"
    "Send a question and I'll answer from the kept items, with sources:\n"
    "• <i>What's the latest on Florida reinsurance pricing?</i>\n"
    "• <i>Any nuclear-verdict or TPLF signals this month?</i>\n\n"
    "📥 <b>Capture</b>\n"
    "Forward an X/Twitter post, a link, or paste text — I'll file it into the "
    "digest's clipped folder for the next run. Force it with <code>/capture</code>."
)


def _esc(s: str | None) -> str:
    return html.escape(s or "", quote=False)


def _format_reply(result: dict) -> str:
    """Render an answer + numbered sources as one HTML message (length-capped)."""
    answer = result.get("answer")
    sources = result.get("sources") or []
    error = result.get("error")
    if not sources:
        if error:
            return f"⚠️ {_esc(error)}"
        return "No matching items in the archive for that one."

    parts = [_esc(answer) if answer else "<i>(synthesis unavailable — top matches below)</i>"]
    if not answer and error:
        parts.append(f"<i>{_esc(error)}</i>")
    parts.append("\n<b>Sources</b>")
    for s in sources:
        n = s.get("n")
        title = _esc(s.get("title"))
        url = s.get("url")
        line = f"[{n}] {title} <i>({_esc(s.get('source'))})</i>"
        if url:
            line += f' — <a href="{_esc(url)}">link</a>'
        parts.append(line)
    msg = "\n".join(parts)
    return msg[:_MAX_MSG]


_FORWARD_KEYS = (
    "forward_origin", "forward_from", "forward_from_chat",
    "forward_date", "forward_sender_name",
)


def _is_forward(message: dict) -> bool:
    return any(k in message for k in _FORWARD_KEYS)


def _forward_author(message: dict) -> str | None:
    """Best-effort original author of a forwarded message (new + legacy API)."""
    fo = message.get("forward_origin") or {}
    name = (
        (fo.get("sender_user") or {}).get("first_name")
        or fo.get("sender_user_name")
        or (fo.get("chat") or {}).get("title")
    )
    if name:
        return name
    ff = message.get("forward_from") or {}
    return (
        ff.get("first_name")
        or (message.get("forward_from_chat") or {}).get("title")
        or message.get("forward_sender_name")
    )


def _do_question(text: str) -> bool:
    notifier.send_chat_action("typing")
    try:
        notifier.send(_format_reply(semantic.ask(text)))
    except BackendError as exc:
        notifier.send(f"⚠️ {_esc(str(exc))}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("ask-bot: failed to answer")
        notifier.send(f"⚠️ Something went wrong answering that: {_esc(str(exc))}")
    return True


def _capture_takeaway(res: dict) -> str | None:
    """Summarize captured content immediately, reusing the digest's summarizer.

    Returns an HTML takeaway block, or None if there's nothing to summarize or
    the backend is unavailable (capture still succeeds either way).
    """
    body = res.get("body")
    if not body:
        return None
    try:
        from digest.summarize import summarize_item

        out = summarize_item({
            "source": "clipped",
            "title": res.get("title", ""),
            "url": res.get("url"),
            "content": body,
            "topic": "",
        })
    except Exception:  # noqa: BLE001
        logger.warning("ask-bot: inline summary failed", exc_info=True)
        return None

    parts = []
    if out.summary:
        parts.append(_esc(out.summary))
    if out.why_it_matters:
        parts.append(f"<i>Why it matters:</i> {_esc(out.why_it_matters)}")
    if not parts:
        return None
    head = f"🧠 <b>Takeaway</b>{f' ({_esc(out.topic)})' if out.topic else ''}"
    return head + "\n" + "\n".join(parts)


def _do_capture(text: str, message: dict) -> bool:
    notifier.send_chat_action("typing")
    try:
        res = capture.capture(text, author=_forward_author(message))
    except Exception as exc:  # noqa: BLE001
        logger.exception("ask-bot: capture failed")
        notifier.send(f"⚠️ Couldn't capture that: {_esc(str(exc))}")
        return True
    kind = {"tweet": "X post", "article": "full article", "text": "text"}.get(
        res["kind"], res["kind"]
    )
    reply = f"📥 Captured {kind} ({res['chars']} chars):\n<b>{_esc(res['title'])}</b>"
    takeaway = _capture_takeaway(res)
    if takeaway:
        reply += f"\n\n{takeaway}"
    reply += "\n\n<i>Filed for the next digest run.</i>"
    notifier.send(reply[:_MAX_MSG])
    return True


def _handle_message(message: dict) -> bool:
    """Route one inbound message: command / capture / question. True if replied.

    Forwards and messages containing a link are captured into the clipped flow;
    plain text is treated as a question. /ask and /capture force the routing.
    """
    text = (message.get("text") or message.get("caption") or "").strip()
    if not text and not _is_forward(message):
        return False

    if text.startswith("/capture"):
        return _do_capture(text[len("/capture"):].strip(), message)
    if text.startswith("/ask"):
        return _do_question(text[len("/ask"):].strip())
    if text.startswith("/"):  # /start, /help, anything else
        notifier.send(_HELP)
        return True

    if _is_forward(message) or capture.first_url(text):
        return _do_capture(text, message)
    return _do_question(text)


def _is_authorized(update: dict) -> bool:
    """Only the configured chat id may talk to the bot."""
    chat_id = (update.get("message") or {}).get("chat", {}).get("id")
    return chat_id is not None and str(chat_id) == str(settings.telegram_chat_id)


def _drain_backlog() -> int | None:
    """Skip any messages queued before startup; return the next offset to poll."""
    updates = notifier.get_updates(timeout=0)
    if not updates:
        return None
    last = updates[-1]["update_id"]
    logger.info("ask-bot: skipped %d backlog update(s)", len(updates))
    return last + 1


def run_listener(poll_timeout: int = 30) -> None:
    """Block forever, answering authorized questions. Intended for a daemon."""
    if not notifier.enabled:
        raise RuntimeError(
            "Telegram not configured — set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID."
        )
    logger.info("ask-bot: listening (chat_id=%s)", settings.telegram_chat_id)
    offset = _drain_backlog()
    while True:
        updates = notifier.get_updates(offset=offset, timeout=poll_timeout)
        for u in updates:
            offset = u["update_id"] + 1
            if not _is_authorized(u):
                logger.warning("ask-bot: ignoring update from unauthorized chat")
                continue
            _handle_message(u.get("message") or {})
        if not updates:
            time.sleep(1)  # gentle floor when long-poll returns empty/errs
