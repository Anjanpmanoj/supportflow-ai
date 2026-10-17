import os
import sqlite3

# Set DATABASE_PATH in production to control where the SQLite file lives.
DATABASE_NAME = os.getenv("DATABASE_PATH", "supportflow.db")


def get_connection():
    connection = sqlite3.connect(DATABASE_NAME)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _columns(connection, table):
    return {
        row["name"]
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _add_missing_columns(connection, table, new_columns):
    """Safely upgrade databases created by earlier versions."""
    existing = _columns(connection, table)
    for column, definition in new_columns.items():
        if column not in existing:
            connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
            )


def initialize_database():
    connection = get_connection()

    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'customer',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_name TEXT NOT NULL,
                message TEXT NOT NULL,
                category TEXT NOT NULL,
                policy_title TEXT,
                policy TEXT,
                draft_response TEXT,
                status TEXT NOT NULL DEFAULT 'received',
                priority TEXT NOT NULL DEFAULT 'medium',
                escalated INTEGER NOT NULL DEFAULT 0,
                escalation_reason TEXT
            )
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                details TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (ticket_id) REFERENCES tickets(id)
            )
            """
        )

        # Conversation thread on each ticket.
        # kind: 'comment' (customer-visible), 'response' (approved reply),
        #       'note' (internal, staff only)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ticket_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                user_id INTEGER,
                author_name TEXT NOT NULL,
                author_role TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'comment',
                body TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (ticket_id) REFERENCES tickets(id)
            )
            """
        )

        _add_missing_columns(
            connection,
            "tickets",
            {
                "priority": "TEXT NOT NULL DEFAULT 'medium'",
                "escalated": "INTEGER NOT NULL DEFAULT 0",
                "escalation_reason": "TEXT",
                "approved_response": "TEXT",
                "generation_source": "TEXT NOT NULL DEFAULT 'fallback'",
                "generation_status": "TEXT NOT NULL DEFAULT 'fallback'",
                "generation_error": "TEXT",
                # New: ownership, assignment and timestamps.
                "user_id": "INTEGER",
                "assigned_to": "INTEGER",
                "created_at": "TEXT",
                "updated_at": "TEXT",
            },
        )

        _add_missing_columns(
            connection,
            "audit_logs",
            {"actor": "TEXT"},
        )

        # Backfill timestamps for tickets created before this upgrade.
        connection.execute(
            """
            UPDATE tickets
            SET created_at = COALESCE(
                (SELECT MIN(a.created_at) FROM audit_logs a
                 WHERE a.ticket_id = tickets.id),
                datetime('now')
            )
            WHERE created_at IS NULL
            """
        )
        connection.execute(
            "UPDATE tickets SET updated_at = created_at "
            "WHERE updated_at IS NULL"
        )

        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_tickets_user ON tickets(user_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_comments_ticket "
            "ON ticket_comments(ticket_id)"
        )

        connection.commit()

    finally:
        connection.close()
