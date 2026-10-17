"""Create the first admin account and (optionally) sample data on startup.

Everything is driven by environment variables, so no secret lives in code.
Safe to run on every start: existing users and tickets are never touched.

  ADMIN_USERNAME, ADMIN_PASSWORD, ADMIN_EMAIL   first admin account
  SEED_DEMO_DATA=true, DEMO_PASSWORD            demo agent, customer, tickets
"""

import os

from auth import hash_password
from database import get_connection

MIN_PASSWORD_LENGTH = 8


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def _create_user(connection, username, email, password, role):
    """Create a user unless that username or email already exists."""
    row = connection.execute(
        "SELECT id FROM users WHERE username = ? OR email = ?",
        (username, email),
    ).fetchone()
    if row:
        return row["id"]

    cursor = connection.execute(
        """
        INSERT INTO users (username, email, password_hash, role)
        VALUES (?, ?, ?, ?)
        """,
        (username, email, hash_password(password), role),
    )
    return cursor.lastrowid


def seed_admin(connection):
    username = _env("ADMIN_USERNAME")
    password = os.getenv("ADMIN_PASSWORD", "")
    if not (username and password):
        return

    if len(password) < MIN_PASSWORD_LENGTH:
        print("Admin seeding skipped: ADMIN_PASSWORD is too short.")
        return

    email = _env("ADMIN_EMAIL") or f"{username}@example.com"
    _create_user(connection, username, email, password, "admin")


# --------------------------------------------------
# Sample data (clearly fake, for demos and screenshots)
# --------------------------------------------------

POLICY_TITLES = {
    "billing": "Billing and Refund Policy",
    "technical": "Technical Support Policy",
    "account_access": "Account Access Policy",
    "other": "General Support Policy",
}

DEMO_TICKETS = [
    {
        "message": "I was charged twice for my subscription this month. Please help!",
        "category": "billing",
        "status": "received",
        "priority": "high",
        "escalation": "Billing dispute requires review",
        "hours_ago": 3,
        "draft": (
            "Hi demo_customer, thank you for contacting us about your billing "
            "concern. Our support team will review the transaction details "
            "and confirm refund eligibility before a refund can be promised."
        ),
        "approved": None,
        "comments": [],
        "events": [
            ("ticket_created", "Sample ticket", "customer"),
            ("ticket_assigned", "Auto-assigned to demo_agent", "system"),
        ],
    },
    {
        "message": "The reports page keeps crashing when I try to export to PDF.",
        "category": "technical",
        "status": "ai_replied",
        "priority": "medium",
        "escalation": None,
        "hours_ago": 6,
        "draft": (
            "Hi demo_customer, sorry about the trouble with the PDF export. "
            "Could you share the exact error message and the steps that lead "
            "to the crash? Our team will take a look."
        ),
        "approved": True,
        "comments": [("ai", "response", None)],
        "events": [
            ("ticket_created", "Sample ticket", "customer"),
            ("ticket_assigned", "Auto-assigned to demo_agent", "system"),
            ("ai_response_sent", "AI reply sent automatically.", "ai"),
        ],
    },
    {
        "message": "I can't sign in. It says my password is incorrect even after resetting it.",
        "category": "account_access",
        "status": "approved",
        "priority": "medium",
        "escalation": None,
        "hours_ago": 28,
        "draft": (
            "Hi demo_customer, we're sorry you're having trouble accessing "
            "your account. Please use the official password recovery process."
        ),
        "approved": (
            "Hi demo_customer, I checked your account and cleared a stuck "
            "login lock. Please try signing in again with your new password "
            "and let us know if it still fails."
        ),
        "comments": [("agent", "response", "approved")],
        "events": [
            ("ticket_created", "Sample ticket", "customer"),
            ("ticket_assigned", "Auto-assigned to demo_agent", "system"),
            ("human_response_sent", "A human agent replied to the customer.", "agent"),
        ],
    },
    {
        "message": "Do you offer a mobile app for iOS and Android?",
        "category": "other",
        "status": "resolved",
        "priority": "medium",
        "escalation": None,
        "hours_ago": 72,
        "draft": (
            "Hi demo_customer, thank you for your question. We currently "
            "support the web app on mobile browsers, and our team can share "
            "more about future plans."
        ),
        "approved": True,
        "comments": [("ai", "response", None)],
        "events": [
            ("ticket_created", "Sample ticket", "customer"),
            ("ticket_assigned", "Auto-assigned to demo_agent", "system"),
            ("ai_response_sent", "AI reply sent automatically.", "ai"),
            ("ticket_resolved", "", "agent"),
        ],
    },
]


def seed_demo_data(connection):
    if _env("SEED_DEMO_DATA").lower() != "true":
        return

    password = os.getenv("DEMO_PASSWORD", "")
    if len(password) < MIN_PASSWORD_LENGTH:
        print("Demo seeding skipped: set DEMO_PASSWORD (min. 8 characters).")
        return

    # Only seed an empty system, never on top of real tickets.
    if connection.execute("SELECT COUNT(*) AS n FROM tickets").fetchone()["n"]:
        return

    agent_id = _create_user(
        connection, "demo_agent", "agent@demo.local", password, "agent"
    )
    customer_id = _create_user(
        connection, "demo_customer", "customer@demo.local", password, "customer"
    )

    authors = {
        "customer": (customer_id, "demo_customer", "customer"),
        "agent": (agent_id, "demo_agent", "agent"),
        "ai": (None, "AI Assistant", "ai"),
        "system": (None, "system", "system"),
    }

    for sample in DEMO_TICKETS:
        hours = sample["hours_ago"]
        escalated = 1 if sample["escalation"] else 0
        approved = sample["approved"]
        approved_text = sample["draft"] if approved is True else approved

        cursor = connection.execute(
            """
            INSERT INTO tickets (
                customer_name, message, category,
                policy_title, policy, draft_response, approved_response,
                status, priority, escalated, escalation_reason,
                generation_source, generation_status, generation_error,
                user_id, assigned_to, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    'template', 'fallback', 'Sample ticket (not AI generated).',
                    ?, ?, datetime('now', ?), datetime('now', ?))
            """,
            (
                "demo_customer",
                sample["message"],
                sample["category"],
                POLICY_TITLES[sample["category"]],
                "Sample policy text for the demo data.",
                sample["draft"],
                approved_text,
                sample["status"],
                sample["priority"],
                escalated,
                sample["escalation"],
                customer_id,
                agent_id,
                f"-{hours} hours",
                f"-{max(hours - 1, 0)} hours",
            ),
        )
        ticket_id = cursor.lastrowid
        minutes = hours * 60

        for offset, (who, kind, source) in enumerate(sample["comments"], start=1):
            user_id, name, role = authors[who]
            body = approved_text if source == "approved" else sample["draft"]
            connection.execute(
                """
                INSERT INTO ticket_comments
                    (ticket_id, user_id, author_name, author_role, kind, body,
                     created_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now', ?))
                """,
                (ticket_id, user_id, name, role, kind, body,
                 f"-{max(minutes - 5 * offset, 0)} minutes"),
            )

        for action, details, who in sample["events"]:
            actor = authors[who][1]
            connection.execute(
                """
                INSERT INTO audit_logs
                    (ticket_id, action, details, actor, created_at)
                VALUES (?, ?, ?, ?, datetime('now', ?))
                """,
                (ticket_id, action, details, actor, f"-{minutes} minutes"),
            )


def seed_initial_data():
    connection = get_connection()
    try:
        seed_admin(connection)
        seed_demo_data(connection)
        connection.commit()
    finally:
        connection.close()
