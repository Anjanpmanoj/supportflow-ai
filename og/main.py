from dotenv import load_dotenv

load_dotenv()

import os
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from typing import Literal, Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field

from ai_service import generate_support_response
from auth import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from database import get_connection, initialize_database


@asynccontextmanager
async def lifespan(app: FastAPI):
    initialize_database()
    yield


app = FastAPI(
    title="SupportFlow AI",
    description="A customer support ticket workflow API",
    version="0.3.0",
    lifespan=lifespan,
)


@contextmanager
def db():
    """Open a database connection and always close it."""
    connection = get_connection()
    try:
        yield connection
    finally:
        connection.close()


# --------------------------------------------------
# Roles and workflow rules
# --------------------------------------------------

STAFF_ROLES = {"agent", "admin"}

# --- Automation switches (override in .env with true / false) ---------
# Every new ticket is assigned to a human agent (admins can change it).
AUTO_ASSIGN = os.getenv("AUTO_ASSIGN", "true").lower() == "true"
# The AI assistant replies to the customer straight away, BUT only when it
# is safe: the AI really produced the answer and the ticket is not
# escalated (billing disputes, possible hacks, outages stay with a human).
AUTO_SEND_AI_REPLY = os.getenv("AUTO_SEND_AI_REPLY", "true").lower() == "true"
AI_AUTHOR_NAME = "AI Assistant"

# Ticket statuses:
#   received     held for human review (escalated, or AI unavailable)
#   ai_replied   the AI assistant already answered; a human follows up
#   in_progress  a human is working on it
#   approved     a human response was sent ("Support replied")
#   rejected     a human rejected the AI draft
#   resolved / closed

# What a customer is allowed to see of the internal status.
# A rejected AI draft is an internal detail: to the customer the
# ticket is simply still being worked on.
PUBLIC_STATUS = {"rejected": "in_progress"}

# Status changes done through POST /tickets/{id}/status.
# (Approve / reject / edit-draft / reply have their own endpoints.)
STATUS_ACTIONS = {
    "start": {
        "from": {"received", "rejected", "ai_replied"},
        "to": "in_progress",
        "audit": "ticket_started",
        "staff_only": True,
    },
    "resolve": {
        "from": {"approved", "ai_replied"},
        "to": "resolved",
        "audit": "ticket_resolved",
        "staff_only": True,
    },
    "close": {
        "from": {"approved", "resolved", "ai_replied"},
        "to": "closed",
        "audit": "ticket_closed",
        "staff_only": False,
    },
    "reopen": {
        "from": {"approved", "resolved", "closed"},
        "to": "received",
        "audit": "ticket_reopened",
        "staff_only": False,
    },
    # The customer was not satisfied with the AI answer and wants a person.
    "human": {
        "from": {"ai_replied"},
        "to": "received",
        "audit": "human_requested",
        "staff_only": False,
    },
}

# Audit events customers may see, with friendly wording.
CUSTOMER_EVENT_LABELS = {
    "ticket_created": "Ticket submitted",
    "ai_response_sent": "AI assistant replied",
    "human_requested": "You asked to speak to a human agent",
    "ticket_started": "Support started working on your ticket",
    "response_approved": "Support sent a response",
    "human_response_sent": "Support team replied",
    "ticket_resolved": "Marked as resolved",
    "ticket_closed": "Ticket closed",
    "ticket_reopened": "Ticket reopened",
}


def is_staff(user: dict) -> bool:
    return user["role"] in STAFF_ROLES


def ticket_ref(ticket_id: int) -> str:
    """Human-friendly ticket ID shown to users, e.g. SF-000123."""
    return f"SF-{ticket_id:06d}"


# --------------------------------------------------
# Authentication
# --------------------------------------------------

bearer_scheme = HTTPBearer()


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
):
    try:
        payload = decode_access_token(credentials.credentials)
        user_id = int(payload["sub"])
    except Exception:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token",
        )

    with db() as connection:
        user = connection.execute(
            """
            SELECT id, username, email, role, created_at
            FROM users
            WHERE id = ?
            """,
            (user_id,),
        ).fetchone()

    if user is None:
        raise HTTPException(status_code=401, detail="User not found")

    return dict(user)


# Kept so existing tests / overrides that use the old name still work.
get_current_user_from_token = get_current_user


def require_staff(current_user: dict = Depends(get_current_user)):
    if not is_staff(current_user):
        raise HTTPException(status_code=403, detail="Staff access required")
    return current_user


def require_admin(current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=50)
    email: EmailStr
    password: str = Field(max_length=128)


class LoginRequest(BaseModel):
    username: str
    password: str


def create_user(username: str, email: str, password: str, role: str):
    if len(password) < 8:
        raise HTTPException(
            status_code=400,
            detail="Password must be at least 8 characters long",
        )

    with db() as connection:
        try:
            connection.execute(
                """
                INSERT INTO users (username, email, password_hash, role)
                VALUES (?, ?, ?, ?)
                """,
                (username.strip(), email, hash_password(password), role),
            )
            connection.commit()
        except sqlite3.IntegrityError:
            raise HTTPException(
                status_code=409,
                detail="Username or email already exists",
            )


@app.post("/auth/register", status_code=201)
def register_user(request: RegisterRequest):
    # Public registration ALWAYS creates a customer. Staff accounts
    # are created by an admin (POST /admin/users) or create_admin.py.
    create_user(request.username, request.email, request.password, "customer")

    return {
        "message": "User registered successfully",
        "username": request.username.strip(),
        "email": request.email,
        "role": "customer",
    }


@app.post("/auth/login")
def login_user(request: LoginRequest):
    with db() as connection:
        user = connection.execute(
            "SELECT * FROM users WHERE username = ?",
            (request.username.strip(),),
        ).fetchone()

    if user is None or not verify_password(
        request.password, user["password_hash"]
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid username or password",
        )

    access_token = create_access_token(
        user_id=user["id"],
        role=user["role"],
    )

    return {
        "access_token": access_token,
        "token_type": "bearer",
    }


@app.get("/auth/me")
def read_current_user(current_user: dict = Depends(get_current_user)):
    return current_user


# --------------------------------------------------
# Admin: team management
# --------------------------------------------------

class AdminCreateUser(BaseModel):
    username: str = Field(min_length=3, max_length=50)
    email: EmailStr
    password: str = Field(max_length=128)
    role: Literal["customer", "agent", "admin"] = "agent"


class RoleUpdate(BaseModel):
    role: Literal["customer", "agent", "admin"]


@app.get("/admin/users")
def admin_list_users(admin: dict = Depends(require_admin)):
    with db() as connection:
        rows = connection.execute(
            """
            SELECT id, username, email, role, created_at
            FROM users
            ORDER BY id
            """
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/admin/users", status_code=201)
def admin_create_user(
    request: AdminCreateUser,
    admin: dict = Depends(require_admin),
):
    create_user(request.username, request.email, request.password, request.role)
    return {
        "message": f"{request.role.capitalize()} account created",
        "username": request.username.strip(),
        "role": request.role,
    }


@app.patch("/admin/users/{user_id}/role")
def admin_update_role(
    user_id: int,
    update: RoleUpdate,
    admin: dict = Depends(require_admin),
):
    if user_id == admin["id"]:
        raise HTTPException(
            status_code=400,
            detail="You cannot change your own role.",
        )

    with db() as connection:
        user = connection.execute(
            "SELECT id, username FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

        connection.execute(
            "UPDATE users SET role = ? WHERE id = ?", (update.role, user_id)
        )
        connection.commit()

    return {"message": f"{user['username']} is now {update.role}"}


@app.get("/staff")
def list_staff(current_user: dict = Depends(require_staff)):
    """Staff members that tickets can be assigned to."""
    with db() as connection:
        rows = connection.execute(
            """
            SELECT id, username, role FROM users
            WHERE role IN ('agent', 'admin')
            ORDER BY username
            """
        ).fetchall()
    return [dict(row) for row in rows]


# --------------------------------------------------
# Ticket helpers
# --------------------------------------------------

class TicketRequest(BaseModel):
    # Customers can omit this: their account username is used.
    customer_name: Optional[str] = Field(default=None, max_length=100)
    message: str = Field(min_length=5, max_length=3000)


class DraftUpdate(BaseModel):
    draft_response: str = Field(min_length=1, max_length=5000)


class CommentRequest(BaseModel):
    body: str = Field(min_length=1, max_length=3000)
    internal: bool = False


class StatusChange(BaseModel):
    action: Literal["start", "resolve", "close", "reopen", "human"]
    note: Optional[str] = Field(default=None, max_length=3000)


class ReplyRequest(BaseModel):
    body: str = Field(min_length=1, max_length=5000)


class AssignRequest(BaseModel):
    assignee_id: Optional[int] = None


TICKET_SELECT = """
SELECT t.*,
       owner.username AS owner_username,
       assignee.username AS assignee_name
FROM tickets t
LEFT JOIN users owner ON owner.id = t.user_id
LEFT JOIN users assignee ON assignee.id = t.assigned_to
"""


def fetch_ticket(connection, ticket_id: int, user: dict):
    """Load a ticket the user is allowed to see, or raise 404.

    Customers get a 404 (not 403) for other people's tickets so the
    existence of a ticket ID is never revealed.
    """
    row = connection.execute(
        TICKET_SELECT + " WHERE t.id = ?", (ticket_id,)
    ).fetchone()

    if row is None or (
        not is_staff(user) and row["user_id"] != user["id"]
    ):
        raise HTTPException(status_code=404, detail="Ticket not found")

    return row


def serialize_ticket(row, user: dict) -> dict:
    data = dict(row)
    data["ticket_ref"] = ticket_ref(data["id"])

    if is_staff(user):
        data["escalated"] = bool(data["escalated"])
        return data

    # Customers never see AI drafts, internal policy text, priority,
    # escalation flags or generation diagnostics.
    return {
        "id": data["id"],
        "ticket_ref": data["ticket_ref"],
        "customer_name": data["customer_name"],
        "message": data["message"],
        "category": data["category"],
        "status": PUBLIC_STATUS.get(data["status"], data["status"]),
        "created_at": data["created_at"],
        "updated_at": data["updated_at"],
    }


def record_audit_event(
    connection,
    ticket_id: int,
    action: str,
    details: str = "",
    actor: Optional[str] = None,
):
    connection.execute(
        """
        INSERT INTO audit_logs (ticket_id, action, details, actor)
        VALUES (?, ?, ?, ?)
        """,
        (ticket_id, action, details, actor),
    )


def add_comment(connection, ticket_id: int, user: dict, body: str, kind: str):
    connection.execute(
        """
        INSERT INTO ticket_comments
            (ticket_id, user_id, author_name, author_role, kind, body)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (ticket_id, user["id"], user["username"], user["role"], kind, body),
    )


# The AI assistant posts into the thread like a user, with role "ai".
AI_USER = {"id": None, "username": AI_AUTHOR_NAME, "role": "ai"}


def pick_auto_assignee(connection):
    """Choose the human agent with the fewest open tickets.

    Ties go to the oldest account. Admins are only used when there is
    no agent at all. Returns a user id, or None if nobody is available.
    """
    for role in ("agent", "admin"):
        row = connection.execute(
            """
            SELECT u.id
            FROM users u
            WHERE u.role = ?
            ORDER BY (
                SELECT COUNT(*) FROM tickets t
                WHERE t.assigned_to = u.id
                  AND t.status NOT IN ('resolved', 'closed')
            ), u.id
            LIMIT 1
            """,
            (role,),
        ).fetchone()
        if row:
            return row["id"]
    return None


# --------------------------------------------------
# Classification, policies, drafts (unchanged logic)
# --------------------------------------------------

POLICIES = {
    "billing": {
        "title": "Billing and Refund Policy",
        "policy": (
            "For billing questions, verify transaction details and "
            "subscription status before advising the customer. Duplicate "
            "charges and refund requests must be reviewed by a support "
            "agent. Do not promise a refund before eligibility is confirmed."
        ),
    },
    "technical": {
        "title": "Technical Support Policy",
        "policy": (
            "For technical issues, ask for relevant error details and "
            "suggest basic troubleshooting. Escalate persistent or "
            "serious technical problems to the support team."
        ),
    },
    "account_access": {
        "title": "Account Access Policy",
        "policy": (
            "For account access issues, guide the customer to the official "
            "password recovery process. Never ask the customer to share "
            "their password or authentication codes. Escalate suspected "
            "account compromise to a human support agent."
        ),
    },
    "other": {
        "title": "General Support Policy",
        "policy": (
            "For general questions, provide clear and helpful guidance. "
            "If the answer is not available, refer the question to a "
            "human support agent."
        ),
    },
}


def classify_ticket(message: str) -> str:
    text = message.lower()

    if any(word in text for word in [
        "charged", "payment", "billing", "refund", "invoice",
        "subscription", "transaction",
    ]):
        return "billing"

    if any(word in text for word in [
        "password", "login", "log in", "sign in", "account",
        "locked out", "authentication",
    ]):
        return "account_access"

    if any(word in text for word in [
        "error", "crash", "crashing", "bug", "not working",
        "broken", "loading",
    ]):
        return "technical"

    return "other"


def create_draft(customer_name: str, category: str) -> str:
    if category == "billing":
        return (
            f"Hi {customer_name}, thank you for contacting us about "
            "your billing concern. We're sorry for the inconvenience. "
            "Our support team will need to review the transaction details "
            "and confirm refund eligibility before a refund can be promised."
        )

    if category == "technical":
        return (
            f"Hi {customer_name}, we're sorry you're experiencing "
            "this technical issue. Please share any error message and "
            "the steps that caused the problem so our team can investigate."
        )

    if category == "account_access":
        return (
            f"Hi {customer_name}, we're sorry you're having trouble "
            "accessing your account. Please use the official password "
            "recovery process. Never share your password or authentication "
            "codes with anyone."
        )

    return (
        f"Hi {customer_name}, thank you for contacting our support team. "
        "We'll review your question and help you find the right information."
    )


def assess_escalation(message: str, category: str):
    text = message.lower()

    account_risk = [
        "hacked",
        "unauthorized access",
        "someone accessed my account",
        "account compromised",
        "stolen account",
    ]

    if category == "account_access" and any(
        phrase in text for phrase in account_risk
    ):
        return ("urgent", True, "Possible account compromise")

    billing_risk = [
        "charged twice",
        "double charged",
        "duplicate charge",
        "refund",
        "charged incorrectly",
    ]

    if category == "billing" and any(
        phrase in text for phrase in billing_risk
    ):
        return ("high", True, "Billing dispute requires review")

    technical_risk = [
        "production is down",
        "system outage",
        "data loss",
        "all users are affected",
    ]

    if category == "technical" and any(
        phrase in text for phrase in technical_risk
    ):
        return ("high", True, "Potentially severe technical issue")

    return ("medium", False, None)


# --------------------------------------------------
# Basic routes
# --------------------------------------------------

@app.get("/")
def home():
    return {"message": "Welcome to SupportFlow AI", "status": "running"}


@app.get("/health")
def health():
    return {"status": "healthy"}


# --------------------------------------------------
# Tickets
# --------------------------------------------------

@app.post("/tickets", status_code=201)
def create_ticket(
    ticket: TicketRequest,
    current_user: dict = Depends(get_current_user),
):
    staff = is_staff(current_user)

    customer_name = (ticket.customer_name or "").strip()
    if not customer_name:
        if staff:
            raise HTTPException(
                status_code=400,
                detail="Customer name is required.",
            )
        customer_name = current_user["username"]

    category = classify_ticket(ticket.message)
    policy = POLICIES[category]

    priority, escalated, escalation_reason = assess_escalation(
        ticket.message, category
    )

    result = generate_support_response(
        customer_name=customer_name,
        message=ticket.message,
        category=category,
        policy_title=policy["title"],
        policy=policy["policy"],
    )

    if isinstance(result, str):
        draft = result
        generation_source = "ai"
        generation_status = "success"
        generation_error = None
    else:
        draft = result.response
        generation_source = result.source
        generation_status = result.status
        generation_error = result.error_message

    if not draft or not draft.strip():
        draft = create_draft(customer_name, category)
        generation_source = "fallback"
        generation_status = "fallback"

        if not generation_error:
            generation_error = "No usable AI response was returned."

    # Tickets raised by a customer belong to that customer.
    # Tickets a staff member logs on someone's behalf have no owner.
    owner_id = None if staff else current_user["id"]

    # The AI answers first, but only when that is safe: the AI really wrote
    # the reply (not the fallback template) and a human does not need to
    # look at the ticket first (escalated tickets are always held).
    auto_send = bool(
        AUTO_SEND_AI_REPLY
        and not escalated
        and generation_status == "success"
        and generation_source != "fallback"
    )
    status = "ai_replied" if auto_send else "received"

    with db() as connection:
        assignee_id = pick_auto_assignee(connection) if AUTO_ASSIGN else None

        cursor = connection.execute(
            """
            INSERT INTO tickets (
                customer_name, message, category,
                policy_title, policy, draft_response, approved_response,
                status, priority, escalated, escalation_reason,
                generation_source, generation_status, generation_error,
                user_id, assigned_to, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    datetime('now'), datetime('now'))
            """,
            (
                customer_name,
                ticket.message.strip(),
                category,
                policy["title"],
                policy["policy"],
                draft,
                draft if auto_send else None,
                status,
                priority,
                int(escalated),
                escalation_reason,
                generation_source,
                generation_status,
                generation_error,
                owner_id,
                assignee_id,
            ),
        )

        new_id = cursor.lastrowid

        record_audit_event(
            connection,
            new_id,
            "ticket_created",
            f"Draft source: {generation_source}; "
            f"generation status: {generation_status}",
            actor=current_user["username"],
        )

        if assignee_id is not None:
            assignee = connection.execute(
                "SELECT username FROM users WHERE id = ?", (assignee_id,)
            ).fetchone()
            record_audit_event(
                connection,
                new_id,
                "ticket_assigned",
                f"Auto-assigned to {assignee['username']}",
                actor="system",
            )

        if auto_send:
            # The AI reply goes straight into the conversation.
            add_comment(connection, new_id, AI_USER, draft.strip(), "response")
            record_audit_event(
                connection,
                new_id,
                "ai_response_sent",
                "AI reply sent automatically.",
                actor=AI_AUTHOR_NAME,
            )

        connection.commit()

        row = fetch_ticket(connection, new_id, current_user)
        return serialize_ticket(row, current_user)


@app.get("/tickets")
def list_tickets(current_user: dict = Depends(get_current_user)):
    with db() as connection:
        if is_staff(current_user):
            rows = connection.execute(
                TICKET_SELECT + " ORDER BY t.id DESC"
            ).fetchall()
        else:
            rows = connection.execute(
                TICKET_SELECT + " WHERE t.user_id = ? ORDER BY t.id DESC",
                (current_user["id"],),
            ).fetchall()

    return [serialize_ticket(row, current_user) for row in rows]


@app.get("/tickets/{ticket_id}")
def get_ticket(
    ticket_id: int,
    current_user: dict = Depends(get_current_user),
):
    with db() as connection:
        row = fetch_ticket(connection, ticket_id, current_user)
        return serialize_ticket(row, current_user)


@app.patch("/tickets/{ticket_id}/draft")
def update_draft(
    ticket_id: int,
    update: DraftUpdate,
    current_user: dict = Depends(require_staff),
):
    with db() as connection:
        ticket = fetch_ticket(connection, ticket_id, current_user)

        if ticket["status"] in ("ai_replied", "approved", "resolved", "closed"):
            raise HTTPException(
                status_code=409,
                detail=(
                    "A response that was already sent cannot be edited. "
                    "Send a human reply instead, or reopen the ticket."
                ),
            )

        # Editing a rejected draft puts the ticket back in the review queue.
        new_status = "received" if ticket["status"] == "rejected" else ticket["status"]

        connection.execute(
            """
            UPDATE tickets
            SET draft_response = ?, status = ?, updated_at = datetime('now')
            WHERE id = ?
            """,
            (update.draft_response.strip(), new_status, ticket_id),
        )
        record_audit_event(
            connection,
            ticket_id,
            "draft_edited",
            "Support draft was edited.",
            actor=current_user["username"],
        )
        connection.commit()

        return {
            "message": "Draft updated successfully",
            "id": ticket_id,
            "status": new_status,
        }


@app.post("/tickets/{ticket_id}/approve")
def approve_ticket(
    ticket_id: int,
    current_user: dict = Depends(require_staff),
):
    with db() as connection:
        ticket = fetch_ticket(connection, ticket_id, current_user)

        if ticket["status"] not in ("received", "in_progress"):
            raise HTTPException(
                status_code=409,
                detail="Only tickets awaiting review can be approved.",
            )

        draft = ticket["draft_response"]

        if not draft or not draft.strip():
            raise HTTPException(
                status_code=400,
                detail="There is no draft response to approve.",
            )

        connection.execute(
            """
            UPDATE tickets
            SET status = 'approved',
                approved_response = draft_response,
                assigned_to = COALESCE(assigned_to, ?),
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (current_user["id"], ticket_id),
        )

        # The approved reply becomes a message in the ticket thread,
        # which is how the customer receives it.
        add_comment(connection, ticket_id, current_user, draft.strip(), "response")

        record_audit_event(
            connection,
            ticket_id,
            "response_approved",
            "Support response was approved.",
            actor=current_user["username"],
        )
        connection.commit()

        return {
            "message": "Response approved and sent to the customer",
            "id": ticket_id,
            "status": "approved",
            "approved_response": draft,
        }


@app.post("/tickets/{ticket_id}/reply")
def send_human_reply(
    ticket_id: int,
    reply: ReplyRequest,
    current_user: dict = Depends(require_staff),
):
    """A human agent writes their own response to the customer.

    This is the 'real human response' that follows the AI assistant's
    first answer. It can also be used instead of approving the draft.
    """
    with db() as connection:
        ticket = fetch_ticket(connection, ticket_id, current_user)

        if ticket["status"] not in (
            "received", "in_progress", "rejected", "ai_replied"
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Cannot reply to a ticket whose status is "
                    f"'{ticket['status']}'."
                ),
            )

        text = reply.body.strip()

        connection.execute(
            """
            UPDATE tickets
            SET status = 'approved',
                approved_response = ?,
                assigned_to = COALESCE(assigned_to, ?),
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (text, current_user["id"], ticket_id),
        )
        add_comment(connection, ticket_id, current_user, text, "response")
        record_audit_event(
            connection,
            ticket_id,
            "human_response_sent",
            "A human agent replied to the customer.",
            actor=current_user["username"],
        )
        connection.commit()

        return {
            "message": "Your reply was sent to the customer",
            "id": ticket_id,
            "status": "approved",
        }


@app.post("/tickets/{ticket_id}/reject")
def reject_ticket(
    ticket_id: int,
    current_user: dict = Depends(require_staff),
):
    with db() as connection:
        ticket = fetch_ticket(connection, ticket_id, current_user)

        if ticket["status"] not in ("received", "in_progress"):
            raise HTTPException(
                status_code=409,
                detail="Only tickets awaiting review can be rejected.",
            )

        connection.execute(
            """
            UPDATE tickets
            SET status = 'rejected',
                assigned_to = COALESCE(assigned_to, ?),
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (current_user["id"], ticket_id),
        )
        record_audit_event(
            connection,
            ticket_id,
            "draft_rejected",
            "Support draft was rejected.",
            actor=current_user["username"],
        )
        connection.commit()

        return {
            "message": "Draft rejected",
            "id": ticket_id,
            "status": "rejected",
        }


@app.post("/tickets/{ticket_id}/status")
def change_ticket_status(
    ticket_id: int,
    change: StatusChange,
    current_user: dict = Depends(get_current_user),
):
    rule = STATUS_ACTIONS[change.action]
    staff = is_staff(current_user)

    if rule["staff_only"] and not staff:
        raise HTTPException(
            status_code=403,
            detail="Only support staff can do that.",
        )

    with db() as connection:
        ticket = fetch_ticket(connection, ticket_id, current_user)

        if ticket["status"] not in rule["from"]:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Cannot {change.action} a ticket whose status is "
                    f"'{ticket['status']}'."
                ),
            )

        # Starting work claims an unassigned ticket.
        claim = current_user["id"] if (staff and change.action == "start") else None

        connection.execute(
            """
            UPDATE tickets
            SET status = ?,
                assigned_to = COALESCE(assigned_to, ?),
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (rule["to"], claim, ticket_id),
        )

        if change.action == "human":
            # Flag it so it stands out in the agents' queue.
            connection.execute(
                """
                UPDATE tickets
                SET escalated = 1,
                    escalation_reason = COALESCE(
                        NULLIF(escalation_reason, ''),
                        'Customer asked for a human agent'
                    ),
                    priority = CASE
                        WHEN priority IN ('urgent', 'high') THEN priority
                        ELSE 'high'
                    END
                WHERE id = ?
                """,
                (ticket_id,),
            )

        note = (change.note or "").strip()
        if note:
            add_comment(
                connection,
                ticket_id,
                current_user,
                note,
                "note" if staff else "comment",
            )

        record_audit_event(
            connection,
            ticket_id,
            rule["audit"],
            "",
            actor=current_user["username"],
        )
        connection.commit()

        return {
            "message": f"Ticket {ticket_ref(ticket_id)} is now {rule['to']}",
            "id": ticket_id,
            "status": rule["to"],
        }


@app.post("/tickets/{ticket_id}/assign")
def assign_ticket(
    ticket_id: int,
    request: AssignRequest,
    current_user: dict = Depends(require_staff),
):
    admin = current_user["role"] == "admin"

    with db() as connection:
        ticket = fetch_ticket(connection, ticket_id, current_user)

        if ticket["status"] == "closed":
            raise HTTPException(
                status_code=409,
                detail="Closed tickets cannot be reassigned.",
            )

        current_assignee = ticket["assigned_to"]
        new_status = ticket["status"]

        if request.assignee_id is None:
            if not admin and current_assignee not in (None, current_user["id"]):
                raise HTTPException(
                    status_code=403,
                    detail="Only an admin can unassign another agent's ticket.",
                )
            assignee_label = "Unassigned"
        else:
            if not admin and request.assignee_id != current_user["id"]:
                raise HTTPException(
                    status_code=403,
                    detail="Agents can only assign tickets to themselves.",
                )
            if (
                not admin
                and current_assignee not in (None, current_user["id"])
            ):
                raise HTTPException(
                    status_code=409,
                    detail="This ticket is already assigned to another agent.",
                )

            assignee = connection.execute(
                "SELECT id, username, role FROM users WHERE id = ?",
                (request.assignee_id,),
            ).fetchone()

            if assignee is None or assignee["role"] not in STAFF_ROLES:
                raise HTTPException(
                    status_code=400,
                    detail="The assignee must be a staff member.",
                )

            assignee_label = f"Assigned to {assignee['username']}"
            if ticket["status"] == "received":
                new_status = "in_progress"

        connection.execute(
            """
            UPDATE tickets
            SET assigned_to = ?, status = ?, updated_at = datetime('now')
            WHERE id = ?
            """,
            (request.assignee_id, new_status, ticket_id),
        )
        record_audit_event(
            connection,
            ticket_id,
            "ticket_assigned",
            assignee_label,
            actor=current_user["username"],
        )
        connection.commit()

        return {
            "message": assignee_label,
            "id": ticket_id,
            "status": new_status,
        }


# --------------------------------------------------
# Conversation thread
# --------------------------------------------------

@app.get("/tickets/{ticket_id}/comments")
def list_comments(
    ticket_id: int,
    current_user: dict = Depends(get_current_user),
):
    staff = is_staff(current_user)

    with db() as connection:
        fetch_ticket(connection, ticket_id, current_user)

        rows = connection.execute(
            """
            SELECT id, author_name, author_role, kind, body, created_at
            FROM ticket_comments
            WHERE ticket_id = ?
            ORDER BY id ASC
            """,
            (ticket_id,),
        ).fetchall()

    comments = []
    for row in rows:
        item = dict(row)
        if not staff:
            if item["kind"] == "note":
                continue  # internal notes are staff-only
            if item["author_role"] == "ai":
                item["author_name"] = AI_AUTHOR_NAME
            elif item["author_role"] != "customer":
                item["author_name"] = "Support team"
        comments.append(item)

    return comments


@app.post("/tickets/{ticket_id}/comments", status_code=201)
def post_comment(
    ticket_id: int,
    comment: CommentRequest,
    current_user: dict = Depends(get_current_user),
):
    staff = is_staff(current_user)

    if comment.internal and not staff:
        raise HTTPException(
            status_code=403,
            detail="Only staff can add internal notes.",
        )

    with db() as connection:
        ticket = fetch_ticket(connection, ticket_id, current_user)

        if ticket["status"] == "closed" and not comment.internal:
            raise HTTPException(
                status_code=409,
                detail="This ticket is closed. Reopen it to add a message.",
            )

        kind = "note" if comment.internal else "comment"
        add_comment(connection, ticket_id, current_user, comment.body.strip(), kind)

        connection.execute(
            "UPDATE tickets SET updated_at = datetime('now') WHERE id = ?",
            (ticket_id,),
        )
        record_audit_event(
            connection,
            ticket_id,
            "note_added" if comment.internal else "comment_added",
            "",
            actor=current_user["username"],
        )
        connection.commit()

    return {"message": "Note added" if comment.internal else "Message sent"}


# --------------------------------------------------
# Audit history / customer timeline
# --------------------------------------------------

@app.get("/tickets/{ticket_id}/audit")
def get_ticket_audit_history(
    ticket_id: int,
    current_user: dict = Depends(get_current_user),
):
    with db() as connection:
        fetch_ticket(connection, ticket_id, current_user)

        rows = connection.execute(
            """
            SELECT id, ticket_id, action, actor, details, created_at
            FROM audit_logs
            WHERE ticket_id = ?
            ORDER BY id DESC
            """,
            (ticket_id,),
        ).fetchall()

    if is_staff(current_user):
        return [dict(row) for row in rows]

    # Customers get a friendly, details-free timeline.
    return [
        {
            "id": row["id"],
            "action": row["action"],
            "label": CUSTOMER_EVENT_LABELS[row["action"]],
            "created_at": row["created_at"],
        }
        for row in rows
        if row["action"] in CUSTOMER_EVENT_LABELS
    ]
