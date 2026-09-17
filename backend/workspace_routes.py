"""
workspace_routes.py
====================

Routes de l'espace collaboratif IA (page affichee juste apres connexion) :
projets, conversations, messages, upload/telechargement de documents, et le
pont vers les workflows n8n (ChatGPT / Claude).

Architecture (voir le prompt qui a demande cette page) :
  PAGE -> RAILWAY (ici) -> N8N -> workflow ChatGPT/Claude -> RAILWAY -> PAGE.
Le navigateur n'appelle jamais n8n directement, et ne choisit jamais
librement un workflow : seuls les modeles/actions de l'allowlist ci-dessous
sont acceptes, valides ici avant tout appel sortant.

L'identite (user_id, author_name) est TOUJOURS derivee de la session
authentifiee (cookie), jamais d'une valeur envoyee par le navigateur dans
le corps de la requete.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
import time
import uuid

import requests
from flask import Blueprint, Response, jsonify, request, send_file, stream_with_context

from librairies import database, realtime, workspace
from librairies.rate_limit import limiter
from librairies.security import hash_token

SESSION_COOKIE_NAME = "agent_stage_session"

UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "/data/uploads")
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
MAX_FILES_PER_MESSAGE = int(os.environ.get("MAX_FILES_PER_MESSAGE", "5"))
FILE_SIGNING_SECRET = os.environ.get("FILE_SIGNING_SECRET", "")
FILE_LINK_TTL_SECONDS = int(os.environ.get("FILE_LINK_TTL_SECONDS", "600"))

# Deux webhooks distincts (un par modele) : chacun peut etre construit comme
# un workflow n8n independant et simple, plutot qu'un seul workflow qui
# devrait brancher lui-meme sur le modele.
N8N_WEBHOOK_CHATGPT_URL = os.environ.get("N8N_WEBHOOK_CHATGPT_URL", "")
N8N_WEBHOOK_CLAUDE_URL = os.environ.get("N8N_WEBHOOK_CLAUDE_URL", "")
N8N_WEBHOOK_SECRET = os.environ.get("N8N_WEBHOOK_SECRET", "")
N8N_TIMEOUT_SECONDS = int(os.environ.get("N8N_TIMEOUT_SECONDS", "90"))

ALLOWED_MIME_TYPES = {
    "application/pdf",
    "text/plain",
    "text/csv",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
    "application/msword",
    "image/png",
    "image/jpeg",
    "image/webp",
}

ALLOWED_MODELS = {"chatgpt", "claude"}

# Allowlist d'actions : le navigateur ne choisit jamais librement un workflow,
# seuls ces identifiants sont acceptes puis transmis a n8n.
ALLOWED_ACTIONS = {
    "analyze_document",
    "summarize_document",
    "compare_documents",
    "extract_data",
    "generate_chart",
    "generate_report",
}

os.makedirs(UPLOAD_DIR, exist_ok=True)

workspace_bp = Blueprint("workspace", __name__, url_prefix="/api/workspace")


# ---------------------------------------------------------------------------
# Aides
# ---------------------------------------------------------------------------

def _error(status: int, message: str):
    return jsonify(ok=False, error=message), status


def _current_user():
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    return database.user_for_session(hash_token(token))


def _require_user():
    user = _current_user()
    if not user:
        return None, _error(401, "Non authentifie.")
    return user, None


def _sign_file_token(file_id: str, expires_at: int) -> str:
    if not FILE_SIGNING_SECRET:
        raise RuntimeError("FILE_SIGNING_SECRET n'est pas configuree.")
    message = f"{file_id}:{expires_at}".encode("utf-8")
    return hmac.new(FILE_SIGNING_SECRET.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _safe_storage_name(file_id: str, original_name: str) -> str:
    _, ext = os.path.splitext(original_name)
    ext = "".join(ch for ch in ext if ch.isalnum() or ch == ".")[:10]
    return f"{file_id}{ext}"


def _serve_file(file_id: str):
    record = workspace.get_file(file_id)
    if not record:
        return _error(404, "Fichier introuvable.")
    storage_reference = workspace.get_file_storage_reference(file_id)
    storage_path = os.path.join(UPLOAD_DIR, storage_reference)
    if not os.path.isfile(storage_path):
        return _error(404, "Fichier introuvable.")
    return send_file(
        storage_path,
        mimetype=record["mimeType"],
        as_attachment=False,
        download_name=record["name"],
    )


# ---------------------------------------------------------------------------
# Projets
# ---------------------------------------------------------------------------

@workspace_bp.route("/projects", methods=["GET"])
def list_projects_route():
    _user, err = _require_user()
    if err:
        return err
    return jsonify(ok=True, projects=workspace.list_projects())


@workspace_bp.route("/projects", methods=["POST"])
@limiter.limit("20 per minute")
def create_project_route():
    user, err = _require_user()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", ""))[:200]
    try:
        project = workspace.create_project(user["id"], user["displayName"], name)
    except ValueError as exc:
        return _error(400, str(exc))
    return jsonify(ok=True, project=project)


@workspace_bp.route("/projects/<project_id>/conversations", methods=["GET"])
def list_project_conversations_route(project_id):
    user, err = _require_user()
    if err:
        return err
    if not workspace.project_exists(project_id):
        return _error(404, "Projet introuvable.")
    limit = request.args.get("limit", default=30, type=int)
    before = request.args.get("before")
    return jsonify(
        ok=True,
        conversations=workspace.list_project_conversations(project_id, user["id"], limit, before),
    )


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------

@workspace_bp.route("/conversations", methods=["GET"])
def list_conversations_route():
    user, err = _require_user()
    if err:
        return err
    limit = request.args.get("limit", default=30, type=int)
    before = request.args.get("before")
    return jsonify(ok=True, conversations=workspace.list_conversations(user["id"], limit, before))


@workspace_bp.route("/conversations/<conversation_id>", methods=["GET"])
def get_conversation_route(conversation_id):
    user, err = _require_user()
    if err:
        return err
    if not workspace.is_participant(conversation_id, user["id"]):
        # 404 plutot que 403 : ne pas reveler qu'une conversation existe a
        # quelqu'un qui n'y a pas acces.
        return _error(404, "Conversation introuvable.")
    conversation = workspace.get_conversation(conversation_id)
    if not conversation:
        return _error(404, "Conversation introuvable.")
    limit = request.args.get("limit", default=50, type=int)
    before = request.args.get("before")
    messages = workspace.list_messages(conversation_id, limit, before)
    for message in messages:
        message["attachments"] = workspace.list_message_attachments(message["id"])
    return jsonify(ok=True, conversation=conversation, messages=messages)


@workspace_bp.route("/conversations/<conversation_id>/projects", methods=["POST"])
def add_conversation_to_project_route(conversation_id):
    user, err = _require_user()
    if err:
        return err
    if not workspace.is_participant(conversation_id, user["id"]):
        return _error(404, "Conversation introuvable.")
    data = request.get_json(silent=True) or {}
    project_id = str(data.get("projectId", ""))
    if not project_id:
        return _error(400, "projectId requis.")
    try:
        workspace.add_conversation_to_project(project_id, conversation_id, user["id"])
    except ValueError as exc:
        return _error(404, str(exc))
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# Participants
# ---------------------------------------------------------------------------

@workspace_bp.route("/conversations/<conversation_id>/participants", methods=["GET"])
def list_participants_route(conversation_id):
    user, err = _require_user()
    if err:
        return err
    if not workspace.is_participant(conversation_id, user["id"]):
        return _error(404, "Conversation introuvable.")
    return jsonify(ok=True, participants=workspace.list_participants(conversation_id))


@workspace_bp.route("/conversations/<conversation_id>/participants", methods=["POST"])
@limiter.limit("20 per minute")
def add_participant_route(conversation_id):
    user, err = _require_user()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    identifier = str(data.get("identifier", ""))[:200].strip()
    if not identifier:
        return _error(400, "identifier requis.")
    target = database.get_user_by_identifier(identifier)
    if not target:
        return _error(404, "Utilisateur introuvable.")
    try:
        participants = workspace.add_participant(conversation_id, user["id"], target["id"])
    except LookupError as exc:
        return _error(404, str(exc))
    except PermissionError as exc:
        return _error(403, str(exc))
    except ValueError as exc:
        return _error(404, str(exc))
    return jsonify(ok=True, participants=participants)


@workspace_bp.route("/conversations/<conversation_id>/participants/<target_user_id>", methods=["DELETE"])
def remove_participant_route(conversation_id, target_user_id):
    user, err = _require_user()
    if err:
        return err
    try:
        participants = workspace.remove_participant(conversation_id, user["id"], target_user_id)
    except LookupError as exc:
        return _error(404, str(exc))
    except PermissionError as exc:
        return _error(403, str(exc))
    return jsonify(ok=True, participants=participants)


@workspace_bp.route("/conversations/<conversation_id>", methods=["PATCH"])
@limiter.limit("20 per minute")
def rename_conversation_route(conversation_id):
    user, err = _require_user()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    title = str(data.get("title", ""))[:200]
    try:
        conversation = workspace.rename_conversation(conversation_id, user["id"], title)
    except LookupError as exc:
        return _error(404, str(exc))
    except PermissionError as exc:
        return _error(403, str(exc))
    except ValueError as exc:
        return _error(400, str(exc))
    return jsonify(ok=True, conversation=conversation)


@workspace_bp.route("/conversations/<conversation_id>", methods=["DELETE"])
def delete_conversation_route(conversation_id):
    user, err = _require_user()
    if err:
        return err
    try:
        deleted = workspace.delete_conversation(conversation_id, user["id"])
    except PermissionError as exc:
        return _error(403, str(exc))
    if not deleted:
        return _error(404, "Conversation introuvable.")
    return jsonify(ok=True)


@workspace_bp.route("/projects/<project_id>", methods=["DELETE"])
def delete_project_route(project_id):
    user, err = _require_user()
    if err:
        return err
    try:
        deleted = workspace.delete_project(project_id, user["id"])
    except PermissionError as exc:
        return _error(403, str(exc))
    if not deleted:
        return _error(404, "Projet introuvable.")
    return jsonify(ok=True)


@workspace_bp.route("/projects/<project_id>", methods=["PATCH"])
@limiter.limit("20 per minute")
def rename_project_route(project_id):
    user, err = _require_user()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", ""))[:200]
    try:
        project = workspace.rename_project(project_id, user["id"], name)
    except LookupError as exc:
        return _error(404, str(exc))
    except PermissionError as exc:
        return _error(403, str(exc))
    except ValueError as exc:
        return _error(400, str(exc))
    return jsonify(ok=True, project=project)


# ---------------------------------------------------------------------------
# Temps reel : SSE (nouveaux messages) + typing (ephemere, jamais persiste)
# ---------------------------------------------------------------------------

def _sse_frame(event_id, event_type: str, data: dict) -> str:
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event_type}")
    lines.append(f"data: {json.dumps(data)}")
    return "\n".join(lines) + "\n\n"


@workspace_bp.route("/events", methods=["GET"])
def events_route():
    user, err = _require_user()
    if err:
        return err
    conversation_id = request.args.get("conversationId", "")
    if not conversation_id or not workspace.is_participant(conversation_id, user["id"]):
        return _error(404, "Conversation introuvable.")

    # EventSource renvoie automatiquement Last-Event-Id a la reconnexion ;
    # ?after=N ne sert qu'au tout premier appel (pas encore de reconnexion).
    raw_last_id = request.headers.get("Last-Event-ID") or request.args.get("after", "0")
    try:
        after_seq = int(raw_last_id)
    except (TypeError, ValueError):
        after_seq = 0

    def generate():
        # S'abonner AVANT de lire le rattrapage Postgres : un message publie
        # pendant la requete de rattrapage est ainsi mis en tampon par Redis
        # plutot que perdu (au pire il sera vu deux fois, filtre ci-dessous
        # via last_seq - jamais perdu).
        pubsub = realtime.subscribe(conversation_id)
        last_seq = after_seq
        try:
            for message in workspace.list_messages_after(conversation_id, after_seq):
                last_seq = max(last_seq, message["seq"] or 0)
                yield _sse_frame(message["seq"], "message.created", message)
            while True:
                raw = pubsub.get_message(timeout=20, ignore_subscribe_messages=True)
                if raw is None:
                    yield ": heartbeat\n\n"
                    continue
                try:
                    payload = json.loads(raw["data"])
                except (TypeError, ValueError, KeyError):
                    continue
                event_type = payload.get("type", "message")
                data = payload.get("data", {})
                if event_type == "message.created":
                    seq = data.get("seq") or 0
                    if seq <= last_seq:
                        continue  # deja envoye pendant le rattrapage
                    last_seq = seq
                    yield _sse_frame(seq, event_type, data)
                else:
                    # typing et futurs evenements ephemeres : jamais rejoues
                    # au rattrapage, pas d'id SSE (rien a "reconnecter").
                    yield _sse_frame(None, event_type, data)
        finally:
            pubsub.close()

    response = Response(stream_with_context(generate()), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    return response


@workspace_bp.route("/conversations/<conversation_id>/typing", methods=["POST"])
@limiter.limit("60 per minute")
def typing_route(conversation_id):
    user, err = _require_user()
    if err:
        return err
    if not workspace.is_participant(conversation_id, user["id"]):
        return _error(404, "Conversation introuvable.")
    try:
        realtime.publish_event(
            conversation_id,
            "typing",
            {"userId": user["id"], "displayName": user["displayName"], "avatarUrl": user["avatarUrl"]},
        )
    except Exception:
        pass  # ephemere et best-effort : ne jamais faire echouer cet appel
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# Actions autorisees
# ---------------------------------------------------------------------------

@workspace_bp.route("/actions", methods=["GET"])
def list_actions_route():
    _user, err = _require_user()
    if err:
        return err
    return jsonify(ok=True, actions=sorted(ALLOWED_ACTIONS))


# ---------------------------------------------------------------------------
# Resultats reutilisables
# ---------------------------------------------------------------------------

@workspace_bp.route("/results/<result_id>", methods=["GET"])
def get_result_route(result_id):
    _user, err = _require_user()
    if err:
        return err
    result = workspace.get_result(result_id)
    if not result:
        return _error(404, "Resultat introuvable.")
    return jsonify(ok=True, result=result)


# ---------------------------------------------------------------------------
# Fichiers
# ---------------------------------------------------------------------------

@workspace_bp.route("/files", methods=["POST"])
@limiter.limit("30 per minute")
def upload_file_route():
    user, err = _require_user()
    if err:
        return err

    if "file" not in request.files:
        return _error(400, "Aucun fichier recu.")
    uploaded = request.files["file"]
    if not uploaded.filename:
        return _error(400, "Nom de fichier manquant.")

    mime_type = uploaded.mimetype or mimetypes.guess_type(uploaded.filename)[0] or "application/octet-stream"
    if mime_type not in ALLOWED_MIME_TYPES:
        return _error(415, "Type de fichier non autorise.")

    content = uploaded.read()
    if not content:
        return _error(400, "Fichier vide.")
    if len(content) > MAX_UPLOAD_BYTES:
        return _error(413, "Fichier trop volumineux.")

    file_id = workspace.new_id("file")
    storage_name = _safe_storage_name(file_id, uploaded.filename)
    storage_path = os.path.join(UPLOAD_DIR, storage_name)
    with open(storage_path, "wb") as handle:
        handle.write(content)

    record = workspace.create_file_record(
        file_id=file_id,
        user_id=user["id"],
        original_name=uploaded.filename[:200],
        mime_type=mime_type,
        size_bytes=len(content),
        storage_reference=storage_name,
    )
    return jsonify(ok=True, file=record)


@workspace_bp.route("/files/<file_id>", methods=["GET"])
def download_file_route(file_id):
    user, err = _require_user()
    if err:
        return err
    if not workspace.user_can_access_file(file_id, user["id"]):
        return _error(403, "Acces refuse.")
    return _serve_file(file_id)


@workspace_bp.route("/files/<file_id>", methods=["PATCH"])
@limiter.limit("20 per minute")
def rename_file_route(file_id):
    user, err = _require_user()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", ""))[:200]
    try:
        file = workspace.rename_file(file_id, user["id"], name)
    except LookupError as exc:
        return _error(404, str(exc))
    except PermissionError as exc:
        return _error(403, str(exc))
    except ValueError as exc:
        return _error(400, str(exc))
    return jsonify(ok=True, file=file)


@workspace_bp.route("/files/<file_id>/signed", methods=["GET"])
def download_file_signed_route(file_id):
    """Utilise par n8n pour recuperer un document : lien temporaire signe
    (HMAC + expiration courte) plutot qu'une URL publique permanente, car
    n8n n'a pas de session navigateur/cookie."""
    expires_at = request.args.get("exp", type=int)
    token = request.args.get("token", "")
    if not expires_at or not token:
        return _error(400, "Lien invalide.")
    if int(time.time()) > expires_at:
        return _error(410, "Lien expire.")
    try:
        expected = _sign_file_token(file_id, expires_at)
    except RuntimeError:
        return _error(503, "Signature de fichiers non configuree.")
    if not hmac.compare_digest(expected, token):
        return _error(403, "Lien invalide.")
    return _serve_file(file_id)


# ---------------------------------------------------------------------------
# Envoi d'un message -> conversation -> n8n -> rich_response
# ---------------------------------------------------------------------------

@workspace_bp.route("/messages", methods=["POST"])
@limiter.limit("20 per minute")
def send_message_route():
    user, err = _require_user()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    conversation_id = data.get("conversationId") or None
    project_id = data.get("projectId") or None
    model = str(data.get("model", ""))
    message_text = str(data.get("message", ""))[:8000].strip()
    file_ids = data.get("fileIds") or []
    source_result_ids = data.get("sourceResultIds") or []
    action = data.get("action") or None
    request_id = str(data.get("requestId") or "")[:100] or str(uuid.uuid4())

    if model not in ALLOWED_MODELS:
        return _error(400, "Modele invalide.")
    if not message_text and not file_ids:
        return _error(400, "Message vide.")
    if not isinstance(file_ids, list) or len(file_ids) > MAX_FILES_PER_MESSAGE:
        return _error(400, "Nombre de fichiers invalide.")
    if not isinstance(source_result_ids, list):
        return _error(400, "sourceResultIds invalide.")

    action_id = None
    action_parameters = {}
    if action:
        action_id = str(action.get("id", ""))
        if action_id not in ALLOWED_ACTIONS:
            return _error(400, "Action non autorisee.")
        action_parameters = action.get("parameters") or {}

    for file_id in file_ids:
        if not workspace.user_can_access_file(file_id, user["id"]):
            return _error(403, "Acces refuse a un fichier.")
    for result_id in source_result_ids:
        if not workspace.get_result(result_id):
            return _error(404, "Resultat source introuvable.")

    # Idempotence : un meme requestId rejoue (retry reseau, double-clic) ne
    # redeclenche jamais un second workflow ni un second message utilisateur.
    existing_run = workspace.get_workflow_run_by_request_id(request_id)
    if existing_run:
        if not workspace.is_participant(existing_run["conversationId"], user["id"]):
            return _error(404, "Conversation introuvable.")
        if existing_run["status"] == "completed" and existing_run["resultId"]:
            result = workspace.get_result(existing_run["resultId"])
            return jsonify(
                ok=True,
                conversationId=existing_run["conversationId"],
                resultId=result["id"],
                blocks=result["response"].get("blocks", []),
                replay=True,
            )
        if existing_run["status"] == "running":
            return _error(409, "Cette requete est deja en cours de traitement.")

        # status 'failed' ou 'timeout' : on retente sur le MEME run et le
        # MEME message utilisateur (deja enregistres lors de la 1re tentative),
        # sans rien dupliquer en base.
        run_id = workspace.retry_workflow_run(request_id)
        if run_id is None:
            return _error(409, "Cette requete est deja en cours de traitement.")
        conversation_id = existing_run["conversationId"]
        conversation = workspace.get_conversation(conversation_id)
        user_message = workspace.get_message(existing_run["messageId"]) if existing_run["messageId"] else None
        is_new_conversation = False
    else:
        is_new_conversation = not conversation_id
        if is_new_conversation:
            try:
                conversation = workspace.create_conversation(
                    user["id"], user["displayName"], workspace.derive_title(message_text), project_id
                )
            except ValueError as exc:
                return _error(404, str(exc))
            conversation_id = conversation["id"]
        else:
            if not workspace.is_participant(conversation_id, user["id"]):
                return _error(404, "Conversation introuvable.")
            conversation = workspace.get_conversation(conversation_id)
            if not conversation:
                return _error(404, "Conversation introuvable.")

        user_message = workspace.add_message(
            conversation_id=conversation_id,
            user_id=user["id"],
            author_name=user["displayName"],
            role="user",
            content=message_text,
            model=model,
            action_id=action_id,
        )
        if file_ids:
            workspace.link_files_to_message(user_message["id"], file_ids)
        workspace.touch_conversation(conversation_id)

        run = workspace.start_workflow_run(conversation_id, user_message["id"], user["id"], model, request_id)
        if run is None:
            return _error(409, "Cette requete est deja en cours de traitement.")
        run_id = run["id"]

    webhook_url = N8N_WEBHOOK_CHATGPT_URL if model == "chatgpt" else N8N_WEBHOOK_CLAUDE_URL
    if not webhook_url:
        workspace.complete_workflow_run(run_id, status="failed", error="workflow_not_configured")
        return _error(503, f"Le workflow n8n pour {model} n'est pas encore configure.")

    file_links = []
    for file_id in file_ids:
        expires_at = int(time.time()) + FILE_LINK_TTL_SECONDS
        try:
            token = _sign_file_token(file_id, expires_at)
        except RuntimeError:
            workspace.complete_workflow_run(run_id, status="failed", error="file_signing_not_configured")
            return _error(503, "Signature de fichiers non configuree.")
        file_links.append(
            {
                "fileId": file_id,
                "url": f"{request.url_root.rstrip('/')}/api/workspace/files/{file_id}/signed?exp={expires_at}&token={token}",
            }
        )

    payload = {
        "requestId": request_id,
        "conversationId": conversation_id,
        "userId": user["id"],
        "userName": user["displayName"],
        "model": model,
        "message": message_text,
        "fileIds": file_ids,
        "files": file_links,
        "sourceResultIds": source_result_ids,
        "action": {"id": action_id, "type": action_id, "parameters": action_parameters} if action_id else None,
    }

    try:
        n8n_response = requests.post(
            webhook_url,
            json=payload,
            headers={"X-Agent-Stage-Secret": N8N_WEBHOOK_SECRET},
            timeout=N8N_TIMEOUT_SECONDS,
        )
        n8n_response.raise_for_status()
        n8n_data = n8n_response.json()
    except requests.exceptions.Timeout:
        workspace.complete_workflow_run(run_id, status="timeout", error="n8n timeout")
        return _error(504, "Le workflow n'a pas repondu a temps.")
    except Exception as exc:
        workspace.complete_workflow_run(run_id, status="failed", error=str(exc)[:500])
        return _error(502, "Le workflow a echoue.")

    blocks = n8n_data.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        workspace.complete_workflow_run(run_id, status="failed", error="empty_response")
        return _error(502, "Reponse vide du workflow.")

    result = workspace.create_result(
        conversation_id=conversation_id,
        message_id=user_message["id"],
        user_id=user["id"],
        model=model,
        workflow_type=action_id or model,
        request=payload,
        response={"blocks": blocks},
        source_result_ids=source_result_ids,
        metadata=n8n_data.get("metadata"),
    )

    assistant_message = workspace.add_message(
        conversation_id=conversation_id,
        user_id=None,
        author_name="ChatGPT" if model == "chatgpt" else "Claude",
        role="assistant",
        content=str(n8n_data.get("summary", ""))[:2000],
        blocks=blocks,
        model=model,
        action_id=action_id,
        result_id=result["id"],
    )
    workspace.touch_conversation(conversation_id)
    workspace.complete_workflow_run(
        run_id, status="completed", result_id=result["id"], n8n_execution_id=n8n_data.get("executionId")
    )

    return jsonify(
        ok=True,
        conversationId=conversation_id,
        isNewConversation=is_new_conversation,
        conversationTitle=conversation["title"],
        userMessage=user_message,
        assistantMessage=assistant_message,
        resultId=result["id"],
    )
