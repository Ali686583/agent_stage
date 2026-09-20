"""
server.py
=========

Backend d'authentification du projet agent_stage.

Reference d'architecture : page_dacceuil.py / librairies/database.py de
MyBusiness (meme principe general : un seul service sert les pages HTML et
l'API, avec le stockage entierement delegue a librairies/database.py,
jamais de SQL dans les routes).

Differences volontaires par rapport a MyBusiness (bonnes pratiques
actuelles plutot que reproduction a l'identique) :
  - Flask + gunicorn au lieu de http.server brut ;
  - mots de passe haches en Argon2id (argon2-cffi) au lieu de PBKDF2 ;
  - base PostgreSQL persistante (plugin Railway) au lieu de SQLite local ;
  - tokens de session et de reset stockes hachees (jamais en clair) ;
  - limitation du nombre de tentatives sur les routes sensibles.

Toutes les valeurs sensibles (base de donnees, email) viennent des
variables d'environnement Railway ; rien n'est ecrit en dur dans ce fichier.
"""

from __future__ import annotations

import io
import os

from flask import Flask, jsonify, request, send_file, send_from_directory
from PIL import Image
from werkzeug.middleware.proxy_fix import ProxyFix

from librairies import connections_bank, database, workflow_bank, workspace
from librairies.email_service import send_password_reset_email
from librairies.rate_limit import limiter
from librairies.security import generate_token, hash_token
from workspace_routes import UPLOAD_DIR, workspace_bp

PORT = int(os.environ.get("PORT", "8080"))
FRONTEND_URL = os.environ.get("FRONTEND_URL", "").rstrip("/")
SESSION_COOKIE_NAME = "agent_stage_session"
SESSION_TTL_DAYS = int(os.environ.get("SESSION_TTL_DAYS", "7"))
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() != "false"
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))

AVATAR_DIR = os.path.join(UPLOAD_DIR, "avatars")
AVATAR_MAX_UPLOAD_BYTES = int(os.environ.get("AVATAR_MAX_UPLOAD_BYTES", str(8 * 1024 * 1024)))
AVATAR_SIZE_PX = 256
ALLOWED_AVATAR_MIME = {"image/png", "image/jpeg", "image/webp"}
os.makedirs(AVATAR_DIR, exist_ok=True)

app = Flask(__name__, static_folder="frontend", static_url_path="")
# Doit couvrir le plus gros upload de document autorise (voir workspace_routes.py).
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES + 1_000_000

# Railway termine les connexions via son propre edge, avec un pool d'IP
# internes rotatives comme dernier maillon de X-Forwarded-For : ProxyFix
# seul ne suffit pas a retrouver l'IP reelle du client. Railway expose en
# revanche cette IP directement dans X-Real-Ip, verifie via /debug/ip.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

limiter.init_app(app)
app.register_blueprint(workspace_bp)

database.init_db()
workflow_bank.init_bank_db()
connections_bank.init_connections_db()
try:
    # Bouton integre "Resume Drive" (mission Google Drive §5) : idempotent,
    # ne doit jamais empecher le demarrage si la banque de boutons n'est pas
    # configuree sur cet environnement (meme garde que le reste de ce fichier).
    workflow_bank.ensure_google_drive_action()
except RuntimeError:
    pass


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
        samesite="Lax",
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


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


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
# Profil : pseudonyme, avatar, suppression de compte
# ---------------------------------------------------------------------------

@app.route("/api/auth/display-name", methods=["POST"])
@limiter.limit("10 per minute")
def set_display_name():
    user = _current_user()
    if not user:
        return _error(401, "Non authentifie.")
    data = request.get_json(silent=True) or {}
    display_name = str(data.get("displayName", ""))[:200]

    # Champ vide (ou uniquement des espaces) + Enregistrer == suppression du
    # pseudonyme : l'email redevient l'identite affichee. Ce n'est pas une
    # erreur de validation, c'est le comportement explicitement demande.
    if not display_name.strip():
        return jsonify(ok=True, user=database.clear_display_name(user["id"]))

    try:
        updated_user = database.set_display_name(user["id"], display_name)
    except ValueError as exc:
        return _error(400, str(exc))
    return jsonify(ok=True, user=updated_user)


@app.route("/api/auth/display-name", methods=["DELETE"])
def delete_display_name():
    user = _current_user()
    if not user:
        return _error(401, "Non authentifie.")
    updated_user = database.clear_display_name(user["id"])
    return jsonify(ok=True, user=updated_user)


@app.route("/api/auth/avatar", methods=["POST"])
@limiter.limit("10 per minute")
def upload_avatar():
    user = _current_user()
    if not user:
        return _error(401, "Non authentifie.")

    if "file" not in request.files:
        return _error(400, "Aucun fichier recu.")
    uploaded = request.files["file"]
    if not uploaded.filename:
        return _error(400, "Nom de fichier manquant.")
    if uploaded.mimetype not in ALLOWED_AVATAR_MIME:
        return _error(415, "Type d'image non autorise.")

    content = uploaded.read()
    if not content:
        return _error(400, "Fichier vide.")
    if len(content) > AVATAR_MAX_UPLOAD_BYTES:
        return _error(413, "Image trop volumineuse.")

    try:
        # verify() detecte un fichier corrompu / qui n'est pas une vraie
        # image ; il consomme le flux, donc on rouvre ensuite pour l'usage reel.
        Image.open(io.BytesIO(content)).verify()
        image = Image.open(io.BytesIO(content)).convert("RGB")
    except Exception:
        return _error(400, "Image invalide.")

    # Recadrage carre centre (jamais de deformation), puis redimensionnement :
    # on ne conserve jamais l'image originale, seulement une version optimisee.
    width, height = image.size
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2
    image = image.crop((left, top, left + side, top + side))
    image = image.resize((AVATAR_SIZE_PX, AVATAR_SIZE_PX), Image.LANCZOS)

    filename = f"{user['id']}.jpg"
    image.save(os.path.join(AVATAR_DIR, filename), format="JPEG", quality=85, optimize=True)

    updated_user = database.set_avatar_reference(user["id"], filename)
    return jsonify(ok=True, user=updated_user)


@app.route("/api/auth/avatar/<user_id>", methods=["GET"])
def get_avatar(user_id):
    # Visible par tout utilisateur authentifie (pas seulement le
    # proprietaire) : les avatars apparaissent dans l'historique partage de
    # l'espace collaboratif, au meme titre que le pseudonyme.
    if not _current_user():
        return _error(401, "Non authentifie.")
    reference = database.get_avatar_reference(user_id)
    if not reference:
        return _error(404, "Aucun avatar.")
    path = os.path.join(AVATAR_DIR, reference)
    if not os.path.isfile(path):
        return _error(404, "Aucun avatar.")
    return send_file(path, mimetype="image/jpeg")


@app.route("/api/auth/account", methods=["DELETE"])
@limiter.limit("5 per minute")
def delete_account():
    user = _current_user()
    if not user:
        return _error(401, "Non authentifie.")

    old_avatar = database.delete_account(user["id"])

    # Fichiers de l'espace collaboratif strictement personnels (jamais
    # rattaches a un message, donc jamais partages) : supprimes pour de bon,
    # disque + base. Les fichiers deja partages dans une conversation restent
    # (donnees collaboratives, voir la politique dans librairies/workspace.py).
    orphan_files = workspace.list_orphan_files_for_user(user["id"])
    for file in orphan_files:
        storage_reference = workspace.get_file_storage_reference(file["id"])
        if storage_reference:
            path = os.path.join(UPLOAD_DIR, storage_reference)
            if os.path.isfile(path):
                os.remove(path)
    workspace.delete_files([f["id"] for f in orphan_files])

    if old_avatar:
        avatar_path = os.path.join(AVATAR_DIR, old_avatar)
        if os.path.isfile(avatar_path):
            os.remove(avatar_path)

    resp = jsonify(ok=True)
    resp.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return resp


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
