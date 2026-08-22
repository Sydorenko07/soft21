"""Local worker: receives commands and runs the existing Playwright monitor.

This program is installed on each user's PC.  Paychain browser data stays here.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import websockets


ROOT = Path(__file__).parents[1]
HERE = Path(__file__).parent
CONFIG_PATH = HERE / "agent-config.json"
SIGNAL_PATH = ROOT / ".start_monitoring.signal"
LOGIN_SIGNAL_PATH = ROOT / ".paychain_login_ready.signal"
RUNTIME_SETTINGS_PATH = ROOT / ".paychain_runtime_settings.json"
ACTIVITY_LOG = ROOT / "logs" / "activity.log"
DOWNLOAD_CONFIG_PATH = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Downloads" / "agent-config.json"
PROFILE_DIR = ROOT / ".browser-profile"
LEGACY_STATE_FILE = ROOT / "storage_state.json"


class Agent:
    def __init__(self, config: dict[str, str]) -> None:
        self.config = config
        self.process: subprocess.Popen[str] | None = None
        self.threshold = "5000"
        self.refresh_seconds = "2"
        self.login_pending = False
        self.login_ready = False
        self.login_notice_pending = False
        self.monitoring_enabled = False
        self.activity_position = ACTIVITY_LOG.stat().st_size if ACTIVITY_LOG.exists() else 0

    def write_runtime_settings(self) -> None:
        RUNTIME_SETTINGS_PATH.write_text(
            json.dumps({"minimum_amount_uah": self.threshold, "refresh_seconds": self.refresh_seconds}),
            encoding="utf-8",
        )

    def process_alive(self) -> bool:
        """Return true only while the browser-monitor child is really alive."""
        if self.process is None:
            return False
        if self.process.poll() is None:
            return True
        self.process = None
        self.login_pending = False
        self.login_ready = False
        self.login_notice_pending = False
        self.monitoring_enabled = False
        SIGNAL_PATH.unlink(missing_ok=True)
        LOGIN_SIGNAL_PATH.unlink(missing_ok=True)
        RUNTIME_SETTINGS_PATH.unlink(missing_ok=True)
        return False

    def start(self, threshold: str, refresh_seconds: str = "2") -> None:
        if self.process_alive():
            self.threshold = threshold
            self.refresh_seconds = refresh_seconds
            self.write_runtime_settings()
            SIGNAL_PATH.write_text("start", encoding="utf-8")
            self.login_pending = False
            self.login_ready = False
            self.monitoring_enabled = True
            LOGIN_SIGNAL_PATH.unlink(missing_ok=True)
            return
        self.terminate_process()
        self.threshold = threshold
        self.refresh_seconds = refresh_seconds
        self.write_runtime_settings()
        SIGNAL_PATH.unlink(missing_ok=True)
        LOGIN_SIGNAL_PATH.unlink(missing_ok=True)
        self.login_ready = False
        self.login_notice_pending = False
        self.monitoring_enabled = True
        command = [
            str(Path(sys.executable)), str(ROOT / "main.py"), "--auto-accept",
            "--minimum-amount", threshold, "--refresh-seconds", refresh_seconds,
            "--start-signal", str(SIGNAL_PATH), "--login-signal", str(LOGIN_SIGNAL_PATH),
            "--runtime-settings", str(RUNTIME_SETTINGS_PATH), "--minimized",
        ]
        self.process = subprocess.Popen(command, cwd=ROOT)
        SIGNAL_PATH.write_text("start", encoding="utf-8")

    def open_login(self, threshold: str, refresh_seconds: str = "2") -> None:
        if self.process_alive():
            self.threshold = threshold
            self.refresh_seconds = refresh_seconds
            self.write_runtime_settings()
            SIGNAL_PATH.unlink(missing_ok=True)
            LOGIN_SIGNAL_PATH.unlink(missing_ok=True)
            self.login_pending = True
            self.login_ready = False
            self.login_notice_pending = False
            self.monitoring_enabled = False
            return
        self.threshold = threshold
        self.refresh_seconds = refresh_seconds
        self.write_runtime_settings()
        SIGNAL_PATH.unlink(missing_ok=True)
        LOGIN_SIGNAL_PATH.unlink(missing_ok=True)
        command = [
            str(Path(sys.executable)), str(ROOT / "main.py"), "--auto-accept",
            "--minimum-amount", threshold, "--refresh-seconds", refresh_seconds,
            "--start-signal", str(SIGNAL_PATH), "--login-signal", str(LOGIN_SIGNAL_PATH),
            "--runtime-settings", str(RUNTIME_SETTINGS_PATH),
        ]
        self.process = subprocess.Popen(command, cwd=ROOT)
        self.login_pending = True
        self.login_ready = False
        self.login_notice_pending = False
        self.monitoring_enabled = False

    def stop(self) -> None:
        """Pause monitoring but keep the browser and Paychain session open."""
        SIGNAL_PATH.unlink(missing_ok=True)
        self.monitoring_enabled = False
        self.login_pending = False
        self.login_ready = False
        self.login_notice_pending = False

    def terminate_process(self) -> None:
        """Fully close the monitor process; used only for disconnect."""
        process = self.process
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        self.process = None
        self.login_pending = False
        self.login_ready = False
        self.login_notice_pending = False
        self.monitoring_enabled = False
        SIGNAL_PATH.unlink(missing_ok=True)
        LOGIN_SIGNAL_PATH.unlink(missing_ok=True)
        RUNTIME_SETTINGS_PATH.unlink(missing_ok=True)

    def clear_paychain_session(self) -> None:
        """Remove the local browser session only on an explicit disconnect."""
        self.terminate_process()
        shutil.rmtree(PROFILE_DIR, ignore_errors=True)
        LEGACY_STATE_FILE.unlink(missing_ok=True)

    @property
    def running(self) -> bool:
        return self.monitoring_enabled and self.process_alive()

    def new_activity(self) -> list[dict[str, str]]:
        if not ACTIVITY_LOG.exists():
            return []
        with ACTIVITY_LOG.open("r", encoding="utf-8") as file:
            file.seek(self.activity_position)
            lines = file.readlines()
            self.activity_position = file.tell()
        events = []
        for line in lines:
            if "ПРИЙНЯТО |" not in line:
                continue
            # Example: timestamp ПРИЙНЯТО | оффер abc | 2500.00 UAH
            parts = [part.strip() for part in line.split("|")]
            if len(parts) >= 3:
                values = parts[-1].split(maxsplit=1)
                if len(values) == 2:
                    amount, currency = values
                    events.append({"amount": amount, "currency": currency})
        return events


def adopt_downloaded_config() -> bool:
    """Move the newest valid pairing file downloaded on this same PC.

    Browsers append `` (1)``, `` (2)`` ... when the file already exists, so
    looking only for the exact name makes reconnecting appear to do nothing.
    """
    candidates = [
        path for path in DOWNLOAD_CONFIG_PATH.parent.glob("agent-config*.json")
        if path.is_file()
    ]
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    required = ("server_ws_url", "agent_id", "agent_token")
    for downloaded in candidates:
        try:
            config = json.loads(downloaded.read_text(encoding="utf-8"))
            if any(not str(config.get(key, "")).strip() for key in required):
                continue
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.unlink(missing_ok=True)
            shutil.move(str(downloaded), str(CONFIG_PATH))
            return True
        except (OSError, json.JSONDecodeError):
            continue
    return False


async def run() -> None:
    # The Windows installer starts this process before the PC is paired.
    # Keep it alive and wait for the one-time config from the Mini App.
    agent = Agent({})
    while True:
        adopt_downloaded_config()
        if not CONFIG_PATH.exists():
            await asyncio.sleep(5)
            continue
        try:
            config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            required = ("server_ws_url", "agent_id", "agent_token")
            if any(not str(config.get(key, "")).strip() for key in required):
                await asyncio.sleep(5)
                continue
            url = f"{config['server_ws_url']}?agent_id={config['agent_id']}&agent_token={config['agent_token']}"
            async with websockets.connect(url, ping_interval=20) as socket:
                await socket.send(json.dumps({"type": "status", "running": False, "status": "Агент готовий"}))
                while True:
                    try:
                        raw = await asyncio.wait_for(socket.recv(), timeout=1)
                        command = json.loads(raw)
                        if command["action"] == "open_login":
                            agent.open_login(command["threshold"], command.get("refresh_seconds", "2"))
                        elif command["action"] == "start":
                            agent.start(command["threshold"], command.get("refresh_seconds", "2"))
                        elif command["action"] == "stop":
                            agent.stop()
                        elif command["action"] == "set_threshold":
                            if agent.login_pending:
                                agent.open_login(command["threshold"], command.get("refresh_seconds", "2"))
                            elif agent.running:
                                agent.threshold = command["threshold"]
                                agent.refresh_seconds = command.get("refresh_seconds", agent.refresh_seconds)
                                agent.write_runtime_settings()
                            else:
                                agent.threshold = command["threshold"]
                                agent.refresh_seconds = command.get("refresh_seconds", agent.refresh_seconds)
                                agent.write_runtime_settings()
                        elif command["action"] == "disconnect":
                            agent.clear_paychain_session()
                            CONFIG_PATH.unlink(missing_ok=True)
                            await socket.close(code=1000, reason="Disconnected by user")
                            break
                    except asyncio.TimeoutError:
                        pass
                    if agent.login_pending and LOGIN_SIGNAL_PATH.exists():
                        agent.login_pending = False
                        agent.login_ready = True
                        agent.login_notice_pending = True
                        LOGIN_SIGNAL_PATH.unlink(missing_ok=True)
                    if agent.login_pending:
                        status = "Очікується вхід у Paychain"
                    elif agent.login_ready:
                        status = "Вхід у Paychain виконано, очікується запуск алгоритму"
                    else:
                        status = "Моніторинг працює" if agent.running else "Зупинено"
                    status_event = {"type": "status", "running": agent.running and not agent.login_pending and not agent.login_ready, "status": status}
                    if agent.login_notice_pending:
                        status_event["login_ready"] = True
                        agent.login_notice_pending = False
                    await socket.send(json.dumps(status_event))
                    for event in agent.new_activity():
                        await socket.send(json.dumps({"type": "status", "running": agent.running, "status": "Угоду прийнято", "accepted": True, **event}))
        except Exception as error:
            # A 403 means the pairing token is stale (for example after a new
            # pairing or a server redeploy). Let the next downloaded config be
            # adopted automatically instead of requiring manual file removal.
            if "403" in str(error) and CONFIG_PATH.exists():
                CONFIG_PATH.unlink(missing_ok=True)
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(run())
