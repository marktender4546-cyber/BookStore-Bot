from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from html import escape
from typing import Any
from urllib.parse import parse_qsl

import uvicorn
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    Contact,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonWebApp,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    WebAppInfo,
)
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

# -----------------------------------------------------------------------------
# Configuration. Keep BOT_TOKEN private: never commit it to GitHub.
# -----------------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {
    int(part.strip())
    for part in os.getenv("ADMIN_IDS", "").split(",")
    if part.strip().isdigit()
}
WEBAPP_URL = (
    os.getenv("WEBAPP_URL", "").strip().rstrip("/")
    or os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
)
DB_PATH = os.getenv("DATABASE_PATH", "bookstore.sqlite3")
PORT = int(os.getenv("PORT", "8000"))

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("bookshop")

app = FastAPI(title="Kitoblar do‘koni / Bookshop", docs_url=None, redoc_url=None)
dp = Dispatcher()
bot: Bot | None = None

# -----------------------------------------------------------------------------
# SQLite persistence
# -----------------------------------------------------------------------------
SEED_BOOKS = [
    ("Mehrobdan chayon", "Abdulla Qodiriy", "O‘zbek adabiyoti", 42000, 12,
     "O‘zbek adabiyotining mashhur tarixiy romani. Savdo uchun namuna mahsulot.", ""),
    ("O‘tkan kunlar", "Abdulla Qodiriy", "O‘zbek adabiyoti", 45000, 10,
     "Otabek va Kumush taqdiri haqidagi mumtoz roman. Savdo uchun namuna mahsulot.", ""),
    ("Kecha va kunduz", "Cho‘lpon", "O‘zbek adabiyoti", 39000, 8,
     "XX asr o‘zbek adabiyotining muhim romanlaridan biri. Savdo uchun namuna mahsulot.", ""),
    ("Ikki eshik orasi", "O‘tkir Hoshimov", "O‘zbek adabiyoti", 52000, 7,
     "Insoniy munosabatlar va urush davri haqida ta’sirli roman. Savdo uchun namuna mahsulot.", ""),
    ("Ufq", "Said Ahmad", "O‘zbek adabiyoti", 47000, 9,
     "O‘zbek xalqining og‘ir va umidli kunlari haqida roman. Savdo uchun namuna mahsulot.", ""),
    ("Jinoyat va jazo", "Fyodor Dostoyevskiy", "Jahon klassikasi", 68000, 6,
     "Vijdon, ayb va inson ruhiyati haqidagi mashhur roman. Nashr tilini mahsulot tavsifida ko‘rsating.", ""),
    ("Anna Karenina", "Lev Tolstoy", "Jahon klassikasi", 72000, 5,
     "Muhabbat, oila va jamiyat haqida jahon adabiyoti durdonasi.", ""),
    ("Usta va Margarita", "Mixail Bulgakov", "Jahon klassikasi", 65000, 6,
     "Satira, fantastika va falsafa uyg‘unlashgan mashhur roman.", ""),
    ("Kichkina shahzoda", "Antuan de Sent-Ekzyuperi", "Qissa", 35000, 14,
     "Do‘stlik va hayot haqida turli yoshdagi kitobxonlarga manzur qissa.", ""),
    ("1984", "Jorj Oruell", "Antiutopiya", 59000, 6,
     "Nazorat, erkinlik va haqiqat mavzularini yorituvchi antiutopik roman.", ""),
    ("Uzuklar hukmdori", "J. R. R. Tolkien", "Fantastika", 95000, 4,
     "Keng tasavvur olami va sarguzashtlarga boy fantastik asar.", ""),
    ("Uch mushketyor", "Aleksandr Dyuma", "Sarguzasht", 61000, 7,
     "Do‘stlik, jasorat va sarguzashtlarga boy klassik roman.", ""),
]


def db_connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=20)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 20000")
    return connection


def init_db() -> None:
    folder = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(folder, exist_ok=True)
    with closing(db_connect()) as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                author TEXT NOT NULL,
                genre TEXT NOT NULL,
                price INTEGER NOT NULL CHECK(price >= 0),
                stock INTEGER NOT NULL DEFAULT 0 CHECK(stock >= 0),
                description TEXT NOT NULL DEFAULT '',
                cover_url TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS cart (
                user_id INTEGER NOT NULL,
                book_id INTEGER NOT NULL REFERENCES books(id),
                quantity INTEGER NOT NULL CHECK(quantity > 0),
                PRIMARY KEY (user_id, book_id)
            );
            CREATE TABLE IF NOT EXISTS favorites (
                user_id INTEGER NOT NULL,
                book_id INTEGER NOT NULL REFERENCES books(id),
                PRIMARY KEY (user_id, book_id)
            );
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                customer_name TEXT NOT NULL,
                phone TEXT NOT NULL,
                address TEXT NOT NULL,
                total INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'new',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS order_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL REFERENCES orders(id),
                book_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                author TEXT NOT NULL,
                price INTEGER NOT NULL,
                quantity INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_books_active ON books(active, genre);
            CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id, id DESC);
            """
        )
        count = con.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        if count == 0:
            con.executemany(
                "INSERT INTO books(title, author, genre, price, stock, description, cover_url) VALUES(?,?,?,?,?,?,?)",
                SEED_BOOKS,
            )
        con.commit()


def money(value: int) -> str:
    return f"{int(value):,}".replace(",", " ") + " so‘m"


def get_book(book_id: int, active_only: bool = True) -> sqlite3.Row | None:
    with closing(db_connect()) as con:
        query = "SELECT * FROM books WHERE id=?"
        params: tuple[Any, ...] = (book_id,)
        if active_only:
            query += " AND active=1"
        return con.execute(query, params).fetchone()


def active_books(genre: str | None = None, query: str | None = None, limit: int | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM books WHERE active=1"
    args: list[Any] = []
    if genre:
        sql += " AND genre=?"
        args.append(genre)
    if query:
        pattern = f"%{query.strip()}%"
        sql += " AND (title LIKE ? OR author LIKE ? OR genre LIKE ? OR description LIKE ?)"
        args.extend([pattern, pattern, pattern, pattern])
    sql += " ORDER BY title COLLATE NOCASE"
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    with closing(db_connect()) as con:
        return list(con.execute(sql, args).fetchall())


def all_genres() -> list[str]:
    with closing(db_connect()) as con:
        return [r[0] for r in con.execute("SELECT DISTINCT genre FROM books WHERE active=1 ORDER BY genre COLLATE NOCASE")]


def cart_rows(user_id: int) -> list[sqlite3.Row]:
    with closing(db_connect()) as con:
        return list(con.execute(
            """SELECT c.book_id, c.quantity, b.title, b.author, b.price, b.stock, b.active
               FROM cart c JOIN books b ON b.id=c.book_id
               WHERE c.user_id=? ORDER BY b.title COLLATE NOCASE""", (user_id,)
        ).fetchall())


def cart_total(user_id: int) -> int:
    return sum(int(row["price"]) * int(row["quantity"]) for row in cart_rows(user_id) if row["active"])


def add_to_cart(user_id: int, book_id: int, amount: int = 1) -> tuple[bool, str]:
    con = db_connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        book = con.execute("SELECT * FROM books WHERE id=? AND active=1", (book_id,)).fetchone()
        if not book:
            con.rollback()
            return False, "Bu kitob hozir katalogda yo‘q."
        row = con.execute("SELECT quantity FROM cart WHERE user_id=? AND book_id=?", (user_id, book_id)).fetchone()
        current = int(row[0]) if row else 0
        new_amount = current + amount
        if new_amount < 1:
            if row:
                con.execute("DELETE FROM cart WHERE user_id=? AND book_id=?", (user_id, book_id))
            con.commit()
            return True, "Savat yangilandi."
        if new_amount > int(book["stock"]):
            con.rollback()
            return False, f"Omborda faqat {book['stock']} dona bor."
        con.execute(
            "INSERT INTO cart(user_id, book_id, quantity) VALUES(?,?,?) "
            "ON CONFLICT(user_id, book_id) DO UPDATE SET quantity=excluded.quantity",
            (user_id, book_id, new_amount),
        )
        con.commit()
        return True, "Savat yangilandi."
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def create_order(user_id: int, customer_name: str, phone: str, address: str,
                 items: list[tuple[int, int]]) -> tuple[int, int]:
    """Create order atomically and reduce stock. items is [(book_id, quantity), ...]."""
    if not items:
        raise ValueError("Savat bo‘sh.")
    con = db_connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        picked: list[tuple[sqlite3.Row, int]] = []
        total = 0
        for book_id, qty in items:
            if qty < 1 or qty > 50:
                raise ValueError("Kitob miqdori noto‘g‘ri.")
            book = con.execute("SELECT * FROM books WHERE id=? AND active=1", (book_id,)).fetchone()
            if not book:
                raise ValueError("Savatdagi kitoblardan biri endi mavjud emas.")
            if int(book["stock"]) < qty:
                raise ValueError(f"«{book['title']}» kitobidan omborda {book['stock']} dona qoldi.")
            picked.append((book, qty))
            total += int(book["price"]) * qty
        cur = con.execute(
            "INSERT INTO orders(user_id, customer_name, phone, address, total, status) VALUES(?,?,?,?,?,'new')",
            (user_id, customer_name[:120], phone[:60], address[:500], total),
        )
        order_id = int(cur.lastrowid)
        for book, qty in picked:
            con.execute(
                "INSERT INTO order_items(order_id, book_id, title, author, price, quantity) VALUES(?,?,?,?,?,?)",
                (order_id, book["id"], book["title"], book["author"], book["price"], qty),
            )
            con.execute("UPDATE books SET stock=stock-? WHERE id=?", (qty, book["id"]))
            con.execute("DELETE FROM cart WHERE user_id=? AND book_id=?", (user_id, book["id"]))
        con.commit()
        return order_id, total
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def order_summary(order_id: int) -> dict[str, Any] | None:
    with closing(db_connect()) as con:
        order = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order:
            return None
        items = list(con.execute("SELECT * FROM order_items WHERE order_id=?", (order_id,)).fetchall())
        return {"order": dict(order), "items": [dict(i) for i in items]}


# -----------------------------------------------------------------------------
# Telegram interface
# -----------------------------------------------------------------------------
class SearchFlow(StatesGroup):
    waiting_query = State()


class AddBookFlow(StatesGroup):
    waiting_data = State()


class CheckoutFlow(StatesGroup):
    waiting_phone = State()
    waiting_address = State()


STATUS_LABELS = {
    "new": "🆕 Новый",
    "processing": "🛠 В обработке",
    "shipped": "🚚 Отправлен",
    "completed": "✅ Завершён",
    "cancelled": "❌ Отменён",
}


def home_keyboard() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="📚 Каталог"), KeyboardButton(text="🔎 Поиск")],
        [KeyboardButton(text="🛒 Корзина"), KeyboardButton(text="❤️ Избранное")],
        [KeyboardButton(text="📦 Мои заказы"), KeyboardButton(text="ℹ️ Помощь")],
    ]
    if WEBAPP_URL.startswith("https://"):
        rows.insert(0, [KeyboardButton(text="✨ Открыть магазин", web_app=WebAppInfo(url=WEBAPP_URL))])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, input_field_placeholder="Выберите действие")


def book_keyboard(book_id: int, user_id: int) -> InlineKeyboardMarkup:
    with closing(db_connect()) as con:
        fav = con.execute("SELECT 1 FROM favorites WHERE user_id=? AND book_id=?", (user_id, book_id)).fetchone()
    heart = "💔 Убрать из избранного" if fav else "❤️ В избранное"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Добавить в корзину", callback_data=f"add:{book_id}")],
        [InlineKeyboardButton(text=heart, callback_data=f"fav:{book_id}")],
        [InlineKeyboardButton(text="📚 К каталогу", callback_data="catalog")],
    ])


SEARCH_QUERY_BY_USER: dict[int, str] = {}


def catalog_keyboard(books: list[sqlite3.Row], genre: str | None = None, page: int = 0, query: str | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    start = page * 8
    for book in books[start:start + 8]:
        short_title = str(book["title"])
        if len(short_title) > 48:
            short_title = short_title[:45] + "..."
        rows.append([InlineKeyboardButton(text=f"📖 {short_title} · {money(book['price'])}", callback_data=f"book:{book['id']}")])
    if len(books) > 8:
        nav: list[InlineKeyboardButton] = []
        if genre:
            genre_index = all_genres().index(genre) if genre in all_genres() else 0
            previous_cb = lambda p: f"genre:{genre_index}:{p}"
            next_cb = previous_cb
        elif query:
            previous_cb = lambda p: f"searchpage:{p}"
            next_cb = previous_cb
        else:
            previous_cb = lambda p: f"page:{p}"
            next_cb = previous_cb
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=previous_cb(page-1)))
        if start + 8 < len(books):
            nav.append(InlineKeyboardButton(text="Далее ➡️", callback_data=next_cb(page+1)))
        if nav:
            rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔎 Поиск", callback_data="search")])
    rows.append([InlineKeyboardButton(text="🛒 Корзина", callback_data="cart"), InlineKeyboardButton(text="❤️ Избранное", callback_data="favorites")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def categories_keyboard() -> InlineKeyboardMarkup:
    genres = all_genres()
    rows: list[list[InlineKeyboardButton]] = []
    for start in range(0, len(genres), 2):
        row = []
        for genre in genres[start:start + 2]:
            genre_index = genres.index(genre)
            row.append(InlineKeyboardButton(text=f"📚 {genre}", callback_data=f"genre:{genre_index}:0"))
        rows.append(row)
    rows.append([InlineKeyboardButton(text="📖 Все книги", callback_data="allbooks")])
    rows.append([InlineKeyboardButton(text="🔎 Поиск", callback_data="search"), InlineKeyboardButton(text="🛒 Корзина", callback_data="cart")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_catalog(message: Message, genre: str | None = None, page: int = 0) -> None:
    if genre is None:
        await message.answer("📚 <b>Книжная полка</b>\nВыберите жанр или откройте весь каталог:", reply_markup=categories_keyboard())
        return
    books = active_books(genre=genre)
    if not books:
        await message.answer("В этой категории пока нет книг.", reply_markup=categories_keyboard())
        return
    text = f"📚 <b>{escape(genre)}</b>\nНайдено книг: {len(books)}. Выберите книгу:"
    await message.answer(text, reply_markup=catalog_keyboard(books, genre, page))


async def notify_admins(text: str) -> None:
    if not bot:
        return
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception as exc:
            log.warning("Не удалось отправить уведомление администратору %s: %s", admin_id, exc)


async def notify_new_order(order_id: int) -> None:
    summary = order_summary(order_id)
    if not summary:
        return
    order = summary["order"]
    lines = [
        f"🛍 <b>Новый заказ №{order_id}</b>",
        f"👤 {escape(order['customer_name'])} (Telegram ID: <code>{order['user_id']}</code>)",
        f"📞 {escape(order['phone'])}",
        f"📍 {escape(order['address'])}",
        "",
    ]
    for item in summary["items"]:
        lines.append(f"• {escape(item['title'])} × {item['quantity']} = {money(item['price'] * item['quantity'])}")
    lines.extend(["", f"💰 <b>Итого: {money(order['total'])}</b>", "Статус: Новый"])
    await notify_admins("\n".join(lines))


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    first = escape(message.from_user.first_name if message.from_user else "читатель")
    app_line = "\n✨ Нажмите «Открыть магазин», чтобы пользоваться Mini App." if WEBAPP_URL.startswith("https://") else "\n🌐 Mini App появится в меню после размещения сайта на HTTPS (например, Render)."
    await message.answer(
        f"Привет, {first}! Добро пожаловать в <b>Книжную полку</b> 📚\n\n"
        "Здесь можно выбирать художественную литературу, искать книги, собирать корзину и оформлять заказ." + app_line,
        reply_markup=home_keyboard(),
    )


@dp.message(Command("help"))
@dp.message(F.text == "ℹ️ Помощь")
async def cmd_help(message: Message) -> None:
    await message.answer(
        "<b>Как пользоваться магазином</b>\n"
        "• «Каталог» — категории и книги\n"
        "• «Поиск» — поиск по названию, автору и жанру\n"
        "• «Корзина» — изменение количества и оформление\n"
        "• «Избранное» — сохранённые книги\n"
        "• «Мои заказы» — история и статусы\n\n"
        "Оплата и доставка согласовываются с продавцом после оформления. "
        "Это демонстрационный каталог: замените примеры на реальные товары и цены."
    )


@dp.message(Command("catalog"))
@dp.message(F.text == "📚 Каталог")
async def cmd_catalog(message: Message) -> None:
    await show_catalog(message)


@dp.callback_query(F.data == "catalog")
async def cb_catalog(callback: CallbackQuery) -> None:
    if callback.message:
        await show_catalog(callback.message)
    await callback.answer()


@dp.callback_query(F.data == "allbooks")
async def cb_allbooks(callback: CallbackQuery) -> None:
    books = active_books()
    if callback.message:
        await callback.message.edit_text(
            f"📚 <b>Все книги</b>\nНайдено книг: {len(books)}. Выберите книгу:",
            reply_markup=catalog_keyboard(books),
        )
    await callback.answer()


@dp.callback_query(F.data.startswith("genre:"))
async def cb_genre(callback: CallbackQuery) -> None:
    try:
        _, index_text, page_text = (callback.data or "").split(":", 2)
        genres = all_genres()
        genre = genres[int(index_text)]
        page = max(0, int(page_text))
    except Exception:
        await callback.answer("Не удалось открыть категорию", show_alert=True)
        return
    books = active_books(genre=genre)
    if callback.message:
        await callback.message.edit_text(
            f"📚 <b>{escape(genre)}</b>\nНайдено книг: {len(books)}. Выберите книгу:",
            reply_markup=catalog_keyboard(books, genre, page),
        )
    await callback.answer()


@dp.callback_query(F.data.startswith("searchpage:"))
async def cb_search_page(callback: CallbackQuery) -> None:
    try:
        page = max(0, int((callback.data or "searchpage:0").split(":", 1)[1]))
        query = SEARCH_QUERY_BY_USER.get(callback.from_user.id, "")
        if not query:
            await callback.answer("Введите поисковый запрос заново", show_alert=True)
            return
    except Exception:
        await callback.answer("Не удалось открыть страницу", show_alert=True)
        return
    books = active_books(query=query)
    if callback.message:
        await callback.message.edit_text(
            f"🔎 <b>Результаты поиска</b> по запросу «{escape(query)}»: {len(books)}",
            reply_markup=catalog_keyboard(books, query=query, page=page),
        )
    await callback.answer()


@dp.callback_query(F.data.startswith("page:"))
async def cb_page(callback: CallbackQuery) -> None:
    try:
        page = max(0, int((callback.data or "page:0").split(":", 1)[1]))
    except ValueError:
        page = 0
    books = active_books()
    if callback.message:
        await callback.message.edit_text(
            f"📚 <b>Все книги</b>\nНайдено книг: {len(books)}. Выберите книгу:",
            reply_markup=catalog_keyboard(books, None, page),
        )
    await callback.answer()


@dp.callback_query(F.data.startswith("book:"))
async def cb_book(callback: CallbackQuery) -> None:
    try:
        book_id = int((callback.data or "book:0").split(":", 1)[1])
    except ValueError:
        await callback.answer("Книга не найдена", show_alert=True)
        return
    book = get_book(book_id)
    if not book or not callback.message:
        await callback.answer("Книга уже недоступна", show_alert=True)
        return
    desc = escape(book["description"] or "Описание скоро появится.")
    text = (
        f"📖 <b>{escape(book['title'])}</b>\n"
        f"✍️ {escape(book['author'])}\n"
        f"🏷 Жанр: {escape(book['genre'])}\n"
        f"💰 Цена: <b>{money(book['price'])}</b>\n"
        f"📦 В наличии: {book['stock']} шт.\n\n{desc}"
    )
    await callback.message.answer(text, reply_markup=book_keyboard(book_id, callback.from_user.id))
    await callback.answer()


@dp.callback_query(F.data.startswith("add:"))
async def cb_add(callback: CallbackQuery) -> None:
    try:
        book_id = int((callback.data or "add:0").split(":", 1)[1])
        ok, text = add_to_cart(callback.from_user.id, book_id, 1)
    except Exception:
        log.exception("Ошибка добавления в корзину")
        ok, text = False, "Не удалось обновить корзину. Попробуйте ещё раз."
    await callback.answer(("✅ " if ok else "⚠️ ") + text, show_alert=not ok)


@dp.callback_query(F.data.startswith("fav:"))
async def cb_favorite(callback: CallbackQuery) -> None:
    try:
        book_id = int((callback.data or "fav:0").split(":", 1)[1])
        with closing(db_connect()) as con:
            exists = con.execute("SELECT 1 FROM favorites WHERE user_id=? AND book_id=?", (callback.from_user.id, book_id)).fetchone()
            if exists:
                con.execute("DELETE FROM favorites WHERE user_id=? AND book_id=?", (callback.from_user.id, book_id))
                message = "Удалено из избранного ❤️‍🩹"
            else:
                con.execute("INSERT OR IGNORE INTO favorites(user_id, book_id) VALUES(?,?)", (callback.from_user.id, book_id))
                message = "Добавлено в избранное ❤️"
            con.commit()
        await callback.answer(message)
    except Exception:
        log.exception("Ошибка избранного")
        await callback.answer("Не удалось обновить избранное", show_alert=True)


@dp.message(Command("search"))
async def cmd_search(message: Message, state: FSMContext) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        query = parts[1].strip()
        await send_search_results(message, query)
    else:
        await state.set_state(SearchFlow.waiting_query)
        await message.answer("🔎 Напишите название книги, автора или жанр. Для отмены отправьте /cancel.")


@dp.message(F.text == "🔎 Поиск")
@dp.callback_query(F.data == "search")
async def begin_search(event: Message | CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SearchFlow.waiting_query)
    msg = event if isinstance(event, Message) else event.message
    if msg:
        await msg.answer("🔎 Напишите название книги, автора или жанр. Для отмены отправьте /cancel.")
    if isinstance(event, CallbackQuery):
        await event.answer()


async def send_search_results(message: Message, query: str) -> None:
    query = query[:40]
    if message.from_user:
        SEARCH_QUERY_BY_USER[message.from_user.id] = query
    books = active_books(query=query)
    if not books:
        await message.answer(f"По запросу «{escape(query)}» ничего не найдено. Попробуйте другое слово.")
        return
    await message.answer(
        f"🔎 <b>Результаты поиска</b> по запросу «{escape(query)}»: {len(books)}",
        reply_markup=catalog_keyboard(books, query=query),
    )


@dp.message(SearchFlow.waiting_query, F.text)
async def search_query_received(message: Message, state: FSMContext) -> None:
    query = (message.text or "").strip()
    await state.clear()
    if query == "/cancel":
        await message.answer("Поиск отменён.", reply_markup=home_keyboard())
        return
    await send_search_results(message, query[:100])


@dp.message(Command("cancel"))
async def cancel_flow(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=home_keyboard())


@dp.message(Command("cart"))
@dp.message(F.text == "🛒 Корзина")
@dp.callback_query(F.data == "cart")
async def show_cart(event: Message | CallbackQuery) -> None:
    user_id = event.from_user.id
    rows = cart_rows(user_id)
    msg = event if isinstance(event, Message) else event.message
    if not msg:
        return
    if not rows:
        await msg.answer("🛒 Корзина пока пуста. Загляните в каталог!", reply_markup=home_keyboard())
    else:
        lines = ["🛒 <b>Ваша корзина</b>"]
        buttons: list[list[InlineKeyboardButton]] = []
        for row in rows:
            if not row["active"]:
                continue
            book_id = int(row["book_id"])
            qty = int(row["quantity"])
            lines.append(f"• {escape(row['title'])} × {qty} = <b>{money(row['price'] * qty)}</b>")
            buttons.append([
                InlineKeyboardButton(text="−", callback_data=f"minus:{book_id}"),
                InlineKeyboardButton(text=f"{qty} шт.", callback_data="noop"),
                InlineKeyboardButton(text="+", callback_data=f"add:{book_id}"),
                InlineKeyboardButton(text="Удалить", callback_data=f"remove:{book_id}"),
            ])
        lines.extend(["", f"<b>Итого: {money(cart_total(user_id))}</b>"])
        buttons.append([InlineKeyboardButton(text="✅ Оформить заказ", callback_data="checkout")])
        buttons.append([InlineKeyboardButton(text="📚 Продолжить покупки", callback_data="catalog")])
        await msg.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    if isinstance(event, CallbackQuery):
        await event.answer()


@dp.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@dp.callback_query(F.data.startswith("minus:"))
async def cb_minus(callback: CallbackQuery) -> None:
    try:
        book_id = int((callback.data or "minus:0").split(":", 1)[1])
        add_to_cart(callback.from_user.id, book_id, -1)
    except Exception:
        log.exception("Ошибка уменьшения корзины")
    await show_cart(callback)


@dp.callback_query(F.data.startswith("remove:"))
async def cb_remove(callback: CallbackQuery) -> None:
    try:
        book_id = int((callback.data or "remove:0").split(":", 1)[1])
        with closing(db_connect()) as con:
            con.execute("DELETE FROM cart WHERE user_id=? AND book_id=?", (callback.from_user.id, book_id))
            con.commit()
    except Exception:
        log.exception("Ошибка удаления из корзины")
    await show_cart(callback)


@dp.message(Command("favorites"))
@dp.message(F.text == "❤️ Избранное")
@dp.callback_query(F.data == "favorites")
async def show_favorites(event: Message | CallbackQuery) -> None:
    user_id = event.from_user.id
    with closing(db_connect()) as con:
        books = list(con.execute(
            "SELECT b.* FROM favorites f JOIN books b ON b.id=f.book_id WHERE f.user_id=? AND b.active=1 ORDER BY b.title COLLATE NOCASE",
            (user_id,),
        ).fetchall())
    msg = event if isinstance(event, Message) else event.message
    if msg:
        if not books:
            await msg.answer("❤️ Избранное пусто. Сохраняйте понравившиеся книги кнопкой «В избранное».")
        else:
            await msg.answer(f"❤️ <b>Избранное</b> · {len(books)} книг", reply_markup=catalog_keyboard(books))
    if isinstance(event, CallbackQuery):
        await event.answer()


@dp.message(Command("orders"))
@dp.message(F.text == "📦 Мои заказы")
async def my_orders(message: Message) -> None:
    with closing(db_connect()) as con:
        rows = list(con.execute("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 10", (message.from_user.id,)).fetchall())
    if not rows:
        await message.answer("📦 У вас пока нет заказов.")
        return
    lines = ["📦 <b>Ваши последние заказы</b>"]
    for row in rows:
        label = STATUS_LABELS.get(row["status"], row["status"])
        lines.append(f"\n<b>№{row['id']}</b> · {money(row['total'])}\n{label} · {row['created_at']}")
    await message.answer("\n".join(lines))


@dp.callback_query(F.data == "checkout")
@dp.message(Command("checkout"))
async def start_checkout(event: Message | CallbackQuery, state: FSMContext) -> None:
    user_id = event.from_user.id
    rows = [r for r in cart_rows(user_id) if r["active"]]
    msg = event if isinstance(event, Message) else event.message
    if not msg:
        return
    if not rows:
        await msg.answer("Сначала добавьте книги в корзину.")
    else:
        await state.set_state(CheckoutFlow.waiting_phone)
        contact_keyboard = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📱 Отправить мой номер", request_contact=True)], [KeyboardButton(text="Отмена")]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await msg.answer(
            f"✅ Оформление заказа\nСумма: <b>{money(cart_total(user_id))}</b>\n\n"
            "Отправьте номер телефона кнопкой ниже или напишите его сообщением.",
            reply_markup=contact_keyboard,
        )
    if isinstance(event, CallbackQuery):
        await event.answer()


async def accept_phone(message: Message, state: FSMContext, phone: str) -> None:
    await state.update_data(phone=phone.strip()[:60])
    await state.set_state(CheckoutFlow.waiting_address)
    await message.answer("Теперь напишите адрес доставки или удобный способ самовывоза.", reply_markup=ReplyKeyboardRemove())


@dp.message(CheckoutFlow.waiting_phone, F.contact)
async def checkout_contact(message: Message, state: FSMContext) -> None:
    contact: Contact = message.contact
    if contact.user_id and contact.user_id != message.from_user.id:
        await message.answer("Пожалуйста, отправьте именно свой номер либо напишите его вручную.")
        return
    await accept_phone(message, state, contact.phone_number)


@dp.message(CheckoutFlow.waiting_phone, F.text)
async def checkout_phone_text(message: Message, state: FSMContext) -> None:
    phone = (message.text or "").strip()
    if phone.lower() in {"отмена", "cancel", "/cancel"}:
        await state.clear()
        await message.answer("Оформление отменено.", reply_markup=home_keyboard())
        return
    if len(phone) < 5 or not any(ch.isdigit() for ch in phone):
        await message.answer("Пожалуйста, отправьте корректный номер телефона или нажмите «Отправить мой номер».")
        return
    await accept_phone(message, state, phone)


@dp.message(CheckoutFlow.waiting_address, F.text)
async def checkout_address(message: Message, state: FSMContext) -> None:
    address = (message.text or "").strip()
    if address.lower() in {"отмена", "cancel", "/cancel"}:
        await state.clear()
        await message.answer("Оформление отменено.", reply_markup=home_keyboard())
        return
    if len(address) < 3:
        await message.answer("Напишите адрес или способ самовывоза чуть подробнее.")
        return
    data = await state.get_data()
    rows = [r for r in cart_rows(message.from_user.id) if r["active"]]
    try:
        order_id, total = create_order(
            message.from_user.id,
            message.from_user.full_name or "Покупатель",
            data.get("phone", ""), address, [(int(r["book_id"]), int(r["quantity"])) for r in rows],
        )
    except ValueError as exc:
        await state.clear()
        await message.answer(f"Не удалось оформить заказ: {escape(str(exc))}\nПроверьте корзину и попробуйте снова.", reply_markup=home_keyboard())
        return
    except Exception:
        log.exception("Ошибка создания заказа")
        await state.clear()
        await message.answer("Произошла техническая ошибка. Попробуйте позднее.", reply_markup=home_keyboard())
        return
    await state.clear()
    await message.answer(
        f"🎉 <b>Заказ №{order_id} оформлен!</b>\nСумма: <b>{money(total)}</b>\n\n"
        "Продавец свяжется с вами для подтверждения доставки и оплаты.",
        reply_markup=home_keyboard(),
    )
    await notify_new_order(order_id)


# Admin functions. ADMIN_IDS is a comma-separated list of numeric Telegram user IDs.
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


@dp.message(Command("admin"))
async def admin_help(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда доступна только администратору.")
        return
    await message.answer(
        "<b>Панель администратора</b>\n"
        "/addbook — добавить книгу (следуйте подсказке)\n"
        "/setstock ID КОЛИЧЕСТВО — установить остаток\n"
        "/delbook ID — скрыть книгу из каталога\n"
        "/orders_admin — последние заказы\n"
        "/orderstatus ID new|processing|shipped|completed|cancelled — статус заказа\n\n"
        "Для добавления книги используйте формат: Название | Автор | Жанр | Цена в сумах | Количество | Описание | URL обложки или -"
    )


@dp.message(Command("addbook"))
async def admin_add_book(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда доступна только администратору.")
        return
    await state.set_state(AddBookFlow.waiting_data)
    await message.answer(
        "Отправьте данные одной строкой, разделяя поля символом |\n\n"
        "<code>Название | Автор | Жанр | Цена в сумах | Количество | Описание | URL обложки или -</code>\n\n"
        "Пример: <code>Новая книга | Иван Иванов | Роман | 55000 | 8 | Краткое описание | -</code>\n"
        "Для отмены отправьте /cancel."
    )


@dp.message(AddBookFlow.waiting_data, F.text)
async def admin_add_book_data(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        await message.answer("Недостаточно прав.")
        return
    if (message.text or "").strip() == "/cancel":
        await state.clear()
        await message.answer("Добавление отменено.")
        return
    fields = [part.strip() for part in (message.text or "").split("|", 6)]
    if len(fields) != 7 or not fields[0] or not fields[1] or not fields[2] or not fields[5]:
        await message.answer("Нужно ровно 7 полей через |. Попробуйте снова или отправьте /cancel.")
        return
    try:
        price, stock = int(fields[3].replace(" ", "")), int(fields[4])
        if price < 0 or stock < 0:
            raise ValueError
    except ValueError:
        await message.answer("Цена и количество должны быть целыми неотрицательными числами. Попробуйте снова.")
        return
    cover = "" if fields[6] == "-" else fields[6]
    if cover and not cover.startswith(("https://", "http://")):
        await message.answer("Ссылка на обложку должна начинаться с https:// или http://. Повторите ввод или укажите -.")
        return
    with closing(db_connect()) as con:
        cur = con.execute(
            "INSERT INTO books(title, author, genre, price, stock, description, cover_url) VALUES(?,?,?,?,?,?,?)",
            (fields[0][:180], fields[1][:120], fields[2][:80], price, stock, fields[5][:1000], cover[:1000]),
        )
        book_id = cur.lastrowid
        con.commit()
    await state.clear()
    await message.answer(f"✅ Книга добавлена в каталог. ID: <code>{book_id}</code>", reply_markup=home_keyboard())


@dp.message(Command("setstock"))
async def admin_set_stock(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Недостаточно прав.")
        return
    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.answer("Использование: /setstock ID КОЛИЧЕСТВО")
        return
    try:
        book_id, stock = int(parts[1]), int(parts[2])
        if book_id < 1 or stock < 0:
            raise ValueError
    except ValueError:
        await message.answer("ID должен быть положительным, количество — неотрицательным числом.")
        return
    with closing(db_connect()) as con:
        cur = con.execute("UPDATE books SET stock=? WHERE id=?", (stock, book_id))
        con.commit()
    await message.answer("✅ Остаток обновлён." if cur.rowcount else "Книга с таким ID не найдена.")


@dp.message(Command("delbook"))
async def admin_delete_book(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Недостаточно прав.")
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /delbook ID")
        return
    with closing(db_connect()) as con:
        cur = con.execute("UPDATE books SET active=0 WHERE id=?", (int(parts[1]),))
        con.commit()
    await message.answer("✅ Книга скрыта из каталога." if cur.rowcount else "Книга не найдена.")


@dp.message(Command("orders_admin"))
async def admin_orders(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Недостаточно прав.")
        return
    with closing(db_connect()) as con:
        rows = list(con.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 15").fetchall())
    if not rows:
        await message.answer("Заказов пока нет.")
        return
    lines = ["<b>Последние заказы</b>"]
    for row in rows:
        lines.append(f"\n№{row['id']} · {money(row['total'])} · {STATUS_LABELS.get(row['status'], row['status'])}\n{escape(row['customer_name'])} · {escape(row['phone'])}")
    await message.answer("\n".join(lines))


@dp.message(Command("orderstatus"))
async def admin_order_status(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Недостаточно прав.")
        return
    parts = (message.text or "").split()
    valid_statuses = set(STATUS_LABELS)
    if len(parts) != 3 or not parts[1].isdigit() or parts[2] not in valid_statuses:
        await message.answer("Использование: /orderstatus ID new|processing|shipped|completed|cancelled")
        return
    order_id, status = int(parts[1]), parts[2]
    with closing(db_connect()) as con:
        order = con.execute("SELECT user_id FROM orders WHERE id=?", (order_id,)).fetchone()
        cur = con.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
        con.commit()
    if not cur.rowcount:
        await message.answer("Заказ с таким ID не найден.")
        return
    await message.answer(f"✅ Статус заказа №{order_id}: {STATUS_LABELS[status]}")
    if bot and order:
        try:
            await bot.send_message(order["user_id"], f"📦 Статус вашего заказа №{order_id} изменён: <b>{STATUS_LABELS[status]}</b>")
        except Exception:
            pass


# -----------------------------------------------------------------------------
# Mini App: a self-contained responsive storefront served from this same file.
# -----------------------------------------------------------------------------
MINI_APP_HTML = r'''<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="color-scheme" content="light dark"><title>Книжная полка</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    :root{--bg:var(--tg-theme-bg-color,#f6f4ef);--fg:var(--tg-theme-text-color,#20211f);--muted:var(--tg-theme-hint-color,#7b7d78);--card:var(--tg-theme-secondary-bg-color,#fff);--accent:var(--tg-theme-button-color,#376b52);--accentText:var(--tg-theme-button-text-color,#fff);--line:rgba(90,96,85,.16);--warm:#e8d6b6;--radius:18px}
    *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;padding:0 0 calc(92px + env(safe-area-inset-bottom));}
    .wrap{max-width:860px;margin:0 auto;padding:16px}.hero{position:relative;overflow:hidden;border-radius:24px;padding:22px 20px 20px;background:linear-gradient(130deg,#173b30,#38775a 68%,#7e9f76);color:white;box-shadow:0 10px 30px #173b3026}
    .eyebrow{font-size:11px;letter-spacing:.14em;text-transform:uppercase;opacity:.76}.hero h1{font-family:Georgia,serif;font-weight:600;font-size:clamp(27px,7vw,39px);line-height:1.06;margin:9px 0}.hero p{margin:0;max-width:250px;font-size:13px;opacity:.85}.hero:after{content:"❧";position:absolute;right:2px;bottom:-42px;font:155px Georgia,serif;color:#ffffff19;transform:rotate(-18deg)}
    .search{display:flex;align-items:center;gap:9px;margin:16px 0 13px;background:var(--card);border:1px solid var(--line);border-radius:15px;padding:0 14px}.search input{background:transparent;border:0;outline:0;color:var(--fg);width:100%;padding:14px 0;font-size:15px}
    .chips{display:flex;gap:8px;overflow:auto;padding:1px 0 12px;scrollbar-width:none}.chips::-webkit-scrollbar{display:none}.chip{white-space:nowrap;border:1px solid var(--line);background:var(--card);color:var(--fg);padding:8px 13px;border-radius:100px;font-size:12px}.chip.active{background:var(--accent);color:var(--accentText);border-color:var(--accent)}
    .section-title{display:flex;align-items:baseline;justify-content:space-between;margin:8px 2px 12px}.section-title h2{font:600 23px Georgia,serif;margin:0}.section-title small{color:var(--muted)}
    .grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.book{overflow:hidden;border:1px solid var(--line);border-radius:17px;background:var(--card);min-width:0;box-shadow:0 3px 14px #1b281008}.cover{height:154px;display:flex;align-items:center;justify-content:center;position:relative;overflow:hidden;background:linear-gradient(135deg,#dfc7a4,#b7c8b5)}.cover:before{content:"";position:absolute;width:110px;height:145px;border:1px solid #ffffff70;transform:rotate(18deg);border-radius:50%}.cover:after{content:"";position:absolute;inset:13px;border:1px solid #ffffff65;border-radius:4px}.cover img{height:100%;width:100%;object-fit:cover;position:absolute;inset:0}.monogram{font:50px Georgia,serif;color:#fff9;z-index:1;text-shadow:0 2px 10px #172a2040}.book-body{padding:11px}.genre{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.07em}.title{font-weight:700;font-size:14px;line-height:1.3;min-height:36px;margin:5px 0 2px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}.author{font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.price-row{display:flex;align-items:center;justify-content:space-between;gap:5px;margin-top:12px}.price{font-weight:750;font-size:13px}.plus{width:34px;height:34px;border:0;border-radius:11px;background:var(--accent);color:var(--accentText);font-size:23px;line-height:1}
    .empty{padding:34px 18px;text-align:center;background:var(--card);border:1px dashed var(--line);border-radius:18px;color:var(--muted)}.bottom{position:fixed;z-index:5;bottom:0;left:0;right:0;background:var(--bg);border-top:1px solid var(--line);padding:10px 16px calc(10px + env(safe-area-inset-bottom));backdrop-filter:blur(14px)}.bottom-inner{max-width:860px;margin:0 auto;display:flex;gap:9px}.primary,.secondary{border:0;border-radius:14px;padding:13px 15px;font-weight:700;font-size:14px}.primary{background:var(--accent);color:var(--accentText);flex:1}.secondary{background:var(--card);color:var(--fg);border:1px solid var(--line)}.count{display:inline-flex;min-width:22px;height:22px;border-radius:99px;background:#fff2;align-items:center;justify-content:center;margin-left:4px}
    .view{display:none}.view.active{display:block}.back{border:0;background:transparent;color:var(--accent);font-weight:700;padding:8px 0 16px}.detail-cover{height:240px;border-radius:18px;overflow:hidden;background:linear-gradient(140deg,#e9d7ba,#8aab90);display:flex;justify-content:center;align-items:center;position:relative}.detail-cover img{width:100%;height:100%;object-fit:cover}.detail-cover .monogram{font-size:85px}.detail h1{font:600 30px/1.12 Georgia,serif;margin:15px 0 5px}.detail .author{font-size:14px}.detail .description{line-height:1.7;margin:16px 0;color:var(--fg)}.detail .price{font-size:22px}.cartline{display:grid;grid-template-columns:1fr auto;gap:8px;padding:13px 0;border-bottom:1px solid var(--line)}.cartline .sub{font-size:12px;color:var(--muted)}.qty{display:flex;align-items:center;gap:9px}.qty button{width:30px;height:30px;border:1px solid var(--line);border-radius:9px;background:var(--card);color:var(--fg);font-size:19px}.form{display:grid;gap:11px;margin-top:15px}.form label{font-size:12px;color:var(--muted)}.form input,.form textarea{width:100%;padding:13px;border-radius:12px;border:1px solid var(--line);background:var(--card);color:var(--fg);font:inherit;margin-top:5px}.form textarea{min-height:78px;resize:vertical}.notice{font-size:12px;color:var(--muted);margin-top:12px}.toast{position:fixed;z-index:20;left:16px;right:16px;top:12px;padding:12px 14px;border-radius:13px;background:var(--fg);color:var(--bg);box-shadow:0 8px 25px #0002;display:none}.toast.show{display:block;animation:pop .2s ease-out}@keyframes pop{from{transform:translateY(-8px);opacity:0}to{transform:none;opacity:1}}
    @media(min-width:600px){.grid{grid-template-columns:repeat(3,minmax(0,1fr));gap:15px}.cover{height:190px}.book-body{padding:14px}.hero{padding:28px}.hero p{max-width:none}.wrap{padding:22px}}
  </style>
</head>
<body>
<div class="wrap">
  <div class="hero"><div class="eyebrow">Магазин художественной литературы</div><h1>Книга, в которую<br>хочется вернуться.</h1><p>Истории для тихих вечеров и больших открытий.</p></div>
  <div class="view active" id="catalogView">
    <label class="search"><span>⌕</span><input id="searchInput" type="search" placeholder="Название книги или автор" autocomplete="off"></label>
    <div class="chips" id="chips"></div>
    <div class="section-title"><h2 id="sectionHeading">Выбор редакции</h2><small id="resultsCount"></small></div>
    <div class="grid" id="bookGrid"></div>
  </div>
  <div class="view" id="detailView"></div>
  <div class="view" id="cartView"><button class="back" onclick="showCatalog()">← Продолжить покупки</button><div class="section-title"><h2>Ваша корзина</h2><small id="cartCount"></small></div><div id="cartItems"></div><div id="checkoutBox"></div></div>
  <div class="view" id="successView"><div class="empty"><div style="font-size:48px">✦</div><h2>Заказ принят!</h2><p id="successText"></p><button class="primary" onclick="showCatalog()">Вернуться в каталог</button></div></div>
</div>
<div class="bottom"><div class="bottom-inner"><button class="secondary" onclick="showCatalog()">📚 Каталог</button><button class="primary" id="cartButton" onclick="showCart()">🛒 Корзина <span class="count" id="cartBadge">0</span></button></div></div>
<div class="toast" id="toast"></div>
<script>
const tg=window.Telegram?.WebApp;
if(tg){tg.ready();tg.expand();try{tg.setHeaderColor('secondary_bg_color');tg.setBackgroundColor('bg_color')}catch(e){}}
const state={books:[],categories:[],category:'Все',query:'',cart:{},view:'catalog',selected:null,loading:false};
const ruMoney=n=>new Intl.NumberFormat('ru-RU').format(n)+' сум';
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function toast(t){const el=document.getElementById('toast');el.textContent=t;el.classList.add('show');setTimeout(()=>el.classList.remove('show'),2300);if(tg?.HapticFeedback)tg.HapticFeedback.notificationOccurred('success')}
function setView(viewId){state.view=viewId;document.querySelectorAll('.view').forEach(e=>e.classList.remove('active'));document.getElementById(viewId).classList.add('active');window.scrollTo({top:0,behavior:'smooth'});}
function showCatalog(){setView('catalogView')}
function visibleBooks(){return state.books.filter(b=>(state.category==='Все'||b.genre===state.category)&&(!state.query||`${b.title} ${b.author} ${b.genre} ${b.description}`.toLowerCase().includes(state.query.toLowerCase())))}
function renderChips(){const el=document.getElementById('chips');el.innerHTML=['Все',...state.categories].map(c=>`<button type="button" class="chip ${state.category===c?'active':''}" data-category="${esc(c)}">${esc(c)}</button>`).join('');el.querySelectorAll('[data-category]').forEach(btn=>btn.addEventListener('click',()=>chooseCategory(btn.dataset.category)))}
function chooseCategory(c){state.category=c;renderChips();renderBooks()}
function coverMarkup(book,large=false){const url=String(book.cover_url||'');const safe=/^https?:\/\//i.test(url)?esc(url):'';return `<div class="${large?'detail-cover':'cover'}">${safe?`<img loading="lazy" src="${safe}" alt="Обложка: ${esc(book.title)}" onerror="this.remove()">`:''}<span class="monogram">${esc((book.title||'К').trim().charAt(0).toLocaleUpperCase('ru'))}</span></div>`}
function renderBooks(){const books=visibleBooks();document.getElementById('resultsCount').textContent=books.length+' книг';document.getElementById('sectionHeading').textContent=state.query?'Результаты поиска':(state.category==='Все'?'Выбор редакции':state.category);const grid=document.getElementById('bookGrid');if(!books.length){grid.innerHTML='<div class="empty" style="grid-column:1/-1">Ничего не найдено. Измените запрос или категорию.</div>';return}grid.innerHTML=books.map(b=>`<article class="book"><div onclick="openBook(${b.id})">${coverMarkup(b)}<div class="book-body"><div class="genre">${esc(b.genre)}</div><div class="title">${esc(b.title)}</div><div class="author">${esc(b.author)}</div></div></div><div class="book-body" style="padding-top:0"><div class="price-row"><span class="price">${ruMoney(b.price)}</span><button class="plus" aria-label="Добавить ${esc(b.title)} в корзину" onclick="addCart(${b.id})">+</button></div></div></article>`).join('')}
function openBook(id){const b=state.books.find(x=>x.id===id);if(!b)return;state.selected=b;const el=document.getElementById('detailView');el.innerHTML=`<button class="back" onclick="showCatalog()">← Назад в каталог</button>${coverMarkup(b,true)}<div class="detail"><div class="genre" style="margin-top:16px">${esc(b.genre)}</div><h1>${esc(b.title)}</h1><div class="author">${esc(b.author)}</div><div class="description">${esc(b.description||'Описание скоро появится.')}</div><p class="notice">В наличии: ${b.stock} шт.</p><div class="price-row"><span class="price">${ruMoney(b.price)}</span><button class="primary" style="flex:0 1 auto" onclick="addCart(${b.id})">Добавить в корзину</button></div></div>`;setView('detailView')}
function cartQty(){return Object.values(state.cart).reduce((s,x)=>s+x.qty,0)}
function cartTotal(){return Object.values(state.cart).reduce((s,x)=>s+x.book.price*x.qty,0)}
function renderBadge(){document.getElementById('cartBadge').textContent=cartQty()}
function addCart(id){const b=state.books.find(x=>x.id===id);if(!b)return;if(b.stock<=0){toast('Этой книги сейчас нет в наличии');return}const current=state.cart[id]?.qty||0;if(current>=b.stock){toast('Доступного количества больше нет');return}state.cart[id]={book:b,qty:current+1};renderBadge();toast('Добавлено в корзину')}
function changeQty(id,delta){const item=state.cart[id];if(!item)return;const n=item.qty+delta;if(n<1)delete state.cart[id];else if(n>item.book.stock){toast('Доступного количества больше нет');return}else item.qty=n;renderBadge();renderCart()}
function showCart(){renderCart();setView('cartView')}
function renderCart(){const items=Object.values(state.cart);document.getElementById('cartCount').textContent=cartQty()+' шт.';const holder=document.getElementById('cartItems');if(!items.length){holder.innerHTML='<div class="empty">Корзина пуста.<br>Найдите историю по душе в каталоге.</div>';document.getElementById('checkoutBox').innerHTML='';return}holder.innerHTML=items.map(({book:b,qty})=>`<div class="cartline"><div><b>${esc(b.title)}</b><div class="sub">${esc(b.author)} · ${ruMoney(b.price)} / шт.</div><div style="font-weight:750;margin-top:6px">${ruMoney(b.price*qty)}</div></div><div class="qty"><button onclick="changeQty(${b.id},-1)" aria-label="Уменьшить">−</button><b>${qty}</b><button onclick="changeQty(${b.id},1)" aria-label="Увеличить">+</button></div></div>`).join('');document.getElementById('checkoutBox').innerHTML=`<div class="price-row" style="margin-top:16px"><span>Итого</span><span class="price" style="font-size:20px">${ruMoney(cartTotal())}</span></div><form class="form" onsubmit="submitOrder(event)"><label>Ваше имя<input name="name" maxlength="120" autocomplete="name" required placeholder="Как к вам обращаться?"></label><label>Телефон<input name="phone" maxlength="60" autocomplete="tel" required placeholder="+998 ..."></label><label>Адрес доставки / самовывоза<textarea name="address" maxlength="500" required placeholder="Город, улица, ориентир или самовывоз"></textarea></label><button class="primary" type="submit" ${state.loading?'disabled':''}>${state.loading?'Отправляем…':'Подтвердить заказ'}</button><div class="notice">После оформления продавец свяжется с вами, чтобы подтвердить детали и оплату. Оплата онлайн не подключена.</div></form>`}
async function submitOrder(event){event.preventDefault();if(state.loading)return;const initData=tg?.initData||'';if(!initData){toast('Откройте магазин из Telegram, чтобы оформить заказ');return}const form=new FormData(event.target);const data={name:form.get('name'),phone:form.get('phone'),address:form.get('address'),items:Object.values(state.cart).map(x=>({book_id:x.book.id,quantity:x.qty}))};if(!data.items.length)return;state.loading=true;renderCart();try{const response=await fetch('/api/orders',{method:'POST',headers:{'Content-Type':'application/json','X-Telegram-Init-Data':initData},body:JSON.stringify(data)});const result=await response.json();if(!response.ok)throw new Error(result.detail||'Не удалось оформить заказ');state.cart={};renderBadge();document.getElementById('successText').textContent=`Ваш заказ №${result.order_id} на сумму ${ruMoney(result.total)} зарегистрирован. Продавец свяжется с вами для подтверждения.`;setView('successView');if(tg?.HapticFeedback)tg.HapticFeedback.notificationOccurred('success');}catch(e){toast(e.message||'Ошибка сети');renderCart()}finally{state.loading=false}}
async function load(){try{const r=await fetch('/api/books');if(!r.ok)throw new Error('catalog');state.books=await r.json();state.categories=[...new Set(state.books.map(b=>b.genre))].sort((a,b)=>a.localeCompare(b,'ru'));renderChips();renderBooks();renderBadge()}catch(e){document.getElementById('bookGrid').innerHTML='<div class="empty" style="grid-column:1/-1">Не удалось загрузить каталог. Обновите страницу.</div>'}}
document.getElementById('searchInput').addEventListener('input',e=>{state.query=e.target.value.trim();renderBooks()});load();
</script>
</body></html>'''


@app.get("/", response_class=HTMLResponse)
async def mini_app_home() -> HTMLResponse:
    return HTMLResponse(MINI_APP_HTML)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "bookstore"}


@app.get("/api/books")
async def api_books() -> list[dict[str, Any]]:
    result = []
    for row in active_books():
        result.append({
            "id": int(row["id"]), "title": row["title"], "author": row["author"],
            "genre": row["genre"], "price": int(row["price"]), "stock": int(row["stock"]),
            "description": row["description"], "cover_url": row["cover_url"] if str(row["cover_url"]).startswith(("http://", "https://")) else "",
        })
    return result


class MiniOrderItem(BaseModel):
    book_id: int = Field(gt=0)
    quantity: int = Field(gt=0, le=50)


class MiniOrder(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    phone: str = Field(min_length=5, max_length=60)
    address: str = Field(min_length=3, max_length=500)
    items: list[MiniOrderItem] = Field(min_length=1, max_length=30)


def validate_telegram_init_data(init_data: str) -> dict[str, Any] | None:
    """Validate Telegram.WebApp.initData using Telegram's HMAC-SHA-256 scheme."""
    if not BOT_TOKEN or not init_data:
        return None
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=False))
        received_hash = parsed.pop("hash", "")
        # The optional third-party Ed25519 signature is not part of the bot-token hash check.
        parsed.pop("signature", None)
        if not received_hash:
            return None
        check_string = "\n".join(f"{key}={value}" for key, value in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated_hash, received_hash):
            return None
        auth_date = int(parsed.get("auth_date", "0"))
        now = int(time.time())
        if auth_date <= 0 or auth_date > now + 60 or now - auth_date > 24 * 60 * 60:
            return None
        user = json.loads(parsed.get("user", "{}"))
        if not isinstance(user, dict) or not isinstance(user.get("id"), int):
            return None
        return user
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


@app.post("/api/orders")
async def api_create_order(
    payload: MiniOrder,
    x_telegram_init_data: str = Header(default=""),
) -> dict[str, Any]:
    user = validate_telegram_init_data(x_telegram_init_data)
    if not user:
        raise HTTPException(status_code=401, detail="Авторизация Telegram недействительна. Закройте и снова откройте Mini App.")
    items_map: dict[int, int] = {}
    for item in payload.items:
        items_map[item.book_id] = items_map.get(item.book_id, 0) + item.quantity
    try:
        user_id = int(user["id"])
        order_id, total = create_order(
            user_id, payload.name, payload.phone, payload.address,
            list(items_map.items()),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("Не удалось оформить заказ из Mini App")
        raise HTTPException(status_code=500, detail="Не удалось сохранить заказ. Попробуйте ещё раз.") from exc
    if bot:
        await notify_new_order(order_id)
        try:
            await bot.send_message(
                user_id,
                f"🎉 <b>Заказ №{order_id} оформлен!</b>\nСумма: <b>{money(total)}</b>\n"
                "Продавец свяжется с вами для подтверждения доставки и оплаты.",
            )
        except Exception as exc:
            log.info("Заказ создан, но уведомление покупателю не отправлено: %s", exc)
    return {"ok": True, "order_id": order_id, "total": total}


# -----------------------------------------------------------------------------
# Run both the public web app and Telegram long-polling in one Render process.
# -----------------------------------------------------------------------------
async def main() -> None:
    global bot
    if not BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN is not set. Windows PowerShell: $env:BOT_TOKEN='TOKEN_FROM_BOTFATHER'\n"
            "Linux/macOS: export BOT_TOKEN='TOKEN_FROM_BOTFATHER'"
        )
    init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    commands = [
        BotCommand(command="start", description="Открыть магазин"),
        BotCommand(command="catalog", description="Каталог книг"),
        BotCommand(command="search", description="Найти книгу"),
        BotCommand(command="cart", description="Корзина"),
        BotCommand(command="favorites", description="Избранное"),
        BotCommand(command="orders", description="Мои заказы"),
        BotCommand(command="help", description="Помощь"),
    ]
    await bot.set_my_commands(commands)
    if WEBAPP_URL.startswith("https://"):
        try:
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Книжный магазин", web_app=WebAppInfo(url=WEBAPP_URL)))
            log.info("Mini App URL: %s", WEBAPP_URL)
        except Exception as exc:
            log.warning("Не удалось назначить Mini App кнопкой меню: %s", exc)
    else:
        log.warning("WEBAPP_URL not set to HTTPS. Bot commands work, but Telegram Mini App button is hidden.")
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS is empty: admin commands and order notifications will be unavailable.")
    await bot.delete_webhook(drop_pending_updates=False)
    log.info("Starting bookshop bot and HTTP server on 0.0.0.0:%s", PORT)
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="info"))
    try:
        await asyncio.gather(
            server.serve(),
            dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types()),
        )
    finally:
        if bot:
            await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Остановка по Ctrl+C")
