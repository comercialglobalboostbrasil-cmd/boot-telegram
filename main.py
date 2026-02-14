import os
import json
import sqlite3
import asyncio
import base64
import re
import logging
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Any, Optional, Tuple

import requests
import qrcode

from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart, Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from fastapi import FastAPI, Request
import uvicorn


# =========================================================
# LOGGING (aparece no Render)
# =========================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vip-bot")
log.info("APP SUBIU (main.py carregou)")


# =========================================================
# ENV VARS
# =========================================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
INVICTUS_API_TOKEN = os.getenv("INVICTUS_API_TOKEN")
POSTBACK_URL = os.getenv("POSTBACK_URL")

PRICE_CENTS = int(os.getenv("PRICE_CENTS", "2990"))
OFFER_HASH = os.getenv("OFFER_HASH", "")
PRODUCT_HASH = os.getenv("PRODUCT_HASH", "")

FIXED_NAME = os.getenv("FIXED_NAME", "Cliente VIP")
FIXED_EMAIL = os.getenv("FIXED_EMAIL", "cliente@exemplo.com")
FIXED_PHONE = os.getenv("FIXED_PHONE", "11999999999")
FIXED_DOCUMENT = os.getenv("FIXED_DOCUMENT", "00000000000")

GROUP_INVITE_LINK = os.getenv("GROUP_INVITE_LINK")  # link fixo (mais simples)
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID")          # -100... (melhor: link temporário)

APP_HOST = os.getenv("APP_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("PORT", "10000"))

DB_PATH = "db.sqlite3"

# validações mínimas
missing = []
if not BOT_TOKEN: missing.append("BOT_TOKEN")
if not INVICTUS_API_TOKEN: missing.append("INVICTUS_API_TOKEN")
if not POSTBACK_URL: missing.append("POSTBACK_URL")
if not OFFER_HASH: missing.append("OFFER_HASH")
if not PRODUCT_HASH: missing.append("PRODUCT_HASH")
if missing:
    raise RuntimeError(f"Faltam variáveis no Render Environment: {', '.join(missing)}")


# =========================================================
# DB (SQLite)
# =========================================================
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

def get_user(telegram_id: int) -> Tuple[str, Optional[str]]:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT status, expires_at FROM users WHERE telegram_id=?", (telegram_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return "inactive", None
    return row[0], row[1]

def set_user_active(telegram_id: int) -> datetime:
    expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    conn = db()
    conn.execute(
        "INSERT INTO users(telegram_id, status, expires_at) VALUES(?,?,?) "
        "ON CONFLICT(telegram_id) DO UPDATE SET status=excluded.status, expires_at=excluded.expires_at",
        (telegram_id, "active", expires_at.isoformat()),
    )
    conn.commit()
    conn.close()
    return expires_at

def set_user_inactive(telegram_id: int):
    conn = db()
    conn.execute(
        "INSERT INTO users(telegram_id, status, expires_at) VALUES(?,?,NULL) "
        "ON CONFLICT(telegram_id) DO UPDATE SET status='inactive', expires_at=NULL",
        (telegram_id, "inactive"),
    )
    conn.commit()
    conn.close()

def save_transaction(telegram_id: int, invictus_tx_id: Optional[str], status: str, raw_response: dict):
    conn = db()
    conn.execute(
        "INSERT INTO transactions(telegram_id, invictus_tx_id, status, created_at, raw_response) VALUES(?,?,?,?,?)",
        (telegram_id, invictus_tx_id, status, datetime.now(timezone.utc).isoformat(), json.dumps(raw_response, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()

def update_transaction_status(invictus_tx_id: str, status: str):
    conn = db()
    conn.execute("UPDATE transactions SET status=? WHERE invictus_tx_id=?", (status, invictus_tx_id))
    conn.commit()
    conn.close()

def find_telegram_by_tx(invictus_tx_id: str) -> Optional[int]:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT telegram_id FROM transactions WHERE invictus_tx_id=? ORDER BY id DESC LIMIT 1", (invictus_tx_id,))
    row = cur.fetchone()
    conn.close()
    return int(row[0]) if row else None


# =========================================================
# EXTRATORES (Pix/QR/Link) – tolerantes a mudanças
# =========================================================
EMV_START = "000201"
URL_REGEX = re.compile(r"(https?://[^\s\"\\]+)")

def walk_values(obj: Any):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from walk_values(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from walk_values(item)
    else:
        yield obj

def find_emv(resp_json: dict, raw_text: str) -> Optional[str]:
    for v in walk_values(resp_json):
        if isinstance(v, str):
            s = v.strip()
            if EMV_START in s:
                if s.startswith(EMV_START) and len(s) > 50:
                    return s
                idx = s.find(EMV_START)
                cand = s[idx:].strip()
                if len(cand) > 50:
                    return cand

    if raw_text and EMV_START in raw_text:
        idx = raw_text.find(EMV_START)
        cand = raw_text[idx: idx + 3000]
        cand = cand.split('"')[0].split("\\")[0].strip()
        if len(cand) > 50:
            return cand
    return None

def looks_like_base64(s: str) -> bool:
    if not s or len(s) < 200:
        return False
    if s.startswith("data:image/") and "base64," in s:
        return True
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\n\r")
    sample = s[:300]
    return all(c in allowed for c in sample)

def find_qr_source(resp_json: dict, raw_text: str) -> Tuple[Optional[str], Optional[str]]:
    for v in walk_values(resp_json):
        if isinstance(v, str):
            s = v.strip()
            if s.startswith("data:image/") and "base64," in s:
                return "base64", s.split("base64,", 1)[-1]
            if looks_like_base64(s):
                return "base64", s
            if s.startswith("http://") or s.startswith("https://"):
                if any(x in s.lower() for x in ["qr", "qrcode", "pix", ".png", ".jpg", ".jpeg"]):
                    return "url", s

    if raw_text:
        m = re.search(r"data:image/[^;]+;base64,([A-Za-z0-9+/=\n\r]+)", raw_text)
        if m:
            return "base64", m.group(1)

        mu = URL_REGEX.search(raw_text)
        if mu:
            u = mu.group(1)
            if any(x in u.lower() for x in ["qr", "qrcode", "pix", ".png", ".jpg", ".jpeg"]):
                return "url", u

    return None, None

def generate_qr_from_emv(emv: str) -> BytesIO:
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(emv)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = BytesIO()
    bio.name = "pix_qr.png"
    img.save(bio, format="PNG")
    bio.seek(0)
    return bio


# =========================================================
# INVICTUS: criar transação PIX
# =========================================================
def invictus_create_pix(telegram_id: int) -> Tuple[dict, Optional[str], Optional[str], Tuple[Optional[str], Optional[str]], str]:
    """
    Retorna:
      resp_json,
      tx_id,
      emv_copia_cola,
      (qr_kind, qr_value),
      raw_text
    """
    url = (
        "https://api.invictuspay.app.br/api/public/v1/transactions"
        f"?api_token={INVICTUS_API_TOKEN}"
        f"&postback_url={POSTBACK_URL}"
    )
    headers = {"Content-Type": "application/json", "Accept": "application/json"}

    payload = {
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
            "title": "Acesso VIP - 30 dias",
            "price": PRICE_CENTS,
            "quantity": 1,
            "operation_type": 1,
            "tangible": False
        }],
        "expire_in_days": 1,
        "tracking": {"telegram_id": telegram_id}
    }

    log.info("INVICTUS: criando transação pix (POST)")
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    raw_text = r.text or ""
    log.info(f"INVICTUS: status_code={r.status_code}")
    log.info(f"INVICTUS: raw_first_1200={raw_text[:1200]}")

    r.raise_for_status()

    try:
        resp_json = r.json()
    except Exception as e:
        log.error(f"INVICTUS: falha ao parsear JSON: {e}")
        resp_json = {"_non_json_response": raw_text[:4000]}

    log.info("INVICTUS_CREATE_JSON: " + json.dumps(resp_json, ensure_ascii=False)[:2500])

    tx_id = str(
        resp_json.get("id")
        or resp_json.get("transaction_id")
        or resp_json.get("uuid")
        or (resp_json.get("data") or {}).get("id")
        or ""
    ).strip() or None

    emv = find_emv(resp_json, raw_text)
    qr_kind, qr_value = find_qr_source(resp_json, raw_text)

    return resp_json, tx_id, emv, (qr_kind, qr_value), raw_text


# =========================================================
# TELEGRAM BOT
# =========================================================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Gerar Pix (30 dias)", callback_data="pay")],
        [InlineKeyboardButton(text="📌 Ver assinatura", callback_data="status")]
    ])

def fmt_dt(iso: Optional[str]) -> str:
    if not iso:
        return "-"
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return iso

@dp.message(CommandStart())
async def start_cmd(message: types.Message):
    await message.answer(
        f"🔥 Acesso VIP (30 dias)\n"
        f"💰 Valor: R$ {PRICE_CENTS/100:.2f}\n\n"
        f"Clique abaixo para gerar seu Pix ou ver sua validade:",
        reply_markup=main_keyboard()
    )

@dp.message(Command("status"))
async def status_cmd(message: types.Message):
    status, expires_at = get_user(message.from_user.id)
    if status == "active" and expires_at:
        await message.answer(f"✅ Assinatura ATIVA\n📅 Válida até: {fmt_dt(expires_at)}\n\nPara renovar quando vencer, use /start.")
    else:
        await message.answer("⚠️ Você está SEM assinatura ativa.\nUse /start para gerar o Pix e ativar por 30 dias.")

@dp.callback_query(lambda c: c.data == "status")
async def status_cb(call: types.CallbackQuery):
    status, expires_at = get_user(call.from_user.id)
    if status == "active" and expires_at:
        await call.message.answer(f"✅ Assinatura ATIVA\n📅 Válida até: {fmt_dt(expires_at)}")
    else:
        await call.message.answer("⚠️ Você está SEM assinatura ativa.\nClique em “Gerar Pix (30 dias)” para ativar.")
    await call.answer()

@dp.callback_query(lambda c: c.data == "pay")
async def pay_cb(call: types.CallbackQuery):
    telegram_id = call.from_user.id
    log.info("CLICK PAY RECEBIDO - iniciando criação pix")

    try:
        resp_json, tx_id, emv, (qr_kind, qr_val), raw_text = invictus_create_pix(telegram_id)
        save_transaction(telegram_id, tx_id, "pending", resp_json)

        # Enviar QR
        sent_qr = False
        if qr_kind == "base64" and qr_val:
            try:
                img_bytes = base64.b64decode(qr_val)
                bio = BytesIO(img_bytes)
                bio.name = "pix_qr.png"
                bio.seek(0)
                await bot.send_photo(call.message.chat.id, photo=bio, caption="📌 QR Code Pix")
                sent_qr = True
            except Exception as e:
                log.warning(f"Falha ao enviar QR base64: {e}")

        if (not sent_qr) and qr_kind == "url" and qr_val:
            try:
                img = requests.get(qr_val, timeout=20).content
                bio = BytesIO(img)
                bio.name = "pix_qr.png"
                bio.seek(0)
                await bot.send_photo(call.message.chat.id, photo=bio, caption="📌 QR Code Pix")
                sent_qr = True
            except Exception as e:
                log.warning(f"Falha ao baixar/enviar QR url: {e}")

        if (not sent_qr) and emv:
            try:
                bio = generate_qr_from_emv(emv)
                await bot.send_photo(call.message.chat.id, photo=bio, caption="📌 QR Code Pix")
                sent_qr = True
            except Exception as e:
                log.warning(f"Falha ao gerar/enviar QR do EMV: {e}")

        # Mensagem sem URL do checkout (APENAS copia e cola)
        msg = "💳 **Pix gerado**\n\n"
        if emv:
            msg += f"📋 **Pix Copia e Cola:**\n`{emv}`\n\n"
        msg += "✅ Assim que o pagamento for confirmado, o acesso será liberado automaticamente."

        await call.message.answer(msg, parse_mode="Markdown")
        await call.answer()

        if not emv:
            log.warning("Não encontrei EMV na resposta. Veja INVICTUS_CREATE_JSON nos logs.")

    except requests.HTTPError as e:
        await call.message.answer("❌ Erro ao gerar Pix (HTTP). Abra Render → Logs e veja INVICTUS: raw/status_code.")
        log.error(f"HTTPError Invictus: {e}")
        await call.answer()
    except Exception as e:
        await call.message.answer("❌ Falha ao gerar Pix. Abra Render → Logs.")
        log.error(f"Erro geral gerar Pix: {e}")
        await call.answer()


# =========================================================
# FASTAPI WEBHOOK (postback)
# =========================================================
app = FastAPI()

@app.post("/invictus/postback")
async def invictus_postback(request: Request):
    payload = await request.json()
    log.info("INVICTUS_POSTBACK_JSON: " + json.dumps(payload, ensure_ascii=False)[:2500])

    tx_id = str(payload.get("id") or payload.get("transaction_id") or payload.get("uuid") or "").strip()
    status = (payload.get("status") or payload.get("payment_status") or payload.get("state") or "").strip().lower()

    if (not tx_id) and isinstance(payload.get("data"), dict):
        d = payload["data"]
        tx_id = str(d.get("id") or d.get("transaction_id") or d.get("uuid") or "").strip()
        status = (d.get("status") or d.get("payment_status") or d.get("state") or "").strip().lower()

    if tx_id:
        update_transaction_status(tx_id, status or "unknown")

    approved_values = {
        "approved", "paid", "confirmed", "completed", "success",
        "aprovado", "pago"
    }

    if tx_id and status in approved_values:
        telegram_id = find_telegram_by_tx(tx_id)

        if not telegram_id:
            tracking = payload.get("tracking") or (payload.get("data") or {}).get("tracking")
            if isinstance(tracking, dict) and tracking.get("telegram_id"):
                telegram_id = int(tracking["telegram_id"])

        if telegram_id:
            expires_at = set_user_active(int(telegram_id))

            try:
                if GROUP_INVITE_LINK:
                    await bot.send_message(
                        int(telegram_id),
                        "✅ Pagamento confirmado!\n\n"
                        f"Acesso VIP:\n{GROUP_INVITE_LINK}\n\n"
                        f"📅 Válido até: {expires_at.strftime('%Y-%m-%d %H:%M UTC')}"
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
                        f"Link de acesso (expira em 30 min):\n{invite.invite_link}\n\n"
                        f"📅 Válido até: {expires_at.strftime('%Y-%m-%d %H:%M UTC')}"
                    )
                else:
                    await bot.send_message(
                        int(telegram_id),
                        "✅ Pagamento confirmado!\n\n"
                        "Acesso liberado, mas falta configurar GROUP_INVITE_LINK ou GROUP_CHAT_ID no Render."
                    )
            except Exception as e:
                log.error(f"Erro ao liberar acesso: {e}")

    return {"ok": True}


# =========================================================
# EXPIRAÇÃO / RENOVAÇÃO
# =========================================================
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
                            "⚠️ Sua assinatura expirou.\n\n"
                            f"💰 Renovação: R$ {PRICE_CENTS/100:.2f} / 30 dias\n"
                            "Clique em /start para gerar um novo Pix."
                        )
                except Exception:
                    continue

        except Exception as e:
            log.error(f"expiration_job error: {e}")

        await asyncio.sleep(600)


# =========================================================
# RUN (Bot + API)
# =========================================================
async def start_all():
    init_db()
    log.info("DB inicializado. Iniciando bot e API...")

    bot_task = asyncio.create_task(dp.start_polling(bot))
    exp_task = asyncio.create_task(expiration_job())

    config = uvicorn.Config(app, host=APP_HOST, port=APP_PORT, log_level="info")
    server = uvicorn.Server(config)
    api_task = asyncio.create_task(server.serve())

    await asyncio.gather(bot_task, exp_task, api_task)

if __name__ == "__main__":
    asyncio.run(start_all())
