import asyncio
import html
import logging
import os
from datetime import datetime, timezone

from telegram import LinkPreviewOptions, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, filters

import db
import ranker
import scraper

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

TOKEN = os.environ["BOT_TOKEN"]
OWNER_IDS = {int(x) for x in os.getenv("OWNER_IDS", "").split(",") if x.strip()}
INTERVAL_MIN = int(os.getenv("INTERVAL_MIN", "30"))
MAX_AGE_MIN = int(os.getenv("MAX_AGE_MIN", "45"))  # only news published within this window
TOP_N = int(os.getenv("TOP_N", "10"))  # biggest stories per cycle
CATEGORIES = {"all", "india", "world", "business", "sports", "crime"}
CMD_FILTER = filters.UpdateType.MESSAGES | filters.UpdateType.CHANNEL_POSTS

_cycle_lock = asyncio.Lock()


# ---------- helpers ----------
def _thread_id(update: Update) -> int:
    chat, msg = update.effective_chat, update.effective_message
    if chat.is_forum and msg.message_thread_id:
        return msg.message_thread_id
    return 0


async def _is_allowed(update: Update) -> bool:
    chat, user = update.effective_chat, update.effective_user
    if chat.type in (ChatType.CHANNEL, ChatType.PRIVATE):
        return True  # only admins can post in a channel anyway
    if user and user.id in OWNER_IDS:
        return True
    if not user:
        return False
    member = await chat.get_member(user.id)
    return member.status in ("administrator", "creator")


def _fmt(a: scraper.Article) -> str:
    ist = a.published.astimezone(scraper.IST)
    mins = max(0, int((datetime.now(timezone.utc) - a.published).total_seconds() // 60))
    ago = f"{mins} min ago" if mins < 60 else f"{mins // 60}h {mins % 60}m ago"
    icon = "\U0001F6A8" if "crime" in a.tags else "\U0001F534"
    parts = [f"{icon} <b>{html.escape(a.title)}</b>"]
    if a.summary:
        s = a.summary if len(a.summary) <= 300 else a.summary[:297].rsplit(" ", 1)[0] + "..."
        parts.append(html.escape(s))
    parts.append(
        f"\U0001F4F0 <b>{html.escape(a.source)}</b>\n"
        f"\U0001F4C5 {ist:%d %b %Y}  \U0001F552 {ist:%I:%M %p} IST ({ago})"
    )
    if a.also:
        parts[-1] += "\n\U0001F501 Also: " + html.escape(", ".join(a.also))
    parts.append(f'\U0001F517 <a href="{html.escape(a.url, quote=True)}">Read full story</a>')
    return "\n\n".join(parts)


# ---------- commands ----------
HELP = (
    "<b>Fresh India News Bot</b>\n"
    "Brand-new news (last ~1 hour) from The Hindu, HT, Al Jazeera, BBC, IANS, The Quint, "
    "Indian Express, Mint, TOI, The Wire, Scroll and PTI. Top 10 biggest breaking stories every 30 minutes.\n\n"
    "/subscribe [all|india|world|business|sports|crime] - start news in this chat/topic\n"
    "/unsubscribe - stop news here\n"
    "/status - what is set up here\n\n"
    "In topic groups, run /subscribe inside each topic (e.g. India topic -> /subscribe india)."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(HELP, parse_mode=ParseMode.HTML)


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_allowed(update):
        await update.effective_message.reply_text("Only group admins can do this.")
        return
    cat = (context.args[0].lower() if context.args else "all")
    if cat not in CATEGORIES:
        await update.effective_message.reply_text("Category must be: all, india, world, business, sports or crime.")
        return
    db.add_sub(update.effective_chat.id, _thread_id(update), cat)
    await update.effective_message.reply_text(f"Done. Fresh '{cat}' news will be posted here every 30 minutes.")


async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_allowed(update):
        await update.effective_message.reply_text("Only group admins can do this.")
        return
    db.remove_sub(update.effective_chat.id, _thread_id(update))
    await update.effective_message.reply_text("Stopped. No more news here.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    row = db.sub_for(update.effective_chat.id, _thread_id(update))
    txt = f"Subscribed: {row[0]}" if row else "Not subscribed here. Use /subscribe."
    await update.effective_message.reply_text(txt)


async def fetchnow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id not in OWNER_IDS:
        return
    await update.effective_message.reply_text("Running a cycle now...")
    await run_cycle(context)


# ---------- the hourly job ----------
async def _send(bot, chat_id, thread_id, text, image="", spoiler=False):
    for _ in range(3):
        try:
            if image:
                try:
                    await bot.send_photo(
                        chat_id, image, caption=text, parse_mode=ParseMode.HTML,
                        message_thread_id=thread_id or None, has_spoiler=spoiler,
                    )
                    return True
                except BadRequest as e:
                    log.info("photo failed (%s), falling back to text", e)  # bad/huge image URL
            await bot.send_message(
                chat_id, text, parse_mode=ParseMode.HTML,
                message_thread_id=thread_id or None,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            return True
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except (Forbidden, BadRequest) as e:
            log.warning("removing dead target %s/%s: %s", chat_id, thread_id, e)
            db.remove_sub(chat_id, thread_id)
            return False
        except TelegramError as e:
            log.warning("send error %s/%s: %s", chat_id, thread_id, e)
            await asyncio.sleep(2)
    return False


async def run_cycle(context: ContextTypes.DEFAULT_TYPE):
    if _cycle_lock.locked():
        return
    async with _cycle_lock:
        targets = db.subs()
        if not targets:
            log.info("no subscriptions yet")
            return
        fresh, old = await asyncio.to_thread(scraper.fetch_fresh, db.is_seen, MAX_AGE_MIN)
        picked, covered = ranker.pick_top(fresh, TOP_N)
        log.info("fresh=%d picked=%d targets=%d", len(fresh), len(picked), len(targets))
        for art in picked:
            text = _fmt(art)
            for chat_id, thread_id, cat in db.subs():
                if cat == "all" or cat == art.category or cat in art.tags:
                    await _send(context.bot, chat_id, thread_id, text,
                                art.image, spoiler="crime" in art.tags)
                    await asyncio.sleep(1.2)  # stay under Telegram flood limits
        # posted stories + their duplicates + too-old pages are done; unpicked fresh ones
        # stay eligible for the next cycle while still inside MAX_AGE_MIN
        db.mark_seen(old + covered)


def _load_static_targets():
    """TARGETS env: 'chat_id[:thread_id[:category]],...'  e.g. -1001234567890,-1009876:12:india"""
    for item in os.getenv("TARGETS", "").split(","):
        item = item.strip()
        if not item:
            continue
        p = item.split(":")
        cat = p[2] if len(p) > 2 and p[2] in CATEGORIES else "all"
        db.add_sub(int(p[0]), int(p[1]) if len(p) > 1 and p[1] else 0, cat)


def main():
    db.init()
    _load_static_targets()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], start, filters=CMD_FILTER))
    app.add_handler(CommandHandler("subscribe", subscribe, filters=CMD_FILTER))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe, filters=CMD_FILTER))
    app.add_handler(CommandHandler("status", status, filters=CMD_FILTER))
    app.add_handler(CommandHandler("fetchnow", fetchnow))
    app.job_queue.run_repeating(run_cycle, interval=INTERVAL_MIN * 60, first=15)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
