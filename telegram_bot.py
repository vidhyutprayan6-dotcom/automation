#!/usr/bin/env python3
"""
Telegram remote controller for the existing BlackBird automation (main.py).

First Telegram "Start" (/start)  → connect + show button menu only
Keyboard /start                  → choose /HTTP or /SOCKS5 (or /Cancel)
 /HTTP                           → start (original New Proxy → input)
 /SOCKS5                         → start (New Proxy → SOCKS5 → input)
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
from screen_recorder import get_recording_service

# Shown in welcome so you can verify the Mac is running THIS file
BOT_UI_VERSION = "v2026-08-25-proxy-type"

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
PROXY_CHOICE_KEY = "awaiting_proxy_type"  # True while /HTTP|/SOCKS5|/Cancel shown

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
        f"📋 МЕНЮ  BlackBird Automation ({BOT_UI_VERSION})\n\n"
        "Подключено. Пять кнопок ниже:\n"
        "• /status — текущий статус задачи\n"
        "• /start — запустить автоматизацию\n"
        "• /stop — остановить автоматизацию + BlackBird\n"
        "• /card — заменить карты (показывает сохранённое количество)\n"
        "• /proxy — заменить прокси (показывает сохранённое количество)"
    ),
    "choose_proxy_type": (
        "▶️ ВЫБОР ТИПА ПРОКСИ\n\n"
        "Выберите режим перед запуском:\n"
        "• /HTTP — обычный путь (New Proxy → поле ввода)\n"
        "• /SOCKS5 — New Proxy → SOCKS5 → поле ввода\n"
        "• /Cancel — вернуться в главное меню без запуска"
    ),
    "start_cancelled": (
        "↩️ ОТМЕНА ЗАПУСКА: возврат в меню.\n"
        "Автоматизация не запущена."
    ),
    "started": (
        "▶️ СТАРТ: задача СЕЙЧАС ВЫПОЛНЯЕТСЯ.\n"
        "Другие подключённые пользователи уведомлены.\n"
        "Вы получите сообщение 🎉 о завершении, когда она закончится сама."
    ),
    "already_running": (
        "⚠️ СТАРТ ЗАБЛОКИРОВАН: задача уже ВЫПОЛНЯЕТСЯ.\n"
        "Вторая задача не запущена.\n"
        "Используйте /status или /stop."
    ),
    "stopped": (
        "🛑 СТОП: вы завершили задачу.\n"
        "Процесс автоматизации остановлен.\n"
        "Приложение BlackBird закрыто.\n"
        "Другие подключённые пользователи уведомлены."
    ),
    "not_running": (
        "💤 СТОП: ничего не выполнялось.\n"
        "Автоматизация неактивна. BlackBird уже выключен."
    ),
    "running": (
        "🟢 СТАТУС: ВЫПОЛНЯЕТСЯ\n"
        "Автоматизация сейчас в процессе."
    ),
    "idle": (
        "⚪ СТАТУС: ОЖИДАНИЕ\n"
        "Ни одна задача не выполняется.\n"
        "Нажмите /start, чтобы начать."
    ),
    "finished_status": (
        "🏁 СТАТУС: ПОСЛЕДНЯЯ ЗАДАЧА ЗАВЕРШЕНА\n"
        "Предыдущая задача завершилась сама.\n"
        "Сейчас ни одна задача не выполняется."
    ),
    "failed_status": (
        "🔴 СТАТУС: ПОСЛЕДНЯЯ ЗАДАЧА ЗАВЕРШИЛАСЬ С ОШИБКОЙ\n"
        "Предыдущая задача неожиданно остановилась.\n"
        "Сейчас ни одна задача не выполняется."
    ),
    "job_failed": (
        "💥 НЕОЖИДАННАЯ ОШИБКА\n"
        "Задача остановилась до обработки всех прокси.\n"
        "Код выхода: {code}\n"
        "Проверьте logs/automation.log на VPS."
    ),
    "start_failed": "❌ НЕ УДАЛОСЬ ЗАПУСТИТЬ.\nПроверьте логи на VPS.",
    "stop_failed": "❌ НЕ УДАЛОСЬ ОСТАНОВИТЬ.\nПроверьте логи на VPS.",
    "missing_config": "❌ Бот не настроен. Задайте TELEGRAM_BOT_TOKEN в .env",
    "card_count": "📊 СЕЙЧАС СОХРАНЕНО КАРТ: {count} строк(и) в card.txt",
    "proxy_count": "📊 СЕЙЧАС СОХРАНЕНО ПРОКСИ: {count} строк(и) в data.txt",
    "card_prompt": (
        "💳 РЕЖИМ ВВОДА КАРТ\n\n"
        "Отправьте строки с картами (по одной в строке):\n"
        "number|MM|YY|CVC|Name\n\n"
        "Пример:\n"
        "4426454034937026|03|28|978|Andrew Grant\n\n"
        "Вновь сохранённые строки ЗАМЕНЯЮТ все ранее сохранённые карты.\n"
        "Дубликаты среди новых строк удаляются.\n"
        "Нажмите /save, чтобы сохранить, или /cancel, чтобы вернуться."
    ),
    "proxy_prompt": (
        "🌐 РЕЖИМ ВВОДА ПРОКСИ\n\n"
        "Отправьте строки с прокси (по одной в строке).\n\n"
        "Принимаемый формат вставки:\n"
        "IP:Port:Username:Password\n"
        "Пример:\n"
        "69.10.54.69:9648:rps56862:rps56862\n\n"
        "При /save автоматически преобразуется и сохраняется как:\n"
        "Username:Password:IP:Port\n"
        "Пример:\n"
        "rps56862:rps56862:69.10.54.69:9648\n\n"
        "Также можно вставить сохранённый формат напрямую.\n"
        "Вновь сохранённые строки ЗАМЕНЯЮТ все ранее сохранённые прокси.\n"
        "Дубликаты среди новых строк удаляются.\n"
        "Нажмите /save, чтобы сохранить, или /cancel, чтобы вернуться."
    ),
    "buffered": (
        "📥 БУФЕР: {count} новых строк(и) готово.\n"
        "Нажмите /save, чтобы сохранить, или /cancel, чтобы отменить."
    ),
    "nothing_to_save": (
        "⚠️ СОХРАНЕНИЕ: пока ничего не получено.\n"
        "Сначала отправьте данные, затем /save — или /cancel, чтобы вернуться."
    ),
    "save_cancelled": "ℹ️ /save проигнорировано — вы не в режиме /card или /proxy.",
    "input_cancelled": "↩️ ОТМЕНА: возврат в меню. Ничего не сохранено.",
    "card_saved": (
        "💾 ЗАМЕНА КАРТ ВЫПОЛНЕНА\n"
        "• Удалено прежних записей: {previous}\n"
        "• Сейчас сохранено всего: {total}"
    ),
    "proxy_saved": (
        "💾 ЗАМЕНА ПРОКСИ ВЫПОЛНЕНА\n"
        "• Удалено прежних записей: {previous}\n"
        "• Сейчас сохранено всего: {total}\n"
        "• Автопреобразовано IP:Port:User:Pass → User:Pass:IP:Port: {converted}\n"
        "• Формат хранения: Username:Password:IP:Port"
    ),
    "save_failed": "❌ Не удалось сохранить. Проверьте логи на VPS.",
    "card_invalid": (
        "⚠️ Не найдено корректных строк с картами.\n"
        "Формат: number|MM|YY|CVC|Name"
    ),
    "proxy_invalid": (
        "⚠️ Не найдено корректных строк с прокси.\n"
        "Вставка: IP:Port:Username:Password\n"
        "Или хранение: Username:Password:IP:Port"
    ),
}

manager = ProcessManager()
_process_lock = asyncio.Lock()
_notify_chat_ids: set[int] = set()
_watch_task: asyncio.Task | None = None
NOTIFY_CHATS_FILE = LOG_DIR / "notify_chats.txt"
RESULTS_FILE = LOG_DIR / "last_run.json"
recording_service = get_recording_service(PROJECT_DIR)


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
        return "Другой пользователь"
    if user.username:
        return f"@{user.username}"
    if user.full_name:
        return user.full_name
    return f"Пользователь {user.id}"


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
    text = f"👥 ОБЩАЯ АКТИВНОСТЬ\n{_actor_label(update)} {action_text}"
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
        "✅ CORRECT — ЗАДАЧА ЗАВЕРШЕНА\n\n"
        f"🌐 Активные прокси (браузер открылся): {active} / {total} всего\n"
        f"💳 Карта подключена (Stripe Pay): {paid_of_active} активных\n"
        f"⏭ Неактивные прокси (пропущены): {inactive}\n"
        f"⚠️ Ошибка карты/Stripe: {stripe_failed}\n"
        f"🔧 Ошибка настройки: {setup_failed}\n\n"
        "🪟 Каждый прокси-браузер закрыт перед следующим прокси.\n"
        "👀 BlackBird остаётся запущенным и активным для проверки.\n"
        "✅ Закрыт только процесс автоматизации."
    )


def _count_file_rows(path: Path, kind: str) -> int:
    lines = _read_existing_data_lines(path)
    if kind == "card":
        return len(_filter_card_lines(lines))
    return len(_filter_proxy_lines(lines))


def _status_message() -> str:
    if manager.is_running():
        pid = manager.current_pid()
        extra = f"\nPID процесса: {pid}" if pid else ""
        return MESSAGES["running"] + extra
    code = manager.last_exit_code
    if manager.last_stop_was_user:
        return MESSAGES["idle"] + "\nПоследнее действие: остановлено через /stop."
    results = _read_run_results()
    if results and results.get("outcome") == "completed":
        return MESSAGES["finished_status"] + "\n\n" + _format_run_summary(results)
    if results and results.get("outcome") == "error":
        err = results.get("error") or f"код выхода {code}"
        return MESSAGES["failed_status"] + f"\n{err}"
    if code == 0:
        return MESSAGES["finished_status"]
    if code is not None and code != 0:
        return MESSAGES["failed_status"] + f"\nКод выхода: {code}"
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
            # Automation process is gone — end recording session (upload remainder)
            # while the always-on recorder service itself keeps running.
            await asyncio.to_thread(recording_service.end_session)
            results = _read_run_results()
            if results and results.get("outcome") == "completed":
                text = _format_run_summary(results)
            elif results and results.get("outcome") == "error":
                err = results.get("error") or f"exit {code}"
                text = MESSAGES["job_failed"].format(code=code if code is not None else "?")
                text += f"\n{err}"
            elif code in (0, None):
                text = (
                    "✅ CORRECT — ЗАДАЧА ЗАВЕРШЕНА\n"
                    "Пакет завершён. Каждый прокси-браузер закрыт перед следующим "
                    "прокси; BlackBird остаётся запущенным для проверки.\n"
                    "Откройте /status для подробностей, если доступны."
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
        input_field_placeholder="Команды или вставка после /card или /proxy",
    )



def proxy_type_keyboard() -> ReplyKeyboardMarkup:
    """Shown after keyboard /start — choose HTTP, SOCKS5, or Cancel."""
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("/HTTP"), KeyboardButton("/SOCKS5")],
            [KeyboardButton("/Cancel")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        one_time_keyboard=False,
        input_field_placeholder="Выберите /HTTP, /SOCKS5 или /Cancel",
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
            "⏳ Загрузка меню…",
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


async def _start_automation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    proxy_type: str = "http",
) -> None:
    _remember_chat(update)
    if not update.message:
        return
    context.user_data[PROXY_CHOICE_KEY] = False
    if manager.is_running():
        await update.message.reply_text(
            MESSAGES["already_running"], reply_markup=main_keyboard()
        )
        return
    # Start recording for this run before launching automation so the full
    # session is captured. If start fails, the session is closed again.
    await asyncio.to_thread(recording_service.begin_session)
    async with _process_lock:
        ok, key = await asyncio.to_thread(manager.start, proxy_type)
    if ok:
        _start_job_watcher(context.application)
        mode_label = "SOCKS5" if proxy_type == "socks5" else "HTTP"
        await update.message.reply_text(
            MESSAGES["started"] + f"\nРежим прокси: {mode_label}",
            reply_markup=main_keyboard(),
        )
        await _notify_other_users(
            context.application,
            update,
            f"запустил(а) автоматизацию ({mode_label}). ▶️",
        )
    else:
        await asyncio.to_thread(recording_service.end_session)
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
        context.user_data[PROXY_CHOICE_KEY] = False
        logger.info(
            "/start (first connect) user_id=%s ui=%s — send 5-button keyboard",
            uid,
            BOT_UI_VERSION,
        )
        await show_main_menu(update, MESSAGES["welcome"])
        await _notify_other_users(
            context.application,
            update,
            "подключился(ась) к боту. 👋",
        )
        return

    if manager.is_running():
        context.user_data[PROXY_CHOICE_KEY] = False
        if update.message:
            await update.message.reply_text(
                MESSAGES["already_running"], reply_markup=main_keyboard()
            )
        return

    context.user_data[PROXY_CHOICE_KEY] = True
    logger.info("/start (proxy-type choice) from user_id=%s", uid)
    if update.message:
        await update.message.reply_text(
            MESSAGES["choose_proxy_type"],
            reply_markup=proxy_type_keyboard(),
        )


async def cmd_http(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start automation with original HTTP path (New Proxy → input)."""
    uid = update.effective_user.id if update.effective_user else "?"
    _remember_chat(update)
    if context.user_data.get(EDIT_MODE_KEY):
        _clear_edit_state(context)
    logger.info("/HTTP from user_id=%s", uid)
    await _start_automation(update, context, proxy_type="http")


async def cmd_socks5(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start automation with SOCKS5 click between New Proxy and input."""
    uid = update.effective_user.id if update.effective_user else "?"
    _remember_chat(update)
    if context.user_data.get(EDIT_MODE_KEY):
        _clear_edit_state(context)
    logger.info("/SOCKS5 from user_id=%s", uid)
    await _start_automation(update, context, proxy_type="socks5")



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
    # Always end the recording session on /stop so the remainder is uploaded,
    # while the always-on recorder service itself keeps running with the bot.
    await asyncio.to_thread(recording_service.end_session)
    if update.message:
        if ok:
            await update.message.reply_text(MESSAGES["stopped"], reply_markup=main_keyboard())
            await _notify_other_users(
                context.application,
                update,
                "остановил(а) автоматизацию и закрыл(а) BlackBird. 🛑",
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
            "проверил(а) статус автоматизации. 🔎",
        )


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force-refresh the 5-button keyboard (does not start automation)."""
    uid = update.effective_user.id if update.effective_user else "?"
    _remember_chat(update)
    logger.info("/menu from user_id=%s ui=%s", uid, BOT_UI_VERSION)
    context.user_data[USER_CONNECTED_KEY] = True
    context.user_data[PROXY_CHOICE_KEY] = False
    _clear_edit_state(context)
    await show_main_menu(update, MESSAGES["welcome"])
    await _notify_other_users(
        context.application,
        update,
        "открыл(а) главное меню. 📋",
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /Cancel or /cancel:
      - During proxy-type choice after /start → back to main menu (no start)
      - During /card or /proxy input → leave without saving
    """
    uid = update.effective_user.id if update.effective_user else "?"
    _remember_chat(update)
    awaiting_type = bool(context.user_data.get(PROXY_CHOICE_KEY))
    mode = context.user_data.get(EDIT_MODE_KEY)
    logger.info(
        "/cancel from user_id=%s awaiting_proxy_type=%s mode=%s",
        uid,
        awaiting_type,
        mode,
    )
    context.user_data[USER_CONNECTED_KEY] = True
    context.user_data[PROXY_CHOICE_KEY] = False
    _clear_edit_state(context)
    if awaiting_type and not mode:
        await show_main_menu(update, MESSAGES["start_cancelled"])
        await _notify_other_users(
            context.application,
            update,
            "отменил(а) выбор типа прокси. ↩️",
        )
        return
    await show_main_menu(update, MESSAGES["input_cancelled"])
    await _notify_other_users(
        context.application,
        update,
        f"отменил(а) режим ({mode or 'ввод'}) без сохранения. ↩️",
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
            "открыл(а) режим замены карт. 💳",
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
            "открыл(а) режим замены прокси. 🌐",
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
                f"заменил(а) сохранённые записи карт ({previous} → {len(incoming)}). 💳",
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
                    f"заменил(а) сохранённые записи прокси ({previous} → {len(incoming)}; "
                    f"{converted} автопреобразовано в Username:Password:IP:Port). 🌐"
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
            "⚠️ Пустое сообщение. Отправьте строки данных, затем нажмите /save.",
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
        f"добавил(а) в буфер {len(lines)} новых строк(и) {mode} для замены "
        f"(ожидает {len(buffer)}). 📥",
    )


async def _post_init(app: Application) -> None:
    """Register slash commands so they also appear in Telegram's command menu."""
    # Telegram Bot API requires command names to be lowercase only
    # (a-z, 0-9, _). Keyboard labels may still show /HTTP and /SOCKS5.
    try:
        await app.bot.set_my_commands(
            [
                BotCommand("start", "Подключиться / выбрать HTTP или SOCKS5"),
                BotCommand("http", "Запуск: обычный путь (New Proxy -> ввод)"),
                BotCommand("socks5", "Запуск: New Proxy -> SOCKS5 -> ввод"),
                BotCommand("status", "Проверить статус выполнения"),
                BotCommand("stop", "Остановить автоматизацию + BlackBird"),
                BotCommand("card", "Заменить карты -> /save"),
                BotCommand("proxy", "Заменить прокси -> /save"),
                BotCommand("save", "Сохранить вставленные данные карт/прокси"),
                BotCommand("cancel", "Отмена выбора / ввода карт/прокси"),
                BotCommand("menu", "Обновить меню из 5 кнопок"),
            ]
        )
    except Exception as exc:  # noqa: BLE001
        # Never block bot startup on command-menu registration.
        logger.warning("set_my_commands failed (bot will still run): %s", exc)
    logger.info(
        "Bot ready ui=%s file=%s buttons=/status /start /card /proxy /stop",
        BOT_UI_VERSION,
        Path(__file__).resolve(),
    )
    # Always-on recorder: lives with the bot, idle until automation starts.
    started = await asyncio.to_thread(recording_service.start_service)
    logger.info("Recording service: %s", "on" if started else "off")


async def _post_shutdown(app: Application) -> None:
    """Stop the recorder service only when the Telegram bot itself stops."""
    await asyncio.to_thread(recording_service.stop_service)
    logger.info("Bot shutdown complete — recording service stopped")


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
    print("[bot] After /start: choose /HTTP, /SOCKS5, or /Cancel")
    print("[bot] /card and /proxy send stored COUNT as the first message")
    print("[bot] Screen recorder service starts with this bot and stays up until the bot stops")
    print(f"[bot] File: {Path(__file__).resolve()}")

    app = (
        Application.builder()
        .token(token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    # Command names are matched case-insensitively; register lowercase so
    # /HTTP, /http, /SOCKS5, /socks5 all work with the reply keyboard.
    app.add_handler(CommandHandler("http", cmd_http))
    app.add_handler(CommandHandler("socks5", cmd_socks5))
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
