from fastapi import FastAPI
from pydantic import BaseModel, Field
import json
from pathlib import Path

app = FastAPI(
    title="SupportFlow AI",
    description="An intelligent customer support ticket system",
    version="0.2.0",
)
POLICY_FILE = Path(__file__).parent / "knowledge_base" / "policies.json"


def load_policies():
    with POLICY_FILE.open("r", encoding="utf-8") as file:
        return json.load(file)


POLICIES = load_policies()


def retrieve_policy(category: str) -> dict:
    return POLICIES.get(category, POLICIES["other"])

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
@app.post("/tickets")
def create_ticket(ticket: TicketRequest):
    category = classify_ticket(ticket.message)
    policy = retrieve_policy(category)

    return {
        "customer_name": ticket.customer_name,
        "message": ticket.message,
        "category": category,
        "policy_title": policy["title"],
        "policy": policy["policy"],
        "status": "received",
    }