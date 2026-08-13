"""
bot.py

Telegram bot skeleton for the confluence signal engine.

Since Pocket Option OTC pairs have no legitimate public price API, this bot
runs in MANUAL PASTE mode: you paste recent OHLC candles from the platform,
the bot runs the confluence engine and replies with a signal (or an abstain).

Commands:
    /start          - welcome + inline pair-picker buttons
    /pairs          - reopen the pair/timeframe button menu
    /help           - paste format + command list
    /signal PAIR TF - set pair/timeframe by typing instead of buttons
                       e.g. /signal EURUSD-OTC 1m
    /log win|loss   - record the outcome of your last signal in this chat
    /stats          - win rate + total signals logged, this chat only

Flow: tap a pair button -> tap a timeframe button -> paste candles.

Paste format (send as a normal message after /signal):
    One candle per line, oldest first: open,high,low,close
    Example:
        1.08421,1.08430,1.08415,1.08427
        1.08427,1.08440,1.08420,1.08435
        ...

Env vars required:
    BOT_TOKEN - your Telegram bot token from @BotFather
"""

import os
import logging
import sqlite3
from datetime import datetime, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from signal_engine import evaluate, Direction, MIN_CANDLES
from formatter import format_signal

# common Pocket Option OTC pairs shown as buttons — edit this list to match
# whatever's actually on your platform
OTC_PAIRS = [
    "EURUSD-OTC", "GBPUSD-OTC", "USDJPY-OTC", "AUDUSD-OTC",
    "USDCAD-OTC", "EURJPY-OTC", "USDCHF-OTC", "NZDUSD-OTC",
]
TIMEFRAMES = ["1m", "5m", "15m"]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "signals.db")
STAKE_AMOUNT = os.environ.get("STAKE_AMOUNT")  # optional display string, e.g. "$10"
CHANNEL_ID = os.environ.get("CHANNEL_ID")  # optional: e.g. "@yourchannel" or "-1001234567890"
# if set, every signal (fired or abstain) is also posted here, in addition to
# replying in whichever chat sent the candle paste

# in-memory: chat_id -> {"pair": str, "timeframe": str}
pending_context = {}
# in-memory: chat_id -> last signal id logged, for /log
last_signal_id = {}


# ---------- storage ----------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            pair TEXT,
            timeframe TEXT,
            direction TEXT,
            confidence REAL,
            price REAL,
            created_at TEXT,
            outcome TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def save_signal(chat_id, pair, timeframe, signal) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        """INSERT INTO signals (chat_id, pair, timeframe, direction, confidence, price, created_at, outcome)
           VALUES (?, ?, ?, ?, ?, ?, ?, NULL)""",
        (
            chat_id,
            pair,
            timeframe,
            signal.direction.value,
            signal.confidence,
            signal.price,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    signal_id = cur.lastrowid
    conn.close()
    return signal_id


def record_outcome(chat_id, signal_id, outcome):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE signals SET outcome = ? WHERE id = ? AND chat_id = ?",
        (outcome, signal_id, chat_id),
    )
    conn.commit()
    conn.close()


def get_stats(chat_id):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT outcome, COUNT(*) FROM signals WHERE chat_id = ? AND outcome IS NOT NULL GROUP BY outcome",
        (chat_id,),
    ).fetchall()
    total_fired = conn.execute(
        "SELECT COUNT(*) FROM signals WHERE chat_id = ? AND direction != 'ABSTAIN'",
        (chat_id,),
    ).fetchone()[0]
    conn.close()
    counts = dict(rows)
    wins = counts.get("win", 0)
    losses = counts.get("loss", 0)
    logged = wins + losses
    win_rate = (wins / logged * 100) if logged else 0.0
    return {
        "total_fired": total_fired,
        "logged": logged,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
    }


# ---------- parsing ----------

def parse_candles(text: str):
    """Parse pasted candle text into list of {open, high, low, close} dicts.
    Accepts comma or whitespace separated open,high,low,close per line."""
    candles = []
    for raw_line in text.strip().splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = [p for p in line.replace(",", " ").split() if p]
        if len(parts) < 4:
            continue
        try:
            o, h, l, c = (float(x) for x in parts[:4])
        except ValueError:
            continue
        candles.append({"open": o, "high": h, "low": l, "close": c})
    return candles


# ---------- command handlers ----------

def pairs_keyboard():
    rows = []
    for i in range(0, len(OTC_PAIRS), 2):
        row = [InlineKeyboardButton(p, callback_data=f"pair:{p}") for p in OTC_PAIRS[i:i + 2]]
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def timeframe_keyboard(pair: str):
    row = [InlineKeyboardButton(tf, callback_data=f"tf:{pair}:{tf}") for tf in TIMEFRAMES]
    return InlineKeyboardMarkup([row])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Confluence signal bot ready.\n\n"
        "Pick a pair below, then a timeframe, then paste your OHLC candles "
        f"(need at least {MIN_CANDLES}, oldest first, one per line: open,high,low,close).\n\n"
        "After a trade settles: /log win or /log loss\n"
        "/stats — see your win rate\n"
        "/pairs — reopen this menu anytime",
        reply_markup=pairs_keyboard(),
    )


async def pairs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Pick a pair:", reply_markup=pairs_keyboard())


async def pair_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pair = query.data.split(":", 1)[1]
    await query.edit_message_text(
        f"Pair: {pair}\nNow pick a timeframe:",
        reply_markup=timeframe_keyboard(pair),
    )


async def timeframe_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, pair, timeframe = query.data.split(":", 2)
    chat_id = query.message.chat_id
    pending_context[chat_id] = {"pair": pair, "timeframe": timeframe}
    await query.edit_message_text(
        f"Set to {pair} · {timeframe}.\n\n"
        f"Now paste your OHLC candles (need at least {MIN_CANDLES}, oldest first, "
        "one per line: open,high,low,close)."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>Paste format</b>\n"
        "One candle per line, oldest first:\n"
        "<code>open,high,low,close</code>\n\n"
        "<b>Commands</b>\n"
        "/pairs — pick a pair + timeframe with buttons\n"
        "/signal PAIR TIMEFRAME — set it by typing instead, e.g. /signal EURUSD-OTC 1m\n"
        "/log win — mark your last signal a win\n"
        "/log loss — mark your last signal a loss\n"
        "/stats — win rate for this chat\n\n"
        "The engine abstains when indicators don't agree — that's intentional, "
        "not a bug.",
        parse_mode=ParseMode.HTML,
    )


async def signal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /signal PAIR TIMEFRAME  (e.g. /signal EURUSD-OTC 1m)")
        return
    pair, timeframe = args[0], args[1]
    pending_context[chat_id] = {"pair": pair, "timeframe": timeframe}
    await update.message.reply_text(
        f"Set to {pair} · {timeframe}. Now paste your OHLC candles (one per line, oldest first)."
    )


async def handle_paste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text or ""
    candles = parse_candles(text)

    if not candles:
        return  # not a candle paste, ignore silently (could be normal chat)

    ctx = pending_context.get(chat_id, {"pair": "UNKNOWN", "timeframe": "?"})
    result = evaluate(candles)

    msg = format_signal(result, ctx["pair"], ctx["timeframe"], STAKE_AMOUNT)
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

    if CHANNEL_ID and str(chat_id) != str(CHANNEL_ID):
        try:
            await context.bot.send_message(chat_id=CHANNEL_ID, text=msg, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error(f"Failed to post to channel {CHANNEL_ID}: {e}")
            await update.message.reply_text(
                f"⚠️ Couldn't post to the channel — make sure the bot is an admin there. ({e})"
            )

    signal_id = save_signal(chat_id, ctx["pair"], ctx["timeframe"], result)
    if result.direction != Direction.ABSTAIN:
        last_signal_id[chat_id] = signal_id


async def log_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args or context.args[0].lower() not in ("win", "loss"):
        await update.message.reply_text("Usage: /log win  or  /log loss")
        return
    outcome = context.args[0].lower()
    signal_id = last_signal_id.get(chat_id)
    if not signal_id:
        await update.message.reply_text("No recent signal to log for this chat.")
        return
    record_outcome(chat_id, signal_id, outcome)
    await update.message.reply_text(f"Logged as {outcome}.")


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    s = get_stats(chat_id)
    await update.message.reply_text(
        f"Signals fired: {s['total_fired']}\n"
        f"Outcomes logged: {s['logged']} ({s['wins']}W / {s['losses']}L)\n"
        f"Win rate: {s['win_rate']:.1f}%"
    )


def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN environment variable is not set")

    init_db()

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("pairs", pairs_cmd))
    app.add_handler(CommandHandler("signal", signal_cmd))
    app.add_handler(CommandHandler("log", log_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CallbackQueryHandler(pair_button, pattern=r"^pair:"))
    app.add_handler(CallbackQueryHandler(timeframe_button, pattern=r"^tf:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_paste))

    logger.info("Bot starting (manual paste mode — Pocket Option OTC has no live API)")
    app.run_polling()


if __name__ == "__main__":
    main()
