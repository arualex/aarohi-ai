
import os
import sqlite3
import secrets
import logging
from functools import wraps

from flask import Flask, render_template, request, jsonify, session
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)

app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)

DB = os.getenv("DATABASE_PATH", "aarohi.db")


# ==============================
# DATABASE
# ==============================

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY,
                email TEXT UNIQUE,
                password TEXT
            );

            CREATE TABLE IF NOT EXISTS characters(
                id INTEGER PRIMARY KEY,
                user_id INTEGER,
                name TEXT,
                personality TEXT,
                greeting TEXT
            );

            CREATE TABLE IF NOT EXISTS messages(
                id INTEGER PRIMARY KEY,
                user_id INTEGER,
                character_id INTEGER,
                role TEXT,
                content TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)

        default = c.execute(
            "SELECT id FROM characters WHERE user_id=0 LIMIT 1"
        ).fetchone()

        if not default:
            c.execute("""
                INSERT INTO characters
                (user_id, name, personality, greeting)
                VALUES(0, ?, ?, ?)
            """, (
                "Aarohi",
                "Friendly, warm, caring, witty Hindi-speaking AI companion",
                "हाय! मैं आरोही हूँ 😊 आज आपका दिन कैसा रहा?"
            ))


# ==============================
# LOGIN SECURITY
# ==============================

def login_required(fn):
    @wraps(fn)
    def inner(*args, **kwargs):
        if not session.get("uid"):
            return jsonify(error="कृपया पहले Login करें"), 401
        return fn(*args, **kwargs)
    return inner


# ==============================
# HOME
# ==============================

@app.route("/")
def home():
    return render_template("index.html")


# ==============================
# REGISTER
# ==============================

@app.post("/api/register")
def register():
    d = request.get_json(silent=True) or {}

    email = d.get("email", "").strip().lower()
    password = d.get("password", "")

    if not email or len(password) < 8:
        return jsonify(
            error="ईमेल और कम से कम 8 अक्षर का पासवर्ड दें"
        ), 400

    try:
        with db() as c:
            c.execute(
                "INSERT INTO users(email,password) VALUES(?,?)",
                (email, generate_password_hash(password))
            )

        return jsonify(ok=True)

    except sqlite3.IntegrityError:
        return jsonify(error="यह ईमेल पहले से रजिस्टर है"), 409


# ==============================
# LOGIN
# ==============================

@app.post("/api/login")
def login():
    d = request.get_json(silent=True) or {}

    email = d.get("email", "").strip().lower()
    password = d.get("password", "")

    with db() as c:
        user = c.execute(
            "SELECT * FROM users WHERE email=?",
            (email,)
        ).fetchone()

    if not user or not check_password_hash(
        user["password"], password
    ):
        return jsonify(error="ईमेल या पासवर्ड गलत है"), 401

    session.clear()
    session["uid"] = user["id"]

    return jsonify(ok=True)


# ==============================
# LOGOUT
# ==============================

@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


# ==============================
# CHARACTER LIST
# ==============================

@app.get("/api/characters")
def characters():
    with db() as c:
        rows = c.execute("""
            SELECT id, name, personality, greeting
            FROM characters
            WHERE user_id IN (0, ?)
            ORDER BY id
        """, (session.get("uid", -1),)).fetchall()

    return jsonify([dict(row) for row in rows])


# ==============================
# CREATE CHARACTER
# ==============================

@app.post("/api/characters")
@login_required
def create_character():
    d = request.get_json(silent=True) or {}

    name = d.get("name", "").strip()

    if not name:
        return jsonify(error="Character का नाम जरूरी है"), 400

    with db() as c:
        cur = c.execute("""
            INSERT INTO characters
            (user_id, name, personality, greeting)
            VALUES(?, ?, ?, ?)
        """, (
            session["uid"],
            name[:40],
            d.get("personality", "Friendly and helpful")[:500],
            d.get("greeting", "नमस्ते!")[:300]
        ))

    return jsonify(id=cur.lastrowid)


# ==============================
# GEMINI AI ENGINE
# ==============================

def ai_reply(prompt, history, character):

    key = os.getenv("GEMINI_API_KEY")

    if not key:
        return "Gemini API Key सेट नहीं है। Render Environment चेक करें।"

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=key)

        system_instruction = (
            f"You are {character['name']}. "
            f"Your personality is {character['personality']}. "
            "You are a warm, friendly and caring AI companion. "
            "Reply naturally in the language used by the user. "
            "Speak Hindi when the user speaks Hindi. "
            "Keep conversations natural, respectful and safe. "
            "Remember the conversation context. "
            "Do not claim to be a human."
        )

        contents = []

        for item in history[-12:]:
            if item["role"] in ("user", "assistant"):
                role = (
                    "model"
                    if item["role"] == "assistant"
                    else "user"
                )

                contents.append(
                    types.Content(
                        role=role,
                        parts=[
                            types.Part(text=item["content"])
                        ]
                    )
                )

        contents.append(
            types.Content(
                role="user",
                parts=[types.Part(text=prompt)]
            )
        )

        response = client.models.generate_content(
            model=os.getenv(
                "GEMINI_MODEL",
                "gemini-3.6-flash"
            ),
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                max_output_tokens=700
            )
        )

        answer = response.text

        if answer:
            return answer.strip()

        return "मुझे फिर से बताइए 😊"

    except Exception:
        app.logger.exception("Gemini API Error")
        return (
            "अभी जवाब नहीं मिल पाया। "
            "थोड़ी देर बाद फिर कोशिश करें।"
        )


# ==============================
# CHAT API
# ==============================

@app.post("/api/chat")
@login_required
def chat():

    d = request.get_json(silent=True) or {}

    try:
        cid = int(d.get("character_id", 0))
    except (ValueError, TypeError):
        return jsonify(error="गलत Character ID"), 400

    msg = d.get("message", "").strip()

    if not msg:
        return jsonify(error="खाली मैसेज नहीं भेज सकते"), 400

    if len(msg) > 10000:
        return jsonify(error="मैसेज बहुत लंबा है"), 400

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
                WHERE id=? AND user_id IN (0, ?)
            """, (cid, session["uid"])).fetchone()

        if not character:
            return jsonify(error="Aarohi Character नहीं मिला"), 404

        actual_id = character["id"]

        history_rows = c.execute("""
            SELECT role, content
            FROM messages
            WHERE user_id=? AND character_id=?
            ORDER BY id DESC LIMIT 12
        """, (session["uid"], actual_id)).fetchall()

        history = [
            dict(row) for row in reversed(history_rows)
        ]

        c.execute("""
            INSERT INTO messages
            (user_id, character_id, role, content)
            VALUES(?, ?, ?, ?)
        """, (
            session["uid"],
            actual_id,
            "user",
            msg
        ))

        reply = ai_reply(
            msg,
            history,
            dict(character)
        )

        c.execute("""
            INSERT INTO messages
            (user_id, character_id, role, content)
            VALUES(?, ?, ?, ?)
        """, (
            session["uid"],
            actual_id,
            "assistant",
            reply
        ))

    return jsonify(reply=reply)


# ==============================
# AI COACH
# ==============================

@app.post("/api/coach")
@login_required
def coach():

    d = request.get_json(silent=True) or {}

    text = d.get("text", "").strip()

    if not text:
        return jsonify(error="मैसेज लिखें"), 400

    suggestion = ai_reply(
        "इस मैसेज का विश्लेषण करो और 3 बेहतर जवाब सुझाओ: " + text,
        [],
        {
            "name": "Aarohi AI Coach",
            "personality": "Helpful communication coach"
        }
    )

    return jsonify(suggestion=suggestion)


# ==============================
# HEALTH CHECK
# ==============================

@app.get("/health")
def health():
    return jsonify(
        status="ok",
        app="Aarohi AI"
    )


# ==============================
# INITIALIZE DATABASE
# ==============================

init_db()


# ==============================
# START SERVER
# ==============================

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", 5000)),
        debug=False
    )
    
