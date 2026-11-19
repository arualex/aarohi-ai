# Aarohi AI

A Flask starter for an original AI character chat platform.

## Run locally
1. Python 3.10+
2. `python -m venv .venv`
3. Activate the environment
4. `pip install -r requirements.txt`
5. Copy `.env.example` to `.env` and set `SECRET_KEY` and `OPENAI_API_KEY`
6. `python app.py`

## Deploy on Render
Push this folder to a GitHub repository, create a Render Web Service from it, and add `OPENAI_API_KEY` as a secret environment variable. `render.yaml` provides the initial configuration.

## Included
- Responsive chat interface
- Signup/login with hashed passwords
- Built-in Aarohi character and user-created characters
- Chat history stored in SQLite
- AI chat and AI Coach endpoint

## Before public launch
This is a functional starter, not a production-audited service. Add CSRF protection, rate limits, email verification, password reset, consent/age gate appropriate to your audience, moderation, privacy policy, database backups, and migrate SQLite to managed PostgreSQL for persistent production hosting. Voice calls require a speech-to-text/text-to-speech provider and are not enabled in this starter. Never commit API keys to GitHub.
