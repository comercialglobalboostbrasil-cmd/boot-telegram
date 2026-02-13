import os
import json
import sqlite3
import asyncio
import base64
import re
from datetime import datetime, timedelta, timezone
from io import BytesIO

import requests
import qrcode

from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart, Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from fastapi import FastAPI, Request
import uvicorn

# =========================
# ENV CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
INVICTUS_API_TOKEN = os.getenv("INVICTUS_API_TOKEN")
POSTBACK_URL = os.getenv("POSTBACK_URL")

PRICE_CENTS = int(os.getenv("PRICE_CENTS", "2990"))
OFFER_HASH = os.getenv("OFFER_HASH")
PRODUCT_HASH = os.getenv("PRODUCT_HASH")

FIXED_NAME = os.getenv("FIXED_NAME")
FIXED_EMAIL = os.getenv("FIXED_EMAIL")
FIXED_PHONE = os.getenv("FIXED_PHONE")
FIXED_DOCUMENT = os.getenv("FIXED_DOCUMENT")

GROUP_INVITE_LINK = os.getenv("GROUP_INVITE_LINK")
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID")

APP_PORT = int(os.getenv("PORT", "10000"))
DB_PATH = "db.sqlite3"

# =========================
# DATABASE
# =========================
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            status TEXT DEFAULT 'inactive',
            expires_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            invictus_tx_id TEXT,
            status TEXT,
            raw_response TEXT
        )
    """)
    conn.commit()
    conn.close()

def save_transaction(telegram_id, tx_id, status, raw):
    conn = db()
    conn.execute(
        "INSERT INTO transactions (telegram_id, invictus_tx_id, status, raw_response) VALUES (?, ?, ?, ?)",
        (telegram_id, tx_id, status, json.dumps(raw))
    )
    conn.commit()
    conn.close()

def find_telegram_by_tx(tx_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT telegram_id FROM transactions WHERE invictus_tx_id=? ORDER BY id DESC LIMIT 1", (tx_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None

def set_user_active(telegram_id):
    expires = datetime.now(timezone.utc) + timedelta(days=30)
    conn = db()
    conn.execute("""
        INSERT INTO users (telegram_id, status, expires_at)
        VALUES (?, 'active', ?)
        ON CONFLICT(telegram_id)
        DO UPDATE SET status='active', expires_at=excluded.expires_at
    """, (telegram_id, expires.isoformat()))
    conn.commit()
    conn.close()
    return expires

def set_user_inactive(telegram_id):
    conn = db()
    conn.execute("""
        UPDATE users SET status='inactive', expires_at=NULL WHERE telegram_id=?
    """, (telegram_id,))
    conn.commit()
    conn.close()

# =========================
# PIX EXTRACTOR AUTOMÁTICO
# =========================
def walk_values(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from walk_values(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from walk_values(item)
    else:
        yield obj

def find_pix_emv(resp_json):
    for v in walk_values(resp_json):
        if isinstance(v, str):
            s = v.strip()
            if s.startswith("000201") and len(s) > 50:
                return s
    return None

def find_qr_base64(resp_json):
    for v in walk_values(resp_json):
        if isinstance(v, str) and len(v) > 300:
            return v
    return None

def generate_qr(payload):
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = BytesIO()
    bio.name = "pix.png"
    img.save(bio, format="PNG")
    bio.seek(0)
    return bio

# =========================
# INVICTUS API
# =========================
def create_pix(telegram_id):
    url = f"https://api.invictuspay.app.br/api/public/v1/transactions?api_token={INVICTUS_API_TOKEN}&postback_url={POSTBACK_URL}"

    data = {
        "amount": PRICE_CENTS,
        "offer_hash": OFFER_HASH,
        "payment_method": "pix",
        "customer": {
            "name": FIXED_NAME,
            "email": FIXED_EMAIL,
            "phone_number": FIXED_PHONE,
            "document": FIXED_DOCUMENT
        },
        "cart": [{
            "product_hash": PRODUCT_HASH,
            "title": "Acesso VIP",
            "price": PRICE_CENTS,
            "quantity": 1,
            "operation_type": 1,
            "tangible": False
        }],
        "tracking": {"telegram_id": telegram_id}
    }

    r = requests.post(url, json=data)
    r.raise_for_status()
    resp = r.json()

    tx_id = str(resp.get("id") or resp.get("transaction_id") or resp.get("uuid") or "")

    pix_payload = find_pix_emv(resp)
    qr_base64 = find_qr_base64(resp)

    return resp, tx_id, pix_payload, qr_base64

# =========================
# TELEGRAM BOT
# =========================
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

@dp.message(CommandStart())
async def start(message: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Gerar Pix (30 dias)", callback_data="pay")]
    ])
    await message.answer(
        f"Acesso VIP 30 dias\nValor: R$ {PRICE_CENTS/100:.2f}",
        reply_markup=kb
    )

@dp.callback_query(lambda c: c.data == "pay")
async def pay(call: types.CallbackQuery):
    telegram_id = call.from_user.id
    resp, tx_id, pix_payload, qr_base64 = create_pix(telegram_id)

    save_transaction(telegram_id, tx_id, "pending", resp)

    if qr_base64:
        try:
            img_bytes = base64.b64decode(qr_base64)
            bio = BytesIO(img_bytes)
            bio.name = "pix.png"
            bio.seek(0)
            await bot.send_photo(call.message.chat.id, bio, caption="QR Code Pix")
        except:
            pass

    if pix_payload:
        if not qr_base64:
            bio = generate_qr(pix_payload)
            await bot.send_photo(call.message.chat.id, bio, caption="QR Code Pix")
        await call.message.answer(f"Pix Copia e Cola:\n`{pix_payload}`", parse_mode="Markdown")
    else:
        await call.message.answer("Erro: Pix não encontrado na resposta da API.")

    await call.answer()

# =========================
# WEBHOOK
# =========================
app = FastAPI()

@app.post("/invictus/postback")
async def postback(request: Request):
    payload = await request.json()
    print("POSTBACK:", payload)

    tx_id = str(payload.get("id") or payload.get("transaction_id") or payload.get("uuid") or "")
    status = (payload.get("status") or "").lower()

    if tx_id and status in ["approved", "paid", "confirmed", "completed", "pago", "aprovado"]:
        telegram_id = find_telegram_by_tx(tx_id)
        if telegram_id:
            expires = set_user_active(telegram_id)
            if GROUP_INVITE_LINK:
                await bot.send_message(
                    telegram_id,
                    f"Pagamento confirmado!\nAcesso liberado:\n{GROUP_INVITE_LINK}\nVálido até {expires.date()}"
                )

    return {"ok": True}

# =========================
# START
# =========================
async def main():
    init_db()
    config = uvicorn.Config(app, host="0.0.0.0", port=APP_PORT, log_level="info")
    server = uvicorn.Server(config)

    await asyncio.gather(
        dp.start_polling(bot),
        server.serve()
    )

if __name__ == "__main__":
    asyncio.run(main())
