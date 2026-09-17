"""
librairies/n8n_client.py
=========================

Client pour l'API de gestion n8n (pas les webhooks d'execution : la creation
de workflows). Utilise UNIQUEMENT cote serveur, jamais depuis le frontend.

N8N_API_KEY et N8N_API_URL sont fournies par l'environnement Railway (jamais
codees en dur, jamais journalisees, jamais renvoyees dans une reponse API).
Si l'une des deux manque, on leve une erreur de configuration explicite
plutot que d'echouer silencieusement ou de simuler un resultat.
"""

from __future__ import annotations

import os
import uuid

import requests

N8N_API_URL = os.environ.get("N8N_API_URL", "").rstrip("/")
N8N_API_KEY = os.environ.get("N8N_API_KEY", "")
N8N_WEBHOOK_SECRET = os.environ.get("N8N_WEBHOOK_SECRET", "")


class N8nConfigError(RuntimeError):
    """Levee quand N8N_API_URL/N8N_API_KEY manquent : jamais de valeur par defaut silencieuse."""


def _require_config() -> None:
    if not N8N_API_URL or not N8N_API_KEY:
        raise N8nConfigError(
            "N8N_API_URL et/ou N8N_API_KEY ne sont pas configurees sur ce service."
        )


def _skeleton_workflow_definition(name: str, webhook_path: str) -> dict:
    """
    Squelette minimal, du meme modele que les workflows ChatGPT/Claude deja
    construits manuellement dans cette session : Webhook -> If (verifie le
    secret partage) -> Respond 200 (placeholder, a remplacer par le vrai
    traitement) / Respond 403. Cree INACTIF : c'est a l'utilisateur de
    completer la logique reelle dans n8n puis de l'activer, exactement comme
    pour les workflows ChatGPT/Claude.
    """
    webhook_node_id = str(uuid.uuid4())
    if_node_id = str(uuid.uuid4())
    respond_ok_id = str(uuid.uuid4())
    respond_forbidden_id = str(uuid.uuid4())

    return {
        "name": name,
        "nodes": [
            {
                "id": webhook_node_id,
                "name": "Webhook",
                "type": "n8n-nodes-base.webhook",
                "typeVersion": 2.1,
                "position": [0, 96],
                "parameters": {
                    "httpMethod": "POST",
                    "path": webhook_path,
                    "responseMode": "responseNode",
                    "options": {},
                },
            },
            {
                "id": if_node_id,
                "name": "If",
                "type": "n8n-nodes-base.if",
                "typeVersion": 2.3,
                "position": [224, 96],
                "parameters": {
                    "conditions": {
                        "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 3},
                        "conditions": [
                            {
                                "id": str(uuid.uuid4()),
                                "leftValue": "={{ $json.headers['x-agent-stage-secret'] }}",
                                "rightValue": N8N_WEBHOOK_SECRET,
                                "operator": {"type": "string", "operation": "equals", "name": "filter.operator.equals"},
                            }
                        ],
                    }
                },
            },
            {
                "id": respond_ok_id,
                "name": "Respond 200",
                "type": "n8n-nodes-base.respondToWebhook",
                "typeVersion": 1.4,
                "position": [448, 0],
                "parameters": {
                    "respondWith": "json",
                    "responseBody": (
                        '{"summary": "Ce workflow n\'a pas encore ete configure dans n8n.",'
                        ' "blocks": [{"type": "markdown", "content": "Ce bouton existe mais son'
                        ' workflow n8n n\'a pas encore ete complete. Ouvrez-le dans n8n pour y'
                        ' brancher le traitement reel (IA, logique metier, etc.)."}]}'
                    ),
                },
            },
            {
                "id": respond_forbidden_id,
                "name": "Respond 403",
                "type": "n8n-nodes-base.respondToWebhook",
                "typeVersion": 1.4,
                "position": [448, 192],
                "parameters": {
                    "respondWith": "json",
                    "responseCode": 403,
                    "responseBody": '{"error": "forbidden"}',
                },
            },
        ],
        "connections": {
            "Webhook": {"main": [[{"node": "If", "type": "main", "index": 0}]]},
            "If": {
                "main": [
                    [{"node": "Respond 200", "type": "main", "index": 0}],
                    [{"node": "Respond 403", "type": "main", "index": 0}],
                ]
            },
        },
        "settings": {},
    }


def create_action_workflow(name: str) -> dict:
    """
    Cree reellement un nouveau workflow n8n (squelette Webhook/If/Respond) via
    l'API de gestion n8n. Renvoie {n8n_workflow_id, webhook_path, editor_url,
    active}. Ne simule jamais un resultat : toute erreur HTTP remonte telle
    quelle a l'appelant.
    """
    _require_config()
    webhook_path = f"action-{uuid.uuid4()}"
    payload = _skeleton_workflow_definition(name, webhook_path)

    response = requests.post(
        f"{N8N_API_URL}/api/v1/workflows",
        headers={"X-N8N-API-KEY": N8N_API_KEY, "Content-Type": "application/json"},
        json=payload,
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    workflow_id = data["id"]

    return {
        "n8nWorkflowId": workflow_id,
        "webhookPath": webhook_path,
        "editorUrl": f"{N8N_API_URL}/workflow/{workflow_id}",
        "active": bool(data.get("active")),
    }
