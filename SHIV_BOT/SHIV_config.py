"""Environment-backed configuration for the SHIV DeltaTera bot.

No credentials are stored in source code. Copy .env.example to .env and
provide the values required for the features you enable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))
load_dotenv()


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}")
    return value


def _csv_int(name: str) -> tuple[int, ...]:
    values: list[int] = []
    for item in os.getenv(name, "").split(","):
        item = item.strip()
        if item:
            try:
                values.append(int(item))
            except ValueError as exc:
                raise RuntimeError(f"{name} contains an invalid Telegram ID") from exc
    return tuple(dict.fromkeys(values))


def _csv(name: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in os.getenv(name, "").split(",") if item.strip())


def _custom_emoji_ids() -> dict[str, int]:
    """Read ``key:id,key:id`` custom emoji configuration safely."""
    raw = os.getenv("CUSTOM_EMOJI_IDS", "").strip()
    if not raw:
        return {}
    values: dict[str, int] = {}
    for item in raw.split(","):
        key, separator, value = item.strip().partition(":")
        if not separator or not key or not value.isdigit():
            raise RuntimeError(
                "CUSTOM_EMOJI_IDS must use key:numeric_id pairs separated by commas"
            )
        values[key.lower()] = int(value)
    return values


# Public, key-free Terabox resolver endpoints (fallback order).
DEFAULT_TERABOX_API_URL: Final[str] = "https://tera-core.vercel.app/api"
DEFAULT_TERABOX_FALLBACK_API_URL: Final[str] = "https://terasnap.netlify.app/api"
# Diskwala-style public mirror (no key).
DEFAULT_DISKWALLA_API_URL: Final[str] = "https://terabox.hnn.workers.dev/api"

# Owner fallback if ADMIN_IDS/OWNER_ID are not set in .env
DEFAULT_ADMIN_IDS: Final[tuple[int, ...]] = (8418584090,)


@dataclass(frozen=True)
class Plan:
    key: str
    title: str
    days: int
    price_inr: int
    downloads_per_day: int | None
    uploads_per_day: int | None


PLANS: Final[dict[str, Plan]] = {
    "pro_7d": Plan("pro_7d", "Pro · 7 days", 7, 15, 17, 15),
    "pro_30d": Plan("pro_30d", "Pro · 30 days", 30, 27, 17, 15),
    "beta_30d": Plan("beta_30d", "Beta VIP · 30 days", 30, 69, None, None),
}


@dataclass(frozen=True)
class Settings:
    bot_token: str
    api_id: int
    api_hash: str
    admin_ids: tuple[int, ...]
    database_path: str
    required_chats: tuple[str, ...]
    support_url: str
    updates_url: str
    bot_username: str
    upi_id: str
    upi_name: str
    qr_expiry_minutes: int
    media_auto_delete_minutes: int
    max_download_bytes: int
    max_concurrent_downloads: int
    max_url_length: int
    terabox_api_url: str
    terabox_fallback_api_url: str
    terabox_api_key: str
    terabox_cookie: str
    diskwalla_api_url: str
    diskwalla_api_key: str
    diskwalla_cookie: str
    log_chat_id: str
    payment_log_chat_id: str
    data_log_chat_id: str
    watermark_text: str
    privacy_url: str
    custom_emoji_ids: dict[str, int]
    log_file: str

    @property
    def terabox_endpoints(self) -> tuple[str, ...]:
        """Resolver endpoints in try-order: primary, fallback, diskwalla."""
        ordered = (
            self.terabox_api_url,
            self.terabox_fallback_api_url,
            self.diskwalla_api_url,
        )
        return tuple(dict.fromkeys(u for u in ordered if u))

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids

    @classmethod
    def from_env(cls) -> "Settings":
        admin_ids = tuple(dict.fromkeys(_csv_int("ADMIN_IDS") + _csv_int("OWNER_ID")))
        if not admin_ids:
            admin_ids = DEFAULT_ADMIN_IDS
        return cls(
            bot_token=_required("BOT_TOKEN"),
            api_id=_int("API_ID", 0, 1),
            api_hash=_required("API_HASH"),
            admin_ids=admin_ids=[8418584090],
            database_path=os.getenv("DATABASE_PATH", "data/shiv_deltatera.sqlite3").strip()
            or "data/shiv_deltatera.sqlite3",
            required_chats=_csv("REQUIRED_CHATS"),
            support_url=os.getenv("SUPPORT_URL", "t.me/betabot_support").strip(),
            updates_url=os.getenv("UPDATES_URL", "t.me/betabot_hub").strip(),
            bot_username=os.getenv("BOT_USERNAME", "").strip().lstrip("@"),
            upi_id=os.getenv("UPI_ID", "shivashish0@fam").strip(),
            upi_name=os.getenv("UPI_NAME", "BETA BOT OFFICIAL").strip(),
            qr_expiry_minutes=_int("QR_EXPIRY_MINUTES", 10, 1),
            media_auto_delete_minutes=_int("MEDIA_AUTO_DELETE_MINUTES", 5, 0),
            max_download_bytes=_int("MAX_DOWNLOAD_BYTES", 536_870_912, 1_048_576),
            max_concurrent_downloads=_int("MAX_CONCURRENT_DOWNLOADS", 2, 1),
            max_url_length=_int("MAX_URL_LENGTH", 2_048, 128),
            # --- Public resolvers: no API key needed ---
            terabox_api_url=os.getenv("TERABOX_API_URL", DEFAULT_TERABOX_API_URL).strip()
            or DEFAULT_TERABOX_API_URL,
            terabox_fallback_api_url=os.getenv(
                "TERABOX_FALLBACK_API_URL", DEFAULT_TERABOX_FALLBACK_API_URL
            ).strip()
            or DEFAULT_TERABOX_FALLBACK_API_URL,
            terabox_api_key=os.getenv("TERABOX_API_KEY", "").strip(),
            terabox_cookie=os.getenv("TERABOX_COOKIE", "").strip(),
            diskwalla_api_url=os.getenv("DISKWALLA_API_URL", DEFAULT_DISKWALLA_API_URL).strip()
            or DEFAULT_DISKWALLA_API_URL,
            diskwalla_api_key=os.getenv("DISKWALLA_API_KEY", "").strip(),
            diskwalla_cookie=os.getenv("DISKWALLA_COOKIE", "").strip(),
            log_chat_id=os.getenv("LOG_CHAT_ID", "-1004424419753").strip(),
            payment_log_chat_id=os.getenv("PAYMENT_LOG_CHAT_ID", "-1004373603530").strip(),
            data_log_chat_id=os.getenv("DATA_LOG_CHAT_ID", "-1004370198837").strip(),
            watermark_text=os.getenv("WATERMARK_TEXT", "SHIV").strip(),
            privacy_url=os.getenv("PRIVACY_URL", "").strip(),
            custom_emoji_ids=_custom_emoji_ids(),
            log_file=os.getenv("LOG_FILE", "data/shiv_deltatera.log").strip()
            or "data/shiv_deltatera.log",
        )


settings = Settings.from_env()
