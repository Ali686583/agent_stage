"""
server.py
=========

Backend d'authentification du projet agent_stage.

Reference d'architecture : page_dacceuil.py / librairies/database.py de
MyBusiness (meme principe general : un point d'entree HTTP qui delegue tout
le stockage a librairies/database.py, jamais de SQL dans les routes).

Differences volontaires par rapport a MyBusiness (bonnes pratiques
actuelles plutot que reproduction a l'identique) :
  - Flask + gunicorn au lieu de http.server brut ;
  - mots de passe haches en Argon2id (argon2-cffi) au lieu de PBKDF2 ;
  - base PostgreSQL persistante (plugin Railway) au lieu de SQLite local ;
  - tokens de session et de reset stockes hachees (jamais en clair) ;
  - CORS restreint a une liste d'origines explicites (jamais '*') ;
  - limitation du nombre de tentatives sur les routes sensibles.

Toutes les valeurs sensibles (base de donnees, SMTP, origines autorisees)
viennent des variables d'environnement Railway ; rien n'est ecrit en dur
dans ce fichier.
"""

from __future__ import annotations

import os

from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from librairies import database
from librairies.email_service import send_password_reset_email
from librairies.security import generate_token, hash_token

PORT = int(os.environ.get("PORT", "8080"))
FRONTEND_URL = os.environ.get("FRONTEND_URL", "").rstrip("/")
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]
SESSION_COOKIE_NAME = "agent_stage_session"
SESSION_TTL_DAYS = int(os.environ.get("SESSION_TTL_DAYS", "7"))
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() != "false"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1_000_000  # 1 Mo, largement suffisant pour ces routes

CORS(
    app,
    resources={r"/api/*": {"origins": ALLOWED_ORIGINS or []}},
    supports_credentials=True,
)

limiter = Limiter(get_remote_address, app=app, default_limits=[], storage_uri="memory://")

database.init_db()


# ---------------------------------------------------------------------------
# Aides
# ---------------------------------------------------------------------------

def _error(status: int, message: str):
    return jsonify(ok=False, error=message), status


def _set_session_cookie(resp, user_id: str) -> None:
    raw_token = generate_token()
    database.create_session(user_id, hash_token(raw_token))
    resp.set_cookie(
        SESSION_COOKIE_NAME,
        raw_token,
        max_age=SESSION_TTL_DAYS * 86400,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="None" if COOKIE_SECURE else "Lax",
        path="/",
    )


def _current_user():
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    return database.user_for_session(hash_token(token))


# ---------------------------------------------------------------------------
# Sante
# ---------------------------------------------------------------------------

@app.route("/health")
def health():
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# Authentification
# ---------------------------------------------------------------------------

@app.route("/api/auth/register", methods=["POST"])
@limiter.limit("5 per minute")
def register():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", ""))[:200]
    email = str(data.get("email", ""))[:200]
    password = str(data.get("password", ""))[:200]

    try:
        user = database.create_user(username, email, password)
    except ValueError as exc:
        return _error(409, str(exc))
    except Exception:
        app.logger.exception("register failed")
        return _error(500, "Erreur serveur.")

    resp = jsonify(ok=True, user=user)
    _set_session_cookie(resp, user["id"])
    return resp


@app.route("/api/auth/login", methods=["POST"])
@limiter.limit("10 per minute")
def login():
    data = request.get_json(silent=True) or {}
    identifier = str(data.get("identifier", ""))[:200]
    password = str(data.get("password", ""))[:200]

    if not identifier or not password:
        return _error(400, "Identifiant et mot de passe requis.")

    user = database.authenticate_user(identifier, password)
    if not user:
        return _error(401, "Identifiant ou mot de passe incorrect.")

    resp = jsonify(ok=True, user=user)
    _set_session_cookie(resp, user["id"])
    return resp


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        database.delete_session(hash_token(token))
    resp = jsonify(ok=True)
    resp.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return resp


@app.route("/api/auth/me", methods=["GET"])
def me():
    user = _current_user()
    if not user:
        return _error(401, "Non authentifie.")
    return jsonify(ok=True, user=user)


# ---------------------------------------------------------------------------
# Mot de passe oublie
# ---------------------------------------------------------------------------

@app.route("/api/auth/forgot-password", methods=["POST"])
@limiter.limit("3 per minute")
def forgot_password():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", ""))[:200]

    # Reponse volontairement identique dans tous les cas : ne revele jamais
    # si l'adresse existe ou non dans la base.
    generic = jsonify(
        ok=True,
        message="Si un compte existe avec cette adresse, un email de reinitialisation a ete envoye.",
    )

    user = database.get_user_by_email(email)
    if user and FRONTEND_URL:
        raw_token = generate_token()
        database.create_reset_token(user["id"], hash_token(raw_token))
        reset_link = f"{FRONTEND_URL}/reset-password.html?token={raw_token}"
        try:
            send_password_reset_email(user["email"], reset_link)
        except Exception:
            app.logger.exception("echec envoi email de reinitialisation")

    return generic


@app.route("/api/auth/reset-password", methods=["POST"])
@limiter.limit("5 per minute")
def reset_password():
    data = request.get_json(silent=True) or {}
    token = str(data.get("token", ""))[:500]
    password = str(data.get("password", ""))[:200]

    if not token or not password:
        return _error(400, "Requete invalide.")

    user_id = database.consume_reset_token(hash_token(token))
    if not user_id:
        return _error(400, "Ce lien de reinitialisation est invalide ou a expire.")

    try:
        database.set_password(user_id, password)
    except ValueError as exc:
        return _error(400, str(exc))

    database.delete_all_sessions_for_user(user_id)
    database.invalidate_reset_tokens_for_user(user_id)
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# Erreurs generiques
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def not_found(_exc):
    return _error(404, "Route inconnue.")


@app.errorhandler(429)
def rate_limited(_exc):
    return _error(429, "Trop de tentatives, reessayez plus tard.")


@app.errorhandler(500)
def server_error(_exc):
    return _error(500, "Erreur serveur.")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
