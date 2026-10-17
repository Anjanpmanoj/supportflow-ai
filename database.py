
import sqlite3

DATABASE_NAME = "supportflow.db"


def get_connection():
    connection = sqlite3.connect(DATABASE_NAME)
    connection.row_factory = sqlite3.Row
    return connection


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
                role TEXT NOT NULL DEFAULT 'agent',
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

        # Safely upgrade databases created by earlier versions.
        existing_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(tickets)"
            ).fetchall()
        }

        new_columns = {
            "priority": "TEXT NOT NULL DEFAULT 'medium'",
            "escalated": "INTEGER NOT NULL DEFAULT 0",
            "escalation_reason": "TEXT",
            "approved_response": "TEXT",
            "generation_source": "TEXT NOT NULL DEFAULT 'fallback'",
            "generation_status": "TEXT NOT NULL DEFAULT 'fallback'",
            "generation_error": "TEXT",
        }

        for column, definition in new_columns.items():
            if column not in existing_columns:
                connection.execute(
                    f"ALTER TABLE tickets ADD COLUMN "
                    f"{column} {definition}"
                )

        connection.commit()

    finally:
        connection.close()
