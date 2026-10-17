"""Create the first admin (or promote an existing user).

Public registration only creates customers, so run this once from the
project folder to get an admin / agent account:

    python create_admin.py <username>                # make admin
    python create_admin.py <username> --role agent   # make agent

If the username already exists it is promoted. Otherwise you are asked
for an email and password and the account is created.
"""

import argparse
import getpass
import sys

from dotenv import load_dotenv

load_dotenv()

from auth import hash_password
from database import get_connection, initialize_database


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("username")
    parser.add_argument("--role", choices=["agent", "admin"], default="admin")
    parser.add_argument("--email")
    args = parser.parse_args()

    initialize_database()
    connection = get_connection()

    try:
        user = connection.execute(
            "SELECT id, role FROM users WHERE username = ?",
            (args.username,),
        ).fetchone()

        if user:
            connection.execute(
                "UPDATE users SET role = ? WHERE id = ?",
                (args.role, user["id"]),
            )
            connection.commit()
            print(f"'{args.username}' is now {args.role} (was {user['role']}).")
            return

        email = args.email or input("Email: ").strip()
        password = getpass.getpass("Password (min 8 characters): ")
        if len(password) < 8:
            sys.exit("Password must be at least 8 characters.")
        if getpass.getpass("Repeat password: ") != password:
            sys.exit("Passwords do not match.")

        connection.execute(
            """
            INSERT INTO users (username, email, password_hash, role)
            VALUES (?, ?, ?, ?)
            """,
            (args.username, email, hash_password(password), args.role),
        )
        connection.commit()
        print(f"Created {args.role} account '{args.username}'.")

    finally:
        connection.close()


if __name__ == "__main__":
    main()
