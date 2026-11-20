
import os
import sqlite3
import secrets
import logging
import hmac
import re
from functools import wraps
from urllib.parse import urlencode

from flask import (
    Flask, render_template, request, jsonify,
    session, redirect, render_template_string
)
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)

app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "true").lower() == "true",
)

DB = os.getenv("DATABASE_PATH", "aarohi.db")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip().lower()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
UPI_ID = os.getenv("UPI_ID", "")
UPI_NAME = os.getenv("UPI_NAME", "Aarohi AI")
CREDIT_PRICE = float(os.getenv("CREDIT_PRICE", "1"))

logging.basicConfig(level=logging.INFO)


# ======================================
# DATABASE
# ======================================

def db():
    con = sqlite3.connect(DB, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=20000")
    return con


def columns(con, table):
    return {
        row["name"]
        for row in con.execute(f"PRAGMA table_info({table})")
    }


def init_db():
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")

        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE,
            password TEXT
        );

        CREATE TABLE IF NOT EXISTS characters(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            name TEXT,
            personality TEXT,
            greeting TEXT
        );

        CREATE TABLE IF NOT EXISTS messages(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            character_id INTEGER,
            role TEXT,
            content TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS payments(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            credits INTEGER NOT NULL,
            utr TEXT UNIQUE,
            status TEXT DEFAULT 'pending',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            approved_at DATETIME
        );
        """)

        # Existing database migrations
        user_cols = columns(c, "users")

        if "status" not in user_cols:
            c.execute("""
                ALTER TABLE users
                ADD COLUMN status TEXT DEFAULT 'pending'
            """)

        if "credits" not in user_cols:
            c.execute("""
                ALTER TABLE users
                ADD COLUMN credits INTEGER DEFAULT 0
            """)

        if "created_at" not in user_cols:
            c.execute("""
                ALTER TABLE users
                ADD COLUMN created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            """)

        # Existing accounts need manual approval
        c.execute("""
            UPDATE users
            SET status='pending'
            WHERE status IS NULL OR status=''
        """)

        c.execute("""
            UPDATE users SET credits=0 WHERE credits IS NULL
        """)

        # Five default characters
        characters_data = [
            (
                "Aarohi",
                "Friendly, warm, caring, witty Hindi-speaking AI companion",
                "हाय! मैं आरोही हूँ 😊 आज आपका दिन कैसा रहा?"
            ),
            (
                "Priya",
                "Intelligent, thoughtful, supportive and friendly AI companion",
                "नमस्ते! मैं प्रिया हूँ। बताइए, आज क्या बात करें?"
            ),
            (
                "Anaya",
                "Cheerful, funny, energetic and playful AI companion",
                "हेलो! 😊 आज कुछ मजेदार बातें करते हैं!"
            ),
            (
                "Meera",
                "Calm, caring, empathetic and understanding AI companion",
                "नमस्ते ❤️ मैं मीरा हूँ। आपकी बातें सुनने के लिए यहाँ हूँ।"
            ),
            (
                "Riya",
                "Romantic, charming, affectionate and respectful AI companion",
                "हाय! ❤️ कैसे हैं आप? आज मुझे क्या बताना चाहेंगे?"
            )
        ]

        for name, personality, greeting in characters_data:
            exists = c.execute("""
                SELECT id FROM characters
                WHERE user_id=0 AND name=?
            """, (name,)).fetchone()

            if not exists:
                c.execute("""
                    INSERT INTO characters
                    (user_id,name,personality,greeting)
                    VALUES(0,?,?,?)
                """, (name, personality, greeting))


# ======================================
# AUTHENTICATION
# ======================================

def login_required(fn):
    @wraps(fn)
    def inner(*args, **kwargs):
        uid = session.get("uid")

        if not uid:
            return jsonify(error="पहले Login करें"), 401

        with db() as c:
            user = c.execute(
                "SELECT status FROM users WHERE id=?",
                (uid,)
            ).fetchone()

        if not user or user["status"] != "approved":
            session.clear()
            return jsonify(error="आपका Account Active नहीं है"), 403

        return fn(*args, **kwargs)

    return inner


def admin_required(fn):
    @wraps(fn)
    def inner(*args, **kwargs):
        if not session.get("admin"):
            return jsonify(error="Admin Login जरूरी है"), 401
        return fn(*args, **kwargs)

    return inner


# ======================================
# HOME
# ======================================

@app.route("/")
def home():
    return render_template("index.html")


# ======================================
# USER REGISTER
# ======================================

@app.post("/api/register")
def register():
    d = request.get_json(silent=True) or {}

    email = str(d.get("email", "")).strip().lower()
    password = str(d.get("password", ""))

    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return jsonify(error="सही Email डालें"), 400

    if len(password) < 8:
        return jsonify(error="पासवर्ड कम से कम 8 अक्षर का होना चाहिए"), 400

    try:
        with db() as c:
            cur = c.execute("""
                INSERT INTO users(email,password,status,credits)
                VALUES(?,?,'pending',0)
            """, (email, generate_password_hash(password)))

        return jsonify(
            ok=True,
            message="Registration सफल! Admin Approval का इंतजार करें।",
            user_id=cur.lastrowid
        )

    except sqlite3.IntegrityError:
        return jsonify(error="यह Email पहले से Register है"), 409


# ======================================
# USER LOGIN
# ======================================

@app.post("/api/login")
def login():
    d = request.get_json(silent=True) or {}

    email = str(d.get("email", "")).strip().lower()
    password = str(d.get("password", ""))

    with db() as c:
        user = c.execute(
            "SELECT * FROM users WHERE email=?",
            (email,)
        ).fetchone()

    if not user or not check_password_hash(user["password"], password):
        return jsonify(error="Email या Password गलत है"), 401

    if user["status"] == "pending":
        return jsonify(error="आपका Account Admin Approval में है"), 403

    if user["status"] != "approved":
        return jsonify(error="आपका Account Block है"), 403

    session.clear()
    session["uid"] = user["id"]

    return jsonify(ok=True, message="Login Successful")


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


# ======================================
# USER PROFILE / WALLET
# ======================================

@app.get("/api/me")
@login_required
def me():
    with db() as c:
        user = c.execute("""
            SELECT id,email,status,credits,created_at
            FROM users WHERE id=?
        """, (session["uid"],)).fetchone()

    return jsonify(dict(user))


# ======================================
# CHARACTERS
# ======================================

@app.get("/api/characters")
def characters():
    with db() as c:
        rows = c.execute("""
            SELECT id,name,personality,greeting
            FROM characters
            WHERE user_id=0 OR user_id=?
            ORDER BY id
        """, (session.get("uid", -1),)).fetchall()

    return jsonify([dict(row) for row in rows])


@app.post("/api/characters")
@login_required
def create_character():
    d = request.get_json(silent=True) or {}
    name = str(d.get("name", "")).strip()

    if not name:
        return jsonify(error="Character का नाम जरूरी है"), 400

    with db() as c:
        cur = c.execute("""
            INSERT INTO characters
            (user_id,name,personality,greeting)
            VALUES(?,?,?,?)
        """, (
            session["uid"],
            name[:40],
            str(d.get("personality", "Friendly and helpful"))[:500],
            str(d.get("greeting", "नमस्ते!"))[:300]
        ))

    return jsonify(id=cur.lastrowid, ok=True)


# ======================================
# GEMINI AI ENGINE
# ======================================

def ai_reply(prompt, history, character):
    key = os.getenv("GEMINI_API_KEY")

    if not key:
        return "Gemini API Key सेट नहीं है।", False

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=key)

        system_instruction = (
            f"You are {character['name']}. "
            f"Personality: {character['personality']}. "
            "You are a friendly, warm and respectful AI companion. "
            "Reply naturally in the user's language. "
            "Speak Hindi when the user speaks Hindi. "
            "Do not claim to be human."
        )

        contents = []

        for item in history[-12:]:
            if item["role"] in ("user", "assistant"):
                role = "model" if item["role"] == "assistant" else "user"

                contents.append(
                    types.Content(
                        role=role,
                        parts=[types.Part(text=item["content"])]
                    )
                )

        contents.append(
            types.Content(
                role="user",
                parts=[types.Part(text=prompt)]
            )
        )

        response = client.models.generate_content(
            model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                max_output_tokens=700
            )
        )

        answer = response.text

        if answer:
            return answer.strip(), True

        return "मुझे फिर से बताइए 😊", True

    except Exception:
        app.logger.exception("Gemini API Error")
        return "अभी AI जवाब नहीं दे पाया। दोबारा कोशिश करें।", False


# ======================================
# CREDIT SYSTEM
# ======================================

def consume_credit(uid):
    with db() as c:
        cur = c.execute("""
            UPDATE users
            SET credits=credits-1
            WHERE id=? AND status='approved' AND credits>0
        """, (uid,))

        return cur.rowcount == 1


def refund_credit(uid):
    with db() as c:
        c.execute("""
            UPDATE users
            SET credits=credits+1
            WHERE id=?
        """, (uid,))


# ======================================
# CHAT API
# ======================================

@app.post("/api/chat")
@login_required
def chat():
    d = request.get_json(silent=True) or {}

    try:
        cid = int(d.get("character_id", 0))
    except (ValueError, TypeError):
        return jsonify(error="गलत Character ID"), 400

    msg = str(d.get("message", "")).strip()
    uid = session["uid"]

    if not msg:
        return jsonify(error="खाली Message नहीं भेज सकते"), 400

    if len(msg) > 10000:
        return jsonify(error="Message बहुत लंबा है"), 400

    with db() as c:
        if cid == 0:
            character = c.execute("""
                SELECT * FROM characters
                WHERE user_id=0
                ORDER BY id LIMIT 1
            """).fetchone()
        else:
            character = c.execute("""
                SELECT * FROM characters
                WHERE id=? AND (user_id=0 OR user_id=?)
            """, (cid, uid)).fetchone()

        if not character:
            return jsonify(error="Character नहीं मिला"), 404

        actual_id = character["id"]

        history_rows = c.execute("""
            SELECT role,content FROM messages
            WHERE user_id=? AND character_id=?
            ORDER BY id DESC LIMIT 12
        """, (uid, actual_id)).fetchall()

        history = [dict(row) for row in reversed(history_rows)]

    if not consume_credit(uid):
        return jsonify(
            error="आपके Credits खत्म हो गए हैं। Wallet में Recharge करें।",
            credits_required=True
        ), 402

    reply, success = ai_reply(msg, history, dict(character))

    if not success:
        refund_credit(uid)
        return jsonify(error=reply), 503

    with db() as c:
        c.execute("""
            INSERT INTO messages
            (user_id,character_id,role,content)
            VALUES(?,?,?,?)
        """, (uid, actual_id, "user", msg))

        c.execute("""
            INSERT INTO messages
            (user_id,character_id,role,content)
            VALUES(?,?,?,?)
        """, (uid, actual_id, "assistant", reply))

        user = c.execute(
            "SELECT credits FROM users WHERE id=?",
            (uid,)
        ).fetchone()

    return jsonify(
        reply=reply,
        credits=user["credits"]
    )


# ======================================
# AI COACH
# ======================================

@app.post("/api/coach")
@login_required
def coach():
    d = request.get_json(silent=True) or {}
    text = str(d.get("text", "")).strip()
    uid = session["uid"]

    if not text:
        return jsonify(error="Message लिखें"), 400

    if not consume_credit(uid):
        return jsonify(error="Credits खत्म हो गए हैं"), 402

    suggestion, success = ai_reply(
        "इस Message का विश्लेषण करो और 3 बेहतर जवाब सुझाओ: " + text,
        [],
        {
            "name": "Aarohi AI Coach",
            "personality": "Helpful communication coach"
        }
    )

    if not success:
        refund_credit(uid)
        return jsonify(error=suggestion), 503

    with db() as c:
        user = c.execute(
            "SELECT credits FROM users WHERE id=?",
            (uid,)
        ).fetchone()

    return jsonify(
        suggestion=suggestion,
        credits=user["credits"]
    )


# ======================================
# UPI PAYMENT REQUEST
# ======================================

@app.get("/api/payment/info")
@login_required
def payment_info():
    with db() as c:
        user = c.execute(
            "SELECT credits FROM users WHERE id=?",
            (session["uid"],)
        ).fetchone()

    return jsonify(
        upi_id=UPI_ID,
        upi_name=UPI_NAME,
        credit_price=CREDIT_PRICE,
        credits=user["credits"]
    )


@app.post("/api/payment/request")
@login_required
def payment_request():
    d = request.get_json(silent=True) or {}

    utr = str(d.get("utr", "")).strip()
    try:
        amount = round(float(d.get("amount", 0)), 2)
        credits = int(d.get("credits", 0))
    except (ValueError, TypeError):
        return jsonify(error="गलत Payment Details"), 400

    if not UPI_ID:
        return jsonify(error="Admin ने UPI ID सेट नहीं की है"), 503

    if not re.fullmatch(r"[A-Za-z0-9]{6,40}", utr):
        return jsonify(error="सही UTR / Transaction ID डालें"), 400

    if amount <= 0 or credits <= 0:
        return jsonify(error="Amount और Credits सही डालें"), 400

    if amount != round(credits * CREDIT_PRICE, 2):
        return jsonify(error="Payment Amount और Credits मेल नहीं खाते"), 400

    try:
        with db() as c:
            cur = c.execute("""
                INSERT INTO payments(user_id,amount,credits,utr)
                VALUES(?,?,?,?)
            """, (session["uid"], amount, credits, utr))

        return jsonify(
            ok=True,
            payment_id=cur.lastrowid,
            message="Payment Request भेज दी गई। Admin Verification के बाद Credits मिलेंगे।"
        )

    except sqlite3.IntegrityError:
        return jsonify(error="यह UTR पहले इस्तेमाल हो चुका है"), 409


# ======================================
# WALLET PAGE
# ======================================

WALLET_HTML = """
<!DOCTYPE html>
<html lang="hi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Aarohi AI Wallet</title>
<style>
body{background:#f5f3ef;color:#222;font-family:Arial;padding:20px}
.card{max-width:440px;margin:30px auto;background:white;padding:24px;
border-radius:18px;box-shadow:0 5px 25px #ddd}
input,button{width:100%;padding:13px;margin:9px 0;
box-sizing:border-box;border-radius:9px;border:1px solid #ddd}
button{background:#7352bd;color:white;border:0;font-weight:bold}
a{color:#7352bd}
h2{text-align:center}
.balance{font-size:25px;text-align:center;color:#7352bd}
</style>
</head>
<body>
<div class="card">
<h2>💜 Aarohi AI Wallet</h2>
<div class="balance">Credits: <b id="balance">...</b></div>
<p>1 Credit = ₹<span id="price">1</span></p>
<label>कितने Credits खरीदने हैं?</label>
<input id="credits" type="number" min="1" value="100">
<p>कुल पेमेंट: ₹<b id="amount">100</b></p>
<p>UPI ID: <b id="upi"></b></p>
<button onclick="pay()">UPI App से Pay करें</button>
<hr>
<p>पेमेंट के बाद Transaction UTR डालें:</p>
<input id="utr" placeholder="UPI Transaction ID / UTR">
<button onclick="submitPayment()">Payment Verify करवाएँ</button>
<p id="msg"></p>
<a href="/">वापस Chat पर जाएँ</a>
</div>
<script>
let price=1;
async function load(){
 const r=await fetch('/api/payment/info');
 if(!r.ok){document.getElementById('msg').innerText='पहले Login करें';return;}
 const d=await r.json();
 price=d.credit_price;
 document.getElementById('balance').innerText=d.credits;
 document.getElementById('price').innerText=price;
 document.getElementById('upi').innerText=d.upi_id;
 update();
}
function update(){
 const n=Math.max(0,parseInt(document.getElementById('credits').value)||0);
 document.getElementById('amount').innerText=(n*price).toFixed(2);
}
document.getElementById('credits').addEventListener('input',update);
function pay(){
 const n=parseInt(document.getElementById('credits').value)||0;
 if(n<1)return alert('Credits डालें');
 const amount=(n*price).toFixed(2);
 const upi=document.getElementById('upi').innerText;
 if(!upi)return alert('UPI ID उपलब्ध नहीं है');
 const url='upi://pay?'+new URLSearchParams({
 pa:upi,pn:'Aarohi AI',am:amount,cu:'INR',
 tn:'Aarohi AI Credits'
 }).toString();
 location.href=url;
}
async function submitPayment(){
 const credits=parseInt(document.getElementById('credits').value)||0;
 const amount=credits*price;
 const utr=document.getElementById('utr').value.trim();
 const r=await fetch('/api/payment/request',{
 method:'POST',
 headers:{'Content-Type':'application/json'},
 body:JSON.stringify({credits,amount,utr})
 });
 const d=await r.json();
 document.getElementById('msg').innerText=d.message||d.error;
}
load();
</script>
</body>
</html>
"""


@app.get("/wallet")
def wallet():
    if not session.get("uid"):
        return redirect("/")
    return render_template_string(WALLET_HTML)


# ======================================
# ADMIN LOGIN
# ======================================

ADMIN_HTML = """
<!DOCTYPE html>
<html lang="hi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=
