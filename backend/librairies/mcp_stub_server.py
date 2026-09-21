"""
librairies/mcp_stub_server.py
================================

Serveur MCP minimal, TOUJOURS DISPONIBLE et SANS AUCUN OUTIL (tools/list
renvoie toujours []). Sert uniquement a remplir les emplacements "serveur
MCP" non utilises d'une requete (voir librairies/jobs.py,
MAX_MCP_SERVERS_PER_REQUEST) : le workflow n8n a un nombre FIXE de blocs
"MCP Client" (voir librairies/connections_bank.py) -- verifie en conditions
reelles qu'un bloc pointant vers une URL vide ou injoignable fait echouer
TOUT l'Agent IA, meme avec "continuer en cas d'erreur" active sur ce bloc
seul (l'erreur remonte au niveau de la configuration de l'Agent, pas du
sous-noeud). Seul un serveur qui repond correctement (meme sans aucun
outil) evite ce probleme -- d'ou ce point d'ancrage neutre, plutot qu'une
URL vide ou factice.

Protocole MCP "HTTP+SSE" (le seul transport supporte par le noeud MCP
Client de l'instance n8n de ce projet, verifie en conditions reelles --
voir connections_bank.MCP_TRANSPORTS) : un GET ouvre un flux SSE annoncant
l'URL de POST (avec un identifiant de session) ; un POST y depose une
requete JSON-RPC, dont la reponse est relayee sur le flux SSE ouvert --
jamais dans la reponse HTTP du POST lui-meme (accuse 202 uniquement). Les
deux requetes HTTP peuvent atterrir sur deux workers Gunicorn differents :
Redis Pub/Sub (meme mecanisme que librairies/realtime.py) relie les deux
sans etat en memoire partagee entre workers.
"""

from __future__ import annotations

import json
import os

import redis

REDIS_URL = os.environ.get("REDIS_URL", "")

_client = redis.Redis.from_url(REDIS_URL, decode_responses=True) if REDIS_URL else None


def is_configured() -> bool:
    return _client is not None


def _channel(session_id: str) -> str:
    return f"mcp-stub:{session_id}"


def publish_response(session_id: str, response: dict | None) -> None:
    if response is None or _client is None:
        return
    _client.publish(_channel(session_id), json.dumps(response))


def subscribe(session_id: str):
    """Renvoie un objet pubsub deja abonne. L'appelant doit toujours faire
    pubsub.close() (dans un finally) a la deconnexion du flux SSE."""
    pubsub = _client.pubsub()
    pubsub.subscribe(_channel(session_id))
    return pubsub


def handle_jsonrpc(request_body: dict) -> dict | None:
    """Repond a une requete JSON-RPC MCP minimale : accepte "initialize" et
    "tools/list" (toujours []), refuse proprement tout appel d'outil
    ("tools/call" -- ne devrait jamais arriver, ce serveur n'annonce aucun
    outil) par une erreur JSON-RPC standard plutot qu'un plantage. Renvoie
    None pour une notification (pas de reponse attendue, ex :
    "notifications/initialized")."""
    method = request_body.get("method")
    request_id = request_body.get("id")
    if method == "initialize":
        result = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "agent-stage-mcp-stub", "version": "1.0.0"},
        }
    elif method in ("notifications/initialized", "notifications/cancelled"):
        return None
    elif method == "tools/list":
        result = {"tools": []}
    else:
        if request_id is None:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    if request_id is None:
        return None
    return {"jsonrpc": "2.0", "id": request_id, "result": result}
