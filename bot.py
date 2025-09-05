import os
import telebot
from telebot import types
from flask import Flask, request
from sqlalchemy import create_engine, text

# ==============================
# CONFIG
# ==============================
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("❌ BOT_TOKEN not set in Environment Variables!")

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # <-- তোমার এডমিন numeric ID
bot = telebot.TeleBot(TOKEN)

# ==============================
# DATABASE (SQLAlchemy)
# ==============================
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("❌ DATABASE_URL not set in Environment Variables!")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)

# ---- Create tables (if not exists)
with engine.begin() as conn:
    conn.execute(text("""
    CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY,
        balance INTEGER DEFAULT 0,
        refer_by BIGINT,
        ref_count INTEGER DEFAULT 0,
        ref_earn INTEGER DEFAULT 0
    )
    """))

    conn.execute(text("""
    CREATE TABLE IF NOT EXISTS withdraws (
        id SERIAL PRIMARY KEY,
        user_id BIGINT,
        method  TEXT,
        number  TEXT,
        amount  INTEGER,
        status  TEXT DEFAULT 'Pending'
    )
    """))

    conn.execute(text("""
    CREATE TABLE IF NOT EXISTS tasks (
        id SERIAL PRIMARY KEY,
        user_id  BIGINT,
        username TEXT,
        file_id  TEXT,
        status   TEXT DEFAULT 'Pending'
    )
    """))

    conn.execute(text("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """))

    # ডিফল্ট task_price (যদি না থাকে)
    conn.execute(text("""
        INSERT INTO settings (key, value)
        VALUES ('task_price', '7')
        ON CONFLICT (key) DO NOTHING
    """))

# ==============================
# STATE
# ==============================
withdraw_steps = {}  # {user_id: {step, method, number}}
admin_steps = {}     # {admin_id: {action, step, target_id, old_balance}}

# ==============================
# SETTINGS HELPERS
# ==============================
def get_setting(key: str, default=None):
    with engine.begin() as conn:
        row = conn.execute(text("SELECT value FROM settings WHERE key=:k"), {"k": key}).fetchone()
        return row[0] if row else default

def set_setting(key: str, value: str):
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO settings (key, value)
            VALUES (:k, :v)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """), {"k": key, "v": value})

# ==============================
# HELPERS
# ==============================
def send_main_menu(uid: int):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    # প্রথম লাইন
    kb.add(types.KeyboardButton("💰 Balance"), types.KeyboardButton("👥 Refer"))
    # দ্বিতীয় লাইন
    kb.add(types.KeyboardButton("💵 Withdraw"))
    # তৃতীয় লাইন (নতুন)
    kb.add(types.KeyboardButton("🎁 Create Gmail"), types.KeyboardButton("💌 Support group 🛑"))
    bot.send_message(uid, "👋 মেনু থেকে একটি অপশন সিলেক্ট করুন:", reply_markup=kb)

def send_admin_menu(uid: int):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(types.KeyboardButton("➕ Add Balance"), types.KeyboardButton("✏️ Set Balance"))
    kb.add(types.KeyboardButton("➖ Reduce Balance"), types.KeyboardButton("📋 All Requests"))
    kb.add(types.KeyboardButton("👥 User List"), types.KeyboardButton("📂 Task Requests"))
    kb.add(types.KeyboardButton("⚙️ Set Task Price"))  # নতুন
    kb.add(types.KeyboardButton("⬅️ Back"))
    bot.send_message(uid, "🔐 Admin Panel:", reply_markup=kb)

def send_withdraw_card_to_admin(row):
    """row: (id, user_id, method, number, amount, status)"""
    req_id, u_id, method, number, amount, status = row
    text_msg = (f"🆔 {req_id} | 👤 {u_id}\n"
                f"💳 {method} ({number})\n"
                f"💵 {amount}৳ | 📌 {status}")
    if status == "Pending":
        ikb = types.InlineKeyboardMarkup()
        ikb.add(
            types.InlineKeyboardButton("✅ Approve", callback_data=f"approve_{req_id}"),
            types.InlineKeyboardButton("❌ Reject",  callback_data=f"reject_{req_id}")
        )
        bot.send_message(ADMIN_ID, text_msg, reply_markup=ikb)
    else:
        bot.send_message(ADMIN_ID, text_msg)

def apply_ref_bonus_if_increase(target_user_id: int, delta_increase: int):
    """
    টার্গেট ইউজারের ব্যালেন্স যদি পজিটিভ ডেল্টায় বাড়ে, তাহলে
    তার রেফারারকে ৩% বোনাস দাও।
    """
    if delta_increase <= 0:
        return
    with engine.begin() as conn:
        row = conn.execute(text("SELECT refer_by FROM users WHERE user_id=:uid"), {"uid": target_user_id}).fetchone()
        if not row:
            return
        referrer = row[0]
        if not referrer:
            return
        bonus = int(delta_increase * 0.03)
        if bonus > 0:
            conn.execute(text("""
                UPDATE users
                SET balance = COALESCE(balance,0) + :b,
                    ref_earn = COALESCE(ref_earn,0) + :b
                WHERE user_id = :rid
            """), {"b": bonus, "rid": referrer})
    # নোটিফিকেশন আলাদা try ব্লকে
    if delta_increase > 0:
        try:
            bot.send_message(referrer, f"🎉 আপনার রেফার্ড {target_user_id} এর ব্যালেন্স বৃদ্ধি পেয়েছে। আপনি পেলেন {bonus}৳ (3%)")
        except Exception:
            pass

# ==============================
# START + REFER ATTACH
# ==============================
@bot.message_handler(commands=['start'])
def cmd_start(message: types.Message):
    user_id = message.chat.id

    # ensure user exists
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO users (user_id)
            VALUES (:uid)
            ON CONFLICT (user_id) DO NOTHING
        """), {"uid": user_id})

    # refer attach: /start <referrer_id>
    parts = message.text.split()
    if len(parts) > 1:
        try:
            referrer_id = int(parts[1])
            if referrer_id != user_id:
                with engine.begin() as conn:
                    # ensure referrer row exists
                    conn.execute(text("""
                        INSERT INTO users (user_id)
                        VALUES (:rid)
                        ON CONFLICT (user_id) DO NOTHING
                    """), {"rid": referrer_id})

                    # only attach if current user's refer_by is NULL
                    ref = conn.execute(text("SELECT refer_by FROM users WHERE user_id=:uid"), {"uid": user_id}).fetchone()
                    if ref and ref[0] is None:
                        # attach refer_by
                        conn.execute(text("UPDATE users SET refer_by=:rid WHERE user_id=:uid"),
                                     {"rid": referrer_id, "uid": user_id})
                        # increment ref_count, ref_earn and give +1৳ bonus to referrer
                        conn.execute(text("""
                            UPDATE users
                            SET ref_count = COALESCE(ref_count,0) + 1,
                                ref_earn  = COALESCE(ref_earn,0) + 1,
                                balance   = COALESCE(balance,0) + 1
                            WHERE user_id=:rid
                        """), {"rid": referrer_id})
                try:
                    bot.send_message(referrer_id, f"🎉 আপনার রেফারে নতুন একজন জয়েন করেছে!\nআপনি বোনাস 1৳ পেয়েছেন।")
                except Exception:
                    pass
        except Exception:
            pass

    send_main_menu(user_id)

# ==============================
# USER BUTTONS
# ==============================
@bot.message_handler(func=lambda m: m.text == "💰 Balance")
def on_balance(message: types.Message):
    uid = message.chat.id
    with engine.begin() as conn:
        row = conn.execute(text("SELECT balance FROM users WHERE user_id=:uid"), {"uid": uid}).fetchone()
    bal = row[0] if row else 0
    bot.send_message(uid, f"💳 আপনার ব্যালেন্স: {bal}৳")

@bot.message_handler(func=lambda m: m.text == "👥 Refer")
def on_refer(message: types.Message):
    uid = message.chat.id
    link = f"https://t.me/{bot.get_me().username}?start={uid}"
    with engine.begin() as conn:
        row = conn.execute(text("SELECT COALESCE(ref_count,0), COALESCE(ref_earn,0) FROM users WHERE user_id=:uid"),
                           {"uid": uid}).fetchone()
    ref_count = row[0] if row else 0
    ref_earn = row[1] if row else 0
    bot.send_message(
        uid,
        f"🔗 আপনার রেফার লিঙ্ক:\n{link}\n\n"
        f"👥 মোট রেফার করেছে: {ref_count}\n"
        f"💰 রেফার থেকে আয়: {ref_earn}৳\n\n"
        f"✅ নিয়ম: আপনার রেফার্ড ইউজারের ব্যালেন্স যখনই বাড়বে,\n"
        f"আপনি পাবেন সেই বৃদ্ধির 3%।\n\n"
        f"🔔 চাইলে প্রত্যেক রেফারে সরাসরি 1৳ পান।"
    )

@bot.message_handler(func=lambda m: m.text == "💵 Withdraw")
def on_withdraw(message: types.Message):
    uid = message.chat.id
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(types.KeyboardButton("📲 Bkash"), types.KeyboardButton("📲 Nagad"))
    kb.add(types.KeyboardButton("⬅️ Back"))
    withdraw_steps[uid] = {"step": "method"}
    bot.send_message(uid, "💵 কোন পেমেন্ট মেথডে নিতে চান?", reply_markup=kb)

# --- Support group ---
@bot.message_handler(func=lambda m: m.text == "💌 Support group 🛑")
def support_group(message: types.Message):
    bot.send_message(
        message.chat.id,
        "ℹ️ যেকোনো সমস্যা হলে সাপোর্ট গ্রুপে জানাতে পারেন:\n"
        "👉 https://t.me/+f9tOe5fPe0Q0NGZl"
    )

# --- Create Gmail task ---
@bot.message_handler(func=lambda m: m.text == "🎁 Create Gmail")
def create_gmail(message: types.Message):
    task_price_str = get_setting("task_price", "7")
    try:
        task_price = float(task_price_str)
    except Exception:
        task_price = 7
    bot.send_message(
        message.chat.id,
        f"💰আপনি প্রতি জিমেইল এ পাবেন : {task_price} টাকা🎁\n"
        "📍 [কিভাবে কাজ করবেন?](https://t.me/taskincometoday/16)",
        parse_mode="Markdown"
    )
    bot.send_message(message.chat.id, "📂 এখন আপনার `.xlsx` ফাইলটি আপলোড করুন।")

# --- Receive .xlsx file ---
@bot.message_handler(content_types=['document'])
def handle_file(message: types.Message):
    doc = message.document
    uid = message.chat.id
    username = message.from_user.username or ""

    # .xlsx ভ্যালিডেশন (file name বা mime type)
    is_xlsx = False
    if doc.file_name and doc.file_name.lower().endswith(".xlsx"):
        is_xlsx = True
    elif doc.mime_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
        is_xlsx = True

    if not is_xlsx:
        bot.send_message(uid, "❌ অনুগ্রহ করে শুধুমাত্র `.xlsx` ফাইল আপলোড করুন।")
        return

    # DB তে টাস্ক সেভ
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO tasks (user_id, username, file_id, status)
            VALUES (:uid, :uname, :fid, 'Pending')
        """), {"uid": uid, "uname": username, "fid": doc.file_id})

    bot.send_message(uid, "✅ আপনার ফাইলটি সফলভাবে জমা হয়েছে, আমরা যাচাই করছি।")
    # এডমিনকে অ্যালার্ট
    try:
        bot.send_message(ADMIN_ID, f"🆕 নতুন টাস্ক সাবমিশন\n👤 User: {uid} (@{username})\n📄 File: {doc.file_name}")
    except Exception:
        pass

# ==============================
# ADMIN PANEL + ITEMS
# ==============================
@bot.message_handler(commands=['admin'])
def admin_panel(message: types.Message):
    if message.chat.id != ADMIN_ID:
        bot.send_message(message.chat.id, "❌ আপনি এডমিন নন।")
        return
    send_admin_menu(message.chat.id)

@bot.message_handler(func=lambda msg: msg.text == "📋 All Requests" and msg.chat.id == ADMIN_ID)
def all_requests_handler(message: types.Message):
    with engine.begin() as conn:
        rows = conn.execute(text("""
            SELECT id, user_id, method, number, amount, status
            FROM withdraws
            ORDER BY id DESC
            LIMIT 10
        """)).fetchall()
    if not rows:
        bot.send_message(ADMIN_ID, "📭 কোনো রিকোয়েস্ট পাওয়া যায়নি।")
    else:
        for row in rows:
            send_withdraw_card_to_admin(row)

@bot.message_handler(func=lambda msg: msg.text == "👥 User List" and msg.chat.id == ADMIN_ID)
def user_list_handler(message: types.Message):
    with engine.begin() as conn:
        total_users, total_balance = conn.execute(text("""
            SELECT COUNT(*), COALESCE(SUM(balance), 0) FROM users
        """)).fetchone()
        rows = conn.execute(text("""
            SELECT user_id, balance
            FROM users
            ORDER BY user_id DESC
            LIMIT 20
        """)).fetchall()

    text_msg = f"👥 মোট ইউজার: {total_users}\n💰 মোট ব্যালেন্স: {total_balance}৳\n\n"
    if not rows:
        text_msg += "📭 এখনো কোনো ইউজার নেই।"
    else:
        text_msg += "📌 সর্বশেষ ২০ জন ইউজার:\n"
        for u in rows:
            text_msg += f"🆔 {u[0]} | 💰 Balance: {u[1]}৳\n"
    bot.send_message(ADMIN_ID, text_msg)

# --- Task Requests (Admin) ---
@bot.message_handler(func=lambda msg: msg.text == "📂 Task Requests" and msg.chat.id == ADMIN_ID)
def task_requests_handler(message: types.Message):
    with engine.begin() as conn:
        rows = conn.execute(text("""
            SELECT t.id, t.user_id, t.username,
                   COALESCE(u.balance,0) AS bal
            FROM tasks t
            LEFT JOIN users u ON u.user_id = t.user_id
            WHERE t.status='Pending'
            ORDER BY t.id DESC
            LIMIT 15
        """)).fetchall()

    if not rows:
        bot.send_message(ADMIN_ID, "📭 কোনো Pending Task নেই।")
        return

    for tid, uid, uname, bal in rows:
        text_msg = (f"🗂️ Task #{tid}\n"
                    f"👤 User: {uid} @{uname if uname else '—'}\n"
                    f"💰 Balance: {bal}৳")
        ikb = types.InlineKeyboardMarkup()
        ikb.add(
            types.InlineKeyboardButton("📥 Open File", callback_data=f"topen_{tid}"),
            types.InlineKeyboardButton("✅ Approve",  callback_data=f"tapprove_{tid}"),
            types.InlineKeyboardButton("❌ Reject",   callback_data=f"treject_{tid}")
        )
        bot.send_message(ADMIN_ID, text_msg, reply_markup=ikb)

# ==============================
# BACK BUTTON (GLOBAL)
# ==============================
@bot.message_handler(func=lambda m: m.text == "⬅️ Back")
def on_back(message: types.Message):
    uid = message.chat.id
    withdraw_steps.pop(uid, None)
    admin_steps.pop(uid, None)
    if uid == ADMIN_ID:
        send_admin_menu(uid)
    else:
        send_main_menu(uid)

# ==============================
# WITHDRAW FLOW + ADMIN FLOW (catch-all)
# ==============================
@bot.message_handler(func=lambda m: True)
def catch_all(message: types.Message):
    uid = message.chat.id
    text_msg = message.text

    # ---------- Withdraw flow ----------
    if uid in withdraw_steps:
        step = withdraw_steps[uid]["step"]

        if step == "method":
            if text_msg in ["📲 Bkash", "📲 Nagad"]:
                withdraw_steps[uid]["method"] = text_msg
                withdraw_steps[uid]["step"] = "number"
                bot.send_message(uid, f"📱 আপনার {text_msg} নম্বর লিখুন:")
            else:
                bot.send_message(uid, "❌ Bkash/Nagad সিলেক্ট করুন বা ⬅️ Back চাপুন।")
            return

        if step == "number":
            withdraw_steps[uid]["number"] = text_msg
            withdraw_steps[uid]["step"] = "amount"
            bot.send_message(uid, "💵 কত টাকা Withdraw করবেন? (সর্বনিম্ন 50৳)")
            return

        if step == "amount":
            try:
                amount = int(text_msg)
            except Exception:
                bot.send_message(uid, "❌ পরিমাণ সংখ্যায় দিন।")
                return

            with engine.begin() as conn:
                row = conn.execute(text("SELECT COALESCE(balance,0) FROM users WHERE user_id=:uid"),
                                   {"uid": uid}).fetchone()
                balance = row[0] if row else 0

                if amount < 50:
                    bot.send_message(uid, "⚠️ সর্বনিম্ন withdraw 50৳")
                elif amount > balance:
                    bot.send_message(uid, f"❌ আপনার ব্যালেন্সে যথেষ্ট টাকা নেই (বর্তমান: {balance}৳)")
                else:
                    method = withdraw_steps[uid]["method"]
                    number = withdraw_steps[uid]["number"]

                    # Create request & deduct now
                    conn.execute(text("""
                        INSERT INTO withdraws (user_id, method, number, amount, status)
                        VALUES (:uid, :m, :n, :a, 'Pending')
                    """), {"uid": uid, "m": method, "n": number, "a": amount})

                    conn.execute(text("""
                        UPDATE users SET balance = COALESCE(balance,0) - :a WHERE user_id=:uid
                    """), {"a": amount, "uid": uid})

                    bot.send_message(uid, f"✅ Withdraw Request সাবমিট হয়েছে!\n💳 {method}\n☎️ {number}\n💵 {amount}৳")
                    try:
                        bot.send_message(ADMIN_ID, f"🔔 নতুন Withdraw Request:\n👤 {uid}\n💳 {method} ({number})\n💵 {amount}৳")
                    except Exception:
                        pass

            withdraw_steps.pop(uid, None)
            return

    # ---------- Admin flow ----------
    if uid == ADMIN_ID:
        # Add
        if text_msg == "➕ Add Balance":
            admin_steps[uid] = {"action": "add", "step": "userid"}
            bot.send_message(uid, "🎯 ইউজারের ID দিন:")
            return

        if admin_steps.get(uid, {}).get("action") == "add":
            if admin_steps[uid]["step"] == "userid":
                try:
                    target = int(text_msg)
                    admin_steps[uid]["target_id"] = target
                    admin_steps[uid]["step"] = "amount"
                    bot.send_message(uid, "💵 কত টাকা যোগ করবেন?")
                except Exception:
                    bot.send_message(uid, "❌ সঠিক ইউজার ID দিন।")
                return
            elif admin_steps[uid]["step"] == "amount":
                try:
                    amount = int(text_msg)
                    target = admin_steps[uid]["target_id"]
                    with engine.begin() as conn:
                        conn.execute(text("""
                            UPDATE users SET balance = COALESCE(balance,0) + :a WHERE user_id=:uid
                        """), {"a": amount, "uid": target})
                    apply_ref_bonus_if_increase(target, amount)
                    bot.send_message(uid, f"✅ {target} এর ব্যালেন্সে {amount}৳ যোগ হয়েছে।")
                    try:
                        bot.send_message(target, f"🎉 আপনার ব্যালেন্সে {amount}৳ যোগ হয়েছে।")
                    except Exception:
                        pass
                except Exception:
                    bot.send_message(uid, "❌ সঠিক সংখ্যা দিন।")
                admin_steps.pop(uid, None)
                return

        # Set
        if text_msg == "✏️ Set Balance":
            admin_steps[uid] = {"action": "set", "step": "userid"}
            bot.send_message(uid, "🎯 ইউজারের ID দিন:")
            return

        if admin_steps.get(uid, {}).get("action") == "set":
            if admin_steps[uid]["step"] == "userid":
                try:
                    target = int(text_msg)
                    admin_steps[uid]["target_id"] = target
                    with engine.begin() as conn:
                        row = conn.execute(text("SELECT COALESCE(balance,0) FROM users WHERE user_id=:uid"),
                                           {"uid": target}).fetchone()
                        old_balance = row[0] if row else 0
                    admin_steps[uid]["old_balance"] = old_balance
                    admin_steps[uid]["step"] = "amount"
                    bot.send_message(uid, f"💵 নতুন ব্যালেন্স কত হবে? (বর্তমান {old_balance}৳)")
                except Exception:
                    bot.send_message(uid, "❌ সঠিক ইউজার ID দিন।")
                return
            elif admin_steps[uid]["step"] == "amount":
                try:
                    new_amount = int(text_msg)
                    target = admin_steps[uid]["target_id"]
                    old_balance = admin_steps[uid]["old_balance"]
                    with engine.begin() as conn:
                        conn.execute(text("UPDATE users SET balance=:b WHERE user_id=:uid"),
                                     {"b": new_amount, "uid": target})
                    delta = new_amount - old_balance
                    apply_ref_bonus_if_increase(target, delta)
                    bot.send_message(uid, f"✅ {target} এর ব্যালেন্স {new_amount}৳ এ সেট হয়েছে।")
                    try:
                        bot.send_message(target, f"⚠️ অ্যাডমিন আপনার ব্যালেন্স সেট করেছে: {new_amount}৳")
                    except Exception:
                        pass
                except Exception:
                    bot.send_message(uid, "❌ সঠিক সংখ্যা দিন।")
                admin_steps.pop(uid, None)
                return

        # Reduce
        if text_msg == "➖ Reduce Balance":
            admin_steps[uid] = {"action": "reduce", "step": "userid"}
            bot.send_message(uid, "🎯 ইউজারের ID দিন:")
            return

        if admin_steps.get(uid, {}).get("action") == "reduce":
            if admin_steps[uid]["step"] == "userid":
                try:
                    target = int(text_msg)
                    admin_steps[uid]["target_id"] = target
                    admin_steps[uid]["step"] = "amount"
                    bot.send_message(uid, "💵 কত টাকা কমাবেন?")
                except Exception:
                    bot.send_message(uid, "❌ সঠিক ইউজার ID দিন।")
                return
            elif admin_steps[uid]["step"] == "amount":
                try:
                    amount = int(text_msg)
                    target = admin_steps[uid]["target_id"]
                    with engine.begin() as conn:
                        conn.execute(text("""
                            UPDATE users SET balance = COALESCE(balance,0) - :a WHERE user_id=:uid
                        """), {"a": amount, "uid": target})
                    bot.send_message(uid, f"✅ {target} এর ব্যালেন্স থেকে {amount}৳ কেটে নেওয়া হয়েছে।")
                    try:
                        bot.send_message(target, f"⚠️ আপনার ব্যালেন্স থেকে {amount}৳ কমানো হয়েছে।")
                    except Exception:
                        pass
                except Exception:
                    bot.send_message(uid, "❌ সঠিক সংখ্যা দিন।")
                admin_steps.pop(uid, None)
                return

        # --- NEW: Set Task Price via Admin Panel ---
        if text_msg == "⚙️ Set Task Price":
            admin_steps[uid] = {"action": "set_task_price", "step": "ask"}
            current = get_setting("task_price", "7")
            bot.send_message(uid, f"🛠️ বর্তমান টাস্ক প্রাইস {current}৳\nনতুন প্রাইস লিখুন:")
            return

        if admin_steps.get(uid, {}).get("action") == "set_task_price":
            try:
                new_price = float(text_msg)
                if new_price < 0:
                    raise ValueError("negative")
                set_setting("task_price", str(new_price))
                bot.send_message(uid, f"✅ টাস্ক প্রাইস এখন {new_price}৳ করা হয়েছে।")
            except Exception:
                bot.send_message(uid, "❌ সঠিক সংখ্যা লিখুন। (উদাহরণ: 7)")
            admin_steps.pop(uid, None)
            return

# ==============================
# WITHDRAW APPROVE / REJECT (INLINE)
# ==============================
@bot.callback_query_handler(func=lambda c: c.data.startswith("approve_") or c.data.startswith("reject_") or
                                      c.data.startswith("tapprove_") or c.data.startswith("treject_") or
                                      c.data.startswith("topen_"))
def on_inline_decision(call: types.CallbackQuery):
    # শুধু এডমিন
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "অনুমতি নেই")
        return

    data = call.data

    # ---- Withdraw decisions ----
    if data.startswith("approve_") or data.startswith("reject_"):
        action, req_id_str = data.split("_", 1)
        try:
            req_id = int(req_id_str)
        except Exception:
            bot.answer_callback_query(call.id, "ভুল ID")
            return

        with engine.begin() as conn:
            row = conn.execute(text("""
                SELECT user_id, amount, status FROM withdraws WHERE id=:id
            """), {"id": req_id}).fetchone()

        if not row:
            bot.answer_callback_query(call.id, "রিকোয়েস্ট পাওয়া যায়নি")
            return

        u_id, amount, status = row
        if status != "Pending":
            bot.answer_callback_query(call.id, "ইতিমধ্যে প্রসেস হয়েছে")
            return

        if action == "approve":
            with engine.begin() as conn:
                conn.execute(text("UPDATE withdraws SET status='Approved' WHERE id=:id"), {"id": req_id})
            try:
                bot.send_message(u_id, f"✅ আপনার Withdraw Request {amount}৳ Approved হয়েছে!")
            except Exception:
                pass
            try:
                bot.edit_message_text(f"🆔 {req_id} Withdraw Approved ✅",
                                      chat_id=call.message.chat.id, message_id=call.message.message_id)
            except Exception:
                pass
            bot.answer_callback_query(call.id, "Approved ✅")

        elif action == "reject":
            with engine.begin() as conn:
                conn.execute(text("UPDATE withdraws SET status='Rejected' WHERE id=:id"), {"id": req_id})
                conn.execute(text("""
                    UPDATE users SET balance = COALESCE(balance,0) + :a WHERE user_id=:uid
                """), {"a": amount, "uid": u_id})
            try:
                bot.send_message(u_id, f"❌ আপনার Withdraw Request {amount}৳ Rejected হয়েছে। টাকা ফেরত দেওয়া হয়েছে।")
            except Exception:
                pass
            try:
                bot.edit_message_text(f"🆔 {req_id} Withdraw Rejected ❌",
                                      chat_id=call.message.chat.id, message_id=call.message.message_id)
            except Exception:
                pass
            bot.answer_callback_query(call.id, "Rejected ❌")
        return

    # ---- Task requests (open / approve / reject) ----
    if data.startswith("topen_"):
        tid = int(data.split("_", 1)[1])
        with engine.begin() as conn:
            r = conn.execute(text("SELECT file_id FROM tasks WHERE id=:id"), {"id": tid}).fetchone()
        if not r:
            bot.answer_callback_query(call.id, "ফাইল পাওয়া যায়নি")
            return
        file_id = r[0]
        try:
            bot.send_document(ADMIN_ID, file_id, caption=f"🗂️ Task #{tid} file")
        except Exception:
            pass
        bot.answer_callback_query(call.id, "ফাইল পাঠানো হলো")
        return

    if data.startswith("tapprove_") or data.startswith("treject_"):
        is_approve = data.startswith("tapprove_")
        tid = int(data.split("_", 1)[1])

        with engine.begin() as conn:
            row = conn.execute(text("SELECT user_id, status FROM tasks WHERE id=:id"), {"id": tid}).fetchone()

        if not row:
            bot.answer_callback_query(call.id, "টাস্ক পাওয়া যায়নি")
            return

        u_id, status = row
        if status != "Pending":
            bot.answer_callback_query(call.id, "ইতিমধ্যে প্রসেস হয়েছে")
            return

        new_status = "Approved" if is_approve else "Rejected"
        with engine.begin() as conn:
            conn.execute(text("UPDATE tasks SET status=:st WHERE id=:id"), {"st": new_status, "id": tid})

        try:
            if is_approve:
                bot.send_message(u_id, "✅ আপনার Gmail অ্যাপ্রুভ হয়েছে। আপনার Report কাউন্ট করে আপনার ব্যালান্স যুক্ত হয়ে যাবে ধন্যবাদ!")
            else:
                bot.send_message(u_id, "❌ দুঃখিত, আপনার Gmail রিজেক্ট করা হয়েছে।")
        except Exception:
            pass

        try:
            bot.edit_message_text(f"🗂️ Task #{tid} → {new_status}",
                                  chat_id=call.message.chat.id, message_id=call.message.message_id)
        except Exception:
            pass

        bot.answer_callback_query(call.id, f"{new_status} ✅" if is_approve else f"{new_status} ❌")
        return

# ==============================
# RUN (Flask + Webhook)
# ==============================
app = Flask(__name__)

@app.route('/' + TOKEN, methods=['POST'])
def getMessage():
    json_str = request.get_data().decode('UTF-8')
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "!", 200

@app.route('/')
def webhook():
    # তোমার Render host বসাও
    public_base = os.getenv("PUBLIC_BASE_URL", "https://YOUR-RENDER-HOST.onrender.com")
    # পুরনো webhook থাকলে সরাও এবং নতুন সেট করো
    bot.remove_webhook()
    bot.set_webhook(url=f"{public_base}/{TOKEN}")
    return "Webhook set!", 200

if __name__ == "__main__":
    print("🤖 Bot is running...")
    # লোকাল টেস্টের সময় এটা চলবে; Render এ gunicorn দিয়ে চালানো উত্তম
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))


