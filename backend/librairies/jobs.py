"""
librairies/jobs.py
====================

Traitement asynchrone des demandes IA/n8n (Phase 4). Le web (workspace_routes.py)
se contente d'enregistrer le message utilisateur et de mettre ce travail en
file d'attente Redis (RQ) ; c'est CE module, execute par un worker separe
(voir worker.py et le service Railway "worker"), qui appelle reellement n8n.

Objectif : qu'un appel n8n lent (jusqu'a N8N_TIMEOUT_SECONDS) n'occupe
jamais un thread du serveur web, et ne bloque donc jamais les autres
utilisateurs (voir le prompt d'architecture qui a demande cette phase).

Ce module doit rester importable SANS contexte de requete Flask (le worker
RQ n'en a pas) : jamais de `flask.request` ici, seulement des arguments
explicites et des variables d'environnement (FRONTEND_URL a la place de
request.url_root).
"""

from __future__ import annotations

import os
import time

import redis
import requests
from rq import Queue

from librairies import workspace
from librairies.security import sign_file_token

REDIS_URL = os.environ.get("REDIS_URL", "")
if not REDIS_URL:
    raise RuntimeError("REDIS_URL n'est pas configuree : voir librairies/jobs.py.")

_redis = redis.Redis.from_url(REDIS_URL)
queue = Queue("agent_stage_workflows", connection=_redis)

FRONTEND_URL = os.environ.get("FRONTEND_URL", "").rstrip("/")
FILE_LINK_TTL_SECONDS = int(os.environ.get("FILE_LINK_TTL_SECONDS", "600"))

# Deux webhooks distincts (un par modele) : chacun peut etre construit comme
# un workflow n8n independant et simple, plutot qu'un seul workflow qui
# devrait brancher lui-meme sur le modele.
N8N_WEBHOOK_CHATGPT_URL = os.environ.get("N8N_WEBHOOK_CHATGPT_URL", "")
N8N_WEBHOOK_CLAUDE_URL = os.environ.get("N8N_WEBHOOK_CLAUDE_URL", "")
N8N_WEBHOOK_SECRET = os.environ.get("N8N_WEBHOOK_SECRET", "")
N8N_TIMEOUT_SECONDS = int(os.environ.get("N8N_TIMEOUT_SECONDS", "90"))


def _fail(run_id: str, conversation_id: str, message_id: str, request_id: str, status: str, error: str) -> None:
    workspace.complete_workflow_run(run_id, status=status, error=error)
    try:
        workspace.publish_workflow_failed(conversation_id, request_id, message_id, error)
    except Exception:
        pass  # best-effort : le run reste correctement marque en base malgre tout


def execute_workflow_run(
    run_id: str,
    conversation_id: str,
    user_message_id: str,
    user_id: str,
    user_name: str,
    model: str,
    action_id: str | None,
    action_parameters: dict,
    message_text: str,
    file_ids: list[str],
    source_result_ids: list[str],
    request_id: str,
) -> None:
    webhook_url = N8N_WEBHOOK_CHATGPT_URL if model == "chatgpt" else N8N_WEBHOOK_CLAUDE_URL
    if not webhook_url:
        _fail(run_id, conversation_id, user_message_id, request_id, "failed", "workflow_not_configured")
        return

    file_links = []
    for file_id in file_ids:
        expires_at = int(time.time()) + FILE_LINK_TTL_SECONDS
        try:
            token = sign_file_token(file_id, expires_at)
        except RuntimeError:
            _fail(run_id, conversation_id, user_message_id, request_id, "failed", "file_signing_not_configured")
            return
        file_links.append(
            {
                "fileId": file_id,
                "url": f"{FRONTEND_URL}/api/workspace/files/{file_id}/signed?exp={expires_at}&token={token}",
            }
        )

    payload = {
        "requestId": request_id,
        "conversationId": conversation_id,
        "userId": user_id,
        "userName": user_name,
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
        _fail(run_id, conversation_id, user_message_id, request_id, "timeout", "n8n timeout")
        return
    except Exception as exc:
        _fail(run_id, conversation_id, user_message_id, request_id, "failed", str(exc)[:500])
        return

    blocks = n8n_data.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        _fail(run_id, conversation_id, user_message_id, request_id, "failed", "empty_response")
        return

    result = workspace.create_result(
        conversation_id=conversation_id,
        message_id=user_message_id,
        user_id=user_id,
        model=model,
        workflow_type=action_id or model,
        request=payload,
        response={"blocks": blocks},
        source_result_ids=source_result_ids,
        metadata=n8n_data.get("metadata"),
    )

    workspace.add_message(
        conversation_id=conversation_id,
        user_id=None,
        author_name="ChatGPT" if model == "chatgpt" else "Claude",
        role="assistant",
        content=str(n8n_data.get("summary", ""))[:2000],
        blocks=blocks,
        model=model,
        action_id=action_id,
        result_id=result["id"],
        publish_extra={"requestId": request_id},
    )
    workspace.touch_conversation(conversation_id)
    workspace.complete_workflow_run(
        run_id, status="completed", result_id=result["id"], n8n_execution_id=n8n_data.get("executionId")
    )
