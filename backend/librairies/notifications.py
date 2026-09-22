"""
librairies/notifications.py
==============================

Persistance des notifications (mentions/reponses, mission notifications
Phase 6). La creation elle-meme se fait depuis
librairies/workspace.py::add_message (meme transaction que l'insertion du
message et de message_mentions, pour une coherence atomique -- voir
create_notifications_for_message ci-dessous, appele depuis LA-BAS avec la
connexion deja ouverte). Ce module fournit en plus les routes de
lecture/marquage, qui n'ont pas besoin de cette meme transaction.
"""

from __future__ import annotations

import uuid

from librairies.database import _db


def _new_id(prefix: str) -> str:
    # Meme format que workspace.new_id, delibererement duplique (pas
    # importe) pour eviter un import circulaire notifications.py <->
    # workspace.py (workspace.add_message() appelle ce module DANS sa
    # propre transaction, voir create_notifications_for_message ci-dessous).
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def create_notifications_for_message(
    conn, *, actor_user_id: str, author_name: str, conversation_id: str,
    message_id: str, content: str, mentioned_user_ids: list[str] | None, reply_to_message_id: str | None,
) -> set[str]:
    """Appele DANS la transaction de workspace.add_message (meme `conn`,
    jamais commitee ici -- l'appelant s'en charge). Renvoie l'ensemble des
    destinataires notifies, pour que l'appelant publie ensuite un evenement
    temps reel a chacun (best-effort, hors transaction -- voir
    realtime.publish_user_event). Deduplique mention+reponse (mission §6b) :
    un utilisateur mentionne ET auteur du message repondu n'est notifie
    qu'UNE fois (la mention, inseree en premier)."""
    preview = (content or "")[:200]
    notified: set[str] = set()

    for mentioned_user_id in dict.fromkeys(mentioned_user_ids or []):
        if mentioned_user_id == actor_user_id or mentioned_user_id in notified:
            continue
        conn.execute(
            """INSERT INTO notifications (id, recipient_user_id, actor_user_id, type, conversation_id, message_id, preview_text)
               VALUES (%s,%s,%s,'mention',%s,%s,%s)""",
            (_new_id("notif"), mentioned_user_id, actor_user_id, conversation_id, message_id, preview),
        )
        notified.add(mentioned_user_id)

    if reply_to_message_id:
        parent = conn.execute("SELECT user_id FROM messages WHERE id = %s", (reply_to_message_id,)).fetchone()
        parent_owner = parent["user_id"] if parent else None
        if parent_owner and parent_owner != actor_user_id and parent_owner not in notified:
            conn.execute(
                """INSERT INTO notifications (id, recipient_user_id, actor_user_id, type, conversation_id, message_id, preview_text)
                   VALUES (%s,%s,%s,'reply',%s,%s,%s)""",
                (_new_id("notif"), parent_owner, actor_user_id, conversation_id, message_id, preview),
            )
            notified.add(parent_owner)

    return notified


def _public_notification(row: dict) -> dict:
    return {
        "id": row["id"],
        "seq": row["seq"],
        "type": row["type"],
        "actorUserId": row.get("actor_user_id"),
        "conversationId": row.get("conversation_id"),
        "messageId": row.get("message_id"),
        "previewText": row.get("preview_text"),
        "readAt": row["read_at"].isoformat() if row.get("read_at") else None,
        "createdAt": row["created_at"].isoformat() if row.get("created_at") else None,
    }


def list_notifications(user_id: str, *, before: str | None = None, limit: int = 30) -> list[dict]:
    clauses = ["recipient_user_id = %(user_id)s"]
    params: dict = {"user_id": user_id, "limit": min(max(limit, 1), 100)}
    if before:
        clauses.append("created_at < %(before)s")
        params["before"] = before
    where_sql = " AND ".join(clauses)
    with _db() as conn:
        rows = conn.execute(
            f"SELECT * FROM notifications WHERE {where_sql} ORDER BY created_at DESC LIMIT %(limit)s",
            params,
        ).fetchall()
    return [_public_notification(r) for r in rows]


def count_unread(user_id: str) -> int:
    with _db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM notifications WHERE recipient_user_id = %s AND read_at IS NULL",
            (user_id,),
        ).fetchone()
    return int(row["c"]) if row else 0


def mark_read(notification_id: str, user_id: str) -> bool:
    with _db() as conn:
        cur = conn.execute(
            "UPDATE notifications SET read_at = now() WHERE id = %s AND recipient_user_id = %s AND read_at IS NULL",
            (notification_id, user_id),
        )
    return cur.rowcount > 0


def mark_all_read(user_id: str) -> int:
    with _db() as conn:
        cur = conn.execute(
            "UPDATE notifications SET read_at = now() WHERE recipient_user_id = %s AND read_at IS NULL",
            (user_id,),
        )
    return cur.rowcount
