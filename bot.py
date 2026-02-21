import os
import sqlite3
from datetime import datetime, timedelta

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "8415947180:AAGZl86fioOcWr6GYOMeMILGNls_0evDuCo")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8475664365"))

TON_WALLET_ADDRESS = os.getenv("TON_WALLET_ADDRESS", "UQBq73P29d7H_wKmQyIlejJUL6bFWX0Somo6x5DumxyxM2Hu")
TONCENTER_API_KEY = os.getenv("TONCENTER_API_KEY", "09330ec3846b4e16921726dc27504a0bafab44f5bd9b58f51a20046025c8a438")
TONCENTER_BASE = os.getenv("TONCENTER_BASE", "https://toncenter.com/api/v2")

SUB_PRICE_TON = float(os.getenv("SUB_PRICE_TON", "10"))

DB = "bot.db"

# ----------------- DB -----------------
def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS subscriptions (
        user_id INTEGER PRIMARY KEY,
        starts_at TEXT NOT NULL,
        ends_at TEXT NOT NULL,
        status TEXT NOT NULL,
        last_reminded_at TEXT
    );

    CREATE TABLE IF NOT EXISTS ton_invoices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        amount_ton REAL NOT NULL,
        memo TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        matched_tx_hash TEXT
    );
    """)
    conn.commit()
    conn.close()

def ensure_user(user_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
    if cur.fetchone() is None:
        cur.execute(
            "INSERT INTO users(user_id, created_at) VALUES(?,?)",
            (user_id, datetime.utcnow().isoformat())
        )
    conn.commit()
    conn.close()

def upsert_subscription(user_id: int, days: int = 30):
    now = datetime.utcnow()
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT ends_at FROM subscriptions WHERE user_id=?", (user_id,))
    row = cur.fetchone()

    if row:
        ends = datetime.fromisoformat(row["ends_at"])
        base = ends if ends > now else now
        new_ends = base + timedelta(days=days)
        cur.execute(
            "UPDATE subscriptions SET ends_at=?, status=? WHERE user_id=?",
            (new_ends.isoformat(), "active", user_id)
        )
    else:
        ends = now + timedelta(days=days)
        cur.execute(
            "INSERT INTO subscriptions(user_id, starts_at, ends_at, status, last_reminded_at) VALUES(?,?,?,?,?)",
            (user_id, now.isoformat(), ends.isoformat(), "active", None)
        )
    conn.commit()
    conn.close()

def subscription_status(user_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT ends_at, status FROM subscriptions WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return False, None
    ends = datetime.fromisoformat(row["ends_at"])
    if ends < datetime.utcnow():
        return False, ends
    return True, ends

def is_admin(user_id: int) -> bool:
    return ADMIN_ID and user_id == ADMIN_ID

# ----------------- TON helpers -----------------
def create_invoice(user_id: int, amount_ton: float) -> str:
    memo = f"SUB1M_{user_id}_{int(datetime.utcnow().timestamp())}"
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO ton_invoices(user_id, amount_ton, memo, created_at, status) VALUES(?,?,?,?,?)",
        (user_id, amount_ton, memo, datetime.utcnow().isoformat(), "pending")
    )
    conn.commit()
    conn.close()
    return memo

def toncenter_get_transactions(address: str, limit: int = 30):
    params = {"address": address, "limit": limit}
    if TONCENTER_API_KEY:
        params["api_key"] = TONCENTER_API_KEY

    r = requests.get(f"{TONCENTER_BASE}/getTransactions", params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"toncenter error: {data}")
    return data.get("result", [])

def tx_in_message_text(tx: dict) -> str:
    # toncenter usually has tx["in_msg"]["message"] (comment/text)
    in_msg = tx.get("in_msg") or {}
    msg = in_msg.get("message") or ""
    return str(msg)

def tx_in_amount_ton(tx: dict) -> float:
    # toncenter: in_msg.value is in nanotons (string)
    in_msg = tx.get("in_msg") or {}
    value = in_msg.get("value") or "0"
    try:
        nano = int(value)
    except:
        nano = 0
    return nano / 1_000_000_000

def tx_hash(tx: dict) -> str:
    # Some responses include "transaction_id": {"hash": "..."}
    tid = tx.get("transaction_id") or {}
    h = tid.get("hash")
    return h or ""

def verify_invoice(memo: str) -> bool:
    """
    Returns True if we find incoming tx to TON_WALLET_ADDRESS with:
    - comment contains memo
    - amount >= expected
    """
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM ton_invoices WHERE memo=?", (memo,))
    inv = cur.fetchone()
    conn.close()
    if not inv or inv["status"] != "pending":
        return False

    txs = toncenter_get_transactions(TON_WALLET_ADDRESS, limit=40)

    for tx in txs:
        msg = tx_in_message_text(tx)
        amt = tx_in_amount_ton(tx)
        if memo in msg and amt + 1e-9 >= float(inv["amount_ton"]):
            # mark paid
            conn2 = db()
            cur2 = conn2.cursor()
            cur2.execute(
                "UPDATE ton_invoices SET status='paid', matched_tx_hash=? WHERE memo=?",
                (tx_hash(tx), memo)
            )
            conn2.commit()
            conn2.close()

            upsert_subscription(inv["user_id"], days=30)
            return True

    return False

# ----------------- UI -----------------
def menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧾 Obuna (1 oy) — 10 TON", callback_data="buy_sub_1m")],
        [InlineKeyboardButton("✅ To‘lovni tekshirish", callback_data="check_payment")],
        [InlineKeyboardButton("📅 Obuna status", callback_data="sub_status")],
        [InlineKeyboardButton("ℹ️ About", callback_data="about")],
    ])

# ----------------- Handlers -----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    init_db()
    ensure_user(update.effective_user.id)
    await update.message.reply_text(
        "👋 Salom!\n"
        f"💳 Obuna: 1 oy = {SUB_PRICE_TON:g} TON\n"
        "To‘lov TON orqali.\n\nMenyu:",
        reply_markup=menu_kb()
    )

async def on_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    ensure_user(user_id)

    data = q.data

    if data == "about":
        await q.edit_message_text(
            "ℹ️ About\n"
            "— To‘lov: TON transfer + comment (memo)\n"
            "— To‘lov topilsa obuna 30 kunga yoqiladi\n"
            "— Kafolatli profit yo‘q, bu obuna-xizmat.\n\nMenyu:",
            reply_markup=menu_kb()
        )
        return

    if data == "sub_status":
        active, ends = subscription_status(user_id)
        if ends:
            await q.edit_message_text(
                ("✅ Aktiv" if active else "⚠️ Aktiv emas") + f"\n📅 Tugash: {ends.date()}",
                reply_markup=menu_kb()
            )
        else:
            await q.edit_message_text("❌ Obuna yo‘q", reply_markup=menu_kb())
        return

    if data == "buy_sub_1m":
        if not TON_WALLET_ADDRESS:
            await q.edit_message_text("❌ TON_WALLET_ADDRESS sozlanmagan (.env).", reply_markup=menu_kb())
            return

        memo = create_invoice(user_id, SUB_PRICE_TON)
        text = (
            "🧾 1 oylik obuna to‘lovi\n\n"
            f"💰 Summa: {SUB_PRICE_TON:g} TON\n"
            f"🏦 Address:\n{TON_WALLET_ADDRESS}\n\n"
            f"📝 Comment (majburiy):\n{memo}\n\n"
            "✅ To‘lov qilgandan keyin: “To‘lovni tekshirish” tugmasini bosing."
        )
        await q.edit_message_text(text, reply_markup=menu_kb())
        return

    if data == "check_payment":
        # last pending invoice for this user
        conn = db()
        cur = conn.cursor()
        cur.execute(
            "SELECT memo FROM ton_invoices WHERE user_id=? AND status='pending' ORDER BY id DESC LIMIT 1",
            (user_id,)
        )
        row = cur.fetchone()
        conn.close()

        if not row:
            await q.edit_message_text(
                "❌ Pending invoice topilmadi.\nAvval “Obuna (1 oy)” ni bosing.",
                reply_markup=menu_kb()
            )
            return

        memo = row["memo"]
        try:
            ok = verify_invoice(memo)
        except Exception as e:
            await q.edit_message_text(f"⚠️ Tekshiruv xatosi:\n{e}\n\nYana urinib ko‘ring.", reply_markup=menu_kb())
            return

        if ok:
            active, ends = subscription_status(user_id)
            await q.edit_message_text(
                f"✅ To‘lov topildi! Obuna yoqildi.\n📅 Tugash: {ends.date() if ends else '—'}",
                reply_markup=menu_kb()
            )
        else:
            await q.edit_message_text(
                "⏳ Hali to‘lov topilmadi.\n"
                "1–3 daqiqa kuting va qayta tekshiring.\n"
                "(Comment to‘g‘ri yozilganiga ishonch hosil qiling.)",
                reply_markup=menu_kb()
            )
        return

async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("⛔ Admin emas.")
        return

    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) AS c FROM subscriptions WHERE status='active'")
    active_cnt = cur.fetchone()["c"]

    cur.execute("""
        SELECT user_id, ends_at, status
        FROM subscriptions
        ORDER BY ends_at ASC
        LIMIT 50
    """)
    subs = cur.fetchall()

    cur.execute("""
        SELECT user_id, amount_ton, memo, created_at, status
        FROM ton_invoices
        ORDER BY id DESC
        LIMIT 20
    """)
    invs = cur.fetchall()

    conn.close()

    text = f"📊 ADMIN\n\n✅ Active subs: {active_cnt}\n\n👥 Subscriptions:\n"
    for s in subs:
        ends = datetime.fromisoformat(s["ends_at"]).date()
        text += f"- {s['user_id']} | {s['status']} | ends: {ends}\n"

    text += "\n🧾 Last invoices:\n"
    for i in invs:
        text += f"- {i['user_id']} | {i['amount_ton']} TON | {i['status']} | {i['memo']} | {i['created_at'][:19]}\n"

    if len(text) > 3500:
        text = text[:3500] + "\n...\n(too long)"

    await update.message.reply_text(text)

def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN yo‘q. .env ga BOT_TOKEN=... qo‘ying.")
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CallbackQueryHandler(on_btn))

    app.run_polling()

if __name__ == "__main__":
    main()