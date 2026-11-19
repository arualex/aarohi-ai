import os, sqlite3
from functools import wraps
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-this-before-deploying")
DB = os.getenv("DATABASE_PATH", "aarohi.db")

def db():
    con=sqlite3.connect(DB); con.row_factory=sqlite3.Row; return con

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, email TEXT UNIQUE, password TEXT);
        CREATE TABLE IF NOT EXISTS characters(id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT, personality TEXT, greeting TEXT);
        CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, user_id INTEGER, character_id INTEGER, role TEXT, content TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP);
        """)
        if not c.execute("SELECT 1 FROM characters LIMIT 1").fetchone():
            c.execute("INSERT INTO characters(user_id,name,personality,greeting) VALUES(0,?,?,?)",
                      ("Aarohi","Friendly, warm, witty Hindi-speaking companion","हाय! मैं आरोही हूँ 😊 आज आपका दिन कैसा रहा?"))

def login_required(fn):
    @wraps(fn)
    def inner(*a,**kw):
        if not session.get("uid"): return jsonify(error="Login required"),401
        return fn(*a,**kw)
    return inner

@app.route("/")
def home(): return render_template("index.html")

@app.post("/api/register")
def register():
    d=request.get_json(); email=d.get("email","").strip().lower(); pw=d.get("password","")
    if not email or len(pw)<8: return jsonify(error="ईमेल और कम से कम 8 अक्षर का पासवर्ड दें"),400
    try:
        with db() as c: c.execute("INSERT INTO users(email,password) VALUES(?,?)",(email,generate_password_hash(pw)))
        return jsonify(ok=True)
    except sqlite3.IntegrityError: return jsonify(error="यह ईमेल पहले से रजिस्टर है"),409

@app.post("/api/login")
def login():
    d=request.get_json()
    with db() as c: u=c.execute("SELECT * FROM users WHERE email=?",(d.get("email","").lower(),)).fetchone()
    if not u or not check_password_hash(u["password"],d.get("password","")): return jsonify(error="ईमेल या पासवर्ड गलत है"),401
    session["uid"]=u["id"]; return jsonify(ok=True)

@app.post("/api/logout")
def logout(): session.clear(); return jsonify(ok=True)

@app.get("/api/characters")
def characters():
    with db() as c: rows=c.execute("SELECT id,name,personality,greeting FROM characters WHERE user_id IN (0,?)",(session.get("uid",-1),)).fetchall()
    return jsonify([dict(x) for x in rows])

@app.post("/api/characters")
@login_required
def create_character():
    d=request.get_json()
    if not d.get("name"): return jsonify(error="नाम जरूरी है"),400
    with db() as c:
        cur=c.execute("INSERT INTO characters(user_id,name,personality,greeting) VALUES(?,?,?,?)",
          (session["uid"],d["name"][:40],d.get("personality","Friendly and helpful")[:500],d.get("greeting","नमस्ते!")[:300]))
        return jsonify(id=cur.lastrowid)

def ai_reply(prompt, history, character):
    # Configure OPENAI_API_KEY and optionally OPENAI_MODEL in hosting environment.
    key=os.getenv("OPENAI_API_KEY")
    if not key: return "मैं अभी डेमो मोड में हूँ। असली AI जवाब चालू करने के लिए सर्वर में OPENAI_API_KEY सेट करें।"
    try:
        from openai import OpenAI
        client=OpenAI(api_key=key)
        msgs=[{"role":"system","content":f"You are {character['name']}. Personality: {character['personality']}. Reply naturally in the user's language. Be respectful and safe."}]
        msgs += [{"role":m["role"],"content":m["content"]} for m in history[-12:]]
        msgs.append({"role":"user","content":prompt})
        r=client.chat.completions.create(model=os.getenv("OPENAI_MODEL","gpt-4o-mini"),messages=msgs)
        return r.choices[0].message.content
    except Exception as e:
        app.logger.exception("AI request failed")
        return "अभी जवाब नहीं मिल पाया। थोड़ी देर बाद फिर कोशिश करें।"

@app.post("/api/chat")
@login_required
def chat():
    d=request.get_json(); cid=int(d.get("character_id",0)); msg=d.get("message","").strip()
    if not msg: return jsonify(error="खाली मैसेज"),400
    with db() as c:
        ch=c.execute("SELECT * FROM characters WHERE id=? AND user_id IN (0,?)",(cid,session["uid"])).fetchone()
        if not ch: return jsonify(error="Character नहीं मिला"),404
        hist=c.execute("SELECT role,content FROM messages WHERE user_id=? AND character_id=? ORDER BY id DESC LIMIT 12",(session["uid"],cid)).fetchall()
        history=[dict(x) for x in reversed(hist)]
        c.execute("INSERT INTO messages(user_id,character_id,role,content) VALUES(?,?,?,?)",(session["uid"],cid,"user",msg))
        reply=ai_reply(msg,history,dict(ch))
        c.execute("INSERT INTO messages(user_id,character_id,role,content) VALUES(?,?,?,?)",(session["uid"],cid,"assistant",reply))
    return jsonify(reply=reply)

@app.post("/api/coach")
@login_required
def coach():
    d=request.get_json(); text=d.get("text","").strip()
    if not text: return jsonify(error="मैसेज लिखें"),400
    return jsonify(suggestion=ai_reply("इस मैसेज का विश्लेषण करो और 3 बेहतर जवाब सुझाओ: "+text,[],{"name":"Aarohi AI Coach","personality":"Helpful communication coach"}))

@app.get("/health")
def health(): return {"status":"ok","app":"Aarohi AI"}

init_db()
if __name__=="__main__": app.run(host="0.0.0.0",port=int(os.getenv("PORT",5000)),debug=True)
