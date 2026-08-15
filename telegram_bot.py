#!/usr/bin/env python3
"""
Telegram remote controller for the existing BlackBird automation (main.py).

First Telegram "Start" (/start)  → connect + show button menu only
Keyboard /start                  → start automation
Keyboard /stop                   → stop automation
Keyboard /status                 → running / stopped
Keyboard /card                   → enter cards, then /save → card.txt
Keyboard /proxy                  → enter proxies, then /save → data.txt
                                   (IP:Port:User:Pass auto-saved as User:Pass:IP:Port)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telegram import BotCommand, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from process_manager import ProcessManager

# Shown in welcome so you can verify the Mac is running THIS file
BOT_UI_VERSION = "v2026-08-15-proxy-reorder"

PROJECT_DIR = Path(__file__).resolve().parent
CARD_FILE = PROJECT_DIR / "card.txt"
DATA_FILE = PROJECT_DIR / "data.txt"
LOG_DIR = PROJECT_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "bot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("telegram_bot")

load_dotenv(PROJECT_DIR / ".env")

USER_CONNECTED_KEY = "bot_menu_shown"
EDIT_MODE_KEY = "edit_mode"  # None | "card" | "proxy"
EDIT_BUFFER_KEY = "edit_buffer"  # list[str]

CARD_HEADER = (
    "# One card per line: number|MM|YY|CVC|Cardholder Name\n"
    "# Cards cycle across workflows (wrap when fewer cards than proxies).\n"
    "# Length may differ from data.txt / email.txt — that is OK.\n"
)
PROXY_HEADER = (
    "# One proxy per line (Username:Password:IP:Port). Exact case preserved.\n"
    "# Paste IP:Port:Username:Password via Telegram — it is auto-converted on /save.\n"
    "# Workflow count = number of proxy lines (top → bottom). Always.\n"
)

MESSAGES = {
    "welcome": (
        f"📋 MENU  BlackBird Automation ({BOT_UI_VERSION})\n\n"
        "Connected. Five buttons below:\n"
        "• /status — current job status\n"
        "• /start — start automation\n"
        "• /stop — stop automation + BlackBird\n"
        "• /card — replace cards (shows stored count)\n"
        "• /proxy — replace proxies (shows stored count)"
    ),
    "started": (
        "▶️ START: job is RUNNING now.\n"
        "Other connected users have been notified.\n"
        "You will receive a 🎉 completion message when it finishes by itself."
    ),
    "already_running": (
        "⚠️ START BLOCKED: a job is already RUNNING.\n"
        "A second job was not started.\n"
        "Use /status or /stop."
    ),
    "stopped": (
        "🛑 STOP: you ended the job.\n"
        "Automation process terminated.\n"
        "BlackBird app closed.\n"
        "Other connected users have been notified."
    ),
    "not_running": (
        "💤 STOP: nothing was running.\n"
        "Automation is idle. BlackBird is already off."
    ),
    "running": (
        "🟢 STATUS: RUNNING\n"
        "Automation is in progress right now."
    ),
    "idle": (
        "⚪ STATUS: IDLE\n"
        "No job is running.\n"
        "Press /start to begin."
    ),
    "finished_status": (
        "🏁 STATUS: LAST JOB COMPLETED\n"
        "The previous job finished by itself.\n"
        "No job is running now."
    ),
    "failed_status": (
        "🔴 STATUS: LAST JOB ENDED WITH AN ERROR\n"
        "The previous job stopped unexpectedly.\n"
        "No job is running now."
    ),
    "job_failed": (
        "💥 UNEXPECTED ERROR\n"
        "The task stopped before finishing all proxies.\n"
        "Exit code: {code}\n"
        "Check logs/automation.log on the VPS."
    ),
    "start_failed": "❌ START FAILED.\nPlease check the VPS logs.",
    "stop_failed": "❌ STOP FAILED.\nPlease check the VPS logs.",
    "missing_config": "❌ Bot is not configured. Set TELEGRAM_BOT_TOKEN in .env",
    "card_count": "📊 CARDS STORED NOW: {count} row(s) in card.txt",
    "proxy_count": "📊 PROXIES STORED NOW: {count} row(s) in data.txt",
    "card_prompt": (
        "💳 CARD INPUT MODE\n\n"
        "Send card lines (one per line):\n"
        "number|MM|YY|CVC|Name\n\n"
        "Example:\n"
        "4426454034937026|03|28|978|Andrew Grant\n\n"
        "The newly saved rows REPLACE all previously stored cards.\n"
        "Duplicates within the new rows are removed.\n"
        "Press /save to keep, or /cancel to return."
    ),
    "proxy_prompt": (
        "🌐 PROXY INPUT MODE\n\n"
        "Send proxy lines (one per line).\n\n"
        "Accepted paste format:\n"
        "IP:Port:Username:Password\n"
        "Example:\n"
        "69.10.54.69:9648:rps56862:rps56862\n\n"
        "On /save this is auto-converted and stored as:\n"
        "Username:Password:IP:Port\n"
        "Example:\n"
        "rps56862:rps56862:69.10.54.69:9648\n\n"
        "You may also paste the stored format directly.\n"
        "The newly saved rows REPLACE all previously stored proxies.\n"
        "Duplicates within the new rows are removed.\n"
        "Press /save to keep, or /cancel to return."
    ),
    "buffered": (
        "📥 BUFFER: {count} new line(s) ready.\n"
        "Press /save to keep, or /cancel to discard."
    ),
    "nothing_to_save": (
        "⚠️ SAVE: nothing received yet.\n"
        "Send data first, then /save — or /cancel to return."
    ),
    "save_cancelled": "ℹ️ SAVE ignored — not in /card or /proxy mode.",
    "input_cancelled": "↩️ CANCEL: returned to menu. Nothing was saved.",
    "card_saved": (
        "💾 CARD REPLACEMENT DONE\n"
        "• Previous records removed: {previous}\n"
        "• Total stored now: {total}"
    ),
    "proxy_saved": (
        "💾 PROXY REPLACEMENT DONE\n"
        "• Previous records removed: {previous}\n"
        "• Total stored now: {total}\n"
        "• Auto-converted IP:Port:User:Pass → User:Pass:IP:Port: {converted}\n"
        "• Stored format: Username:Password:IP:Port"
    ),
    "save_failed": "❌ Failed to save. Check VPS logs.",
    "card_invalid": (
        "⚠️ No valid card lines found.\n"
        "Format: number|MM|YY|CVC|Name"
    ),
    "proxy_invalid": (
        "⚠️ No valid proxy lines found.\n"
        "Paste: IP:Port:Username:Password\n"
        "Or stored: Username:Password:IP:Port"
    ),
}

manager = ProcessManager()
_process_lock = asyncio.Lock()
_notify_chat_ids: set[int] = set()
_watch_task: asyncio.Task | None = None
NOTIFY_CHATS_FILE = LOG_DIR / "notify_chats.txt"
RESULTS_FILE = LOG_DIR / "last_run.json"


def _load_notify_chats() -> None:
    if not NOTIFY_CHATS_FILE.is_file():
        return
    for raw in NOTIFY_CHATS_FILE.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if raw.lstrip("-").isdigit():
            _notify_chat_ids.add(int(raw))


def _save_notify_chats() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    NOTIFY_CHATS_FILE.write_text(
        "\n".join(str(cid) for cid in sorted(_notify_chat_ids)) + "\n",
        encoding="utf-8",
    )


def _remember_chat(update: Update) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    if chat.id not in _notify_chat_ids:
        _notify_chat_ids.add(chat.id)
        _save_notify_chats()


def _actor_label(update: Update) -> str:
    """Human-readable identity for shared action notifications."""
    user = update.effective_user
    if user is None:
        return "Another user"
    if user.username:
        return f"@{user.username}"
    if user.full_name:
        return user.full_name
    return f"User {user.id}"


async def _notify_other_users(
    app: Application,
    update: Update,
    action_text: str,
) -> None:
    """Send an action notification to every connected chat except the actor."""
    _remember_chat(update)
    if not _notify_chat_ids:
        _load_notify_chats()
    actor_chat_id = update.effective_chat.id if update.effective_chat else None
    text = f"👥 SHARED ACTIVITY\n{_actor_label(update)} {action_text}"
    for chat_id in list(_notify_chat_ids):
        if chat_id == actor_chat_id:
            continue
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=main_keyboard(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed shared-action notification to chat %s: %s",
                chat_id,
                exc,
            )


def _read_run_results() -> dict | None:
    if not RESULTS_FILE.is_file():
        return None
    try:
        data = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:  # noqa: BLE001
        return None
    return None


def _format_run_summary(data: dict) -> str:
    total = int(data.get("total", 0))
    active = int(data.get("active", 0))
    paid = int(data.get("paid", 0))
    inactive = int(data.get("inactive", 0))
    stripe_failed = int(data.get("stripe_failed", 0))
    setup_failed = int(data.get("setup_failed", 0))
    paid_of_active = f"{paid}/{active}" if active else f"{paid}/0"
    return (
        "🎉 TASK COMPLETED\n\n"
        f"🌐 Active proxies: {active} / {total} total\n"
        f"💳 Paid (Stripe OK): {paid_of_active} active\n"
        f"⏭ Inactive (skipped): {inactive}\n"
        f"⚠️ Card/Stripe failed: {stripe_failed}\n"
        f"🔧 Setup failed: {setup_failed}\n\n"
        "👀 BlackBird and all proxy browsers remain running for review.\n"
        "🪟 All proxy-browser windows were minimized; BlackBird is active.\n"
        "✅ Only the automation process has closed."
    )


def _count_file_rows(path: Path, kind: str) -> int:
    lines = _read_existing_data_lines(path)
    if kind == "card":
        return len(_filter_card_lines(lines))
    return len(_filter_proxy_lines(lines))


def _status_message() -> str:
    if manager.is_running():
        pid = manager.current_pid()
        extra = f"\nProcess PID: {pid}" if pid else ""
        return MESSAGES["running"] + extra
    code = manager.last_exit_code
    if manager.last_stop_was_user:
        return MESSAGES["idle"] + "\nLast action: stopped by /stop."
    results = _read_run_results()
    if results and results.get("outcome") == "completed":
        return MESSAGES["finished_status"] + "\n\n" + _format_run_summary(results)
    if results and results.get("outcome") == "error":
        err = results.get("error") or f"exit code {code}"
        return MESSAGES["failed_status"] + f"\n{err}"
    if code == 0:
        return MESSAGES["finished_status"]
    if code is not None and code != 0:
        return MESSAGES["failed_status"] + f"\nExit code: {code}"
    return MESSAGES["idle"]


async def _watch_job_until_done(app: Application) -> None:
    """Notify Telegram when automation exits by itself (not after /stop)."""
    logger.info("Job watcher started")
    try:
        while True:
            await asyncio.sleep(3)
            if manager.last_stop_was_user:
                logger.info("Job watcher: user stopped the job — no auto notice")
                return
            if manager.is_running():
                continue
            code = manager.poll_exit_code()
            if manager.last_stop_was_user:
                return
            results = _read_run_results()
            if results and results.get("outcome") == "completed":
                text = _format_run_summary(results)
            elif results and results.get("outcome") == "error":
                err = results.get("error") or f"exit {code}"
                text = MESSAGES["job_failed"].format(code=code if code is not None else "?")
                text += f"\n{err}"
            elif code in (0, None):
                text = (
                    "🎉 TASK COMPLETED\n"
                    "The batch finished. Only automation closed; BlackBird and "
                    "all proxy browsers remain running for review.\n"
                    "Open /status for details if available."
                )
            else:
                text = MESSAGES["job_failed"].format(code=code)
            logger.info("Job watcher: automation ended code=%s chats=%s", code, list(_notify_chat_ids))
            if not _notify_chat_ids:
                _load_notify_chats()
            for chat_id in list(_notify_chat_ids):
                try:
                    await app.bot.send_message(chat_id=chat_id, text=text)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Failed to notify chat %s: %s", chat_id, exc)
            return
    except asyncio.CancelledError:
        logger.info("Job watcher cancelled")
        raise


def _start_job_watcher(app: Application) -> None:
    global _watch_task
    if _watch_task is not None and not _watch_task.done():
        _watch_task.cancel()
    _watch_task = asyncio.create_task(_watch_job_until_done(app))


def main_keyboard() -> ReplyKeyboardMarkup:
    """
    Main keyboard shown on first connect — exactly 5 buttons:
      /status  /start
      /card    /proxy
      /stop
    """
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("/status"), KeyboardButton("/start")],
            [KeyboardButton("/card"), KeyboardButton("/proxy")],
            [KeyboardButton("/stop")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        one_time_keyboard=False,
        input_field_placeholder="Commands, or paste after /card or /proxy",
    )


def save_keyboard() -> ReplyKeyboardMarkup:
    """Shown in /card or /proxy input mode: save or return to menu."""
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("/save"), KeyboardButton("/cancel")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        one_time_keyboard=False,
    )


async def show_main_menu(update: Update, text: str) -> None:
    """
    Force Telegram to replace any old reply keyboard with the 5-button menu.
    Remove first, then send the new keyboard (avoids stuck old 3-button UI).
    """
    if not update.message:
        return
    try:
        await update.message.reply_text(
            "⏳ Loading menu…",
            reply_markup=ReplyKeyboardRemove(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("ReplyKeyboardRemove failed: %s", exc)
    await update.message.reply_text(text, reply_markup=main_keyboard())


def _clear_edit_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data[EDIT_MODE_KEY] = None
    context.user_data[EDIT_BUFFER_KEY] = []


def _parse_incoming_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip().strip("\ufeff")
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return lines


def _dedupe_preserve_order(lines: list[str]) -> list[str]:
    """Keep first occurrence of each exact row; drop later duplicates."""
    seen: set[str] = set()
    unique: list[str] = []
    for line in lines:
        key = line.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(key)
    return unique


def _filter_card_lines(lines: list[str]) -> list[str]:
    valid: list[str] = []
    for line in lines:
        parts = [p.strip() for p in line.split("|")]
        if len(parts) == 5 and parts[0] and parts[4]:
            valid.append("|".join(parts))
        elif len(parts) == 4 and parts[0] and parts[3]:
            valid.append("|".join(parts))
    return _dedupe_preserve_order(valid)


def _is_ipv4(value: str) -> bool:
    parts = value.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False


def _is_port(value: str) -> bool:
    return value.isdigit() and 1 <= int(value) <= 65535


def _looks_like_host(value: str) -> bool:
    """IPv4 or hostname — must contain a dot and no spaces."""
    return bool(value) and "." in value and not any(ch.isspace() for ch in value)


def normalize_proxy_line(line: str) -> tuple[str | None, bool]:
    """
    Return (Username:Password:IP:Port, converted?).

    Accepted inputs:
      - IP:Port:Username:Password  → rewritten to Username:Password:IP:Port
      - Username:Password:IP:Port  → kept as-is
      - Username:Password@IP:Port  → rewritten to Username:Password:IP:Port (legacy)
    """
    cleaned = line.strip().strip("\ufeff")
    if not cleaned or any(ch.isspace() for ch in cleaned):
        return None, False

    if "@" in cleaned:
        creds, _, hostport = cleaned.rpartition("@")
        host, _, port = hostport.rpartition(":")
        if (
            creds.count(":") == 1
            and _looks_like_host(host)
            and _is_port(port)
        ):
            return f"{creds}:{host}:{port}", True
        return None, False

    parts = cleaned.split(":")
    if len(parts) != 4:
        return None, False
    left_a, left_b, right_a, right_b = parts
    # Paste format from the client: IP:Port:Username:Password
    if _is_ipv4(left_a) and _is_port(left_b) and right_a and right_b:
        return f"{right_a}:{right_b}:{left_a}:{left_b}", True
    # Stored / BlackBird input format: Username:Password:IP:Port
    if _looks_like_host(right_a) and _is_port(right_b) and left_a and left_b:
        return cleaned, False
    return None, False


def _filter_proxy_lines(lines: list[str]) -> list[str]:
    """Normalize + dedupe proxy rows (count / read path)."""
    normalized, _converted = _normalize_proxy_lines(lines)
    return normalized


def _normalize_proxy_lines(lines: list[str]) -> tuple[list[str], int]:
    """
    Normalize proxy rows to Username:Password:IP:Port.
    Returns (deduped rows, how many input rows were reordered/converted).
    """
    valid: list[str] = []
    converted = 0
    for line in lines:
        proxy, was_converted = normalize_proxy_line(line)
        if proxy is None:
            continue
        if was_converted:
            converted += 1
        valid.append(proxy)
    return _dedupe_preserve_order(valid), converted


def _read_existing_data_lines(path: Path) -> list[str]:
    """Read non-comment, non-empty data rows from an existing txt file."""
    if not path.is_file():
        return []
    return _parse_incoming_lines(path.read_text(encoding="utf-8"))


def _write_card_file(lines: list[str]) -> None:
    body = CARD_HEADER + "\n".join(lines) + ("\n" if lines else "")
    CARD_FILE.write_text(body, encoding="utf-8")


def _write_proxy_file(lines: list[str]) -> None:
    body = PROXY_HEADER + "\n".join(lines) + ("\n" if lines else "")
    DATA_FILE.write_text(body, encoding="utf-8")


async def _start_automation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _remember_chat(update)
    async with _process_lock:
        ok, key = await asyncio.to_thread(manager.start)
    if not update.message:
        return
    if ok:
        _start_job_watcher(context.application)
        await update.message.reply_text(MESSAGES["started"], reply_markup=main_keyboard())
        await _notify_other_users(
            context.application,
            update,
            "started the automation. ▶️",
        )
    elif key == "already_running":
        await update.message.reply_text(
            MESSAGES["already_running"], reply_markup=main_keyboard()
        )
    else:
        await update.message.reply_text(
            MESSAGES["start_failed"], reply_markup=main_keyboard()
        )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /start is used twice in Telegram:
      1) First "Start" when opening the bot → connect + show menu (no automation)
      2) Keyboard button /start after that   → start automation
    """
    uid = update.effective_user.id if update.effective_user else "?"
    _remember_chat(update)
    # Leaving edit mode if user presses /start from elsewhere
    if context.user_data.get(EDIT_MODE_KEY):
        _clear_edit_state(context)

    menu_shown = context.user_data.get(USER_CONNECTED_KEY, False)
    if not menu_shown:
        context.user_data[USER_CONNECTED_KEY] = True
        logger.info(
            "/start (first connect) user_id=%s ui=%s — send 5-button keyboard",
            uid,
            BOT_UI_VERSION,
        )
        await show_main_menu(update, MESSAGES["welcome"])
        await _notify_other_users(
            context.application,
            update,
            "connected to the bot. 👋",
        )
        return

    logger.info("/start (keyboard — start automation) from user_id=%s", uid)
    await _start_automation(update, context)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else "?"
    logger.info("/stop from user_id=%s", uid)
    _remember_chat(update)
    if context.user_data.get(EDIT_MODE_KEY):
        _clear_edit_state(context)
    global _watch_task
    if _watch_task is not None and not _watch_task.done():
        _watch_task.cancel()
        _watch_task = None
    async with _process_lock:
        ok, key = await asyncio.to_thread(manager.stop)
    if update.message:
        if ok:
            await update.message.reply_text(MESSAGES["stopped"], reply_markup=main_keyboard())
            await _notify_other_users(
                context.application,
                update,
                "stopped the automation and closed BlackBird. 🛑",
            )
        elif key == "not_running":
            await update.message.reply_text(
                MESSAGES["not_running"], reply_markup=main_keyboard()
            )
        else:
            await update.message.reply_text(
                MESSAGES["stop_failed"], reply_markup=main_keyboard()
            )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else "?"
    logger.info("/status from user_id=%s", uid)
    _remember_chat(update)
    if context.user_data.get(EDIT_MODE_KEY):
        _clear_edit_state(context)
    text = await asyncio.to_thread(_status_message)
    if update.message:
        await update.message.reply_text(text, reply_markup=main_keyboard())
        await _notify_other_users(
            context.application,
            update,
            "checked the automation status. 🔎",
        )


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force-refresh the 5-button keyboard (does not start automation)."""
    uid = update.effective_user.id if update.effective_user else "?"
    _remember_chat(update)
    logger.info("/menu from user_id=%s ui=%s", uid, BOT_UI_VERSION)
    context.user_data[USER_CONNECTED_KEY] = True
    _clear_edit_state(context)
    await show_main_menu(update, MESSAGES["welcome"])
    await _notify_other_users(
        context.application,
        update,
        "opened the main menu. 📋",
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Leave /card or /proxy input without saving and restore the main menu."""
    uid = update.effective_user.id if update.effective_user else "?"
    _remember_chat(update)
    mode = context.user_data.get(EDIT_MODE_KEY)
    logger.info("/cancel from user_id=%s mode=%s", uid, mode)
    context.user_data[USER_CONNECTED_KEY] = True
    _clear_edit_state(context)
    await show_main_menu(update, MESSAGES["input_cancelled"])
    await _notify_other_users(
        context.application,
        update,
        f"cancelled {mode or 'input'} mode without saving. ↩️",
    )


async def cmd_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Enter card input mode — show stored count, then /save and /cancel."""
    uid = update.effective_user.id if update.effective_user else "?"
    _remember_chat(update)
    context.user_data[USER_CONNECTED_KEY] = True
    context.user_data[EDIT_MODE_KEY] = "card"
    context.user_data[EDIT_BUFFER_KEY] = []
    count = _count_file_rows(CARD_FILE, "card")
    logger.info("/card from user_id=%s stored_count=%s file=%s", uid, count, CARD_FILE)
    if update.message:
        await update.message.reply_text(
            MESSAGES["card_count"].format(count=count),
            reply_markup=save_keyboard(),
        )
        await update.message.reply_text(
            MESSAGES["card_prompt"],
            reply_markup=save_keyboard(),
        )
        await _notify_other_users(
            context.application,
            update,
            "opened card replacement mode. 💳",
        )


async def cmd_proxy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Enter proxy input mode — show stored count, then /save and /cancel."""
    uid = update.effective_user.id if update.effective_user else "?"
    _remember_chat(update)
    context.user_data[USER_CONNECTED_KEY] = True
    context.user_data[EDIT_MODE_KEY] = "proxy"
    context.user_data[EDIT_BUFFER_KEY] = []
    count = _count_file_rows(DATA_FILE, "proxy")
    logger.info("/proxy from user_id=%s stored_count=%s file=%s", uid, count, DATA_FILE)
    if update.message:
        await update.message.reply_text(
            MESSAGES["proxy_count"].format(count=count),
            reply_markup=save_keyboard(),
        )
        await update.message.reply_text(
            MESSAGES["proxy_prompt"],
            reply_markup=save_keyboard(),
        )
        await _notify_other_users(
            context.application,
            update,
            "opened proxy replacement mode. 🌐",
        )


async def cmd_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Save buffered card/proxy lines to the matching txt file."""
    uid = update.effective_user.id if update.effective_user else "?"
    _remember_chat(update)
    mode = context.user_data.get(EDIT_MODE_KEY)
    buffer: list[str] = list(context.user_data.get(EDIT_BUFFER_KEY) or [])
    logger.info("/save from user_id=%s mode=%s buffer_lines=%s", uid, mode, len(buffer))

    if not update.message:
        return

    if mode not in ("card", "proxy"):
        await update.message.reply_text(
            MESSAGES["save_cancelled"], reply_markup=main_keyboard()
        )
        return

    if not buffer:
        await update.message.reply_text(
            MESSAGES["nothing_to_save"], reply_markup=save_keyboard()
        )
        return

    try:
        if mode == "card":
            incoming = _filter_card_lines(buffer)
            if not incoming:
                await update.message.reply_text(
                    MESSAGES["card_invalid"], reply_markup=save_keyboard()
                )
                return
            previous = _count_file_rows(CARD_FILE, "card")
            await asyncio.to_thread(_write_card_file, incoming)
            _clear_edit_state(context)
            await update.message.reply_text(
                MESSAGES["card_saved"].format(
                    previous=previous, total=len(incoming)
                ),
                reply_markup=main_keyboard(),
            )
            await _notify_other_users(
                context.application,
                update,
                f"replaced the stored card records ({previous} → {len(incoming)}). 💳",
            )
            logger.info(
                "Cards replaced → previous=%s total=%s file=%s",
                previous,
                len(incoming),
                CARD_FILE,
            )
        else:
            incoming, converted = _normalize_proxy_lines(buffer)
            if not incoming:
                await update.message.reply_text(
                    MESSAGES["proxy_invalid"], reply_markup=save_keyboard()
                )
                return
            previous = _count_file_rows(DATA_FILE, "proxy")
            await asyncio.to_thread(_write_proxy_file, incoming)
            _clear_edit_state(context)
            await update.message.reply_text(
                MESSAGES["proxy_saved"].format(
                    previous=previous,
                    total=len(incoming),
                    converted=converted,
                ),
                reply_markup=main_keyboard(),
            )
            await _notify_other_users(
                context.application,
                update,
                (
                    f"replaced the stored proxy records ({previous} → {len(incoming)}; "
                    f"{converted} auto-converted to Username:Password:IP:Port). 🌐"
                ),
            )
            logger.info(
                "Proxies replaced → previous=%s total=%s converted=%s file=%s",
                previous,
                len(incoming),
                converted,
                DATA_FILE,
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Save failed: %s", exc)
        await update.message.reply_text(
            MESSAGES["save_failed"], reply_markup=save_keyboard()
        )


async def on_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Collect free-text lines while in /card or /proxy input mode."""
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    # Ignore command-looking text (handled by CommandHandlers)
    if text.startswith("/"):
        return

    mode = context.user_data.get(EDIT_MODE_KEY)
    if mode not in ("card", "proxy"):
        return
    _remember_chat(update)

    lines = _parse_incoming_lines(text)
    if not lines:
        await update.message.reply_text(
            "⚠️ Empty message. Send data lines, then press /save.",
            reply_markup=save_keyboard(),
        )
        return

    buffer: list[str] = list(context.user_data.get(EDIT_BUFFER_KEY) or [])
    buffer.extend(lines)
    context.user_data[EDIT_BUFFER_KEY] = buffer
    logger.info(
        "Buffered %s line(s) for mode=%s (total=%s)",
        len(lines),
        mode,
        len(buffer),
    )
    await update.message.reply_text(
        MESSAGES["buffered"].format(count=len(buffer)),
        reply_markup=save_keyboard(),
    )
    await _notify_other_users(
        context.application,
        update,
        f"buffered {len(lines)} new {mode} row(s) for replacement "
        f"({len(buffer)} pending). 📥",
    )


async def _post_init(app: Application) -> None:
    """Register slash commands so they also appear in Telegram's command menu."""
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Connect / start automation"),
            BotCommand("status", "Check running status"),
            BotCommand("stop", "Stop automation + BlackBird"),
            BotCommand("card", "Replace cards → /save"),
            BotCommand("proxy", "Replace proxies → /save"),
            BotCommand("save", "Save pasted card/proxy data"),
            BotCommand("cancel", "Leave card/proxy input without saving"),
            BotCommand("menu", "Refresh the 5-button keyboard"),
        ]
    )
    logger.info(
        "Bot ready ui=%s file=%s buttons=/status /start /card /proxy /stop",
        BOT_UI_VERSION,
        Path(__file__).resolve(),
    )


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print(MESSAGES["missing_config"])
        print("Edit .env in the project folder (see .env.example).")
        sys.exit(1)

    # Fail fast if this file is somehow an old copy without card/proxy buttons
    source = Path(__file__).read_text(encoding="utf-8")
    for required in ('KeyboardButton("/card")', 'KeyboardButton("/proxy")', 'KeyboardButton("/stop")'):
        if required not in source:
            print(f"ERROR: telegram_bot.py is outdated — missing {required}")
            print(f"File: {Path(__file__).resolve()}")
            sys.exit(1)

    _load_notify_chats()
    logger.info(
        "Starting Telegram bot ui=%s project=%s notify_chats=%s",
        BOT_UI_VERSION,
        PROJECT_DIR,
        list(_notify_chat_ids),
    )
    print(f"[bot] UI {BOT_UI_VERSION}")
    print("[bot] Keyboard: /status /start /card /proxy /stop")
    print("[bot] /card and /proxy send stored COUNT as the first message")
    print(f"[bot] File: {Path(__file__).resolve()}")

    app = (
        Application.builder()
        .token(token)
        .post_init(_post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("card", cmd_card))
    app.add_handler(CommandHandler("proxy", cmd_proxy))
    app.add_handler(CommandHandler("save", cmd_save))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_message)
    )

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
