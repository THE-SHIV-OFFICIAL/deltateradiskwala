"""SHIV DeltaTera Telegram bot.

Run with:
    python -m SHIV_BOT.SHIV_BOT

The bot is intentionally honest about provider setup: without a configured
resolver it explains what is missing instead of returning fake download URLs.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
from logging.handlers import RotatingFileHandler
import mimetypes
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import quote, urlparse

import aiohttp
import qrcode
from pyrogram import Client, filters, idle
from pyrogram.errors import FloodWait, UserNotParticipant
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .SHIV_config import PLANS, Settings
from .SHIV_database import Database
from .SHIV_extractors import ExtractionError, MediaResolver
from .SHIV_formatting import render_premium


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("shiv-deltatera")

settings = Settings.from_env()
db = Database(settings.database_path)
resolver = MediaResolver(settings)
download_slots = asyncio.Semaphore(settings.max_concurrent_downloads)
pending_payments: dict[int, tuple[str, str]] = {}
bot_username = settings.bot_username

log_path = Path(settings.log_file)
log_path.parent.mkdir(parents=True, exist_ok=True)
if not any(
    isinstance(handler, RotatingFileHandler)
    and Path(getattr(handler, "baseFilename", "")) == log_path.resolve()
    for handler in log.handlers
):
    file_handler = RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    log.addHandler(file_handler)

app = Client(
    "shiv_deltatera_bot",
    api_id=settings.api_id,
    api_hash=settings.api_hash,
    bot_token=settings.bot_token,
    workers=16,
)

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
SUPPORTED_MEDIA = ("video", "audio", "photo", "document")


def chat_id(value: str) -> int | str:
    return int(value) if value.lstrip("-").isdigit() else value


def plan_for_limits(plan_name: str) -> tuple[int | None, int | None]:
    if plan_name == "beta":
        return None, None
    if plan_name in {"pro", "referral_pass"}:
        return 17, 15
    return 3, 3


async def effective_limits(user_id: int) -> tuple[str, int | None, int | None]:
    plan_name = await db.effective_plan(user_id)
    downloads, uploads = plan_for_limits(plan_name)
    return plan_name, downloads, uploads


def button(label: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=data)


def rich_text(text: str) -> tuple[str, list]:
    # Pyrogram skips Markdown parsing when explicit entities are supplied.
    # Remove lightweight Markdown markers first so configured custom emoji IDs
    # do not make raw backticks/asterisks visible to users.
    if settings.custom_emoji_ids:
        text = text.replace("**", "").replace("`", "")
    return render_premium(text, settings.custom_emoji_ids)


async def reply_rich(message: Message, text: str, **kwargs):
    rendered, entities = rich_text(text)
    if entities:
        kwargs["entities"] = entities
    return await message.reply_text(rendered, **kwargs)


async def edit_rich(message: Message, text: str, **kwargs):
    rendered, entities = rich_text(text)
    if entities:
        kwargs["entities"] = entities
    return await message.edit_text(rendered, **kwargs)


async def send_rich(destination: int | str, text: str, **kwargs):
    rendered, entities = rich_text(text)
    if entities:
        kwargs["entities"] = entities
    return await app.send_message(destination, rendered, **kwargs)


def home_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [button("📥 Download", "menu:download"), button("📤 My files", "menu:files")],
        [button("📊 My stats", "menu:stats"), button("💎 Plans", "menu:plans")],
        [button("👥 Refer & earn", "menu:referral"), button("❓ Help", "menu:help")],
    ]
    links = []
    if settings.support_url:
        links.append(InlineKeyboardButton("💬 Support", url=settings.support_url))
    if settings.updates_url:
        links.append(InlineKeyboardButton("📢 Updates", url=settings.updates_url))
    if links:
        rows.append(links)
    return InlineKeyboardMarkup(rows)


async def send_log(chat: str, text: str) -> None:
    if not chat:
        return
    try:
        rendered, entities = rich_text(f"[[emoji:logger]] {text}")
        kwargs = {"disable_web_page_preview": True}
        if entities:
            kwargs["entities"] = entities
        await app.send_message(chat_id(chat), rendered, **kwargs)
    except Exception as exc:
        log.warning("Could not send log to %s: %s", chat, exc)


async def ensure_user(message: Message, referral: int | None = None) -> dict:
    user = message.from_user
    return await db.upsert_user(
        user.id,
        user.username or "",
        user.first_name or "User",
        referral if referral and referral != user.id else None,
    )


async def missing_memberships(user_id: int) -> list[str]:
    missing: list[str] = []
    for required in settings.required_chats:
        try:
            member = await app.get_chat_member(chat_id(required), user_id)
            if member.status in {"left", "kicked", "banned"}:
                missing.append(required)
        except UserNotParticipant:
            missing.append(required)
        except Exception as exc:
            log.warning("Force-join check failed for %s/%s: %s", required, user_id, exc)
            # A broken optional check must not lock every user out.
    return missing


async def force_join(message: Message) -> bool:
    if not settings.required_chats:
        return True
    missing = await missing_memberships(message.from_user.id)
    if not missing:
        referred_by = await db.mark_referral_verified(message.from_user.id)
        if referred_by:
            await app.send_message(
                referred_by,
                "🎉 Your referral joined and passed verification. "
                "Every 3 verified referrals unlocks a 1-day pass.",
            )
        return True
    rows = []
    for item in missing:
        username = item.lstrip("@")
        rows.append([InlineKeyboardButton(f"➕ Join {item}", url=f"https://t.me/{username}")])
    rows.append([button("✅ I joined — verify", "verify:join")])
    await message.reply_text(
        "🔐 Please join the required channel/group before using downloads.\n"
        "Then tap **I joined — verify**.",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return False


def plan_text() -> str:
    return (
        "[[emoji:premium]] **Plans & pricing**\n\n"
        "🆓 Free — 3 downloads + 3 uploads/day\n"
        "🥈 Pro — 17 downloads + 15 uploads/day\n"
        "🥇 Beta VIP — unlimited downloads/uploads\n\n"
        "Choose a plan below. UPI verification is manual so your payment is "
        "reviewed safely by an admin."
    )


def plans_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [button("🥈 Pro · 7d · ₹15", "buy:pro_7d")],
        [button("🥈 Pro · 30d · ₹27", "buy:pro_30d")],
        [button("🥇 Beta VIP · 30d · ₹69", "buy:beta_30d")],
        [button("⬅️ Home", "menu:home")],
    ]
    return InlineKeyboardMarkup(rows)


async def stats_text(user_id: int) -> str | None:
    user = await db.get_user(user_id)
    if not user:
        return None
    plan_name, download_limit, upload_limit = await effective_limits(user_id)
    today = time.strftime("%Y-%m-%d")
    downloads = user["downloads_today"] if user["usage_date"] == today else 0
    uploads = user["uploads_today"] if user["usage_date"] == today else 0
    d_left = "∞" if download_limit is None else str(max(0, download_limit - downloads))
    u_left = "∞" if upload_limit is None else str(max(0, upload_limit - uploads))
    expires = int(user["expires_at"])
    expires_text = time.strftime("%d %b %Y %H:%M UTC", time.gmtime(expires)) if expires else "Free plan"
    return (
        f"📊 **Your account**\n\n"
        f"Plan: `{plan_name}`\n"
        f"Downloads left today: `{d_left}`\n"
        f"Uploads left today: `{u_left}`\n"
        f"Premium expiry: `{expires_text}`\n"
        f"Telegram ID: `{user_id}`"
    )


async def send_stats(message: Message, user_id: int | None = None) -> None:
    text = await stats_text(user_id or message.from_user.id)
    if text:
        await message.reply_text(text, reply_markup=home_keyboard())


@app.on_message(filters.command("start") & filters.private)
async def start_handler(_, message: Message) -> None:
    argument = message.command[1] if len(message.command) > 1 else ""
    referral: int | None = None
    file_row_id: int | None = None
    if argument.startswith("ref_") and argument[4:].isdigit():
        referral = int(argument[4:])
    elif argument.startswith("file_") and argument[5:].isdigit():
        file_row_id = int(argument[5:])

    await ensure_user(message, referral)
    if file_row_id:
        saved = await db.get_file(file_row_id, message.from_user.id)
        if not saved:
            await message.reply_text("That file link is invalid or belongs to another user.")
        else:
            await resend_saved_file(message, saved)
        return
    if not await force_join(message):
        return
    await message.reply_text(
        "⚡ **Welcome to SHIV DeltaTera Bot**\n\n"
        "Send a public TeraBox/DiskWalla link to download media. "
        "You can also send a file to save a private Telegram deep link.\n\n"
        "Use the buttons below to get started.",
        reply_markup=home_keyboard(),
    )
    await send_log(
        settings.log_chat_id,
        f"👤 /start · `{message.from_user.id}` · @{message.from_user.username or 'no_username'}",
    )


@app.on_message(filters.command("help") & filters.private)
async def help_handler(_, message: Message) -> None:
    await message.reply_text(
        "❓ **How to use**\n\n"
        "1. Join required channels if prompted.\n"
        "2. Paste a TeraBox or DiskWalla share URL.\n"
        "3. Wait while the configured resolver prepares the file.\n"
        "4. Download the Telegram media before the auto-delete timer ends.\n\n"
        "Commands: /start · /stats · /plan · /referral · /help\n"
        "Only send links and files you are allowed to access or share.",
        reply_markup=home_keyboard(),
    )


@app.on_message(filters.command("stats") & filters.private)
async def stats_handler(_, message: Message) -> None:
    await ensure_user(message)
    await send_stats(message)


@app.on_message(filters.command("plan") & filters.private)
async def plan_handler(_, message: Message) -> None:
    await ensure_user(message)
    await reply_rich(message, plan_text(), reply_markup=plans_keyboard())


@app.on_message(filters.command("premium") & filters.private)
async def premium_handler(_, message: Message) -> None:
    await ensure_user(message)
    await reply_rich(message, plan_text(), reply_markup=plans_keyboard())


@app.on_message(filters.command("referral") & filters.private)
async def referral_handler(_, message: Message) -> None:
    await ensure_user(message)
    username = bot_username or (await app.get_me()).username
    link = f"https://t.me/{username}?start=ref_{message.from_user.id}"
    await message.reply_text(
        "👥 **Refer & earn**\n\n"
        "Share this link. After 3 verified referrals, you get a 1-day premium pass.\n\n"
        f"`{link}`",
        reply_markup=home_keyboard(),
    )


@app.on_callback_query(filters.regex(r"^menu:(home|stats|plans|referral|help|download|files)$"))
async def menu_callback(_, query: CallbackQuery) -> None:
    await query.answer()
    action = query.matches[0].group(1)
    if action == "home":
        await edit_rich(query.message, "⚡ **SHIV DeltaTera Bot**\n\nChoose an action:", reply_markup=home_keyboard())
    elif action == "stats":
        text = await stats_text(query.from_user.id)
        await edit_rich(query.message, text or "Please send /start first.", reply_markup=home_keyboard())
    elif action == "plans":
        await edit_rich(query.message, plan_text(), reply_markup=plans_keyboard())
    elif action == "referral":
        username = bot_username or (await app.get_me()).username
        link = f"https://t.me/{username}?start=ref_{query.from_user.id}"
        await query.message.edit_text(f"👥 Share your referral link:\n\n`{link}`", reply_markup=home_keyboard())
    elif action == "help":
        await query.message.edit_text(
            "❓ Send a TeraBox/DiskWalla URL or a media file. "
            "Use /stats to check quota and /plan to upgrade.",
            reply_markup=home_keyboard(),
        )
    elif action == "download":
        await query.message.edit_text("📥 Paste your public TeraBox or DiskWalla link here.", reply_markup=home_keyboard())
    else:
        await query.message.edit_text(
            "📤 Send a video/audio/photo/document here. I will save it and return a private deep link.",
            reply_markup=home_keyboard(),
        )


@app.on_callback_query(filters.regex(r"^verify:join$"))
async def verify_callback(_, query: CallbackQuery) -> None:
    if await missing_memberships(query.from_user.id):
        await query.answer("You still need to join every required chat.", show_alert=True)
        return
    await query.answer()
    await db.mark_referral_verified(query.from_user.id)
    await edit_rich(query.message, "[[emoji:success]] Verification complete. Send your link now.", reply_markup=home_keyboard())


@app.on_callback_query(filters.regex(r"^buy:(pro_7d|pro_30d|beta_30d)$"))
async def buy_callback(_, query: CallbackQuery) -> None:
    plan = PLANS[query.matches[0].group(1)]
    if not settings.upi_id:
        await query.answer("UPI is not configured by the owner yet.", show_alert=True)
        return
    await query.answer()
    payment_id = await db.create_payment(
        query.from_user.id,
        plan.key,
        plan.price_inr,
        ttl_seconds=settings.qr_expiry_minutes * 60,
    )
    pending_payments[query.from_user.id] = (payment_id, plan.key)
    payload = (
        f"upi://pay?pa={quote(settings.upi_id)}&pn={quote(settings.upi_name or 'SHIV DeltaTera')}"
        f"&am={plan.price_inr}&cu=INR&tn={quote('BBH_' + payment_id)}"
    )
    image = qrcode.make(payload)
    stream = io.BytesIO()
    stream.name = f"{payment_id}.png"
    image.save(stream, "PNG")
    stream.seek(0)
    await query.message.reply_photo(
        stream,
        caption=(
            f"💳 **{plan.title} · ₹{plan.price_inr}**\n\n"
            f"UPI: `{settings.upi_id}`\n"
            f"Payment reference: `{payment_id}`\n"
            f"QR expires in {settings.qr_expiry_minutes} minutes.\n\n"
            "After payment, tap submit and send the 12-digit UTR or a receipt photo."
        ),
        reply_markup=InlineKeyboardMarkup(
            [[button("📤 Submit UTR / proof", f"submit:{payment_id}")], [button("⬅️ Plans", "menu:plans")]]
        ),
    )
    await send_log(settings.payment_log_chat_id, f"💳 QR created · `{payment_id}` · user `{query.from_user.id}` · {plan.key}")


@app.on_callback_query(filters.regex(r"^submit:([A-Za-z0-9_-]+)$"))
async def submit_payment_callback(_, query: CallbackQuery) -> None:
    await query.answer()
    payment_id = query.matches[0].group(1)
    pending_payments[query.from_user.id] = (payment_id, "")
    await reply_rich(query.message, "Send the UTR/reference number, or send a payment screenshot now.")


async def expiry_watcher() -> None:
    """Send one renewal reminder during the final 24 hours of a plan."""
    while True:
        try:
            expired = await db.refresh_expired_premium()
            if expired:
                await send_log(
                    settings.log_chat_id,
                    f"[[emoji:refresh]] Premium refresh · reset `{len(expired)}` expired account(s)",
                )
            for user in await db.expiring_users():
                await send_rich(
                    user["user_id"],
                    "[[emoji:premium]] Your premium plan expires within 24 hours. Renew now to keep your quota.",
                    reply_markup=InlineKeyboardMarkup(
                        [[button("💎 Renew plan", "menu:plans")]]
                    ),
                )
                await db.mark_expiry_notice(user["user_id"], int(user["expires_at"]))
        except Exception:
            log.exception("Expiry watcher failed")
        await asyncio.sleep(1_800)


async def download_bytes(url: str, destination: str) -> tuple[str, int]:
    timeout = aiohttp.ClientTimeout(total=180, connect=20, sock_read=160)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, allow_redirects=False) as response:
            if response.status >= 400:
                raise ExtractionError(f"Media server returned HTTP {response.status}.")
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > settings.max_download_bytes:
                raise ExtractionError("This file is larger than the bot's configured size limit.")
            written = 0
            with open(destination, "wb") as output:
                async for chunk in response.content.iter_chunked(256 * 1024):
                    written += len(chunk)
                    if written > settings.max_download_bytes:
                        raise ExtractionError("This file is larger than the bot's configured size limit.")
                    output.write(chunk)
            return response.headers.get("Content-Type", "application/octet-stream"), written


async def delete_later(chat: int, message_id: int, delay: int) -> None:
    if delay <= 0:
        return
    await asyncio.sleep(delay * 60)
    try:
        await app.delete_messages(chat, message_id)
    except Exception:
        pass


async def handle_download(message: Message, url: str) -> None:
    if not await force_join(message):
        return
    plan_name, download_limit, _ = await effective_limits(message.from_user.id)
    allowed, remaining = await db.consume_quota(message.from_user.id, "downloads", download_limit)
    if not allowed:
        await message.reply_text("⛔ Daily download quota finished. Use /plan to upgrade.", reply_markup=plans_keyboard())
        return
    status = await message.reply_text("⏳ Added to the download queue…")
    try:
        async with download_slots:
            result = await resolver.resolve(url)
            suffix = Path(urlparse(result.direct_url).path).suffix[:10] or mimetypes.guess_extension(result.mime) or ".bin"
            with tempfile.NamedTemporaryFile(prefix="bbh_", suffix=suffix, delete=False) as temp:
                file_path = temp.name
            try:
                mime, size = await download_bytes(result.direct_url, file_path)
                caption = (
                    f"✅ **{result.title}**\n"
                    f"Source: `{result.source}` · Size: `{size / 1024 / 1024:.1f} MB`\n"
                    f"Plan: `{plan_name}` · Remaining today: `{remaining if remaining is not None else '∞'}`\n\n"
                    f"⚠️ This message auto-deletes in {settings.media_auto_delete_minutes} minutes. Save it first."
                )
                if mime.startswith("video/"):
                    sent = await message.reply_video(file_path, caption=caption, supports_streaming=True)
                elif mime.startswith("audio/"):
                    sent = await message.reply_audio(file_path, caption=caption)
                elif mime.startswith("image/"):
                    sent = await message.reply_photo(file_path, caption=caption)
                else:
                    sent = await message.reply_document(file_path, caption=caption)
                asyncio.create_task(delete_later(message.chat.id, sent.id, settings.media_auto_delete_minutes))
                await send_log(settings.data_log_chat_id, f"📥 Download · `{message.from_user.id}` · {result.source} · {size} bytes")
            finally:
                try:
                    os.remove(file_path)
                except OSError:
                    pass
    except (ExtractionError, aiohttp.ClientError, TimeoutError) as exc:
        await status.edit_text(f"❌ Download failed: {exc}")
        return
    except Exception:
        log.exception("Unexpected download failure")
        await status.edit_text("❌ Download failed due to a temporary server error. Please try again.")
        return
    await status.delete()


@app.on_message(
    filters.text
    & filters.private
    & ~filters.command(
        [
            "start", "help", "stats", "plan", "referral", "myfiles",
            "pending", "approve", "reject", "addpremium", "removepremium",
            "checkuser", "broadcast", "premium", "refreshpremium",
        ]
    )
)
async def text_handler(_, message: Message) -> None:
    await ensure_user(message)
    state = pending_payments.get(message.from_user.id)
    if state:
        payment_id, _ = state
        utr = message.text.strip()
        if re.fullmatch(r"[A-Za-z0-9-]{6,120}", utr):
            accepted = await db.attach_payment_submission(
                payment_id,
                message.from_user.id,
                utr=utr,
                proof_type="text",
            )
            pending_payments.pop(message.from_user.id, None)
            if accepted:
                await message.reply_text("✅ Payment proof submitted. An admin will review it shortly.")
                await send_log(settings.payment_log_chat_id, f"🧾 UTR submitted · `{payment_id}` · `{message.from_user.id}`")
            else:
                await message.reply_text("⌛ This payment QR has expired. Please create a new payment request from /plan.")
            return
    urls = URL_RE.findall(message.text or "")
    if not urls:
        await message.reply_text("Send a supported TeraBox/DiskWalla URL, or use /help.", reply_markup=home_keyboard())
        return
    await handle_download(message, urls[0].rstrip(").,"))


def media_details(message: Message) -> tuple[str, str, str] | None:
    if message.document:
        return "document", message.document.file_id, message.document.file_name or "document"
    if message.video:
        return "video", message.video.file_id, message.video.file_name or "video.mp4"
    if message.audio:
        return "audio", message.audio.file_id, message.audio.file_name or "audio"
    if message.photo:
        return "photo", message.photo.file_id, "photo.jpg"
    return None


async def resend_saved_file(message: Message, saved: dict) -> None:
    caption = f"📤 `{saved['file_name']}`\nThis is your saved Telegram file."
    if saved["media_type"] == "video":
        await message.reply_video(saved["telegram_file_id"], caption=caption)
    elif saved["media_type"] == "audio":
        await message.reply_audio(saved["telegram_file_id"], caption=caption)
    elif saved["media_type"] == "photo":
        await message.reply_photo(saved["telegram_file_id"], caption=caption)
    else:
        await message.reply_document(saved["telegram_file_id"], caption=caption)


@app.on_message(filters.media & filters.private)
async def media_handler(_, message: Message) -> None:
    await ensure_user(message)
    payment_state = pending_payments.get(message.from_user.id)
    if payment_state:
        details = media_details(message)
        if details:
            payment_id, _ = payment_state
            _, file_id, _ = details
            accepted = await db.attach_payment_submission(
                payment_id,
                message.from_user.id,
                proof_file_id=file_id,
                proof_type=details[0],
            )
            pending_payments.pop(message.from_user.id, None)
            if accepted:
                await message.reply_text("✅ Payment screenshot submitted. An admin will review it shortly.")
                await send_log(
                    settings.payment_log_chat_id,
                    f"🧾 Receipt photo submitted · `{payment_id}` · `{message.from_user.id}`",
                )
            else:
                await message.reply_text("⌛ This payment QR has expired. Please create a new payment request from /plan.")
            return
    if not await force_join(message):
        return
    details = media_details(message)
    if not details:
        await message.reply_text("This media type is not supported.")
        return
    _, upload_limit = (await effective_limits(message.from_user.id))[0:3:2]
    allowed, remaining = await db.consume_quota(message.from_user.id, "uploads", upload_limit)
    if not allowed:
        await message.reply_text("⛔ Daily upload quota finished. Use /plan to upgrade.", reply_markup=plans_keyboard())
        return
    media_type, file_id, file_name = details
    row_id = await db.save_file(message.from_user.id, file_id, media_type, file_name)
    username = bot_username or (await app.get_me()).username
    link = f"https://t.me/{username}?start=file_{row_id}"
    await message.reply_text(
        f"✅ Saved securely in Telegram storage.\n\n"
        f"🔗 Private retrieval link:\n`{link}`\n\n"
        f"Uploads remaining today: `{remaining if remaining is not None else '∞'}`",
        reply_markup=home_keyboard(),
    )
    await send_log(settings.data_log_chat_id, f"📤 Upload saved · `{message.from_user.id}` · file `{row_id}`")


@app.on_message(filters.command("myfiles") & filters.private)
async def myfiles_handler(_, message: Message) -> None:
    await message.reply_text("Use the private link returned after each upload to retrieve that file.")


def is_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids


@app.on_message(filters.command("pending") & filters.private)
async def pending_handler(_, message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    payments = await db.pending_payments()
    if not payments:
        await message.reply_text("No pending payments.")
        return
    await message.reply_text("🧾 **Pending payments**\n\n" + "\n".join(
        f"`{p['payment_id']}` · user `{p['user_id']}` · {p['plan_key']} · ₹{p['amount']} · "
        f"UTR `{p['utr'] or 'not submitted'}`"
        for p in payments
    ))
    # Send receipt files to the admin who requested the queue, so approvals
    # do not depend on manually searching Telegram for the proof.
    for payment in payments:
        proof = payment.get("proof_file_id")
        if not proof:
            continue
        caption = (
            f"Payment `{payment['payment_id']}` · user `{payment['user_id']}`\n"
            f"Approve: /approve {payment['payment_id']}\n"
            f"Reject: /reject {payment['payment_id']}"
        )
        if payment.get("proof_type") == "photo":
            await app.send_photo(message.from_user.id, proof, caption=caption)
        else:
            await app.send_document(message.from_user.id, proof, caption=caption)


@app.on_message(filters.command("approve") & filters.private)
async def approve_handler(_, message: Message) -> None:
    if not is_admin(message.from_user.id) or len(message.command) != 2:
        await message.reply_text("Usage: /approve PAYMENT_ID") if is_admin(message.from_user.id) else None
        return
    payment = await db.review_payment(message.command[1], message.from_user.id, True)
    if not payment:
        await message.reply_text("Payment not found or already reviewed.")
        return
    await message.reply_text("✅ Payment approved and plan activated.")
    await send_rich(payment["user_id"], f"[[emoji:premium]] Payment approved. Your `{payment['plan_key']}` plan is active.")
    await send_log(
        settings.payment_log_chat_id,
        f"[[emoji:success]] Payment approved · `{payment['payment_id']}` · admin `{message.from_user.id}`",
    )


@app.on_message(filters.command("reject") & filters.private)
async def reject_handler(_, message: Message) -> None:
    if not is_admin(message.from_user.id) or len(message.command) != 2:
        await message.reply_text("Usage: /reject PAYMENT_ID") if is_admin(message.from_user.id) else None
        return
    payment = await db.review_payment(message.command[1], message.from_user.id, False)
    if not payment:
        await message.reply_text("Payment not found or already reviewed.")
        return
    await message.reply_text("Payment rejected.")
    await send_rich(payment["user_id"], "[[emoji:error]] Payment proof was rejected. Please contact support and submit a valid proof.")
    await send_log(
        settings.payment_log_chat_id,
        f"[[emoji:error]] Payment rejected · `{payment['payment_id']}` · admin `{message.from_user.id}`",
    )


@app.on_message(filters.command("addpremium") & filters.private)
async def addpremium_handler(_, message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    if len(message.command) != 4 or not message.command[1].isdigit() or not message.command[2].isdigit():
        await message.reply_text("Usage: /addpremium USER_ID DAYS pro|beta")
        return
    user_id, days, plan = int(message.command[1]), int(message.command[2]), message.command[3].lower()
    if plan not in {"pro", "beta"} or days < 1 or days > 3650:
        await message.reply_text("Plan must be pro/beta and days must be 1–3650.")
        return
    if await db.grant(user_id, plan, days):
        await message.reply_text("✅ Premium granted.")
        await send_rich(user_id, f"[[emoji:premium]] Admin granted you `{plan}` premium for `{days}` days.")
        await send_log(
            settings.log_chat_id,
            f"[[emoji:premium]] Premium granted · user `{user_id}` · `{plan}` · `{days}` days · admin `{message.from_user.id}`",
        )
    else:
        await message.reply_text("User not found. Ask them to /start first.")


@app.on_message(filters.command("removepremium") & filters.private)
async def removepremium_handler(_, message: Message) -> None:
    if not is_admin(message.from_user.id) or len(message.command) != 2 or not message.command[1].isdigit():
        return
    user_id = int(message.command[1])
    removed = await db.revoke(user_id)
    await message.reply_text("✅ Premium removed." if removed else "User not found.")
    if removed:
        await send_log(
            settings.log_chat_id,
            f"[[emoji:refresh]] Premium revoked · user `{user_id}` · admin `{message.from_user.id}`",
        )


@app.on_message(filters.command("refreshpremium") & filters.private)
async def refreshpremium_handler(_, message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    expired = await db.refresh_expired_premium()
    if expired:
        ids = ", ".join(str(row["user_id"]) for row in expired[:20])
        suffix = "…" if len(expired) > 20 else ""
        await reply_rich(
            message,
            f"[[emoji:refresh]] Refreshed `{len(expired)}` expired premium account(s).\n"
            f"Users: `{ids}{suffix}`",
        )
        await send_log(
            settings.log_chat_id,
            f"[[emoji:refresh]] Manual premium refresh · `{len(expired)}` account(s) · admin `{message.from_user.id}`",
        )
    else:
        await reply_rich(message, "[[emoji:success]] Premium records are already fresh.")


@app.on_message(filters.command("checkuser") & filters.private)
async def checkuser_handler(_, message: Message) -> None:
    if not is_admin(message.from_user.id) or len(message.command) != 2 or not message.command[1].isdigit():
        return
    user = await db.get_user(int(message.command[1]))
    if not user:
        await message.reply_text("User not found.")
        return
    await message.reply_text(
        f"User `{user['user_id']}` · @{user['username'] or 'none'}\n"
        f"Plan `{await db.effective_plan(user['user_id'])}` · expires `{user['expires_at']}`\n"
        f"Referrals verified: `{user['referral_verified']}`"
    )


async def deliver_broadcast(user_id: int, text: str) -> bool:
    for attempt in range(2):
        try:
            await send_rich(user_id, text, disable_web_page_preview=True)
            return True
        except FloodWait as wait:
            log.warning("Broadcast flood wait for %s: %ss", user_id, wait.value)
            if attempt == 0:
                await asyncio.sleep(wait.value)
        except Exception as exc:
            log.warning("Broadcast failed for %s: %s", user_id, exc)
            return False
    return False


@app.on_message(filters.command("broadcast") & filters.private)
async def broadcast_handler(_, message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    text = message.text.partition(" ")[2].strip()
    if not text:
        await message.reply_text("Usage: /broadcast your announcement")
        return
    sent = failed = 0
    for user_id in await db.all_user_ids():
        if await deliver_broadcast(user_id, text):
            sent += 1
            await asyncio.sleep(0.05)
        else:
            failed += 1
    await reply_rich(
        message,
        f"[[emoji:broadcast]] Broadcast complete · delivered `{sent}` · failed `{failed}`",
    )
    await send_log(
        settings.log_chat_id,
        f"[[emoji:broadcast]] Broadcast finished · sent `{sent}` · failed `{failed}` · admin `{message.from_user.id}`",
    )


async def main() -> None:
    global bot_username
    await db.connect()
    await app.start()
    me = await app.get_me()
    bot_username = bot_username or me.username or ""
    log.info("Bot started as @%s", bot_username)
    expiry_task = asyncio.create_task(expiry_watcher())
    try:
        await idle()
    finally:
        expiry_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await expiry_task
        await app.stop()
        await db.close()


if __name__ == "__main__":
    app.run(main())

