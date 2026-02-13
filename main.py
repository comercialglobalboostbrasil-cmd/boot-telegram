import os
import json
import sqlite3
import asyncio
import base64
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
# ENV / CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
INVICTUS_API_TOKEN = os.getenv("INVICTUS_API_TOKEN")
POSTBACK_URL = os.getenv("POSTBACK_URL")

PRICE_CENTS = int(os.getenv("PRICE_CENTS", "2990"))  # 2990 = R$29,90
OFFER_HASH = os.getenv("OFFER_HASH", "")
PRODUCT_HASH = os.getenv("PRODUCT_HASH", "")

FIXED_NAME = os.getenv("FIXED_NAME", "Cliente VIP")
FIXED_EMAIL = os.getenv("FIXED_EMAIL", "cliente@exemplo.com")
FIXED_PHONE = os.getenv("FIXED_PHONE", "11999999999")
FIXED_DOCUMENT = os.getenv("FIXED_DOCUMENT", "00000000000")

# Acesso ao grupo:
GROUP_INVITE_LINK = os.getenv("GROUP_INVITE_LINK")  # link fixo (mais simples)
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID")          # -100... (melhor: link temporário)

APP_HOST = os.getenv("APP_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("PORT", "10000"))

DB_PATH = "db.sqlite3"

# Checagens mínimas
if not BOT_TOKEN:
    raise RuntimeError("Faltou BOT_TOKEN nas variáveis de ambiente.")
if not INVICTUS_API_TOKEN:
    raise RuntimeError("Faltou INVICTUS_API_TOKEN nas variáveis de ambiente.")
if not POSTBACK_URL:
    raise RuntimeError("Faltou POSTBACK_URL nas variáveis de ambiente.")
if not OFFER_HASH or not PRODUCT_HASH:
    raise RuntimeError("Faltou OFFER_HASH e/ou PRODUCT_HASH nas variáveis de ambiente.")


# =========================
# DATABASE (SQLite)
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
            status TEXT NOT NULL DEFAULT 'inactive',
            expires_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            invictus_tx_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            raw_response TEXT
        )
    """)
    conn.commit()
    conn.close()

def get_user(telegram_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT status, expires_at FROM users WHERE telegram_id=?", (telegram_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return ("inactive", None)
    return row[0], row[1]

def set_user_active(telegram_id: int, expires_at: datetime):
    conn = db()
    conn.execute(
        "INSERT INTO users(telegram_id, status, expires_at) VALUES(?,?,?) "
        "ON CONFLICT(telegram_id) DO UPDATE SET status=excluded.status, expires_at=excluded.expires_at",
        (telegram_id, "active", expires_at.isoformat()),
    )
    conn.commit()
    conn.close()

def set_user_inactive(telegram_id: int):
    conn = db()
    conn.execute(
        "INSERT INTO users(telegram_id, status, expires_at) VALUES(?,?,?) "
        "ON CONFLICT(telegram_id) DO UPDATE SET status=excluded.status, expires_at=NULL",
        (telegram_id, "inactive"),
    )
    conn.commit()
    conn.close()

def save_transaction(telegram_id: int, invictus_tx_id: str | None, status: str, raw_response: dict):
    conn = db()
    conn.execute(
        "INSERT INTO transactions(telegram_id, invictus_tx_id, status, created_at, raw_response) VALUES(?,?,?,?,?)",
        (telegram_id, invictus_tx_id, status, datetime.now(timezone.utc).isoformat(), json.dumps(raw_response)),
    )
    conn.commit()
    conn.close()

def update_transaction_status(invictus_tx_id: str, status: str):
    conn = db()
    conn.execute("UPDATE transactions SET status=? WHERE invictus_tx_id=?", (status, invictus_tx_id))
    conn.commit()
    conn.close()

def find_telegram_by_tx(invictus_tx_id: str):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT telegram_id FROM transactions WHERE invictus_tx_id=? ORDER BY id DESC LIMIT 1",
        (invictus_tx_id,)
    )
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


# =========================
# INVICTUS API
# =========================
def invictus_create_pix_transaction(telegram_id: int):
    """
    Cria uma transação Pix usando SEMPRE os mesmos dados FIXOS.
    Retorna:
      - resp_json
      - tx_id (id/uuid/etc)
      - pix_payload (copia e cola)
      - qr_base64 (se vier)
    """
    url = f"https://api.invictuspay.app.br/api/public/v1/transactions?api_token={INVICTUS_API_TOKEN}&postback_url={POSTBACK_URL}"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}

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
        "cart": [
            {
                "product_hash": PRODUCT_HASH,
                "title": "Acesso VIP - 30 dias",
                "price": PRICE_CENTS,
                "quantity": 1,
                "operation_type": 1,
                "tangible": False
            }
        ],
        "expire_in_days": 1,
        "tracking": {"telegram_id": telegram_id}
    }

    r = requests.post(url, headers=headers, json=data, timeout=25)
    r.raise_for_status()
    resp = r.json()

    # Heurísticas comuns (ajuste se necessário com base no JSON real)
    tx_id = str(resp.get("id") or resp.get("transaction_id") or resp.get("uuid") or "")

    # Pix copia e cola (payload)
    pix_payload = (
        resp.get("pix_code")
        or resp.get("pix_copia_cola")
        or resp.get("qr_code")          # às vezes vem aqui
        or resp.get("emv")
        or (resp.get("pix") or {}).get("code")
        or (resp.get("pix") or {}).get("emv")
        or (resp.get("pix") or {}).get("copy_and_paste")
    )

    # QR base64 (se vier pronto)
    qr_base64 = (
        resp.get("qr_code_base64")
        or resp.get("pix_qr_code_base64")
        or (resp.get("pix") or {}).get("qr_code_base64")
        or (resp.get("pix") or {}).get("qr_base64")
    )

    return resp, (tx_id if tx_id else None), pix_payload, qr_base64


def qr_image_from_payload(payload: str) -> BytesIO:
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = BytesIO()
    bio.name = "pix_qr.png"
    img.save(bio, format="PNG")
    bio.seek(0)
    return bio


# =========================
# TELEGRAM BOT
# =========================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

def menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Gerar Pix (30 dias)", callback_data="pay")],
        [InlineKeyboardButton(text="📌 Ver status", callback_data="status")]
    ])

@dp.message(CommandStart())
async def start(message: types.Message):
    await message.answer(
        f"🔥 Acesso VIP (30 dias)\n\n"
        f"💰 Valor: R$ {PRICE_CENTS/100:.2f}\n"
        f"✅ Liberação automática após pagamento.\n\n"
        f"Clique abaixo para gerar seu Pix:",
        reply_markup=menu_keyboard()
    )

@dp.message(Command("status"))
async def status_cmd(message: types.Message):
    status, expires_at = get_user(message.from_user.id)
    if status == "active" and expires_at:
        await message.answer(f"✅ Seu acesso está ATIVO até:\n📅 {expires_at}")
    else:
        await message.answer("⚠️ Você está sem acesso ativo.\nUse /start para gerar o Pix e renovar.")

@dp.callback_query(lambda c: c.data == "status")
async def status_cb(call: types.CallbackQuery):
    status, expires_at = get_user(call.from_user.id)
    if status == "active" and expires_at:
        await call.message.answer(f"✅ Seu acesso está ATIVO até:\n📅 {expires_at}")
    else:
        await call.message.answer("⚠️ Você está sem acesso ativo.\nClique em “Gerar Pix” para liberar.")
    await call.answer()

@dp.callback_query(lambda c: c.data == "pay")
async def pay_cb(call: types.CallbackQuery):
    telegram_id = call.from_user.id
    try:
        resp, tx_id, pix_payload, qr_base64 = invictus_create_pix_transaction(telegram_id)

        save_transaction(telegram_id, tx_id, "pending", resp)

        # Envia QR (preferência: base64 retornado; senão gera pelo payload)
        sent_qr = False
        if isinstance(qr_base64, str) and len(qr_base64) > 200:
            try:
                img_bytes = base64.b64decode(qr_base64)
                bio = BytesIO(img_bytes)
                bio.name = "pix_qr.png"
                bio.seek(0)
                await bot.send_photo(call.message.chat.id, photo=bio, caption="📌 QR Code Pix (desta transação)")
                sent_qr = True
            except Exception:
                sent_qr = False

        if (not sent_qr) and pix_payload:
            bio = qr_image_from_payload(pix_payload)
            await bot.send_photo(call.message.chat.id, photo=bio, caption="📌 QR Code Pix (desta transação)")

        # Envia SOMENTE a chave Pix (copia e cola)
        if pix_payload:
            await call.message.answer(
                f"📋 Pix Copia e Cola:\n`{pix_payload}`\n\n"
                "✅ Assim que o pagamento for confirmado, eu libero seu acesso automaticamente.",
                parse_mode="Markdown"
            )
        else:
            await call.message.answer(
                "⚠️ A API não retornou o Pix Copia e Cola em um campo reconhecido.\n"
                "Abra os logs do Render e veja o JSON da resposta para mapear o campo certo."
            )

        await call.answer()

    except requests.HTTPError:
        await call.message.answer("❌ Erro ao gerar Pix. Verifique API token / offer_hash / product_hash.")
        await call.answer()
    except Exception:
        await call.message.answer("❌ Falha inesperada ao gerar Pix. Tente novamente.")
        await call.answer()


# =========================
# FASTAPI POSTBACK (WEBHOOK)
# =========================
app = FastAPI()

@app.post("/invictus/postback")
async def invictus_postback(request: Request):
    """
    Recebe postback da Invictus.
    Como o schema pode variar, loga o payload e tenta extrair:
      - tx_id
      - status
    Se status for pago/aprovado, libera 30 dias.
    """
    payload = await request.json()
    print("INVICTUS POSTBACK:", json.dumps(payload, ensure_ascii=False))

    # extrai tx_id e status com heurística
    tx_id = str(payload.get("id") or payload.get("transaction_id") or payload.get("uuid") or "")
    status = (payload.get("status") or payload.get("payment_status") or payload.get("state") or "").lower()

    if not tx_id and isinstance(payload.get("data"), dict):
        d = payload["data"]
        tx_id = str(d.get("id") or d.get("transaction_id") or d.get("uuid") or "")
        status = (d.get("status") or d.get("payment_status") or d.get("state") or "").lower()

    if tx_id:
        update_transaction_status(tx_id, status or "unknown")

    approved_values = {"approved", "paid", "confirmed", "completed", "success", "aprovado", "pago"}

    if tx_id and status in approved_values:
        telegram_id = find_telegram_by_tx(tx_id)

        # fallback pelo tracking
        if not telegram_id:
            tracking = payload.get("tracking") or (payload.get("data") or {}).get("tracking")
            if isinstance(tracking, dict) and tracking.get("telegram_id"):
                telegram_id = int(tracking["telegram_id"])

        if telegram_id:
            expires_at = datetime.now(timezone.utc) + timedelta(days=30)
            set_user_active(int(telegram_id), expires_at)

            try:
                if GROUP_INVITE_LINK:
                    await bot.send_message(
                        int(telegram_id),
                        "✅ Pagamento confirmado!\n\n"
                        f"Aqui está seu acesso VIP:\n{GROUP_INVITE_LINK}\n\n"
                        f"📅 Válido até: {expires_at.date().isoformat()} (UTC)"
                    )
                elif GROUP_CHAT_ID:
                    invite = await bot.create_chat_invite_link(
                        chat_id=int(GROUP_CHAT_ID),
                        member_limit=1,
                        expire_date=int((datetime.now(timezone.utc) + timedelta(minutes=30)).timestamp())
                    )
                    await bot.send_message(
                        int(telegram_id),
                        "✅ Pagamento confirmado!\n\n"
                        f"Aqui está seu link (expira em 30 min):\n{invite.invite_link}\n\n"
                        f"📅 Válido até: {expires_at.date().isoformat()} (UTC)"
                    )
                else:
                    await bot.send_message(
                        int(telegram_id),
                        "✅ Pagamento confirmado!\n\n"
                        "Acesso liberado, mas falta configurar GROUP_INVITE_LINK ou GROUP_CHAT_ID no servidor."
                    )
            except Exception as e:
                print("Erro ao liberar acesso:", e)

    return {"ok": True}


# =========================
# EXPIRAÇÃO (RENOVAÇÃO)
# =========================
async def expiration_job():
    while True:
        try:
            conn = db()
            cur = conn.cursor()
            cur.execute("SELECT telegram_id, expires_at FROM users WHERE status='active' AND expires_at IS NOT NULL")
            rows = cur.fetchall()
            conn.close()

            now = datetime.now(timezone.utc)
            for telegram_id, expires_at in rows:
                try:
                    exp = datetime.fromisoformat(expires_at)
                    if exp < now:
                        set_user_inactive(int(telegram_id))
                        await bot.send_message(
                            int(telegram_id),
                            "⚠️ Seu acesso VIP expirou.\n\n"
                            f"💰 Renovação: R$ {PRICE_CENTS/100:.2f} / 30 dias\n"
                            "Use /start para gerar um novo Pix."
                        )
                except Exception:
                    continue
        except Exception as e:
            print("expiration_job error:", e)

        await asyncio.sleep(600)  # 10 min


# =========================
# RUN BOT + API
# =========================
async def start_all():
    init_db()

    bot_task = asyncio.create_task(dp.start_polling(bot))
    exp_task = asyncio.create_task(expiration_job())

    config = uvicorn.Config(app, host=APP_HOST, port=APP_PORT, log_level="info")
    server = uvicorn.Server(config)
    api_task = asyncio.create_task(server.serve())

    await asyncio.gather(bot_task, exp_task, api_task)

if __name__ == "__main__":
    asyncio.run(start_all())
