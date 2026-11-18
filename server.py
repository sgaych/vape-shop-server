import hmac
import hashlib
import json
import os
import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl

import asyncpg
import httpx
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_KEY = os.environ["API_KEY"]  # секрет между ботом и сервером
DATABASE_URL = os.environ["DATABASE_URL"]
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]

CATEGORIES = {"Жидкости", "Устройства", "Обслуга"}
MAX_QTY = 20
MAX_PHOTO = 5 * 1024 * 1024
RESERVE_HOURS = 24  # непринятая бронь снимается через N часов

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    price INT NOT NULL CHECK (price > 0),
    category TEXT NOT NULL,
    img TEXT NOT NULL DEFAULT '',
    active BOOL NOT NULL DEFAULT TRUE
);
CREATE TABLE IF NOT EXISTS carts (
    user_id BIGINT PRIMARY KEY,
    items JSONB NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS orders (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    username TEXT,
    first_name TEXT,
    items JSONB NOT NULL,
    total INT NOT NULL,
    meet TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


async def _init_conn(conn):
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, init=_init_conn)
    async with app.state.pool.acquire() as conn:
        await conn.execute(SCHEMA)
    yield
    await app.state.pool.close()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type", "X-Init-Data"],
)


# ---------- авторизация ----------
def verify_init_data(init_data: str) -> dict:
    """Проверка подписи Telegram initData (HMAC), возвращает dict пользователя."""
    if not init_data:
        raise HTTPException(401, "Нет initData")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", None)
    if not received:
        raise HTTPException(401, "Нет подписи")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received):
        raise HTTPException(401, "Неверная подпись")
    try:
        if time.time() - int(pairs.get("auth_date", "0")) > 86400:
            raise HTTPException(401, "initData устарел")
        return json.loads(pairs["user"])
    except (ValueError, KeyError):
        raise HTTPException(401, "Некорректные данные")


def current_user(x_init_data: str = Header("")) -> dict:
    return verify_init_data(x_init_data)


def admin_user(user: dict = Depends(current_user)) -> dict:
    if user["id"] not in ADMIN_IDS:
        raise HTTPException(403, "Только для админа")
    return user


def bot_only(x_api_key: str = Header("")):
    if not hmac.compare_digest(x_api_key.encode(), API_KEY.encode()):
        raise HTTPException(401, "Неверный ключ")


# ---------- модели ----------
class Line(BaseModel):
    id: int
    qty: int = Field(ge=1, le=MAX_QTY)


class CartIn(BaseModel):
    items: list[Line] = Field(max_length=50)


class OrderIn(BaseModel):
    user_id: int
    username: str | None = None
    first_name: str | None = None
    meet: str = Field("", max_length=200)
    items: list[Line] = Field(min_length=1, max_length=50)


class StatusIn(BaseModel):
    status: str


# ---------- вспомогательное ----------
def merge(items: list[Line]) -> dict[int, int]:
    merged: dict[int, int] = {}
    for it in items:
        merged[it.id] = min(merged.get(it.id, 0) + it.qty, MAX_QTY)
    return merged


async def price_lines(conn, merged: dict[int, int], strict: bool) -> list[dict]:
    """Цены и названия берём ТОЛЬКО из базы."""
    rows = await conn.fetch(
        "SELECT id, name, price, img, category FROM products WHERE id = ANY($1) AND active",
        list(merged),
    )
    found = {r["id"]: r for r in rows}
    lines = []
    for pid, qty in merged.items():
        r = found.get(pid)
        if not r:
            if strict:
                raise HTTPException(400, "Один из товаров больше недоступен, обновите каталог")
            continue
        lines.append({"id": pid, "name": r["name"], "price": r["price"],
                      "img": r["img"], "category": r["category"], "qty": qty})
    return lines


def order_out(r) -> dict:
    return {
        "id": r["id"], "user_id": r["user_id"], "username": r["username"],
        "first_name": r["first_name"], "meet": r["meet"], "lines": r["items"],
        "total": r["total"], "status": r["status"],
    }


# ---------- товары ----------
@app.get("/")
async def root():
    return {"message": "ok"}


@app.get("/products")
async def get_products():
    async with app.state.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, name, price, category, img FROM products WHERE active ORDER BY id"
        )
    return {"products": [dict(r) for r in rows]}


@app.post("/products/add")
async def add_product(
    name: str = Form(..., min_length=1, max_length=100),
    price: int = Form(..., gt=0, le=1_000_000),
    category: str = Form(...),
    photo: UploadFile | None = File(None),
    _admin: dict = Depends(admin_user),
):
    if category not in CATEGORIES:
        raise HTTPException(400, "Неизвестная категория")

    img = ""
    if photo is not None and photo.filename:
        if not (photo.content_type or "").startswith("image/"):
            raise HTTPException(400, "Нужна картинка")
        data = await photo.read()
        if len(data) > MAX_PHOTO:
            raise HTTPException(400, "Фото больше 5 МБ")
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://catbox.moe/user/api.php",
                    data={"reqtype": "fileupload"},
                    files={"fileToUpload": (photo.filename, data, photo.content_type)},
                )
            url = resp.text.strip()
        except httpx.HTTPError:
            raise HTTPException(502, "Хостинг фото недоступен")
        if resp.status_code != 200 or not url.startswith("https://"):
            raise HTTPException(502, "Не удалось загрузить фото")
        img = url

    async with app.state.pool.acquire() as conn:
        pid = await conn.fetchval(
            "INSERT INTO products (name, price, category, img) VALUES ($1, $2, $3, $4) RETURNING id",
            name.strip(), price, category, img,
        )
    return {"status": "ok", "id": pid}


@app.delete("/products/{product_id}")
async def delete_product(product_id: int, _admin: dict = Depends(admin_user)):
    async with app.state.pool.acquire() as conn:
        await conn.execute("UPDATE products SET active = FALSE WHERE id = $1", product_id)
    return {"status": "ok"}


# ---------- корзина (клиент, по подписи initData) ----------
@app.get("/cart")
async def get_cart(user: dict = Depends(current_user)):
    async with app.state.pool.acquire() as conn:
        stored = await conn.fetchval("SELECT items FROM carts WHERE user_id = $1", user["id"]) or []
        merged = {int(i["id"]): int(i["qty"]) for i in stored}
        items = await price_lines(conn, merged, strict=False) if merged else []
    return {"items": items}


@app.post("/cart")
async def update_cart(data: CartIn, user: dict = Depends(current_user)):
    merged = merge(data.items)
    stored = [{"id": pid, "qty": qty} for pid, qty in merged.items()]
    async with app.state.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO carts (user_id, items) VALUES ($1, $2) "
            "ON CONFLICT (user_id) DO UPDATE SET items = EXCLUDED.items",
            user["id"], stored,
        )
    return {"status": "ok"}


# ---------- брони (только бот, по API-ключу) ----------
@app.post("/orders", dependencies=[Depends(bot_only)])
async def create_order(data: OrderIn):
    async with app.state.pool.acquire() as conn:
        async with conn.transaction():
            lines = await price_lines(conn, merge(data.items), strict=True)
            total = sum(l["price"] * l["qty"] for l in lines)
            stored = [{k: l[k] for k in ("id", "name", "price", "qty")} for l in lines]
            row = await conn.fetchrow(
                "INSERT INTO orders (user_id, username, first_name, items, total, meet) "
                "VALUES ($1, $2, $3, $4, $5, $6) RETURNING *",
                data.user_id, data.username, data.first_name, stored, total, data.meet.strip(),
            )
            await conn.execute("DELETE FROM carts WHERE user_id = $1", data.user_id)
    return order_out(row)


@app.post("/orders/{order_id}/status", dependencies=[Depends(bot_only)])
async def set_status(order_id: int, data: StatusIn):
    if data.status not in {"confirmed", "done", "cancelled"}:
        raise HTTPException(400, "Неверный статус")
    async with app.state.pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM orders WHERE id = $1", order_id)
        if not row:
            raise HTTPException(404, "Нет такой брони")
        if row["status"] in {"done", "cancelled", "expired"}:
            raise HTTPException(409, "Бронь уже закрыта")
        row = await conn.fetchrow(
            "UPDATE orders SET status = $2 WHERE id = $1 RETURNING *", order_id, data.status
        )
    return order_out(row)


@app.post("/orders/expire", dependencies=[Depends(bot_only)])
async def expire_orders():
    async with app.state.pool.acquire() as conn:
        rows = await conn.fetch(
            "UPDATE orders SET status = 'expired' "
            "WHERE status = 'new' AND created_at < now() - make_interval(hours => $1) "
            "RETURNING *",
            RESERVE_HOURS,
        )
    return {"expired": [order_out(r) for r in rows]}
