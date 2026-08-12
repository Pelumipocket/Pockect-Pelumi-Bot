"""
bot.py

Telegram bot skeleton for the confluence signal engine.

Since Pocket Option OTC pairs have no legitimate public price API, this bot
runs in MANUAL PASTE mode: you paste recent OHLC candles from the platform,
the bot runs the confluence engine and replies with a signal (or an abstain).

Commands:
    /start          - welcome + instructions
    /help           - paste format + command list
    /signal PAIR TF - set the pair/timeframe label for your next paste
                       e.g. /signal EURUSD-OTC 1m
    /log win|loss   - record the outcome of your last signal in this chat
    /stats          - win rate + total signals logged, this chat only

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

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from signal_engine import evaluate, Direction, MIN_CANDLES
from formatter import format_signal

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "signals.db")
STAKE_AMOUNT = os.environ.get("STAKE_AMOUNT")  # optional display string, e.g. "$10"

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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Confluence signal bot ready.\n\n"
        "1. /signal PAIR TIMEFRAME  (e.g. /signal EURUSD-OTC 1m)\n"
        "2. Paste your OHLC candles, one per line: open,high,low,close\n"
        f"   (need at least {MIN_CANDLES} candles, oldest first)\n"
        "3. I'll reply with a signal, or tell you why I'm staying out.\n\n"
        "After a trade settles: /log win or /log loss\n"
        "/stats — see your win rate\n"
        "/help — full instructions"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>Paste format</b>\n"
        "One candle per line, oldest first:\n"
        "<code>open,high,low,close</code>\n\n"
        "<b>Commands</b>\n"
        "/signal PAIR TIMEFRAME — label your next paste, e.g. /signal EURUSD-OTC 1m\n"
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
    app.add_handler(CommandHandler("signal", signal_cmd))
    app.add_handler(CommandHandler("log", log_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_paste))

    logger.info("Bot starting (manual paste mode — Pocket Option OTC has no live API)")
    app.run_polling()


if __name__ == "__main__":
    main()
