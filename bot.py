import os
import sqlite3
import telebot
from telebot import types

# ==============================
# CONFIG
# ==============================
TOKEN = os.getenv("BOT_TOKEN",)
ADMIN_ID = 7922495578  # <-- তোমার এডমিন numeric ID
bot = telebot.TeleBot(TOKEN)

# ==============================
# DATABASE
# ==============================
conn = sqlite3.connect("bot.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id   INTEGER PRIMARY KEY,
    balance   INTEGER DEFAULT 0,
    refer_by  INTEGER,
    ref_count INTEGER DEFAULT 0,
    ref_earn  INTEGER DEFAULT 0
)
""")

# নিরাপদে (IF NOT EXISTS) আলাদা আলাদা কলাম যোগ করার চেষ্টা — পুরোনো DB থাকলেও চলবে
for alter_sql in [
    "ALTER TABLE users ADD COLUMN refer_by INTEGER",
    "ALTER TABLE users ADD COLUMN ref_count INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN ref_earn INTEGER DEFAULT 0"
]:
    try:
        cursor.execute(alter_sql)
    except Exception:
        pass

cursor.execute("""
CREATE TABLE IF NOT EXISTS withdraws (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    method  TEXT,
    number  TEXT,
    amount  INTEGER,
    status  TEXT DEFAULT 'Pending'
)
""")

# টাস্ক সাবমিশনের জন্য টেবিল
cursor.execute("""
CREATE TABLE IF NOT EXISTS tasks (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  INTEGER,
    username TEXT,
    file_id  TEXT,
    status   TEXT DEFAULT 'Pending'
)
""")

# settings টেবিল (task_price ইত্যাদি স্টোর করার জন্য)
cursor.execute("""
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
)
""")
# ডিফল্ট task_price ইনসার্ট (যদি না থাকে)
cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('task_price', '7')")

conn.commit()

# ==============================
# STATE
# ==============================
withdraw_steps = {}  # {user_id: {step, method, number}}
admin_steps = {}     # {admin_id: {action, step, target_id, old_balance}}

# ==============================
# SETTINGS HELPERS
# ==============================
def get_setting(key: str, default=None):
    cursor.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cursor.fetchone()
    return row[0] if row else default

def set_setting(key: str, value: str):
    cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()

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
    text = (f"🆔 {req_id} | 👤 {u_id}\n"
            f"💳 {method} ({number})\n"
            f"💵 {amount}৳ | 📌 {status}")
    if status == "Pending":
        ikb = types.InlineKeyboardMarkup()
        ikb.add(
            types.InlineKeyboardButton("✅ Approve", callback_data=f"approve_{req_id}"),
            types.InlineKeyboardButton("❌ Reject",  callback_data=f"reject_{req_id}")
        )
        bot.send_message(ADMIN_ID, text, reply_markup=ikb)
    else:
        bot.send_message(ADMIN_ID, text)

def apply_ref_bonus_if_increase(target_user_id: int, delta_increase: int):
    """
    টার্গেট ইউজারের ব্যালেন্স যদি পজিটিভ ডেল্টায় বাড়ে, তাহলে
    তার রেফারারকে ৩% বোনাস দাও।
    """
    if delta_increase <= 0:
        return
    cursor.execute("SELECT refer_by FROM users WHERE user_id=?", (target_user_id,))
    row = cursor.fetchone()
    if not row:
        return
    referrer = row[0]
    if not referrer:
        return
    bonus = int(delta_increase * 0.03)
    if bonus > 0:
        cursor.execute("UPDATE users SET balance = balance + ?, ref_earn = ref_earn + ? WHERE user_id=?",
                       (bonus, bonus, referrer))
        conn.commit()
        try:
            bot.send_message(referrer, f"🎉 আপনার রেফার্ড {target_user_id} এর ব্যালেন্স বৃদ্ধি পেয়েছে। আপনি পেলেন {bonus}৳ (3%)")
        except Exception:
            pass

# ==============================
# START + REFER ATTACH (updated to ensure refer works)
# ==============================
@bot.message_handler(commands=['start'])
def cmd_start(message: types.Message):
    user_id = message.chat.id
    # ensure user exists
    cursor.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    conn.commit()

    # refer attach: /start <referrer_id>
    parts = message.text.split()
    if len(parts) > 1:
        try:
            referrer_id = int(parts[1])
            # ignore self-referrals
            if referrer_id != user_id:
                # ensure referrer row exists so UPDATE works
                cursor.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (referrer_id,))
                conn.commit()

                # only attach if current user's refer_by is empty
                cursor.execute("SELECT refer_by FROM users WHERE user_id=?", (user_id,))
                ref = cursor.fetchone()
                if ref and ref[0] is None:
                    cursor.execute("UPDATE users SET refer_by=? WHERE user_id=?", (referrer_id, user_id))
                    # increment ref_count, ref_earn and give +1৳ bonus to referrer
                    cursor.execute("""
                        UPDATE users
                        SET ref_count = COALESCE(ref_count,0) + 1,
                            ref_earn  = COALESCE(ref_earn,0) + 1,
                            balance   = COALESCE(balance,0) + 1
                        WHERE user_id=?
                    """, (referrer_id,))
                    conn.commit()
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
    cursor.execute("SELECT balance FROM users WHERE user_id=?", (uid,))
    row = cursor.fetchone()
    bal = row[0] if row else 0
    bot.send_message(uid, f"💳 আপনার ব্যালেন্স: {bal}৳")

@bot.message_handler(func=lambda m: m.text == "👥 Refer")
def on_refer(message: types.Message):
    uid = message.chat.id
    link = f"https://t.me/{bot.get_me().username}?start={uid}"
    cursor.execute("SELECT ref_count, ref_earn FROM users WHERE user_id=?", (uid,))
    row = cursor.fetchone()
    ref_count = row[0] if row else 0
    ref_earn = row[1] if row and len(row) > 1 else 0
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
    # ডাইনামিক প্রাইস লোড করা হচ্ছে settings থেকে
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
    cursor.execute(
        "INSERT INTO tasks (user_id, username, file_id, status) VALUES (?, ?, ?, 'Pending')",
        (uid, username, doc.file_id)
    )
    conn.commit()

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
    cursor.execute("SELECT id, user_id, method, number, amount, status FROM withdraws ORDER BY id DESC LIMIT 10")
    rows = cursor.fetchall()
    if not rows:
        bot.send_message(ADMIN_ID, "📭 কোনো রিকোয়েস্ট পাওয়া যায়নি।")
    else:
        for row in rows:
            send_withdraw_card_to_admin(row)

@bot.message_handler(func=lambda msg: msg.text == "👥 User List" and msg.chat.id == ADMIN_ID)
def user_list_handler(message: types.Message):
    cursor.execute("SELECT COUNT(*), COALESCE(SUM(balance), 0) FROM users")
    total_users, total_balance = cursor.fetchone()
    cursor.execute("SELECT user_id, balance FROM users ORDER BY user_id DESC LIMIT 20")
    rows = cursor.fetchall()

    text = f"👥 মোট ইউজার: {total_users}\n💰 মোট ব্যালেন্স: {total_balance}৳\n\n"
    if not rows:
        text += "📭 এখনো কোনো ইউজার নেই।"
    else:
        text += "📌 সর্বশেষ ২০ জন ইউজার:\n"
        for u in rows:
            text += f"🆔 {u[0]} | 💰 Balance: {u[1]}৳\n"
    bot.send_message(ADMIN_ID, text)

# --- Task Requests (Admin) ---
@bot.message_handler(func=lambda msg: msg.text == "📂 Task Requests" and msg.chat.id == ADMIN_ID)
def task_requests_handler(message: types.Message):
    # পেন্ডিং টাস্ক লিস্ট দেখাও
    cursor.execute("""
        SELECT t.id, t.user_id, t.username, u.balance
        FROM tasks t
        LEFT JOIN users u ON u.user_id = t.user_id
        WHERE t.status='Pending'
        ORDER BY t.id DESC
        LIMIT 15
    """)
    rows = cursor.fetchall()

    if not rows:
        bot.send_message(ADMIN_ID, "📭 কোনো Pending Task নেই।")
        return

    for tid, uid, uname, bal in rows:
        text = (f"🗂️ Task #{tid}\n"
                f"👤 User: {uid} @{uname if uname else '—'}\n"
                f"💰 Balance: {bal if bal is not None else 0}৳")
        ikb = types.InlineKeyboardMarkup()
        ikb.add(
            types.InlineKeyboardButton("📥 Open File", callback_data=f"topen_{tid}"),
            types.InlineKeyboardButton("✅ Approve",  callback_data=f"tapprove_{tid}"),
            types.InlineKeyboardButton("❌ Reject",   callback_data=f"treject_{tid}")
        )
        bot.send_message(ADMIN_ID, text, reply_markup=ikb)

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
    text = message.text

    # ---------- Withdraw flow ----------
    if uid in withdraw_steps:
        step = withdraw_steps[uid]["step"]

        if step == "method":
            if text in ["📲 Bkash", "📲 Nagad"]:
                withdraw_steps[uid]["method"] = text
                withdraw_steps[uid]["step"] = "number"
                bot.send_message(uid, f"📱 আপনার {text} নম্বর লিখুন:")
            else:
                bot.send_message(uid, "❌ Bkash/Nagad সিলেক্ট করুন বা ⬅️ Back চাপুন।")
            return

        if step == "number":
            withdraw_steps[uid]["number"] = text
            withdraw_steps[uid]["step"] = "amount"
            bot.send_message(uid, "💵 কত টাকা Withdraw করবেন? (সর্বনিম্ন 50৳)")
            return

        if step == "amount":
            try:
                amount = int(text)
            except Exception:
                bot.send_message(uid, "❌ পরিমাণ সংখ্যায় দিন।")
                return

            cursor.execute("SELECT balance FROM users WHERE user_id=?", (uid,))
            row = cursor.fetchone()
            balance = row[0] if row else 0

            if amount < 50:
                bot.send_message(uid, "⚠️ সর্বনিম্ন withdraw 50৳")
            elif amount > balance:
                bot.send_message(uid, f"❌ আপনার ব্যালেন্সে যথেষ্ট টাকা নেই (বর্তমান: {balance}৳)")
            else:
                method = withdraw_steps[uid]["method"]
                number = withdraw_steps[uid]["number"]

                # Create request & deduct now
                cursor.execute("INSERT INTO withdraws (user_id, method, number, amount, status) VALUES (?,?,?,?, 'Pending')",
                               (uid, method, number, amount))
                cursor.execute("UPDATE users SET balance = balance - ? WHERE user_id=?", (amount, uid))
                conn.commit()

                bot.send_message(uid, f"✅ Withdraw Request সাবমিট হয়েছে!\n💳 {method}\n☎️ {number}\n💵 {amount}৳")
                # এডমিনকে অ্যালার্ট
                try:
                    bot.send_message(ADMIN_ID, f"🔔 নতুন Withdraw Request:\n👤 {uid}\n💳 {method} ({number})\n💵 {amount}৳")
                except Exception:
                    pass

            withdraw_steps.pop(uid, None)
            return

    # ---------- Admin flow ----------
    if uid == ADMIN_ID:
        # Add
        if text == "➕ Add Balance":
            admin_steps[uid] = {"action": "add", "step": "userid"}
            bot.send_message(uid, "🎯 ইউজারের ID দিন:")
            return

        if admin_steps.get(uid, {}).get("action") == "add":
            if admin_steps[uid]["step"] == "userid":
                try:
                    target = int(text)
                    admin_steps[uid]["target_id"] = target
                    admin_steps[uid]["step"] = "amount"
                    bot.send_message(uid, "💵 কত টাকা যোগ করবেন?")
                except Exception:
                    bot.send_message(uid, "❌ সঠিক ইউজার ID দিন।")
                return
            elif admin_steps[uid]["step"] == "amount":
                try:
                    amount = int(text)
                    target = admin_steps[uid]["target_id"]
                    cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (amount, target))
                    conn.commit()
                    # রেফার বোনাস: increase = amount
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
        if text == "✏️ Set Balance":
            admin_steps[uid] = {"action": "set", "step": "userid"}
            bot.send_message(uid, "🎯 ইউজারের ID দিন:")
            return

        if admin_steps.get(uid, {}).get("action") == "set":
            if admin_steps[uid]["step"] == "userid":
                try:
                    target = int(text)
                    admin_steps[uid]["target_id"] = target
                    cursor.execute("SELECT balance FROM users WHERE user_id=?", (target,))
                    row = cursor.fetchone()
                    old_balance = row[0] if row else 0
                    admin_steps[uid]["old_balance"] = old_balance
                    admin_steps[uid]["step"] = "amount"
                    bot.send_message(uid, f"💵 নতুন ব্যালেন্স কত হবে? (বর্তমান {old_balance}৳)")
                except Exception:
                    bot.send_message(uid, "❌ সঠিক ইউজার ID দিন।")
                return
            elif admin_steps[uid]["step"] == "amount":
                try:
                    new_amount = int(text)
                    target = admin_steps[uid]["target_id"]
                    old_balance = admin_steps[uid]["old_balance"]
                    cursor.execute("UPDATE users SET balance = ? WHERE user_id=?", (new_amount, target))
                    conn.commit()
                    # রেফার বোনাস: increase = max(new-old, 0)
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
        if text == "➖ Reduce Balance":
            admin_steps[uid] = {"action": "reduce", "step": "userid"}
            bot.send_message(uid, "🎯 ইউজারের ID দিন:")
            return

        if admin_steps.get(uid, {}).get("action") == "reduce":
            if admin_steps[uid]["step"] == "userid":
                try:
                    target = int(text)
                    admin_steps[uid]["target_id"] = target
                    admin_steps[uid]["step"] = "amount"
                    bot.send_message(uid, "💵 কত টাকা কমাবেন?")
                except Exception:
                    bot.send_message(uid, "❌ সঠিক ইউজার ID দিন।")
                return
            elif admin_steps[uid]["step"] == "amount":
                try:
                    amount = int(text)
                    target = admin_steps[uid]["target_id"]
                    cursor.execute("UPDATE users SET balance = balance - ? WHERE user_id=?", (amount, target))
                    conn.commit()
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
        if text == "⚙️ Set Task Price":
            admin_steps[uid] = {"action": "set_task_price", "step": "ask"}
            current = get_setting("task_price", "7")
            bot.send_message(uid, f"🛠️ বর্তমান টাস্ক প্রাইস {current}৳\nনতুন প্রাইস লিখুন:")
            return

        if admin_steps.get(uid, {}).get("action") == "set_task_price":
            try:
                new_price = float(text)
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

        cursor.execute("SELECT user_id, amount, status FROM withdraws WHERE id=?", (req_id,))
        row = cursor.fetchone()
        if not row:
            bot.answer_callback_query(call.id, "রিকোয়েস্ট পাওয়া যায়নি")
            return

        u_id, amount, status = row
        if status != "Pending":
            bot.answer_callback_query(call.id, "ইতিমধ্যে প্রসেস হয়েছে")
            return

        if action == "approve":
            # already deducted at request time → only mark approved
            cursor.execute("UPDATE withdraws SET status='Approved' WHERE id=?", (req_id,))
            conn.commit()
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
            # refund amount (we deducted previously)
            cursor.execute("UPDATE withdraws SET status='Rejected' WHERE id=?", (req_id,))
            cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (amount, u_id))
            conn.commit()
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
        cursor.execute("SELECT file_id FROM tasks WHERE id=?", (tid,))
        r = cursor.fetchone()
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

        cursor.execute("SELECT user_id, status FROM tasks WHERE id=?", (tid,))
        row = cursor.fetchone()
        if not row:
            bot.answer_callback_query(call.id, "টাস্ক পাওয়া যায়নি")
            return

        u_id, status = row
        if status != "Pending":
            bot.answer_callback_query(call.id, "ইতিমধ্যে প্রসেস হয়েছে")
            return

        new_status = "Approved" if is_approve else "Rejected"
        cursor.execute("UPDATE tasks SET status=? WHERE id=?", (new_status, tid))
        conn.commit()

        # ইউজারকে নোটিফাই (কোনো ব্যালেন্স অটো-চেঞ্জ নেই)
        try:
            if is_approve:
                bot.send_message(u_id, "✅ আপনার Gmail অ্যাপ্রুভ হয়েছে। আপনার Report কাউন্ট করে আপনার ব্যালান্স যুক্ত হয়ে যাবে ধন্যবাদ!")
            else:
                bot.send_message(u_id, "❌ দুঃখিত, আপনার Gmail রিজেক্ট করা হয়েছে।")
        except Exception:
            pass

        # মেসেজ আপডেট
        try:
            bot.edit_message_text(f"🗂️ Task #{tid} → {new_status}",
                                  chat_id=call.message.chat.id, message_id=call.message.message_id)
        except Exception:
            pass

        bot.answer_callback_query(call.id, f"{new_status} ✅" if is_approve else f"{new_status} ❌")
        return

# ==============================
# RUN
# ==============================
if __name__ == "__main__":
    print("🤖 Bot is running...")
    bot.infinity_polling()

