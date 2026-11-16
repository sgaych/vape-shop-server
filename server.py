from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Dict

app = FastAPI()

# --- ИМИТАЦИЯ БАЗЫ ДАННЫХ ---
# В реальном проекте тут будет SQLite или PostgreSQL
# Формат: {user_id: [ {id: 1, name: "...", price: 100, qty: 2} ]}
carts_db: Dict[int, List[dict]] = {}

# Модель данных, которые присылает сайт
class CartItem(BaseModel):
    id: int
    name: str
    price: int
    qty: int

class CartUpdate(BaseModel):
    user_id: int
    items: List[CartItem]

# 1. Получить корзину пользователя (для сайта и бота)
@app.get("/cart/{user_id}")
async def get_cart(user_id: int):
    return {"items": carts_db.get(user_id, [])}

# 2. Обновить корзину (когда пользователь что-то добавил на сайте)
@app.post("/cart/update")
async def update_cart(data: CartUpdate):
    carts_db[data.user_id] = [item.dict() for item in data.items]
    return {"status": "ok", "message": "Корзина обновлена"}

# 3. Очистить корзину (после заказа)
@app.delete("/cart/{user_id}")
async def clear_cart(user_id: int):
    if user_id in carts_db:
        del carts_db[user_id]
    return {"status": "ok"}

# Тестовый эндпоинт (чтобы Render не ругался)
@app.get("/")
async def root():
    return {"message": "Vape Shop Server is running"}