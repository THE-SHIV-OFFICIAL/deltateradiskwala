"""Telegram-friendly formatting helpers.

Telegram custom emoji IDs are account/sticker-pack specific. The bot therefore
accepts verified IDs from configuration and always keeps a Unicode fallback so
messages remain readable when an ID is missing or invalid.
"""

from __future__ import annotations

import re

from pyrogram.enums import MessageEntityType
from pyrogram.types import MessageEntity


EMOJI_FALLBACKS = {
    "welcome": "👋",
    "success": "✅",
    "error": "⚠️",
    "download": "📥",
    "upload": "📤",
    "premium": "💎",
    "broadcast": "📢",
    "logger": "🧾",
    "refresh": "🔄",
    "admin": "🛡️",
    "stats": "📊",
}
MARKER_RE = re.compile(r"\[\[emoji:([a-z0-9_]+)\]\]")


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def render_premium(
    text: str, custom_emoji_ids: dict[str, int]
) -> tuple[str, list[MessageEntity]]:
    """Replace markers and attach Telegram custom-emoji entities.

    Example marker: ``[[emoji:premium]]``. If no ID is configured, the marker
    becomes the matching Unicode emoji and no custom entity is emitted.
    """

    output: list[str] = []
    entities: list[MessageEntity] = []
    cursor = 0
    output_length = 0

    for match in MARKER_RE.finditer(text):
        before = text[cursor : match.start()]
        output.append(before)
        output_length += _utf16_length(before)

        key = match.group(1)
        fallback = EMOJI_FALLBACKS.get(key, "🔹")
        output.append(fallback)
        if key in custom_emoji_ids:
            entities.append(
                MessageEntity(
                    type=MessageEntityType.CUSTOM_EMOJI,
                    offset=output_length,
                    length=_utf16_length(fallback),
                    custom_emoji_id=custom_emoji_ids[key],
                )
            )
        output_length += _utf16_length(fallback)
        cursor = match.end()

    tail = text[cursor:]
    output.append(tail)
    return "".join(output), entities