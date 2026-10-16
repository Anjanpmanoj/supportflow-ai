
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from database import get_connection, initialize_database


@asynccontextmanager
async def lifespan(app: FastAPI):
    initialize_database()
    yield


app = FastAPI(
    title="SupportFlow AI",
    description="A customer support ticket workflow API",
    version="0.2.0",
    lifespan=lifespan,
)


class TicketRequest(BaseModel):
    customer_name: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=5, max_length=3000)


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

    # Potential account compromise: urgent human review.
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
        return (
            "urgent",
            True,
            "Possible account compromise",
        )

    # Billing disputes requiring human review.
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
        return (
            "high",
            True,
            "Billing dispute requires review",
        )

    # Serious technical issues.
    technical_risk = [
        "production is down",
        "system outage",
        "data loss",
        "all users are affected",
    ]

    if category == "technical" and any(
        phrase in text for phrase in technical_risk
    ):
        return (
            "high",
            True,
            "Potentially severe technical issue",
        )

    return ("medium", False, None)


@app.get("/")
def home():
    return {
        "message": "Welcome to SupportFlow AI",
        "status": "running",
    }


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.post("/tickets", status_code=201)
def create_ticket(ticket: TicketRequest):
    category = classify_ticket(ticket.message)
    policy = POLICIES[category]
    draft = create_draft(ticket.customer_name, category)
    priority, escalated, escalation_reason = assess_escalation(
    ticket.message, category
)

    connection = get_connection()

    try:
        cursor = connection.execute(
    """
    INSERT INTO tickets (
        customer_name, message, category,
        policy_title, policy, draft_response, status,
        priority, escalated, escalation_reason
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
    (
        ticket.customer_name,
        ticket.message,
        category,
        policy["title"],
        policy["policy"],
        draft,
        "received",
        priority,
        int(escalated),
        escalation_reason,
    ),
)
        connection.commit()
        ticket_id = cursor.lastrowid

        return {
            "id": ticket_id,
            "customer_name": ticket.customer_name,
            "message": ticket.message,
            "category": category,
            "policy_title": policy["title"],
            "policy": policy["policy"],
            "draft_response": draft,
            "status": "received",
            "priority": priority,
            "escalated": escalated,
            "escalation_reason": escalation_reason,
        }

    finally:
        connection.close()


@app.get("/tickets")
def list_tickets():
    connection = get_connection()

    try:
        rows = connection.execute(
            "SELECT * FROM tickets ORDER BY id DESC"
        ).fetchall()

        return [dict(row) for row in rows]

    finally:
        connection.close()


@app.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: int):
    connection = get_connection()

    try:
        row = connection.execute(
            "SELECT * FROM tickets WHERE id = ?",
            (ticket_id,),
        ).fetchone()

        if row is None:
            raise HTTPException(
                status_code=404,
                detail="Ticket not found",
            )

        return dict(row)

    finally:
        connection.close()
