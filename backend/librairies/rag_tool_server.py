"""
librairies/rag_tool_server.py
================================

Serveur MCP exposant UN outil REEL : "search_documents" (mission RAG §4g).
Meme architecture que librairies/web_search_tool_server.py (JSON-RPC MCP
"HTTP+SSE" via Redis Pub/Sub, clone deliberement plutot que reimplemente) --
mais avec une difference structurelle necessaire : contrairement a la
recherche web (publique, sans notion d'utilisateur), le RAG doit savoir QUEL
utilisateur pose la question pour appliquer l'autorisation AVANT de renvoyer
le moindre passage (voir librairies/rag.py, "POINT CRITIQUE"). La session
SSE est donc associee a un user_id des sa creation (voir
workspace_routes.py::rag_tool_sse_route, register_session ci-dessous),
jamais devine ni pris dans le corps de la requete JSON-RPC (qui vient de
n8n, pas du navigateur -- jamais fiable pour une decision de securite).

Comme web_search_tool_server.py : TOUJOURS attache a l'Agent des workflows
n8n "chatgpt"/"claude" via un champ dedie (jobs.py, "ragSearchToolUrl"),
jamais insere dans le mecanisme mcpServers de la banque de connexions
(gate different, mot "MCP" requis -- voir jobs.py). La simple presence de
cet outil n'autorise pas l'IA a l'utiliser pour tout : c'est sa description
ci-dessous, combinee a la consigne du prompt n8n, qui guide la decision
("Quel prix etait indique dans notre rapport SPS ?" -> pertinent ; "Bonjour"
-> jamais appele).
"""

from __future__ import annotations

import json
import os

import redis

from librairies import rag

REDIS_URL = os.environ.get("REDIS_URL", "")
_SESSION_TTL_SECONDS = int(os.environ.get("FILE_LINK_TTL_SECONDS", "600"))

_client = redis.Redis.from_url(REDIS_URL, decode_responses=True) if REDIS_URL else None


def is_configured() -> bool:
    return _client is not None


def _channel(session_id: str) -> str:
    return f"rag-search-tool:{session_id}"


def _session_key(session_id: str) -> str:
    return f"rag-search-session:{session_id}"


def register_session(session_id: str, user_id: str) -> None:
    """Associe cette session SSE a l'utilisateur pour qui l'Agent execute
    (mission §4h) : lu par _run_search ci-dessous a chaque appel de l'outil,
    jamais transmis par n8n lui-meme."""
    if _client is None:
        return
    _client.setex(_session_key(session_id), _SESSION_TTL_SECONDS, user_id)


def get_session_user(session_id: str) -> str | None:
    if _client is None:
        return None
    return _client.get(_session_key(session_id))


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


_SEARCH_DOCUMENTS_TOOL = {
    "name": "search_documents",
    "description": (
        "Search the organization's internal document library (reports, spreadsheets, PDFs, and other "
        "documents previously shared in this app) for passages relevant to the user's question. Only "
        "call this when the request plausibly needs information from internal documentation that is "
        "NOT already available from the current message, its attachments, or the conversation context "
        "-- never for greetings, general knowledge, math, or creative writing. Never invent document "
        "content -- if no relevant passage is found, say so honestly instead of guessing."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query, in the language most likely to match the internal documentation.",
            },
        },
        "required": ["query"],
    },
}


def _run_search(session_id: str, arguments: dict) -> dict:
    query = str((arguments or {}).get("query") or "").strip()
    if not query:
        return {"content": [{"type": "text", "text": "search_documents error: empty query."}], "isError": True}
    user_id = get_session_user(session_id)
    if not user_id:
        return {
            "content": [{"type": "text", "text": "search_documents error: session not authorized."}],
            "isError": True,
        }
    try:
        results = rag.search_documents(query, user_id, top_k=6)
    except Exception as exc:
        return {
            "content": [{"type": "text", "text": f"search_documents failed ({exc}) -- do not invent results, tell the user this search did not succeed."}],
            "isError": True,
        }
    if not results:
        return {
            "content": [{"type": "text", "text": "Aucun passage pertinent trouve dans la documentation interne pour cette requete."}],
            "isError": False,
        }
    formatted = "\n\n".join(
        f"--- Document : {r['documentName']} (extrait) ---\n{r['content']}" for r in results
    )
    return {"content": [{"type": "text", "text": formatted}], "isError": False}


def handle_jsonrpc(request_body: dict, session_id: str) -> dict | None:
    """Meme structure que web_search_tool_server.handle_jsonrpc ; le
    session_id (jamais fourni par le corps JSON-RPC lui-meme) est
    obligatoire pour resoudre l'utilisateur via get_session_user()."""
    method = request_body.get("method")
    request_id = request_body.get("id")
    if method == "initialize":
        result = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "agent-stage-rag-search-tool", "version": "1.0.0"},
        }
    elif method in ("notifications/initialized", "notifications/cancelled"):
        return None
    elif method == "tools/list":
        result = {"tools": [_SEARCH_DOCUMENTS_TOOL]}
    elif method == "tools/call":
        if request_id is None:
            return None
        params = request_body.get("params") or {}
        if params.get("name") != "search_documents":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": f"Unknown tool: {params.get('name')}"},
            }
        result = _run_search(session_id, params.get("arguments") or {})
    else:
        if request_id is None:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    if request_id is None:
        return None
    return {"jsonrpc": "2.0", "id": request_id, "result": result}
