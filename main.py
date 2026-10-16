from fastapi import FastAPI
from pydantic import BaseModel, Field

app = FastAPI(
    title="SupportFlow AI",
    description="An intelligent customer support ticket system",
    version="0.2.0",
)


class TicketRequest(BaseModel):
    customer_name: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=5, max_length=3000)


def classify_ticket(message: str) -> str:
    """Classify a ticket using simple keyword rules."""

    text = message.lower()

    billing_keywords = [
        "charged", "charge", "payment", "refund",
        "invoice", "subscription", "billing", "money back",
    ]

    account_keywords = [
        "login", "log in", "password", "locked account",
        "can't sign in", "cannot sign in", "account access",
    ]

    technical_keywords = [
        "bug", "error", "crash", "not loading",
        "broken", "technical", "website down", "slow",
    ]

    if any(word in text for word in billing_keywords):
        return "billing"

    if any(word in text for word in account_keywords):
        return "account_access"

    if any(word in text for word in technical_keywords):
        return "technical"

    return "other"


@app.get("/")
def home():
    return {
        "message": "Welcome to SupportFlow AI",
        "status": "running",
    }


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.post("/tickets")
def create_ticket(ticket: TicketRequest):
    category = classify_ticket(ticket.message)

    return {
        "customer_name": ticket.customer_name,
        "message": ticket.message,
        "category": category,
        "status": "received",
    }