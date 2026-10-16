
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
