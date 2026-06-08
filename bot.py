import asyncio
import logging
import os
import time


try:
    import tomllib

    def load_toml_config(path: str = "config.toml") -> dict:
        try:
            with open(path, "rb") as f:
                return tomllib.load(f)
        except FileNotFoundError:
            return {}
        except Exception as e:
            print(f"Не удалось загрузить config.toml: {e}")
            return {}

except ImportError:
    def load_toml_config(path: str = "config.toml") -> dict:
        return {}

config = load_toml_config()
last_brand_id = {}  
import json
import secrets
from typing import Dict, Set, Tuple
import random
import string
from datetime import datetime, timedelta
from pathlib import Path
import sys

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    Update,
    InputMediaPhoto,
)
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Application,
    MessageHandler,
    filters,
)
from telegram.error import Conflict, BadRequest
# Prefer the local patched copy of vinted-api-wrapper (uses certifi CA bundle)
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR / "vinted-api-wrapper"))

from vinted import Vinted
import requests

# --- Настройки ---
POLL_SECONDS = 10  # период опроса API для real-time
# Уведомления только если на странице объявления «Last seen» не старше N минут (нужен Selenium; иначе фильтр не применяется)
MAX_LAST_SEEN_MINUTES = 1
# Авто-ротация vinted cookies (секунды)
VINTED_COOKIE_TTL_SECONDS = int(config.get("vinted_cookie_ttl_seconds") or 1800)

# Прокси для Vinted (если нужен, например, чтобы обойти блокировку дата‑центров)
# Пример: export VINTED_PROXY="http://user:pass@host:port"
# Или через код (не рекомендуется):
# прокси обрабатывается отдельным модулем (vinted/proxies)
from proxies.proxy_factory import build_proxy

"""Читайте прокси и токен исключительно из TOML.
        ключи конфигурации:
            proxy_string, proxy_change_url, telegram_token/tg_token
"""
proxy_str = config.get("proxy_string")
proxy_change = config.get("proxy_change_url")
VINTED_PROXY_HANDLER = build_proxy(proxy_str, proxy_change)
VINTED_PROXY = VINTED_PROXY_HANDLER.get_proxy_string()

# Токен бота берётся только из TOML. Ключи: telegram_token или tg_token
TOKEN = (
    config.get("telegram_token")
    or config.get("tg_token")
    or config.get("tel")  # старый ключ, иногда в файле использовали tel
)
if not TOKEN:
    raise SystemExit(
        "telegram_token не найден в config.toml. Добавьте его туда."
    )

LICENSE_DB = "licenses.json"
LICENSE_DURATION_DAYS = 30
COOKIE_AUTO_UNLOCK_HOURS = int(config.get("cookie_auto_unlock_hours") or 12)

# Default admin IDs (fallback). Real list is loaded from `admins.json` if present.
# Empty by default to avoid implicitly granting access — create `admins.json`
# with a list of admin IDs to allow admin-only actions.
ADMIN_IDS = []

# Популярные бренды (ID с Vinted; список можно дополнять)
BRANDS = [
    "Stone Island",
    "C.P. Company",
    "Mastermind",
    "Raf Simons",
    "Nike",
    "Adidas",
    "Jeremy Scott",
    "Vetements",
    "Balenciaga",
    "New rock",
    "Гоша Рубчинский",
    "Рассвет",
    "Alpha Industries",
    "Number Nine",
    "Hysteric Glamour",
    "Gucci",
    "Louis Vuitton",
    "Project G/R",
    "Dolce & Gabanna",
]

# Известные brand_id (если нет, будет поиск по тексту)
BRAND_IDS = {
    "Stone Island": 73306,
    "C.P. Company": 73952,
    "Mastermind": 1293678,  # Mastermind Japan
    "Raf Simons": 184436,
    "Nike": 53,
    "Adidas": 14,
    "Jeremy Scott": 37535,
    "Vetements": 270420,
    "Balenciaga": 2369,
    "New rock": 432,
    "Гоша Рубчинский": 219304,
    "Рассвет": 330407,  # Paccbet
    "Alpha Industries": 60712,
    "Number Nine": 505614,
    "Hysteric Glamour": 315985,
    "Gucci": 465,
    "Louis Vuitton": 274,
    "Project G/R": 26057,  # общий бренд Project
    "Dolce & Gabanna": 1043,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
log = logging.getLogger("vinted-bot")

# --- Состояние в памяти ---
user_brands: Dict[int, Set[int]] = {}  # chat_id -> {brand_id,...}
# Черновой выбор брендов в меню. Коммит в user_brands только по кнопке "Готово".
draft_brands: Dict[int, Set[int]] = {}  # chat_id -> {brand_idx,...}
# Чаты, где парсинг явно запущен кнопкой в меню.
parsing_enabled_chats: Set[int] = set()
seen: Set[Tuple[int, int]] = set()  # (chat_id, item_id)
last_seen_id: Dict[Tuple[int, int], int] = {}  # (chat_id, brand_idx) -> last item.id
paused_chats: Set[int] = set()  # chat_id -> пауза
last_check: Dict[int, datetime] = {}  # chat_id -> время последней проверки

# Импортируем Selenium вариант
from vinted_selenium import VintedSelenium

# Инициализируем Selenium для обхода Cloudflare
selenium_vinted = None
try:
    selenium_vinted = VintedSelenium(headless=True)
    log.info("Selenium Vinted client initialized (headless mode)")
except Exception as se:
    log.error("Failed to initialize Selenium: %s, will try API fallback", se)

# Класс клиента Vinted; домен фиксируем на .com для глобального поиска
try:
    vinted = Vinted(domain="com", language="en-US", proxy=VINTED_PROXY)
    proxy_status = "enabled" if VINTED_PROXY else "disabled"
    log.info("Vinted API client initialized with proxy: %s", proxy_status)
except Exception as e:
    error_msg = str(e)
    log.error("Failed to initialize Vinted API client with proxy: %s", error_msg)

    # Подсказки по распространённым проблемам
    if "SOCKS" in error_msg:
        log.error("SOCKS proxy requires pysocks library. Install it: pip install pysocks")
        log.error("Or configure an HTTP proxy in config.toml instead of SOCKS5")
    elif VINTED_PROXY:
        log.error("VINTED_PROXY is set to: %s — check if proxy is valid and accessible", VINTED_PROXY)

    # Попробуем инициализировать клиента без прокси — чтобы бот мог запуститься
    try:
        log.info("Попытка инициализации Vinted клиента без прокси (fallback)...")
        vinted = Vinted(domain="de", language="de-DE", proxy=None)
        VINTED_PROXY = None
        log.info("Vinted client initialized without proxy (fallback)")
    except Exception as e2:
        error_msg2 = str(e2)
        log.error("Fallback без прокси также не удался: %s", error_msg2)
        log.error("Проверьте доступность сети/прокси на сервере и параметры в config.toml")
        raise SystemExit(f"Cannot initialize Vinted: {error_msg2}")

# Последнее успешное обновление cookies
vinted_cookies_last_refresh_ts = time.time()


def refresh_vinted_cookies(force: bool = False, reason: str = "") -> bool:
    """Авто-обновление cookies с TTL и логированием."""
    global vinted_cookies_last_refresh_ts
    now = time.time()
    age = now - vinted_cookies_last_refresh_ts
    if not force and age < VINTED_COOKIE_TTL_SECONDS:
        return True
    try:
        vinted.update_cookies()
        vinted_cookies_last_refresh_ts = time.time()
        log.info(
            "Vinted cookies refreshed%s (age=%ss)",
            f" [{reason}]" if reason else "",
            int(age),
        )
        return True
    except Exception as e:
        log.warning("Vinted cookies refresh failed%s: %s", f" [{reason}]" if reason else "", e)
        return False

# --- Лицензии / активации ---
def generate_codes(n=30, length=10):
    alphabet = string.ascii_uppercase + string.digits
    return ["".join(random.choices(alphabet, k=length)) for _ in range(n)]


def load_db():
    if os.path.exists(LICENSE_DB):
        with open(LICENSE_DB, "r") as f:
            data = json.load(f)
            data.setdefault("cookie_codes", {})
            for u in data.get("users", {}).values():
                if isinstance(u, dict):
                    u.setdefault("cookie_unlocked", False)
            return data
    codes = generate_codes()
    db = {
        "codes": {c: {"used_by": None, "expires": None} for c in codes},
        "cookie_codes": {},
        "users": {},
    }
    save_db(db)
    log.info("Сгенерированы коды активации: %s", ", ".join(codes))
    return db


def save_db(db):
    with open(LICENSE_DB, "w") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


db = load_db()


# Load admin IDs from admins.json if present
def load_admin_ids():
    try:
        with open("admins.json", "r", encoding="utf-8") as f:
            data = json.load(f)
            return [int(x) for x in data]
    except Exception:
        return ADMIN_IDS


ADMIN_IDS = load_admin_ids()


def generate_code(length: int = 15) -> str:
    """Генерирует случайный код активации."""
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def brand_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    """Строим клавиатуру по двум кнопкам в строке + кнопка Готово."""
    selected = draft_brands.get(chat_id, user_brands.get(chat_id, set()))
    buttons = []
    for idx, name in enumerate(BRANDS):
        prefix = "✅ " if idx in selected else ""
        buttons.append(
            InlineKeyboardButton(f"{prefix}{name}", callback_data=f"brand:{idx}")
        )
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("♻️ Сбросить", callback_data="clear")])
    rows.append([InlineKeyboardButton("✅ Готово", callback_data="done")])
    return InlineKeyboardMarkup(rows)


def selected_brands_text(chat_id: int) -> str:
    brands = sorted(user_brands.get(chat_id, set()))
    if not brands:
        return "не выбраны"
    return ", ".join(BRANDS[i] for i in brands)


def main_menu(chat_id: int) -> InlineKeyboardMarkup:
    """Главная инлайн-менюшка с основными действиями."""
    paused = chat_id in paused_chats
    running = chat_id in parsing_enabled_chats
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🎯 Выбрать бренды", callback_data="menu:brands"),
                InlineKeyboardButton("ℹ️ Статус", callback_data="menu:status"),
            ],
            [
                InlineKeyboardButton(
                    "⏸️ Пауза" if not paused else "▶️ Возобновить",
                    callback_data="menu:toggle_pause",
                ),
                InlineKeyboardButton("♻️ Сбросить бренды", callback_data="menu:clear"),
            ],
            [
                InlineKeyboardButton(
                    "🚀 Парсить" if not running else "⏹️ Остановить парсинг",
                    callback_data="menu:toggle_parse",
                )
            ],
        ]
    )


def nav_keyboard(chat_id: int) -> ReplyKeyboardMarkup:
    """Нижняя (reply) клавиатура в интерфейсе клиента Telegram."""
    paused = chat_id in paused_chats
    running = chat_id in parsing_enabled_chats
    rows = [
        [
            KeyboardButton("🎯 Бренды"),
            KeyboardButton("ℹ️ Статус"),
        ],
        [
            KeyboardButton("⏸️ Пауза" if not paused else "▶️ Возобновить"),
            KeyboardButton("♻️ Сброс брендов"),
        ],
        [
            KeyboardButton("🚀 Парсить" if not running else "⏹️ Стоп парсинга"),
        ],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_brands.setdefault(chat_id, set())
    # Не сбрасываем paused_chats — иначе «Пауза» отменяется при каждом /start
    if not is_active(chat_id):
        await update.message.reply_text(
            "Нужна активация. Пришли /activate КОД (код на 30 дней).",
            reply_markup=nav_keyboard(chat_id),
        )
        return
    # Сначала подключаем нижнюю reply-клавиатуру
    await update.message.reply_text(
        "Кнопки управления подключены.",
        reply_markup=nav_keyboard(chat_id),
    )
    await update.message.reply_text(
        f"Главное меню.\nВыбрано: {selected_brands_text(chat_id)}",
        reply_markup=main_menu(chat_id),
    )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await status_reply(chat_id, context)


async def pause_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_active(chat_id):
        await update.message.reply_text("Сначала активируй код: /activate КОД.")
        return
    paused_chats.add(chat_id)
    await update.message.reply_text(
        "Оповещения поставлены на паузу. /resume чтобы возобновить.",
        reply_markup=nav_keyboard(chat_id),
    )


async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_active(chat_id):
        await update.message.reply_text("Сначала активируй код: /activate КОД.")
        return
    paused_chats.discard(chat_id)
    last_check[chat_id] = datetime.now()
    await update.message.reply_text(
        "Возобновил оповещения. Проверяю новые объявления.",
        reply_markup=nav_keyboard(chat_id),
    )


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_active(chat_id):
        await update.message.reply_text("Нужна активация. /activate КОД.")
        return
    await update.message.reply_text(
        f"Главное меню.\nВыбрано: {selected_brands_text(chat_id)}",
        reply_markup=main_menu(chat_id),
    )


async def handle_reply_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатий по нижней Reply‑клавиатуре."""
    if not update.message:
        return
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()

    # Приводим к одинаковому виду, чтобы не зависеть от лишних пробелов
    if text == "🎯 Бренды":
        if not is_active(chat_id):
            await update.message.reply_text("Нужна активация. /activate КОД.")
            return
        # Открываем меню выбора брендов (та же логика, что menu:brands)
        draft_brands[chat_id] = set(user_brands.get(chat_id, set()))
        await update.message.reply_text(
            "Выберите бренды (можно несколько), затем нажмите «Готово».",
            reply_markup=brand_keyboard(chat_id),
        )
        return

    if text == "ℹ️ Статус":
        await status_reply(chat_id, context)
        return

    if text in ("⏸️ Пауза", "▶️ Возобновить"):
        if not is_active(chat_id):
            await update.message.reply_text("Нужна активация. /activate КОД.")
            return
        if chat_id in paused_chats:
            paused_chats.discard(chat_id)
            last_check[chat_id] = datetime.now()
            msg = "Возобновил оповещения."
        else:
            paused_chats.add(chat_id)
            msg = "Оповещения поставлены на паузу."
        await update.message.reply_text(
            f"{msg}\nВыбрано: {selected_brands_text(chat_id)}",
            reply_markup=nav_keyboard(chat_id),
        )
        return

    if text == "♻️ Сброс брендов":
        clear_brands(chat_id)
        await update.message.reply_text(
            "Бренды сброшены. Нажми «🎯 Бренды», чтобы выбрать снова.",
            reply_markup=nav_keyboard(chat_id),
        )
        return

    if text in ("🚀 Парсить", "⏹️ Стоп парсинга"):
        if not is_active(chat_id):
            await update.message.reply_text("Нужна активация. /activate КОД.")
            return
        if chat_id in parsing_enabled_chats:
            parsing_enabled_chats.discard(chat_id)
            msg = "Парсинг остановлен."
        else:
            if not has_cookie_access(chat_id):
                await update.message.reply_text(
                    f"Cookie-доступ активируется автоматически через {COOKIE_AUTO_UNLOCK_HOURS} ч после /activate.",
                    reply_markup=nav_keyboard(chat_id),
                )
                return
            brands = user_brands.get(chat_id, set())
            if not brands:
                await update.message.reply_text(
                    "Сначала выбери бренды (кнопка «🎯 Бренды»).",
                    reply_markup=nav_keyboard(chat_id),
                )
                return
            parsing_enabled_chats.add(chat_id)
            paused_chats.discard(chat_id)
            last_check[chat_id] = datetime.now()
            msg = "Парсинг запущен."
        await update.message.reply_text(
            f"{msg}\nВыбрано: {selected_brands_text(chat_id)}",
            reply_markup=nav_keyboard(chat_id),
        )
        return



async def safe_edit_callback_message(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
    *,
    reply_markup=None,
    parse_mode=None,
) -> None:
    """Редактирует сообщение с callback; при ошибке Telegram (редкий тип сообщения и т.д.) шлёт новое."""
    if query.message:
        try:
            await query.edit_message_text(
                text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
            return
        except BadRequest as e:
            err = str(e).lower()
            if "not modified" in err:
                return
            log.warning("edit_message_text failed: %s", e)
    try:
        await context.bot.send_message(
            chat_id,
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
    except Exception as e:
        log.exception("send_message fallback failed: %s", e)


async def status_reply(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    brands = user_brands.get(chat_id, set())
    brands_count = len(brands)
    names = ", ".join(BRANDS[i] for i in sorted(brands)) if brands else "не выбраны"
    paused = "⏸️ пауза" if chat_id in paused_chats else "▶️ работает"
    
    # Проверяем срок лицензии
    exp_raw = db["users"].get(str(chat_id), {}).get("expires")
    is_active_now = False
    exp_str = "нет"
    days_left = 0
    
    if exp_raw:
        try:
            exp_dt = datetime.fromisoformat(exp_raw)
            exp_str = exp_dt.strftime("%d.%m.%Y %H:%M:%S")
            now = datetime.now()
            is_active_now = exp_dt > now
            if is_active_now:
                delta = exp_dt - now
                days_left = delta.days
                hours_left = (delta.seconds // 3600) % 24
                status_lic = f"✅ активна ({days_left}д {hours_left}ч)"
            else:
                status_lic = "❌ истекла"
        except Exception:
            status_lic = "⚠️ ошибка в БД"
    else:
        status_lic = "❌ не активирована"
    
    # Получаем время последней проверки
    ts = last_check.get(chat_id)
    if ts:
        ts_str = ts.strftime("%d.%m.%Y %H:%M:%S")
        time_ago = datetime.now() - ts
        mins_ago = int(time_ago.total_seconds() / 60)
        if mins_ago < 1:
            time_status = f"только что"
        elif mins_ago < 60:
            time_status = f"{mins_ago} мин назад"
        else:
            hours_ago = mins_ago // 60
            time_status = f"{hours_ago}ч назад"
    else:
        ts_str = "ещё не проверяли"
        time_status = "ещё не проверяли"
    
    # Количество отслеживаемых объявлений
    tracked_items = len(seen)
    
    # Формируем подробный статус
    status_text = (
        f"<b>📊 Полный статус бота</b>\n\n"
        f"<b>Состояние:</b> {paused}\n"
        f"<b>Лицензия:</b> {status_lic}\n"
        f"<b>До истечения:</b> {exp_str}\n\n"
        f"<b>Отслеживание:</b>\n"
        f"├ Бренды: {brands_count} {'('+names+')' if names != 'не выбраны' else names}\n"
        f"└ Объявлений в памяти: {tracked_items}\n\n"
        f"<b>Проверки:</b>\n"
        f"├ Последняя: {time_status}\n"
        f"└ Время: {ts_str}"
    )
    
    await context.bot.send_message(
        chat_id,
        status_text,
        reply_markup=main_menu(chat_id),
        parse_mode="HTML"
    )


async def on_brand_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id if query.message else update.effective_chat.id
    log.info("callback: chat=%s data=%s", chat_id, query.data)
    if not is_active(chat_id):
        await safe_edit_callback_message(
            query,
            context,
            chat_id,
            "Срок доступа истёк или не активирован. Пришли /activate КОД.",
        )
        return

    # Обработка главного меню
    if query.data.startswith("menu:"):
        action = query.data.split(":", 1)[1]
        if action == "brands":
            # Открываем режим выбора: редактируем черновик и не трогаем active-мониторинг
            draft_brands[chat_id] = set(user_brands.get(chat_id, set()))
            await safe_edit_callback_message(
                query,
                context,
                chat_id,
                "Выберите бренды (можно несколько), затем нажмите «Готово».",
                reply_markup=brand_keyboard(chat_id),
            )
        elif action == "status":
            await status_reply(chat_id, context)
        elif action == "toggle_pause":
            if chat_id in paused_chats:
                paused_chats.discard(chat_id)
                last_check[chat_id] = datetime.now()
                msg = "Возобновил оповещения."
            else:
                paused_chats.add(chat_id)
                msg = "Оповещения на паузе."
            await safe_edit_callback_message(
                query,
                context,
                chat_id,
                msg,
                reply_markup=main_menu(chat_id),
            )
        elif action == "toggle_parse":
            if chat_id in parsing_enabled_chats:
                parsing_enabled_chats.discard(chat_id)
                msg = "Парсинг остановлен."
            else:
                if not has_cookie_access(chat_id):
                    await safe_edit_callback_message(
                        query,
                        context,
                        chat_id,
                        f"Cookie-доступ активируется автоматически через {COOKIE_AUTO_UNLOCK_HOURS} ч после /activate.",
                        reply_markup=main_menu(chat_id),
                    )
                    return
                brands = user_brands.get(chat_id, set())
                if not brands:
                    msg = "Сначала выберите бренды."
                else:
                    parsing_enabled_chats.add(chat_id)
                    paused_chats.discard(chat_id)
                    last_check[chat_id] = datetime.now()
                    msg = "Парсинг запущен."
            await safe_edit_callback_message(
                query,
                context,
                chat_id,
                f"{msg}\n\nВыбрано: {selected_brands_text(chat_id)}",
                reply_markup=main_menu(chat_id),
            )
        elif action == "clear":
            clear_brands(chat_id)
            await safe_edit_callback_message(
                query,
                context,
                chat_id,
                "Бренды сброшены. Нажмите «Выбрать бренды».",
                reply_markup=main_menu(chat_id),
            )
        return

    if query.data == "clear":
        # Сброс в экране выбора только черновика (без перезапуска/остановки мониторинга до "Готово")
        draft_brands[chat_id] = set()
        await safe_edit_callback_message(
            query,
            context,
            chat_id,
            "Бренды сброшены. Выберите снова.",
            reply_markup=brand_keyboard(chat_id),
        )
        return

    if query.data == "done":
        brands = set(draft_brands.get(chat_id, user_brands.get(chat_id, set())))
        if not brands:
            await safe_edit_callback_message(
                query,
                context,
                chat_id,
                "Бренды не выбраны. Нажмите /start.",
            )
            return
        # Коммит выбранных брендов только после подтверждения.
        user_brands[chat_id] = brands
        draft_brands.pop(chat_id, None)
        # BRANDS — список, поэтому индексы из picked
        names = [BRANDS[i] for i in sorted(brands)]
        await safe_edit_callback_message(
            query,
            context,
            chat_id,
            "Выбрано: " + ", ".join(names) + "\nНажмите «🚀 Парсить» в главном меню.",
            reply_markup=main_menu(chat_id),
        )
        return

    _, bid_str = query.data.split(":")
    bid = int(bid_str)
    picked = draft_brands.setdefault(chat_id, set(user_brands.get(chat_id, set())))
    if bid in picked:
        picked.remove(bid)
        log.debug("brand %s removed for chat %s", bid, chat_id)
    else:
        picked.add(bid)
        log.debug("brand %s added for chat %s", bid, chat_id)
    # Обновляем клавиатуру (ошибка "not modified" допустима)
    try:
        await query.edit_message_reply_markup(reply_markup=brand_keyboard(chat_id))
    except BadRequest as e:
        if "not modified" not in str(e):
            log.error("edit_message_reply_markup error: %s", e)


def format_price(item):
    price = item.price
    if price is None:
        return "—"
    # item.price может быть Price или строкой
    if hasattr(price, "amount"):
        currency = getattr(price, "currency_code", "") or ""
        return f"{price.amount} {currency}".strip()
    return str(price)


async def poll_loop(app):
    """Фоновый опрос Vinted и рассылка новых объявлений."""
    while True:
        try:
            for chat_id, brands in list(user_brands.items()):
                if chat_id not in parsing_enabled_chats:
                    continue
                if chat_id in paused_chats:
                    continue
                for bid in brands:
                    # Если пользователь нажал паузу/стоп во время текущего прохода — выходим сразу.
                    if chat_id in paused_chats or chat_id not in parsing_enabled_chats:
                        break
                    # Вытаскиваем блокирующий HTTP в отдельный поток, чтобы не морозить event loop
                    new_items, latest = await asyncio.to_thread(fetch_brand_items, bid, 96)
                    if chat_id in paused_chats or chat_id not in parsing_enabled_chats:
                        break
                    # Диагностическое логирование: длина и первые id (если есть)
                    try:
                        ids_sample = [getattr(it, "id", None) for it in (new_items or [])][:5]
                        log.debug("Fetched new items for chat=%s brand=%s count=%s sample_ids=%s", chat_id, bid, len(new_items) if new_items else 0, ids_sample)
                    except Exception:
                        log.debug("Fetched new items for chat=%s brand=%s (could not list ids)", chat_id, bid)
                    if new_items:
                        await send_items_to_chat(app.bot, chat_id, new_items)
                    elif latest:
                        # Send latest item with note if no new
                        await send_items_to_chat(app.bot, chat_id, [latest], note="Нет новых товаров, вот последний:")
                    # Небольшой джиттер, чтобы не долбить API ровно по таймеру
                    await asyncio.sleep(random.uniform(0.3, 1.0))
                last_check[chat_id] = datetime.now()
            await asyncio.sleep(POLL_SECONDS)
        except Exception as e:
            log.exception("poll error: %s", e)
            await asyncio.sleep(5)


async def _post_init(app):
    # Запускаем фоновую задачу после инициализации бота
    asyncio.create_task(poll_loop(app))
    # Запускаем задачу, которая будет отслеживать изменения admins.json
    asyncio.create_task(_watch_admins_file())


async def _watch_admins_file(interval: int = 10):
    """Периодически проверяет mtime файла `admins.json` и перезагружает ADMIN_IDS при изменении."""
    path = os.path.join(os.path.dirname(__file__), "admins.json")
    last_mtime = None
    while True:
        try:
            if os.path.exists(path):
                mtime = os.path.getmtime(path)
                if last_mtime is None:
                    last_mtime = mtime
                elif mtime != last_mtime:
                    try:
                        new_admins = load_admin_ids()
                        global ADMIN_IDS
                        ADMIN_IDS = new_admins
                        log.info("admins.json changed — reloaded ADMIN_IDS: %s", ADMIN_IDS)
                    except Exception:
                        log.exception("Failed to reload admins.json")
                    last_mtime = mtime
        except Exception:
            log.exception("Error watching admins.json")
        await asyncio.sleep(interval)


async def send_items_to_chat(bot, chat_id: int, items, note=None):
    """Отправляет список товаров в чат, помечая их как seen.

    Фильтрует по «Last seen» на странице Vinted: шлём только если прошло не больше
    MAX_LAST_SEEN_MINUTES минут (см. настройки выше).
    """
    for item in items:
        # Жёсткая проверка перед каждой отправкой, чтобы пауза/стоп применялись мгновенно.
        if chat_id in paused_chats or chat_id not in parsing_enabled_chats:
            return
        key = (chat_id, item.id)
        if key in seen:
            continue
        
        # Проверяем Last seen перед отправкой
        try:
            last_seen_mins = await get_item_last_seen_minutes(item.url)
            if last_seen_mins >= 0 and last_seen_mins > MAX_LAST_SEEN_MINUTES:
                log.info(
                    "Item %s skipped: Last seen %d min ago (>%d min)",
                    item.id,
                    last_seen_mins,
                    MAX_LAST_SEEN_MINUTES,
                )
                continue
        except Exception as e:
            log.debug("Error checking last seen for item %s: %s", item.id, e)
        
        seen.add(key)
        text = (
            f"{note + '\n' if note else ''}"
            f"🆕 {item.title}\n"
            f"💶 {format_price(item)}\n"
            f"👤 {item.user.login}\n"
            f"{item.url}"
        )
        photo_url = getattr(getattr(item, "photo", None), "url", None)
        # Попробуем собрать все фото с страницы
        photos = await asyncio.to_thread(get_photo_urls, item.url)
        if chat_id in paused_chats or chat_id not in parsing_enabled_chats:
            return
        try:
            if photos:
                media = []
                for i, url in enumerate(photos[:10]):  # Telegram лимит 10 в альбоме
                    if i == 0:
                        media.append(InputMediaPhoto(media=url, caption=text))
                    else:
                        media.append(InputMediaPhoto(media=url))
                await bot.send_media_group(chat_id, media=media)
            elif photo_url:
                await bot.send_photo(chat_id, photo=photo_url, caption=text)
            else:
                await bot.send_message(chat_id, text)
        except Exception as e:
            log.error("send_items_to_chat error: %s", e)
            try:
                await bot.send_message(chat_id, text)
            except Exception:
                pass


async def send_initial_items(bot, chat_id: int, brands):
    """Шлём по 10 свежих объявления для каждой выбранной марки сразу после выбора."""
    for bid in brands:
        new_items, _ = await asyncio.to_thread(fetch_brand_items, bid, 10)
        items = new_items  # for compatibility
        # Диагностическое логирование для начальной отправки
        try:
            ids_sample = [getattr(it, "id", None) for it in (items or [])][:5]
            log.info("Initial fetch for chat=%s brand=%s -> count=%s sample_ids=%s", chat_id, bid, len(items) if items else 0, ids_sample)
        except Exception:
            log.info("Initial fetch for chat=%s brand=%s -> count=%s", chat_id, bid, len(items) if items else 0)

        if not items:
            brand_name = BRANDS[bid]
            await bot.send_message(
                chat_id, f"Пока нет свежих объявлений по {brand_name}."
            )
            continue
        await send_items_to_chat(bot, chat_id, items)
        # запоминаем последний id, чтобы дальше слать только новое
        global last_brand_id
        if items:
            last_brand_id[bid] = max(it.id for it in items)
        log.info("initial items sent for chat=%s brand=%s last_id=%s", chat_id, BRANDS[bid], last_brand_id.get(bid, 0))


def filter_new_items(chat_id: int, bid: int, items):
    """Отфильтровать только новые товары (id > последний виденный для бренда)."""
    try:
        items_list = list(items)
    except TypeError:
        log.warning("filter_new_items: got non-iterable items=%r", items)
        return []

    last_id = last_seen_id.get((chat_id, bid), 0)
    new_items = [it for it in items_list if getattr(it, "id", 0) > last_id]
    if new_items:
        last_seen_id[(chat_id, bid)] = max(last_id, max(it.id for it in new_items))
    return new_items


def clear_brands(chat_id: int):
    user_brands[chat_id] = set()
    draft_brands.pop(chat_id, None)
    parsing_enabled_chats.discard(chat_id)
    # Чистим last_seen_id для этого чата
    for key in list(last_seen_id.keys()):
        if key[0] == chat_id:
            last_seen_id.pop(key, None)


def is_active(chat_id: int) -> bool:
    # Admin IDs have unlimited access
    if chat_id in ADMIN_IDS:
        return True
    # Check user license expiration
    u = db["users"].get(str(chat_id))
    if not u or not u.get("expires"):
        return False
    try:
        exp = datetime.fromisoformat(u["expires"])
    except Exception:
        return False
    return datetime.now() <= exp


def has_cookie_access(chat_id: int) -> bool:
    if chat_id in ADMIN_IDS:
        return True
    user = db["users"].get(str(chat_id), {})
    if user.get("cookie_unlocked"):
        return True
    unlock_at_raw = user.get("cookie_unlock_at")
    if not unlock_at_raw:
        return False
    try:
        unlock_at = datetime.fromisoformat(unlock_at_raw)
    except Exception:
        return False
    if datetime.now() >= unlock_at:
        user["cookie_unlocked"] = True
        save_db(db)
        return True
    return False


def can_parse(chat_id: int) -> bool:
    return is_active(chat_id) and has_cookie_access(chat_id)


async def generate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для админов - генерирует новый код активации."""
    chat_id = update.effective_chat.id
    if chat_id not in ADMIN_IDS:
       # await update.message.reply_text("❌ Только админ может генерировать коды")
        return
    
    code = generate_code(15)
    # Проверяем что код не дублируется
    while code in db["codes"]:
        code = generate_code(15)
    
    db["codes"][code] = {"used_by": None, "expires": None}
    save_db(db)
    
    await update.message.reply_text(f"✅ Код создан:\n\n`{code}`", parse_mode="Markdown")


async def activate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Используй: /activate КОД")
        return
    code = context.args[0].strip()
    code_info = db["codes"].get(code)
    if not code_info:
        await update.message.reply_text("Код не найден.")
        return
    if code_info["used_by"] and code_info["used_by"] != chat_id:
        await update.message.reply_text("Код уже использован другим пользователем.")
        return
    now = datetime.now()
    # Берём ограничение по коду, если есть (для тестовых минутных кодов)
    code_exp_raw = code_info.get("expires")
    code_exp = None
    if code_exp_raw:
        try:
            code_exp = datetime.fromisoformat(code_exp_raw)
        except Exception:
            code_exp = None
    if code_exp and code_exp <= now:
        await update.message.reply_text("Срок действия этого кода уже истёк.")
        return

    # Итоговая дата — минимум между лимитом кода и стандартными 30 днями
    default_exp = now + timedelta(days=LICENSE_DURATION_DAYS)
    if code_exp:
        expires = min(code_exp, default_exp)
    else:
        expires = default_exp

    db["users"][str(chat_id)] = {
        "expires": expires.isoformat(),
        "cookie_unlocked": False,
        "cookie_unlock_at": (now + timedelta(hours=COOKIE_AUTO_UNLOCK_HOURS)).isoformat(),
    }
    db["codes"][code]["used_by"] = chat_id
    db["codes"][code]["expires"] = expires.isoformat()
    save_db(db)
    await update.message.reply_text(
        f"Код активирован до {expires.strftime('%Y-%m-%d %H:%M:%S')}.\n"
        f"Cookie-доступ разблокируется автоматически через {COOKIE_AUTO_UNLOCK_HOURS} ч.\n"
        "Команда /menu — главное меню."
    )


def is_item_recent(item, minutes: int = 10) -> bool:
    """
    Проверяет, был ли товар просмотрен в течение последних N минут.
    
    Поскольку "Last seen" информация находится только на фронтенде и не в API,
    мы не можем получить её без скрейпинга страницы товара.
    
    Эта функция может быть использована для других проверок возраста товара.
    Для полноценной проверки Last seen потребуется дополнительный скрейпинг.
    """
    try:
        # Проверка по timestamp фото (время загрузки фото)
        timestamp = None
        if hasattr(item, "photo") and hasattr(item.photo, "high_resolution"):
            timestamp = getattr(item.photo.high_resolution, "timestamp", None)
        
        if timestamp:
            created_at = datetime.fromtimestamp(timestamp)
            delta = datetime.now() - created_at
            is_recent = delta.total_seconds() < minutes * 60
            log.debug("Item %s: photo timestamp age=%.1f min, recent=%s", 
                     getattr(item, 'id', '?'), delta.total_seconds()/60, is_recent)
            return is_recent
        # Если нет timestamp, принимаем товар (не фильтруем)
        log.debug("Item %s: no timestamp found", getattr(item, 'id', '?'))
        return True
    except Exception as e:
        log.debug("Error checking item recency for item %s: %s", getattr(item, 'id', '?'), e)
        return True


async def get_item_last_seen_minutes(item_url: str) -> int:
    """
    Получает количество минут 'Last seen' для товара со страницы.
    
    Возвращает количество минут или -1 если не удалось получить информацию.
    """
    try:
        # Используем selenium для получения информации о Last seen
        if not selenium_vinted:
            log.debug("Selenium not available for %s", item_url)
            return -1
        
        def scrape_last_seen():
            """Блокирующая функция для скрейпинга."""
            try:
                # Открываем страницу товара
                selenium_vinted.driver.get(item_url)
                import time
                time.sleep(0.5)
                
                # Ищем текст вроде "Last seen 5 minutes ago"
                from selenium.webdriver.common.by import By
                import re
                
                page_text = selenium_vinted.driver.page_source
                
                # Ищем "last seen X minutes ago"
                match = re.search(r'last\s+seen\s+(\d+)\s+minutes?\s+ago', page_text, re.IGNORECASE)
                if match:
                    minutes = int(match.group(1))
                    log.debug("Item %s: last seen %d minutes ago", item_url, minutes)
                    return minutes

                match_h = re.search(r'last\s+seen\s+(\d+)\s+hours?\s+ago', page_text, re.IGNORECASE)
                if match_h:
                    mins = int(match_h.group(1)) * 60
                    log.debug("Item %s: last seen %d hours -> %d min", item_url, int(match_h.group(1)), mins)
                    return mins
                
                # Если нашли "less than a minute" или "just now"
                if re.search(r'(less than a minute|just now)', page_text, re.IGNORECASE):
                    log.debug("Item %s: last seen just now", item_url)
                    return 0
                
                # Если ничего не нашли
                log.debug("Item %s: could not find 'last seen' text", item_url)
                return -1
            except Exception as e:
                log.debug("Error in scrape_last_seen for %s: %s", item_url, e)
                return -1
        
        # Запускаем блокирующую функцию в отдельном потоке
        result = await asyncio.to_thread(scrape_last_seen)
        return result
    except Exception as e:
        log.debug("Error getting last seen for %s: %s", item_url, e)
        return -1


def fetch_brand_items(bid: int, per_page: int = 96):
    """Получаем товары по бренду. Сначала пробуем Selenium (обходит Cloudflare), потом API."""
    brand_name = BRANDS[bid]
    brand_id = BRAND_IDS.get(brand_name)

    def _search_selenium():
        """Пытаемся найти товары через Selenium."""
        global selenium_vinted
        if not selenium_vinted:
            return []
        try:
            if brand_id:
                items = selenium_vinted.search(brand_id=brand_id, per_page=per_page, order="newest_first")
            else:
                items = selenium_vinted.search(brand_name=brand_name, per_page=per_page, order="newest_first")
            return items if items else []
        except Exception as e:
            log.warning("Selenium search failed for %s: %s", brand_name, e)
            return []

    def _search():
        """Пытаемся найти товары через Selenium, потом API."""
        refresh_vinted_cookies(reason="ttl")
        # Сначала пробуем Selenium
        items = _search_selenium()
        if items:
            log.info("Found %s items via Selenium for %s", len(items), brand_name)
            return items
        
        # Если Selenium не сработал, падаем на API
        if brand_id:
            res = vinted.search(
                brand_ids=brand_id, per_page=per_page, order="newest_first"
            )
            if isinstance(res, dict) and res.get("error"):
                log.error("search error for %s: %s", brand_name, res.get("error"))
                return []
            items = res.items() if callable(res.items) else res.items
            if items:
                return [it for it in items if hasattr(it, "id")]
        # Фолбэк: текстовый поиск по названию бренда
        res = vinted.search(
            query=brand_name,
            per_page=per_page,
            order="newest_first",
            price_from=os.getenv("VINTED_PRICE_FROM"),
            price_to=os.getenv("VINTED_PRICE_TO"),
        )
        if isinstance(res, dict) and res.get("error"):
            log.error("text search error for %s: %s", brand_name, res.get("error"))
            return [], None
        items = res.items() if callable(res.items) else res.items
        return [
            it
            for it in (items or [])
            if (it.brand_title or "").lower() == brand_name.lower()
        ]

    try:
        items = _search()
        log.info("search %s (id=%s) -> %s items", brand_name, brand_id, len(items) if items else 0)
        # Filter new items based on global last_id
        global last_brand_id
        latest_item = items[0] if items else None
        if items:
            new_items = [it for it in items if it.id > last_brand_id.get(bid, 0)]
            if new_items:
                last_brand_id[bid] = max(it.id for it in new_items)
            return new_items, latest_item
        return [], latest_item
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 403:
            log.warning("403 Forbidden on %s — likely IP blocked. Refreshing cookies and retrying...", brand_name)
            try:
                # При блоке сначала пробуем сменить IP прокси (если поддерживается)
                try:
                    changed = VINTED_PROXY_HANDLER.handle_block(403)
                    log.info("Proxy handle_block for %s -> %s", brand_name, changed)
                except Exception as pexc:
                    log.warning("Proxy handle_block error for %s: %s", brand_name, pexc)
                refresh_vinted_cookies(force=True, reason="403")
                log.info("Cookies refreshed for %s", brand_name)
            except Exception as ce:
                log.error("cookie refresh failed: %s", ce)
            
            # Retry до 2 раз при 403
            for retry_count in range(2):
                try:
                    import time
                    wait_time = 2 ** retry_count  # exponential backoff: 1s, 2s
                    log.info("Retry #%d for %s after %ds...", retry_count + 1, brand_name, wait_time)
                    time.sleep(wait_time)
                    items = _search()
                    log.info("retry search %s -> %s items", brand_name, len(items) if items else 0)
                    latest_item = items[0] if items else None
                    if items:
                        new_items = [it for it in items if it.id > last_brand_id.get(bid, 0)]
                        if new_items:
                            last_brand_id[bid] = max(it.id for it in new_items)
                        return new_items, latest_item
                    return [], latest_item
                except requests.HTTPError as retry_err:
                    if retry_err.response is not None and retry_err.response.status_code == 403:
                        log.warning("Still 403 on retry #%d for %s, will retry again", retry_count + 1, brand_name)
                        continue
                    log.error("retry #%d failed with different error for %s: %s", retry_count + 1, brand_name, retry_err)
                    return [], None
                except Exception as e2:
                    log.error("retry #%d failed for %s: %s", retry_count + 1, brand_name, e2)
                    return [], None
            
            log.error("All retries exhausted for %s", brand_name)
            return [], None
        log.error("fetch_brand_items HTTP error for %s: %s", brand_name, e)
        return [], None
    except Exception as e:
        # Некоторые ошибки возникают при невалидном JSON (HTTP 200 с HTML). Попробуем обновить куки и повторить один раз.
        err_str = str(e)
        log.warning("fetch_brand_items general error for %s: %s — trying cookie refresh", brand_name, err_str)
        # Попытка получить сырой ответ для диагностики (если парсинг внутри vinted.search рухнул).
        try:
            # Тот же хост, что у клиента (раньше был hardcode .de при domain=com — вводил в заблуждение)
            base = f"{vinted.api_url}/catalog/items"
            if brand_id:
                params = f"?page=1&per_page={per_page}&order=newest_first&brand_ids[]={brand_id}"
            else:
                params = f"?page=1&per_page={per_page}&order=newest_first&search_text={brand_name}"
            raw_url = base + params
            try:
                r_raw = vinted.scraper.get(raw_url, headers=getattr(vinted, "headers", {}), cookies=getattr(vinted, "cookies", {}), timeout=10)
                txt = r_raw.text if hasattr(r_raw, "text") else repr(r_raw)
                snippet = txt[:1000].replace("\n", " ")
                log.warning("Raw response for %s (status=%s, len=%s): %s", brand_name, getattr(r_raw, "status_code", "?"), len(txt) if isinstance(txt, str) else 0, snippet)
            except Exception as rexc:
                log.warning("Failed to fetch raw response for %s: %s", brand_name, rexc)
        except Exception:
            # Не фатально — продолжаем обычную логику
            pass
        try:
            refresh_vinted_cookies(force=True, reason="parse-error")
            log.info("Cookies refreshed for %s after parse error, retrying search once", brand_name)
            try:
                items = _search()
                log.info("retry search %s -> %s items", brand_name, len(items) if items else 0)
                latest_item = items[0] if items else None
                if items:
                    new_items = [it for it in items if it.id > last_brand_id.get(bid, 0)]
                    if new_items:
                        last_brand_id[bid] = max(it.id for it in new_items)
                    return new_items, latest_item
                return [], latest_item
            except Exception as e2:
                log.error("Retry after cookie refresh failed for %s: %s", brand_name, e2)
                return [], None
        except Exception as ce:
            log.error("cookie refresh after parse error failed for %s: %s", brand_name, ce)
            return [], None
    except Exception as e:
        log.error("fetch_brand_items error for %s: %s", brand_name, e)
        return [], None


def get_photo_urls(item_url: str):
    """Грубо вытягиваем все фото с страницы объявления через regex."""
    try:
        r = vinted.scraper.get(item_url, headers=vinted.headers, cookies=vinted.cookies)
        if r.status_code != 200:
            return []
        import re

        urls = re.findall(r"https://images\\d+\\.vinted\\.net/t/[^\"']+\\.webp", r.text)
        # Сохраняем порядок и убираем дубли
        seen_urls = []
        for u in urls:
            if u not in seen_urls:
                seen_urls.append(u)
        return seen_urls
    except Exception as e:
        log.error("get_photo_urls error: %s", e)
        return []


def build_app(token: str):
    app = ApplicationBuilder().token(token).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("brands", start))  # повторный выбор брендов
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("pause", pause_cmd))
    app.add_handler(CommandHandler("resume", resume_cmd))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("generate", generate_cmd))
    app.add_handler(CommandHandler("activate", activate_cmd))
    # Обработка текстовых кнопок с нижней Reply‑клавиатуры
    app.add_handler(
        MessageHandler(
            filters.TEXT & (~filters.COMMAND),
            handle_reply_buttons,
        ),
        group=10,
    )
    app.add_handler(CallbackQueryHandler(on_brand_click))
    # Логируем любые непойманные апдейты для дебага
    async def debug_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            log.info("DEBUG update: %s", update.to_dict())
        except Exception:
            log.exception("debug_all failed")
    app.add_handler(MessageHandler(filters.ALL, debug_all), group=100)

    # Ловим Conflict внутри приложения и гасим его
    async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
        err = context.error
        if isinstance(err, Conflict):
            log.error(
                "Получен Conflict от Telegram (кто-то ещё опрашивает этот токен). Останавливаю приложение."
            )
            try:
                await context.application.stop()
            except RuntimeError:
                pass  # Application already stopped
            try:
                await context.application.shutdown()
            except RuntimeError:
                pass  # Application already shut down
        else:
            log.exception("Unhandled error: %s", err)

    app.add_error_handler(error_handler)
    return app


def main():
    token = TOKEN
    if not token:
        raise SystemExit("TELEGRAM_TOKEN не задан. export TELEGRAM_TOKEN=\"<новый_токен>\"")

    app = build_app(token)
    try:
        # Fix for Windows Python 3.10+: Ensure event loop is set
        if sys.platform == 'win32':
            try:
                asyncio.get_event_loop()
            except RuntimeError:
                asyncio.set_event_loop(asyncio.new_event_loop())
        
        app.run_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
            stop_signals=None,
        )
    except Conflict:
        log.error(
            "Telegram вернул Conflict: уже есть другой процесс, который делает getUpdates с этим токеном. "
            "Останови второй процесс и запусти бот заново."
        )
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
