"""Public control server for the Telegram Mini App and local agents.

It never receives Paychain credentials. Each agent keeps its browser profile
on the user's computer and connects outward to this server over WebSocket.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import secrets
import sqlite3
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


ROOT = Path(__file__).parent
load_dotenv(ROOT / "settings.env")
# On a hosting service set DATABASE_PATH to a mounted persistent volume, e.g.
# /data/control.sqlite3.  Local development keeps the database next to code.
DB_PATH = Path(os.environ.get("DATABASE_PATH", ROOT / "control.sqlite3"))
WEB_DIR = ROOT / "webapp"
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
BOT_INTERNAL_TOKEN = os.environ.get("BOT_INTERNAL_TOKEN", "")
DEV_USER_ID = os.environ.get("DEV_TELEGRAM_USER_ID")
active_agents: dict[str, WebSocket] = {}


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def setup_db() -> None:
    with db() as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS agents (
                agent_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                token_hash TEXT NOT NULL,
                threshold TEXT NOT NULL DEFAULT '5000',
                refresh_seconds TEXT NOT NULL DEFAULT '2',
                running INTEGER NOT NULL DEFAULT 0,
                connected INTEGER NOT NULL DEFAULT 0,
                last_status TEXT NOT NULL DEFAULT 'Не підключено',
                updated_at INTEGER NOT NULL
            )"""
        )
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(agents)")}
        if "refresh_seconds" not in columns:
            connection.execute("ALTER TABLE agents ADD COLUMN refresh_seconds TEXT NOT NULL DEFAULT '2'")


def validate_init_data(init_data: str) -> str:
    """Return the Telegram user ID after official WebApp HMAC validation."""
    if not init_data:
        if DEV_USER_ID:
            return DEV_USER_ID
        raise HTTPException(401, "Відкрийте додаток лише з Telegram.")
    if not BOT_TOKEN:
        raise HTTPException(500, "TELEGRAM_BOT_TOKEN не налаштовано на сервері.")
    values = dict(parse_qsl(init_data, keep_blank_values=True))
    supplied_hash = values.pop("hash", "")
    if not supplied_hash:
        raise HTTPException(401, "Некоректні дані Telegram.")
    try:
        auth_date = int(values.get("auth_date", "0"))
    except ValueError as error:
        raise HTTPException(401, "Некоректні дані Telegram.") from error
    if abs(time.time() - auth_date) > 86_400:
        raise HTTPException(401, "Telegram-сесія застаріла. Відкрийте додаток повторно.")
    data_check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied_hash):
        raise HTTPException(401, "Не вдалося перевірити Telegram-сесію.")
    try:
        return str(json.loads(values["user"])["id"])
    except (KeyError, ValueError, TypeError) as error:
        raise HTTPException(401, "Не знайдено користувача Telegram.") from error


def telegram_user(x_telegram_init_data: str | None = Header(default=None)) -> str:
    return validate_init_data(x_telegram_init_data or "")


def row_for(owner_id: str) -> sqlite3.Row | None:
    with db() as connection:
        return connection.execute(
            "SELECT * FROM agents WHERE owner_id = ? ORDER BY updated_at DESC LIMIT 1", (owner_id,)
        ).fetchone()


class Command(BaseModel):
    action: str
    threshold: float | None = Field(default=None, ge=0)
    refresh_seconds: float | None = Field(default=2, ge=1)


class BotCommand(BaseModel):
    owner_id: str
    action: str


@asynccontextmanager
async def lifespan(_: FastAPI):
    setup_db()
    yield


app = FastAPI(title="Paychain Telegram control", lifespan=lifespan)


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    """Railway health check endpoint that does not require Telegram auth."""
    return {"status": "ok"}


@app.get("/api/state")
async def state(owner_id: str = Depends(telegram_user)) -> dict[str, Any]:
    row = row_for(owner_id)
    if not row:
        return {"paired": False, "connected": False, "running": False, "threshold": "5000", "refresh_seconds": "2", "status": "Потрібно підключити локальний агент."}
    return {
        "paired": True,
        "connected": bool(row["connected"]),
        "running": bool(row["running"]),
        "threshold": row["threshold"],
        "refresh_seconds": row["refresh_seconds"],
        "status": row["last_status"],
    }


@app.get("/download/agent")
async def download_agent() -> Response:
    """Return a clean, secret-free Windows agent bundle."""
    project_root = ROOT.parent
    files = (
        "main.py", "config.example.json", "requirements.txt",
        "install_agent.ps1", "install_agent.cmd", "START/install_agent.cmd", "START/run_agent.cmd",
        "telegram_app/__init__.py", "telegram_app/agent.py",
        "telegram_app/agent-config.example.json",
    )
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for relative in files:
            path = project_root / relative
            if path.is_file():
                bundle.write(path, relative)
    return Response(
        content=archive.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=paychain-agent.zip"},
    )


@app.post("/api/pair")
async def pair(owner_id: str = Depends(telegram_user)) -> dict[str, str]:
    agent_id = secrets.token_urlsafe(12)
    agent_token = secrets.token_urlsafe(32)
    with db() as connection:
        connection.execute(
            "INSERT INTO agents(agent_id, owner_id, token_hash, updated_at) VALUES (?, ?, ?, ?)",
            (agent_id, owner_id, hashlib.sha256(agent_token.encode()).hexdigest(), int(time.time())),
        )
    return {"agent_id": agent_id, "agent_token": agent_token}


@app.post("/api/command")
async def command(payload: Command, owner_id: str = Depends(telegram_user)) -> dict[str, str]:
    return await dispatch_command(payload, owner_id)


async def dispatch_command(payload: Command, owner_id: str) -> dict[str, str]:
    row = row_for(owner_id)
    if not row:
        raise HTTPException(409, "Спершу підключіть локальний агент.")
    if payload.action not in {"start", "open_login", "stop", "set_threshold", "disconnect"}:
        raise HTTPException(400, "Невідома команда.")
    if payload.action in {"start", "open_login", "set_threshold"} and payload.threshold is None:
        raise HTTPException(400, "Вкажіть суму.")
    refresh_seconds = payload.refresh_seconds or 2
    message: dict[str, Any] = {"type": "command", "action": payload.action}
    if payload.threshold is not None:
        message["threshold"] = str(payload.threshold)
    message["refresh_seconds"] = str(refresh_seconds)
    socket = active_agents.get(row["agent_id"])
    if payload.action == "disconnect":
        if not socket:
            raise HTTPException(409, "Спочатку підключіть локальний агент, щоб видалити вхід на цьому ПК.")
        if socket:
            await socket.send_json(message)
        with db() as connection:
            connection.execute("DELETE FROM agents WHERE agent_id=?", (row["agent_id"],))
        return {"ok": "true"}
    if not socket:
        raise HTTPException(409, "Локальний агент не підключений.")
    await socket.send_json(message)
    with db() as connection:
        if payload.threshold is not None:
            connection.execute("UPDATE agents SET threshold=?, refresh_seconds=?, updated_at=? WHERE agent_id=?", (str(payload.threshold), str(refresh_seconds), int(time.time()), row["agent_id"]))
    return {"ok": "true"}


@app.post("/api/bot-command")
async def bot_command(
    payload: BotCommand,
    x_bot_internal_token: str | None = Header(default=None),
) -> dict[str, str]:
    """Accept commands from our Telegram bot without exposing user credentials."""
    if not BOT_INTERNAL_TOKEN:
        raise HTTPException(503, "BOT_INTERNAL_TOKEN не налаштовано на сервері.")
    if not x_bot_internal_token or not hmac.compare_digest(x_bot_internal_token, BOT_INTERNAL_TOKEN):
        raise HTTPException(403, "Некоректний внутрішній токен бота.")
    if payload.action != "stop":
        raise HTTPException(400, "Непідтримувана команда бота.")
    return await dispatch_command(Command(action=payload.action), payload.owner_id)


async def notify_telegram(owner_id: str, text: str) -> None:
    if not BOT_TOKEN:
        return
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={"chat_id": owner_id, "text": text})


@app.websocket("/ws/agent")
async def agent_socket(socket: WebSocket) -> None:
    agent_id = socket.query_params.get("agent_id", "")
    token = socket.query_params.get("agent_token", "")
    with db() as connection:
        row = connection.execute("SELECT * FROM agents WHERE agent_id=?", (agent_id,)).fetchone()
    if not row or not hmac.compare_digest(row["token_hash"], hashlib.sha256(token.encode()).hexdigest()):
        await socket.close(code=1008)
        return
    await socket.accept()
    active_agents[agent_id] = socket
    with db() as connection:
        connection.execute("UPDATE agents SET connected=1, last_status=?, updated_at=? WHERE agent_id=?", ("Агент підключений", int(time.time()), agent_id))
    try:
        while True:
            event = await socket.receive_json()
            if event.get("type") != "status":
                continue
            status = str(event.get("status", ""))[:300]
            running = 1 if event.get("running") else 0
            with db() as connection:
                connection.execute("UPDATE agents SET running=?, last_status=?, updated_at=? WHERE agent_id=?", (running, status, int(time.time()), agent_id))
            if event.get("accepted"):
                await notify_telegram(row["owner_id"], f"✅ Угоду прийнято\nСума: {event.get('amount')} {event.get('currency', 'UAH')}")
    except WebSocketDisconnect:
        pass
    finally:
        active_agents.pop(agent_id, None)
        with db() as connection:
            connection.execute("UPDATE agents SET connected=0, running=0, last_status=?, updated_at=? WHERE agent_id=?", ("Агент відключений", int(time.time()), agent_id))


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="webapp")
