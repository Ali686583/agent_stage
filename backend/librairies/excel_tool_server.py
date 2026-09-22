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
        "isError: true.\n\n"
        "'operation.type' selects which of operation's other fields are required -- ALWAYS include every "
        "field listed as required for the chosen type, never only 'type' alone:\n"
        "  set_cell: requires cell, value\n"
        "  set_range: requires range, values\n"
        "  add_column: requires header (optional: formula, startRow)\n"
        "  delete_column: requires column\n"
        "  add_row: requires values (optional: atRow)\n"
        "  delete_row: requires row\n"
        "  add_sheet: requires name\n"
        "  rename_sheet: requires oldName, newName\n"
        "  delete_sheet: requires name\n"
        "  write_formula: requires formula, and either cell or range\n"
        "  copy_range: requires sourceRange, destCell\n"
        "  sort_range: requires range, keyColumn (optional: ascending, hasHeader)\n"
        "  filter_rows: requires range, column, operator, value (optional: hasHeader)\n"
        "  clear_range: requires range\n"
        "'formula' values may use the literal placeholder {row} for the current row number, e.g. \"=B{row}-C{row}\"."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "fileId": {"type": "string", "description": "The id of the attached Excel file, given in the '(id: ...)' marker of the DOCUMENT JOINT header."},
            "sheetName": {"type": "string", "description": "Target sheet name. Omit to use the active/first sheet."},
            "operation": {
                "type": "object",
                "description": "See the field-by-field requirements in the tool description above.",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": [
                            "set_cell", "set_range", "add_column", "delete_column", "add_row", "delete_row",
                            "add_sheet", "rename_sheet", "delete_sheet", "write_formula", "copy_range",
                            "sort_range", "filter_rows", "clear_range",
                        ],
                        "description": "Which operation to perform. Determines which other fields below are required.",
                    },
                    "cell": {"type": "string", "description": "Cell reference (e.g. 'B2'). Required for set_cell ; alternative to 'range' for write_formula."},
                    "value": {"description": "Value to write (any JSON scalar). Required for set_cell. Also required for filter_rows (the value to compare each cell against)."},
                    "range": {"type": "string", "description": "Range reference (e.g. 'A1:C10'). Required for set_range, sort_range, filter_rows, clear_range ; alternative to 'cell' for write_formula."},
                    "values": {"type": "array", "description": "For set_range: a 2D array of row values, e.g. [[1,2],[3,4]]. For add_row: a flat array of values for the new row."},
                    "header": {"type": "string", "description": "New column's header text. Required for add_column."},
                    "formula": {"type": "string", "description": "Excel formula string, may use the placeholder {row}, e.g. '=B{row}-C{row}'. Required for write_formula ; optional for add_column (a computed column without a formula just gets the header)."},
                    "startRow": {"type": "integer", "description": "First data row to compute for add_column. Optional, defaults to 2 (row 1 is assumed to be the header row)."},
                    "column": {"type": "string", "description": "For delete_column: the column LETTER to delete (e.g. 'C'). For filter_rows: the 1-based column NUMBER within 'range' to filter on (e.g. 2, never a letter)."},
                    "atRow": {"type": "integer", "description": "Row index to insert the new row at, for add_row. Optional -- appends at the end if omitted."},
                    "row": {"type": "integer", "description": "Row index. Required for delete_row."},
                    "name": {"type": "string", "description": "Sheet name. Required for add_sheet (the new sheet's name) and delete_sheet (the sheet to delete)."},
                    "oldName": {"type": "string", "description": "Existing sheet name to rename. Required for rename_sheet."},
                    "newName": {"type": "string", "description": "New name for the sheet. Required for rename_sheet."},
                    "sourceRange": {"type": "string", "description": "Range to copy from. Required for copy_range."},
                    "destCell": {"type": "string", "description": "Top-left destination cell to copy into. Required for copy_range."},
                    "keyColumn": {"type": "integer", "description": "1-based column number within 'range' to sort by. Required for sort_range."},
                    "ascending": {"type": "boolean", "description": "Sort direction for sort_range. Optional, defaults to true."},
                    "hasHeader": {"type": "boolean", "description": "Whether the first row of 'range' is a header row to keep in place. Optional, defaults to true. Used by sort_range and filter_rows."},
                    "operator": {"type": "string", "enum": ["eq", "contains", "gt", "lt"], "description": "Comparison used by filter_rows between each row's 'column' cell and 'value'."},
                },
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
