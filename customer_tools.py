"""Customer lookup used by the support workflow.

Only structured, server-controlled fields are returned (ticket numbers,
categories, statuses, dates). Old customer messages are deliberately left
out so untrusted text never ends up inside the AI prompt.
"""

from typing import Optional

from database import get_connection

NO_HISTORY = "No previous tickets on file for this customer."
CLOSED_STATUSES = {"resolved", "closed"}


def _ref(ticket_id: int) -> str:
    return f"SF-{ticket_id:06d}"


def get_customer_context(
    user_id: Optional[int],
    customer_name: str,
    category: Optional[str] = None,
    limit: int = 5,
) -> dict:
    """Look up a customer's account age and recent ticket history.

    Customers with an account are matched by user id. Tickets logged by
    staff on someone's behalf have no owner, so those match by name.
    """
    if user_id is not None:
        where, params = "user_id = ?", (user_id,)
    else:
        where = "user_id IS NULL AND LOWER(customer_name) = LOWER(?)"
        params = ((customer_name or "").strip(),)

    connection = get_connection()
    try:
        total = connection.execute(
            f"SELECT COUNT(*) AS n FROM tickets WHERE {where}", params
        ).fetchone()["n"]

        rows = connection.execute(
            f"""
            SELECT id, category, status, created_at
            FROM tickets WHERE {where}
            ORDER BY id DESC LIMIT ?
            """,
            params + (limit,),
        ).fetchall()

        member_since = None
        if user_id is not None:
            user = connection.execute(
                "SELECT created_at FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if user and user["created_at"]:
                member_since = str(user["created_at"])[:10]
    finally:
        connection.close()

    recent = [
        {
            "ref": _ref(row["id"]),
            "category": row["category"],
            "status": row["status"],
            "created": str(row["created_at"] or "")[:10],
        }
        for row in rows
    ]

    return {
        "member_since": member_since,
        "total_tickets": total,
        "open_tickets": [t for t in recent if t["status"] not in CLOSED_STATUSES],
        "recent_tickets": recent,
        "same_category_recent": sum(
            1 for t in recent if category and t["category"] == category
        ),
    }


def format_customer_context(info: dict) -> str:
    """Turn the lookup result into a short block for the AI prompt."""
    if not info["total_tickets"] and not info["member_since"]:
        return NO_HISTORY

    lines = []
    if info["member_since"]:
        lines.append(f"- Customer since: {info['member_since']}")

    if not info["total_tickets"]:
        lines.append("- No previous tickets.")
        return "\n".join(lines)

    lines.append(
        f"- Previous tickets: {info['total_tickets']} "
        f"({len(info['open_tickets'])} of the latest still open)"
    )
    for ticket in info["open_tickets"]:
        lines.append(
            f"- Open: {ticket['ref']} ({ticket['category']}, "
            f"status: {ticket['status']})"
        )
    if info["same_category_recent"]:
        lines.append(
            "- They have contacted us about this same category "
            f"{info['same_category_recent']} time(s) recently."
        )
    return "\n".join(lines)
