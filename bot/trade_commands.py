"""Telegram command bot for the standalone trade log."""

import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from trade_log import (
    calculate_stats,
    format_stats,
    format_trade_list,
    initialize_trade_log,
    list_trades,
    log_trade,
    validate_positive_number,
    validate_trade_date,
    validate_symbol,
)

LOGGER = logging.getLogger(__name__)
USAGE = (
    "Trade log commands:\n"
    "/buy SYMBOL QTY PRICE [DATE]\n"
    "/sell SYMBOL QTY PRICE [DATE]\n"
    "/trades - list last 10\n"
    "/stats - FIFO closed-trade statistics\n"
    "DATE format: YYYY-MM-DD"
)


def _authorized(update: Update) -> bool:
    """Restrict commands to the configured chat when one is configured."""
    return not TELEGRAM_CHAT_ID or (
        update.effective_chat is not None and str(update.effective_chat.id) == str(TELEGRAM_CHAT_ID)
    )


async def _reply(update: Update, text: str) -> None:
    if update.effective_message is not None:
        await update.effective_message.reply_text(text)


async def record_trade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /buy and /sell commands."""
    if not _authorized(update):
        return
    side = update.effective_message.text.split()[0].split("@")[0].lstrip("/").upper()
    if len(context.args) not in {3, 4}:
        await _reply(update, f"Usage: /{side.lower()} SYMBOL QTY PRICE [DATE]")
        return
    try:
        symbol = validate_symbol(context.args[0])
        qty = validate_positive_number(context.args[1], "qty")
        price = validate_positive_number(context.args[2], "price")
        trade_date = validate_trade_date(context.args[3] if len(context.args) == 4 else None)
        trade_id = log_trade(symbol, side, qty, price, trade_date)
    except ValueError as exc:
        await _reply(update, f"Rejected: {exc}\n\n{USAGE}")
        return
    await _reply(
        update,
        f"Logged #{trade_id}: {side} {symbol} {qty:g} @ Rs {price:.2f} on {trade_date}",
    )


async def show_trades(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /trades."""
    if _authorized(update):
        await _reply(update, format_trade_list(list_trades(10)))


async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /stats."""
    if _authorized(update):
        await _reply(update, format_stats(calculate_stats()))


async def show_usage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply to unknown commands with command help."""
    if _authorized(update):
        await _reply(update, USAGE)


def run_trade_bot() -> None:
    """Initialize storage and poll Telegram for trade-log commands."""
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    initialize_trade_log()
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    register_trade_handlers(application)
    application.run_polling()


def register_trade_handlers(application: Application) -> None:
    """Register all trade-log commands on a Telegram application."""
    application.add_handler(CommandHandler("buy", record_trade))
    application.add_handler(CommandHandler("sell", record_trade))
    application.add_handler(CommandHandler("trades", show_trades))
    application.add_handler(CommandHandler("stats", show_stats))
    application.add_handler(MessageHandler(filters.COMMAND, show_usage))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_trade_bot()
