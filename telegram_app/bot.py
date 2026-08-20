"""Telegram entry bot that opens the Mini App."""
from __future__ import annotations

import os
from pathlib import Path

import httpx
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes


async def start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    app_url = os.environ["APP_URL"]
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Відкрити додаток", web_app=WebAppInfo(app_url))]])
    await update.effective_message.reply_text("Керуйте своїм локальним агентом у додатку.", reply_markup=keyboard)


async def stop(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Stop only the local agent that belongs to the Telegram command sender."""
    if not update.effective_user or not update.effective_message:
        return
    app_url = os.environ["APP_URL"].rstrip("/")
    internal_token = os.environ["BOT_INTERNAL_TOKEN"]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{app_url}/api/bot-command",
                headers={"X-Bot-Internal-Token": internal_token},
                json={"owner_id": str(update.effective_user.id), "action": "stop"},
            )
        if response.status_code == 409:
            await update.effective_message.reply_text("Локальний агент зараз не підключений.")
            return
        response.raise_for_status()
    except (httpx.HTTPError, KeyError):
        await update.effective_message.reply_text("Не вдалося зупинити агент. Спробуйте ще раз.")
        return
    await update.effective_message.reply_text("⏹ Агент зупинено.")


def main() -> None:
    load_dotenv(Path(__file__).parent / "settings.env")
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", stop))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
