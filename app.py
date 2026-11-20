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
        return "अभी AI जवाब नहीं दे पाया। थोड़ी देर बाद कोशिश करें।", False


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
    if len(text_value) > 10000:
        return jsonify(error="Message बहुत लंबा है"), 400
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
body{font-family:Arial;background:#f5f3ef;padding:18px;color:#222}
.card{max-width:440px;margin:25px auto;background:white;padding:22px;border-radius:18px;box-shadow:0 5px 25px #ddd}
input,button{width:100%;box-sizing:border-box;padding:13px;margin:8px 0;border:1px solid #ddd;border-radius:9px}
button{background:#7352bd;color:white;border:0;font-weight:bold}a{color:#7352bd}
.balance{text-align:center;font-size:25px;color:#7352bd}
</style></head><body><div class="card">
<h2 style="text-align:center">💜 Aarohi AI Wallet</h2>
<div class="balance">Credits: <b id="balance">...</b></div>
<p>1 Credit = ₹<span id="price">1</span></p>
<label>कितने Credits खरीदने हैं?</label><input id="credits" type="number" min="1" max="100000" value="100">
<p>कुल पेमेंट: ₹<b id="amount">100</b></p><p>UPI ID: <b id="upi"></b></p>
<button onclick="pay()">UPI App से Pay करें</button><hr>
<p>पेमेंट के बाद Transaction UTR डालें:</p>
<input id="utr" placeholder="UPI Transaction ID / UTR">
<button onclick="submitPayment()">Payment Verify करवाएँ</button>
<p id="msg"></p><a href="/">वापस Chat पर जाएँ</a>
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
body{font-family:Arial;background:#f4f1f8;padding:20px}.box{max-width:380px;margin:60px auto;background:white;padding:24px;border-radius:16px}
input,button{width:100%;box-sizing:border-box;padding:13px;margin:8px 0;border:1px solid #ddd;border-radius:8px}
button{background:#7352bd;color:white;border:0}
</style></head><body><div class="box"><h2>🔐 Aarohi AI Admin</h2>
<form method="post"><input name="email" type="email" placeholder="Admin Email" required>
<input name="password" type="password" placeholder="Admin Password" required>
<button type="submit">Login</button></form><p style="color:#b00">{{ error }}</p></div></body></html>
"""

ADMIN_PANEL_HTML = """
<!doctype html><html lang="hi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Aarohi AI Admin Panel</title>
<style>
body{font-family:Arial;background:#f5f3f8;color:#222;margin:0;padding:16px}
.wrap{max-width:1000px;margin:auto}.card{background:white;padding:16px;border-radius:14px;margin:14px 0;overflow:auto}
h2{margin-top:4px}table{width:100%;border-collapse:collapse;min-width:650px}
td,th{padding:10px;border-bottom:1px solid #eee;text-align:left}
button{padding:8px 12px;border:0;border-radius:7px;background:#7352bd;color:white;margin:3px;cursor:pointer}
.red{background:#bd3d55}.green{background:#238653}
input{padding:9px;border:1px solid #ddd;border-radius:7px;max-width:130px}
a{color:#7352bd}.top{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}
</style></head><body><div class="wrap">
<div class="top"><h2>💜 Aarohi AI Admin Panel</h2><button class="red" onclick="logout()">Logout</button></div>
<div class="card"><h3>👥 Users / Manual Approval</h3><div id="users">Loading...</div></div>
<div class="card"><h3>💳 UPI Payment Requests</h3><div id="payments">Loading...</div></div>
</div><script>
async function api(url,method='GET',data=null){
 let opt={method,headers:{'Content-Type':'application/json'}};
 if(data)opt.body=JSON.stringify(data);
 let r=await fetch(url,opt),d=await r.json();
 if(!r.ok)throw Error(d.error||'Request failed');return d;
}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
async function loadUsers(){
 let rows=await api('/api/admin/users');
 if(!rows.length){users.innerText='अभी कोई User नहीं है';return;}
 users.innerHTML='<table><tr><th>ID</th><th>Email</th><th>Status</th><th>Credits</th><th>Action</th></tr>'+
 rows.map(u=>`<tr><td>${u.id}</td><td>${esc(u.email)}</td><td>${esc(u.status)}</td><td>${u.credits}</td><td>
 ${u.status!=='approved'?`<button class="green" onclick="userAction(${u.id},'approve')">Approve</button>`:''}
 ${u.status!=='blocked'?`<button class="red" onclick="userAction(${u.id},'block')">Block</button>`:`<button onclick="userAction(${u.id},'approve')">Unblock</button>`}
 <input id="credit${u.id}" type="number" min="1" placeholder="Credits">
 <button onclick="addCredits(${u.id})">Add Credits</button></td></tr>`).join('')+'</table>';
}
async function loadPayments(){
 let rows=await api('/api/admin/payments');
 if(!rows.length){payments.innerText='अभी कोई Payment Request नहीं है';return;}
 payments.innerHTML='<table><tr><th>ID</th><th>User</th><th>Amount</th><th>Credits</th><th>UTR</th><th>Status</th><th>Action</th></tr>'+
 rows.map(p=>`<tr><td>${p.id}</td><td>${esc(p.email)}</td><td>₹${p.amount}</td><td>${p.credits}</td><td>${esc(p.utr)}</td><td>${esc(p.status)}</td><td>
 ${p.status==='pending'?`<button class="green" onclick="paymentAction(${p.id},'approve')">Approve + Credits</button><button class="red" onclick="paymentAction(${p.id},'reject')">Reject</button>`:''}
 </td></tr>`).join('')+'</table>';
}
async function userAction(id,action){try{await api('/api/admin/user/'+id,'POST',{action});await loadUsers();}catch(e){alert(e.message);}}
async function addCredits(id){let n=parseInt(document.getElementById('credit'+id).value)||0;if(n<1)return alert('Credits डालें');try{await api('/api/admin/user/'+id+'/credits','POST',{credits:n});await loadUsers();}catch(e){alert(e.message);}}
async function paymentAction(id,action){if(!confirm(action==='approve'?'Payment verify करके Credits जोड़ें?':'Payment reject करें?'))return;try{await api('/api/admin/payment/'+id,'POST',{action});await loadPayments();await loadUsers();}catch(e){alert(e.message);}}
async function logout(){await fetch('/admin/logout',{method:'POST'});location.href='/admin';}
loadUsers();loadPayments();
</script></body></html>
"""


@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    if session.get("admin"):
        return render_template_string(ADMIN_PANEL_HTML)
    if request.method == "POST":
        if not admin_configured():
            return render_template_string(ADMIN_LOGIN_HTML, error="Render में ADMIN_EMAIL और ADMIN_PASSWORD सेट करें"), 503
        email = str(request.form.get("email", "")).strip().lower()
        password = str(request.form.get("password", ""))
        if hmac.compare_digest(email, ADMIN_EMAIL) and hmac.compare_digest(password, ADMIN_PASSWORD):
            session.clear()
            session["admin"] = True
            session.permanent = True
            return redirect("/admin")
        return render_template_string(ADMIN_LOGIN_HTML, error="Email या Password गलत है"), 401
    return render_template_string(ADMIN_LOGIN_HTML, error="")


@app.post("/admin/logout")
def admin_logout():
    session.clear()
    return redirect("/admin")


@app.get("/api/admin/users")
@admin_required
def admin_users():
    with db() as c:
        rows = c.execute("""
            SELECT id,email,status,credits,created_at FROM users ORDER BY id DESC
        """).fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/admin/user/<int:user_id>")
@admin_required
def admin_user_action(user_id):
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    if action not in ("approve", "block"):
        return jsonify(error="गलत Action"), 400
    status = "approved" if action == "approve" else "blocked"
    with db() as c:
        cur = c.execute("UPDATE users SET status=? WHERE id=?", (status, user_id))
    if not cur.rowcount:
        return jsonify(error="User नहीं मिला"), 404
    return jsonify(ok=True, status=status)


@app.post("/api/admin/user/<int:user_id>/credits")
@admin_required
def admin_add_credits(user_id):
    data = request.get_json(silent=True) or {}
    try:
        amount = int(data.get("credits", 0))
    except (ValueError, TypeError):
        return jsonify(error="गलत Credits"), 400
    if amount < 1 or amount > 1000000:
        return jsonify(error="1 से 1000000 तक Credits डालें"), 400
    with db() as c:
        cur = c.execute("UPDATE users SET credits=credits+? WHERE id=?", (amount, user_id))
    if not cur.rowcount:
        return jsonify(error="User नहीं मिला"), 404
    return jsonify(ok=True)


@app.get("/api/admin/payments")
@admin_required
def admin_payments():
    with db() as c:
        rows = c.execute("""
            SELECT p.id,p.user_id,u.email,p.amount,p.credits,p.utr,p.status,p.created_at
            FROM payments p JOIN users u ON u.id=p.user_id
            ORDER BY p.id DESC
        """).fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/admin/payment/<int:payment_id>")
@admin_required
def admin_payment_action(payment_id):
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    if action not in ("approve", "reject"):
        return jsonify(error="गलत Action"), 400
    with db() as c:
        payment = c.execute(
            "SELECT * FROM payments WHERE id=?", (payment_id,)
        ).fetchone()
        if not payment:
            return jsonify(error="Payment नहीं मिली"), 404
        if payment["status"] != "pending":
            return jsonify(error="यह Payment पहले ही Process हो चुकी है"), 409
        if action == "approve":
            c.execute(
                "UPDATE payments SET status='approved',approved_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'",
                (payment_id,),
            )
            c.execute(
                "UPDATE users SET credits=credits+? WHERE id=?",
                (payment["credits"], payment["user_id"]),
            )
        else:
            c.execute(
                "UPDATE payments SET status='rejected' WHERE id=? AND status='pending'",
                (payment_id,),
            )
    return jsonify(ok=True, status="approved" if action == "approve" else "rejected")


# -------------------- STARTUP --------------------
init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
    
