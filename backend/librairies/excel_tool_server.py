"""
librairies/excel_tool_server.py
==================================

Serveur MCP exposant UN outil REEL : "edit_excel" (mission Excel §5.3).
Meme architecture clonee que web_search_tool_server.py/rag_tool_server.py
(JSON-RPC MCP "HTTP+SSE" via Redis Pub/Sub). Contrairement au RAG, cet outil
produit un EFFET DE BORD (un nouveau fichier) pendant l'execution n8n, qui
doit ensuite etre rattache au message assistant cree APRES coup par le
worker RQ (librairies/jobs.py, process separe) -- pont assure ici par une
liste Redis a courte duree de vie, indexee par run_id (mission §5.3).
"""

from __future__ import annotations

import json
import os

import redis

from librairies import excel_tool

REDIS_URL = os.environ.get("REDIS_URL", "")
_PENDING_TTL_SECONDS = int(os.environ.get("FILE_LINK_TTL_SECONDS", "600"))

_client = redis.Redis.from_url(REDIS_URL, decode_responses=True) if REDIS_URL else None


def is_configured() -> bool:
    return _client is not None


def _channel(session_id: str) -> str:
    return f"excel-edit-tool:{session_id}"


def _session_key(session_id: str) -> str:
    return f"excel-edit-session:{session_id}"


def _pending_key(run_id: str) -> str:
    return f"excel-pending:{run_id}"


def register_session(session_id: str, user_id: str, run_id: str) -> None:
    if _client is None:
        return
    _client.setex(_session_key(session_id), _PENDING_TTL_SECONDS, json.dumps({"userId": user_id, "runId": run_id}))


def get_session_context(session_id: str) -> dict | None:
    if _client is None:
        return None
    raw = _client.get(_session_key(session_id))
    return json.loads(raw) if raw else None


def push_pending_file(run_id: str, file_id: str) -> None:
    if _client is None:
        return
    key = _pending_key(run_id)
    _client.rpush(key, file_id)
    _client.expire(key, _PENDING_TTL_SECONDS)


def pop_pending_files(run_id: str) -> list[str]:
    """Appele une fois par le worker RQ (jobs.py) apres la creation du
    message assistant -- vide et supprime la liste (jamais relu deux fois
    pour le meme run_id)."""
    if _client is None:
        return []
    key = _pending_key(run_id)
    values = _client.lrange(key, 0, -1)
    _client.delete(key)
    return values


def publish_response(session_id: str, response: dict | None) -> None:
    if response is None or _client is None:
        return
    _client.publish(_channel(session_id), json.dumps(response))


def subscribe(session_id: str):
    pubsub = _client.pubsub()
    pubsub.subscribe(_channel(session_id))
    return pubsub


_EDIT_EXCEL_TOOL = {
    "name": "edit_excel",
    "description": (
        "Modify an Excel (.xlsx) file that was attached to this conversation, using a constrained set of "
        "deterministic operations (never free-form code). Use this ONLY when the user explicitly asks to "
        "modify, transform, or compute something in a spreadsheet they attached -- never for merely reading "
        "or explaining its content (the document's content is already provided to you in the conversation "
        "context). This NEVER overwrites the original file -- it always produces a new file, which the user "
        "will see as a downloadable attachment automatically. Include the operation's summary and the "
        "returned preview table in your reply ; never claim a modification succeeded if this tool returned "
        "isError: true."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "fileId": {"type": "string", "description": "The id of the attached Excel file, given in the '(id: ...)' marker of the DOCUMENT JOINT header."},
            "sheetName": {"type": "string", "description": "Target sheet name. Omit to use the active/first sheet."},
            "operation": {
                "type": "object",
                "description": (
                    "One of: set_cell{cell,value}, set_range{range,values}, add_column{header,formula?,startRow?}, "
                    "delete_column{column}, add_row{values,atRow?}, delete_row{row}, add_sheet{name}, "
                    "rename_sheet{oldName,newName}, delete_sheet{name}, write_formula{cell|range,formula}, "
                    "copy_range{sourceRange,destCell}, sort_range{range,keyColumn,ascending?,hasHeader?}, "
                    "filter_rows{range,column,operator(eq|contains|gt|lt),value,hasHeader?}, clear_range{range}. "
                    "'formula'/'formula' templates may use {row} as a placeholder for the current row number."
                ),
                "properties": {"type": {"type": "string"}},
                "required": ["type"],
            },
        },
        "required": ["fileId", "operation"],
    },
}


def _run_edit(session_id: str, arguments: dict) -> dict:
    context = get_session_context(session_id)
    if not context:
        return {"content": [{"type": "text", "text": "edit_excel error: session not authorized."}], "isError": True}
    file_id = str((arguments or {}).get("fileId") or "").strip()
    sheet_name = (arguments or {}).get("sheetName")
    operation = (arguments or {}).get("operation") or {}
    if not file_id:
        return {"content": [{"type": "text", "text": "edit_excel error: fileId is required."}], "isError": True}
    try:
        result = excel_tool.apply_edit(file_id, context["userId"], sheet_name, operation)
    except excel_tool.ExcelToolError as exc:
        return {"content": [{"type": "text", "text": f"edit_excel failed: {exc}"}], "isError": True}
    except Exception as exc:
        return {"content": [{"type": "text", "text": f"edit_excel failed (unexpected error): {str(exc)[:200]}"}], "isError": True}

    push_pending_file(context["runId"], result["newFileId"])
    summary_text = (
        f"{result['summary']}\nNouveau fichier : {result['newFileName']} (sera joint automatiquement a la reponse).\n\n"
        f"Apercu (feuille '{result['sheet']}') :\n{result['previewMarkdownTable']}"
    )
    return {"content": [{"type": "text", "text": summary_text}], "isError": False}


def handle_jsonrpc(request_body: dict, session_id: str) -> dict | None:
    method = request_body.get("method")
    request_id = request_body.get("id")
    if method == "initialize":
        result = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "agent-stage-excel-edit-tool", "version": "1.0.0"},
        }
    elif method in ("notifications/initialized", "notifications/cancelled"):
        return None
    elif method == "tools/list":
        result = {"tools": [_EDIT_EXCEL_TOOL]}
    elif method == "tools/call":
        if request_id is None:
            return None
        params = request_body.get("params") or {}
        if params.get("name") != "edit_excel":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": f"Unknown tool: {params.get('name')}"},
            }
        result = _run_edit(session_id, params.get("arguments") or {})
    else:
        if request_id is None:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    if request_id is None:
        return None
    return {"jsonrpc": "2.0", "id": request_id, "result": result}
