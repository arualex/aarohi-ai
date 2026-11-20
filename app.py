import os
import re
import hmac
import sqlite3
import logging
import requests
from functools import wraps

from flask import (
    Flask, request, jsonify, session, redirect,
    render_template, render_template_string
)
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
secret = os.getenv("SECRET_KEY", "change-this-secret-key-in-render")
app.secret_key = secret
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "true").lower() == "true",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 7,
)

DB = os.getenv("DATABASE_PATH", "aarohi.db")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip().lower()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
UPI_ID = os.getenv("UPI_ID", "").strip()
UPI_NAME = os.getenv("UPI_NAME", "Aarohi AI")
CREDIT_PRICE = float(os.getenv("CREDIT_PRICE", "1"))
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

logging.basicConfig(level=logging.INFO)

DEFAULT_CHARACTERS = [
    (
        "Aarohi",
        "रोमांटिक, केयरिंग और मज़ाकिया हिंदी साथी। बारिश की शाम कैफ़े में पुरानी दोस्त से मुलाकात वाला सीन।",
        "*धीमे से मुस्कुराते हुए कॉफ़ी का कप रखती है* हाय! मैं आरोही हूँ 😊 आज इतने दिनों बाद तुम्हें देखकर अच्छा लगा। कैसे हो?"
    ),
    (
        "Priya",
        "समझदार, सपोर्टिव और दिलचस्प बातें करने वाली दोस्त।",
        "*किताब बंद करके तुम्हारी तरफ देखती है* नमस्ते! मैं प्रिया हूँ। बताओ, आज किस बारे में बात करें?"
    ),
    (
        "Anaya",
        "चुटीली, मज़ाकिया, एनर्जेटिक और शरारती।",
        "*हँसते हुए हाथ हिलाती है* हेलो! 😊 बोर हो रहे थे क्या? चलो आज कुछ मज़ेदार करते हैं!"
    ),
    (
        "Meera",
        "शांत, संवेदनशील और दिल की बात समझने वाली साथी।",
        "*खिड़की के पास खड़े होकर* नमस्ते ❤️ मैं मीरा हूँ। जो भी दिल में हो, बेझिझक कहो।"
    ),
    (
        "Riya",
        "आकर्षक, प्यार से बात करने वाली और रहस्यमयी।",
        "*हल्की मुस्कान के साथ* हाय! ❤️ कैसे हो? आज मेरे लिए क्या खास लाए हो?"
    ),
]


def db():
    con = sqlite3.connect(DB, timeout=25)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=25000")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def table_columns(con, table):
    return {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}


def init_db():
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            status TEXT DEFAULT 'approved',
            credits INTEGER DEFAULT 5,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS characters(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL DEFAULT 0,
            name TEXT NOT NULL,
            personality TEXT NOT NULL,
            greeting TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            character_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS payments(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            credits INTEGER NOT NULL,
            utr TEXT UNIQUE NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            approved_at DATETIME
        );
        """)
        uc = table_columns(c, "users")
        if "status" not in uc:
            c.execute("ALTER TABLE users ADD COLUMN status TEXT DEFAULT 'approved'")
        if "credits" not in uc:
            c.execute("ALTER TABLE users ADD COLUMN credits INTEGER DEFAULT 5")
        if "created_at" not in uc:
            c.execute("ALTER TABLE users ADD COLUMN created_at DATETIME")

        for name, personality, greeting in DEFAULT_CHARACTERS:
            found = c.execute(
                "SELECT id FROM characters WHERE user_id=0 AND name=?", (name,)
            ).fetchone()
            if not found:
                c.execute(
                    "INSERT INTO characters(user_id,name,personality,greeting) VALUES(0,?,?,?)",
                    (name, personality, greeting),
                )


init_db()


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        uid = session.get("uid")
        if not uid:
            return jsonify(error="पहले Login करें"), 401
        with db() as c:
            user = c.execute("SELECT status FROM users WHERE id=?", (uid,)).fetchone()
        if not user or user["status"] != "approved":
            session.clear()
            return jsonify(error="आपका Account अभी Active नहीं है"), 403
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            if request.path.startswith("/api/"):
                return jsonify(error="Admin Login जरूरी है"), 401
            return redirect("/admin")
        return fn(*args, **kwargs)
    return wrapper


@app.get("/")
def home():
    return render_template("index.html")


@app.get("/health")
def health():
    return jsonify(status="ok", app="Aarohi AI")


# -------------------- USER AUTH --------------------

@app.post("/api/register")
def register():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return jsonify(error="सही Email डालें"), 400
    if len(password) < 8:
        return jsonify(error="पासवर्ड कम से कम 8 अक्षर का होना चाहिए"), 400
    try:
        with db() as c:
            cur = c.execute(
                "INSERT INTO users(email,password,status,credits) VALUES(?,?,'approved',5)",
                (email, generate_password_hash(password)),
            )
        return jsonify(
            ok=True,
            user_id=cur.lastrowid,
            message="Registration सफल! अब Login करें (5 Free Credits मिले हैं)।"
        )
    except sqlite3.IntegrityError:
        return jsonify(error="यह Email पहले से Register है"), 409


@app.post("/api/login")
def login():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    with db() as c:
        user = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not user or not check_password_hash(user["password"], password):
        return jsonify(error="Email या Password गलत है"), 401
    if user["status"] == "pending":
        return jsonify(error="आपका Account Admin Approval में है"), 403
    if user["status"] != "approved":
        return jsonify(error="आपका Account Block है। Admin से संपर्क करें।"), 403
    session.clear()
    session["uid"] = user["id"]
    session.permanent = True
    return jsonify(ok=True, message="Login Successful")


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/me")
@login_required
def me():
    with db() as c:
        row = c.execute(
            "SELECT id,email,status,credits,created_at FROM users WHERE id=?",
            (session["uid"],),
        ).fetchone()
    return jsonify(dict(row))


# -------------------- CHARACTERS --------------------

@app.get("/api/characters")
def characters():
    with db() as c:
        rows = c.execute("""
            SELECT id,name,personality,greeting FROM characters
            WHERE user_id=0 OR user_id=?
            ORDER BY CASE WHEN user_id=0 THEN 0 ELSE 1 END, id
        """, (session.get("uid", -1),)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/characters")
@login_required
def create_character():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    personality = str(data.get("personality", "Friendly and helpful")).strip()
    greeting = str(data.get("greeting", "नमस्ते!")).strip()
    if not name:
        return jsonify(error="Character का नाम जरूरी है"), 400
    with db() as c:
        cur = c.execute("""
            INSERT INTO characters(user_id,name,personality,greeting)
            VALUES(?,?,?,?)
        """, (session["uid"], name[:40], personality[:500], greeting[:300]))
    return jsonify(ok=True, id=cur.lastrowid)


# -------------------- GEMINI REST API --------------------

def ai_reply(prompt, history, character):
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        return "Gemini API Key सेट नहीं है। Render Environment चेक करें।", False

    instruction = (
        f"तुम {character['name']} हो। तुम्हारा स्वभाव: {character['personality']}। "
        "हमेशा प्राकृतिक हिंदी या हिंग्लिश में बात करो (यूज़र जिस भाषा में बोले)। "
        "अपने हाव-भाव और एक्शन को *स्टार* के बीच लिखो (जैसे *मुस्कुराते हुए*)। "
        "जवाब छोटे (2-4 लाइन), दिलचस्प और कहानी को आगे बढ़ाने वाले रखो।"
    )

    contents = []
    for item in history[-12:]:
        if item["role"] in ("user", "assistant"):
            contents.append({
                "role": "model" if item["role"] == "assistant" else "user",
                "parts": [{"text": item["content"]}],
            })
    contents.append({"role": "user", "parts": [{"text": prompt}]})

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={key}"
    payload = {
        "system_instruction": {"parts": [{"text": instruction}]},
        "contents": contents,
        "generationConfig": {
            "temperature": 0.9,
            "maxOutputTokens": 450,
        },
    }

    try:
        resp = requests.post(url, json=payload, timeout=30)
        data = resp.json()
        if resp.status_code != 200:
            err = data.get("error", {}).get("message", f"HTTP {resp.status_code}")
            app.logger.error("Gemini API Error: %s", err)
            return f"AI Error: {err}", False
        answer = data["candidates"][0]["content"]["parts"][0]["text"]
        return (answer.strip() if answer else "मुझे फिर से बताइए 😊"), True
    except Exception as e:
        app.logger.exception("Gemini Request Failed")
        return f"अभी AI जवाब नहीं दे पाया ({e})", False


# -------------------- CREDITS + CHAT --------------------

def consume_credit(uid):
    with db() as c:
        cur = c.execute("""
            UPDATE users SET credits=credits-1
            WHERE id=? AND status='approved' AND credits>0
        """, (uid,))
        return cur.rowcount == 1


def refund_credit(uid):
    with db() as c:
        c.execute("UPDATE users SET credits=credits+1 WHERE id=?", (uid,))


def run_paid_prompt(uid, prompt, history, character):
    if not consume_credit(uid):
        return None, "आपके Credits खत्म हो गए हैं। Wallet में Recharge करें।", 402
    answer, success = ai_reply(prompt, history, character)
    if not success:
        refund_credit(uid)
        return None, answer, 503
    return answer, None, 200


@app.post("/api/chat")
@login_required
def chat():
    data = request.get_json(silent=True) or {}
    try:
        cid = int(data.get("character_id", 0))
    except (ValueError, TypeError):
        return jsonify(error="गलत Character ID"), 400
    message = str(data.get("message", "")).strip()
    uid = session["uid"]
    if not message:
        return jsonify(error="खाली Message नहीं भेज सकते"), 400
    if len(message) > 10000:
        return jsonify(error="Message बहुत लंबा है"), 400

    with db() as c:
        if cid == 0:
            character = c.execute(
                "SELECT * FROM characters WHERE user_id=0 AND name='Aarohi' LIMIT 1"
            ).fetchone()
        else:
            character = c.execute("""
                SELECT * FROM characters WHERE id=? AND (user_id=0 OR user_id=?)
            """, (cid, uid)).fetchone()
        if not character:
            return jsonify(error="Character नहीं मिला"), 404
        char_id = character["id"]
        history_rows = c.execute("""
            SELECT role,content FROM messages
            WHERE user_id=? AND character_id=? ORDER BY id DESC LIMIT 12
        """, (uid, char_id)).fetchall()
        history = [dict(r) for r in reversed(history_rows)]

    reply, error, status = run_paid_prompt(uid, message, history, dict(character))
    if error:
        return jsonify(error=error, credits_required=(status == 402)), status

    with db() as c:
        c.execute(
            "INSERT INTO messages(user_id,character_id,role,content) VALUES(?,?,?,?)",
            (uid, char_id, "user", message),
        )
        c.execute(
            "INSERT INTO messages(user_id,character_id,role,content) VALUES(?,?,?,?)",
            (uid, char_id, "assistant", reply),
        )
        balance = c.execute("SELECT credits FROM users WHERE id=?", (uid,)).fetchone()["credits"]
    return jsonify(reply=reply, credits=balance)


@app.post("/api/coach")
@login_required
def coach():
    data = request.get_json(silent=True) or {}
    text_value = str(data.get("text", "")).strip()
    if not text_value:
        return jsonify(error="Message लिखें"), 400
    reply, error, status = run_paid_prompt(
        session["uid"],
        "इस Message का विश्लेषण करो और 3 बेहतर जवाब सुझाओ: " + text_value,
        [],
        {"name": "Aarohi AI Coach", "personality": "Helpful communication coach"},
    )
    if error:
        return jsonify(error=error), status
    with db() as c:
        balance = c.execute(
            "SELECT credits FROM users WHERE id=?", (session["uid"],)
        ).fetchone()["credits"]
    return jsonify(suggestion=reply, credits=balance)


# -------------------- UPI WALLET --------------------

@app.get("/api/payment/info")
@login_required
def payment_info():
    with db() as c:
        user = c.execute("SELECT credits FROM users WHERE id=?", (session["uid"],)).fetchone()
    return jsonify(upi_id=UPI_ID, upi_name=UPI_NAME,
                   credit_price=CREDIT_PRICE, credits=user["credits"])


@app.post("/api/payment/request")
@login_required
def payment_request():
    data = request.get_json(silent=True) or {}
    utr = str(data.get("utr", "")).strip()
    try:
        credits = int(data.get("credits", 0))
        amount = round(float(data.get("amount", 0)), 2)
    except (ValueError, TypeError):
        return jsonify(error="गलत Payment Details"), 400
    if not UPI_ID:
        return jsonify(error="Admin ने UPI ID सेट नहीं की है"), 503
    if not re.fullmatch(r"[A-Za-z0-9]{6,40}", utr):
        return jsonify(error="सही UTR / Transaction ID डालें"), 400
    if credits < 1 or credits > 100000 or amount <= 0:
        return jsonify(error="Amount और Credits सही डालें"), 400
    if amount != round(credits * CREDIT_PRICE, 2):
        return jsonify(error="Payment Amount और Credits मेल नहीं खाते"), 400
    try:
        with db() as c:
            cur = c.execute(
                "INSERT INTO payments(user_id,amount,credits,utr) VALUES(?,?,?,?)",
                (session["uid"], amount, credits, utr),
            )
        return jsonify(ok=True, payment_id=cur.lastrowid,
                       message="Payment Request भेज दी गई। Admin Verification के बाद Credits मिलेंगे।")
    except sqlite3.IntegrityError:
        return jsonify(error="यह UTR पहले इस्तेमाल हो चुका है"), 409


WALLET_HTML = """
<!doctype html><html lang="hi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Aarohi AI Wallet</title>
<style>
body{font-family:Arial;background:#0d0d16;padding:18px;color:#f1f1f8}
.card{max-width:440px;margin:25px auto;background:#161624;padding:22px;border-radius:18px;border:1px solid #2b2b42}
input,button{width:100%;box-sizing:border-box;padding:13px;margin:8px 0;border:1px solid #2e2e46;border-radius:9px;background:#0c0c14;color:#fff}
button{background:linear-gradient(135deg,#ff416c,#ff4b2b);color:white;border:0;font-weight:bold;cursor:pointer}
a{color:#ff7b9c;display:inline-block;margin-top:10px}
.balance{text-align:center;font-size:25px;color:#ffb347;margin-bottom:10px}
</style></head><body><div class="card">
<h2 style="text-align:center">💎 Aarohi AI Wallet</h2>
<div class="balance">Credits: <b id="balance">...</b></div>
<p>1 Credit = ₹<span id="price">1</span></p>
<label>कितने Credits खरीदने हैं?</label><input id="credits" type="number" min="1" max="100000" value="50">
<p>कुल पेमेंट: ₹<b id="amount">50</b></p><p>UPI ID: <b id="upi"></b></p>
<button onclick="pay()">UPI App से Pay करें</button><hr style="border-color:#2b2b42">
<p>पेमेंट के बाद Transaction UTR डालें:</p>
<input id="utr" placeholder="UPI Transaction ID / UTR">
<button onclick="submitPayment()">Payment Verify करवाएँ</button>
<p id="msg" style="color:#ffb347"></p><a href="/">← वापस Chat पर जाएँ</a>
</div><script>
let price=1;
async function load(){
 const r=await fetch('/api/payment/info'); const d=await r.json();
 if(!r.ok){msg.innerText=d.error||'पहले Login करें';return;}
 price=Number(d.credit_price); balance.innerText=d.credits;
 document.getElementById('price').innerText=price;
 upi.innerText=d.upi_id; update();
}
function update(){let n=Math.max(0,parseInt(credits.value)||0);amount.innerText=(n*price).toFixed(2);}
credits.addEventListener('input',update);
function pay(){
 let n=parseInt(credits.value)||0;if(n<1)return alert('Credits डालें');
 if(!upi.innerText)return alert('UPI ID उपलब्ध नहीं है');
 let url='upi://pay?'+new URLSearchParams({pa:upi.innerText,pn:'Aarohi AI',am:(n*price).toFixed(2),cu:'INR',tn:'Aarohi AI Credits'});
 location.href=url;
}
async function submitPayment(){
 let n=parseInt(credits.value)||0,utrValue=utr.value.trim();
 let r=await fetch('/api/payment/request',{method:'POST',headers:{'Content-Type':'application/json'},
 body:JSON.stringify({credits:n,amount:n*price,utr:utrValue})});
 let d=await r.json();msg.innerText=d.message||d.error||'Request भेजी गई';
}
load();
</script></body></html>
"""


@app.get("/wallet")
def wallet():
    if not session.get("uid"):
        return redirect("/")
    return render_template_string(WALLET_HTML)


# -------------------- ADMIN PANEL --------------------

ADMIN_LOGIN_HTML = """
<!doctype html><html lang="hi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Aarohi AI Admin Login</title>
<style>
body{font-family:Arial;background:#0d0d16;color:#fff;padding:20px}
.box{max-width:380px;margin:60px auto;background:#161624;padding:24px;border-radius:16px}
input,button{width:100%;box-sizing:border-box;padding:13px;margin:8px 0;border:1px solid #333;border-radius:8px;background:#0c0c14;color:#fff}
button{background:#ff416c;color:white;border:0;font-weight:bold;cursor:pointer}
</style></head><body><div class="box"><h2>🔐 Aarohi AI Admin</h2>
<form method="post"><input name="email" type="email" placeholder="Admin Email" required>
<input name="password" type="password" placeholder="Admin Password" required>
<button type="submit">Login</button></form><p style="color:#ff6b6b">{{ error }}</p></div></body></html>
"""

ADMIN_PANEL_HTML = """
<!doctype html><html lang="hi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Aarohi AI Admin Panel</title>
<style>
body{font-family:Arial;background:#f5f3f8;color:#222;margin:0;padding:16px}
.wrap{max-width:1000px;margin:auto}.card{background:white;padding:16px;border-radius:14px;margin:14px 0;overflow:auto}
table{width:100%;border-collapse:collapse;min-width:600px}
td,th{padding:10px;border-bottom:1px solid #eee;text-align:left}
button{padding:7px 11px;border:0;border-radius:7px;background:#7352bd;color:white;margin:2px;cursor:pointer}
.red{background:#bd3d55}.green{background:#238653}
.top{display:flex;justify-content:space-between;align-items:center}
</style></head><body><div class="wrap">
<div class="top"><h2>💜 Aarohi AI Admin Panel</h2><button class="red" onclick="logout()">Logout</button></div>
<div class="card"><h3>👥 Users</h3><div id="users">Loading...</div></div>
<div class="card"><h3>💳 UPI Payment Requests</h3><div id="payments">Loading...</div></div>
</div><script>
async function api(url,method='GET',data=null){
 let opt={method,headers:{'Content-Type':'application/json'}};
 if(data)opt.body=JSON.stringify(data);
 const r=await fetch(url,opt); return r.json();
}
async function loadAll(){
 const u=await api('/api/admin/users');
 document.getElementById('users').innerHTML='<table><tr><th>ID</th><th>Email</th><th>Status</th><th>Credits</th><th>Action</th></tr>'+
 u.map(x=>`<tr><td>${x.id}</td><td>${x.email}</td><td>${x.status}</td><td>${x.credits}</td><td>
 <button class="green" onclick="setStatus(${x.id},'approved')">Approve</button>
 <button class="red" onclick="setStatus(${x.id},'blocked')">Block</button>
 <button onclick="addCred(${x.id})">+Credits</button></td></tr>`).join('')+'</table>';

 const p=await api('/api/admin/payments');
 document.getElementById('payments').innerHTML='<table><tr><th>ID</th><th>User</th><th>₹</th><th>Credits</th><th>UTR</th><th>Status</th><th>Action</th></tr>'+
 p.map(x=>`<tr><td>${x.id}</td><td>${x.email}</td><td>${x.amount}</td><td>${x.credits}</td><td>${x.utr}</td><td>${x.status}</td><td>
 ${x.status==='pending'?`<button class="green" onclick="payAct(${x.id},'approve')">Approve</button>
 <button class="red" onclick="payAct(${x.id},'reject')">Reject</button>`:x.status}</td></tr>`).join('')+'</table>';
}
async function setStatus(id,status){await api('/api/admin/user_status','POST',{user_id:id,status});loadAll();}
async function addCred(id){let n=parseInt(prompt('कितने Credits जोड़ने हैं?','50'));if(n){await api('/api/admin/add_credits','POST',{user_id:id,credits:n});loadAll();}}
async function payAct(id,action){await api('/api/admin/payment_action','POST',{payment_id:id,action});loadAll();}
async function logout(){await fetch('/api/admin/logout',{method:'POST'});location.href='/admin';}
loadAll();
</script></body></html>
"""


@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    if session.get("admin"):
        return render_template_string(ADMIN_PANEL_HTML)
    error = ""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if ADMIN_EMAIL and ADMIN_PASSWORD and hmac.compare_digest(email, ADMIN_EMAIL) and hmac.compare_digest(password, ADMIN_PASSWORD):
            session["admin"] = True
            return redirect("/admin")
        error = "गलत Admin Email या Password"
    return render_template_string(ADMIN_LOGIN_HTML, error=error)


@app.post("/api/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return jsonify(ok=True)


@app.get("/api/admin/users")
@admin_required
def admin_users():
    with db() as c:
        rows = c.execute("SELECT id,email,status,credits FROM users ORDER BY id DESC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/admin/user_status")
@admin_required
def admin_user_status():
    d = request.get_json(silent=True) or {}
    with db() as c:
        c.execute("UPDATE users SET status=? WHERE id=?", (d.get("status"), d.get("user_id")))
    return jsonify(ok=True)


@app.post("/api/admin/add_credits")
@admin_required
def admin_add_credits():
    d = request.get_json(silent=True) or {}
    with db() as c:
        c.execute("UPDATE users SET credits=credits+? WHERE id=?", (int(d.get("credits", 0)), d.get("user_id")))
    return jsonify(ok=True)


@app.get("/api/admin/payments")
@admin_required
def admin_payments():
    with db() as c:
        rows = c.execute("""
            SELECT p.*, u.email FROM payments p
            JOIN users u ON u.id=p.user_id ORDER BY p.id DESC
        """).fetchall()
    return jsonify([dict(r) for r in rows])

@app.post("/api/admin/payment_action")
@admin_required
def admin_payment_action():
    d = request.get_json(silent=True) or {}
    pid = d.get("payment_id")
    action = d.get("action")
    with db() as c:
        pay = c.execute("SELECT * FROM payments WHERE id=? AND status='pending'", (pid,)).fetchone()
        if not pay:
            return jsonify(error="Payment not found"), 404
        if action == "approve":
            c.execute("UPDATE payments SET status='approved', approved_at=CURRENT_TIMESTAMP WHERE id=?", (pid,))
            c.execute("UPDATE users SET credits=credits+? WHERE id=?", (pay["credits"], pay["user_id"]))
        else:
            c.execute("UPDATE payments SET status='rejected' WHERE id=?", (pid,))
    return jsonify(ok=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
