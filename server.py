from fastapi import FastAPI
from pydantic import BaseModel
from typing import List, Dict
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- БАЗА ДАННЫХ ---
carts_db: Dict[int, List[dict]] = {}

# Стартовые товары (можешь добавить свои через бота)
products_db = [
    {"id": 1, "name": "Ведьмак: Малина 90мг", "price": 400, "category": "Жидкости", "img": "https://vapenews.ru/uploads/images/news/2024/02/ultra-hard.jpg"},
]

class CartItem(BaseModel):
    id: int
    name: str
    price: int
    qty: int

class CartUpdate(BaseModel):
    user_id: int
    items: List[CartItem]

class NewProduct(BaseModel):
    name: str
    price: int
    category: str
    img: str

@app.get("/products")
async def get_products():
    return {"products": products_db}

@app.post("/products/add")
async def add_product(product: NewProduct):
    new_id = max([p["id"] for p in products_db]) + 1 if products_db else 1
    new_prod = {"id": new_id, "name": product.name, "price": product.price, "category": product.category, "img": product.img}
    products_db.append(new_prod)
    return {"status": "ok", "product": new_prod}

@app.get("/cart/{user_id}")
async def get_cart(user_id: int):
    return {"items": carts_db.get(user_id, [])}

@app.post("/cart/update")
async def update_cart(data: CartUpdate):
    carts_db[data.user_id] = [item.model_dump() for item in data.items]
    return {"status": "ok"}

@app.delete("/cart/{user_id}")
async def clear_cart(user_id: int):
    if user_id in carts_db:
        del carts_db[user_id]
    return {"status": "ok"}

@app.get("/")
async def root():
    return {"message": "Server is running"}