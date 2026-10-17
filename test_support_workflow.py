"""Tests for the LangGraph support workflow and the customer lookup tool.

The Gemini model is replaced by a fake chain, so these run offline.
"""

import os
import tempfile
from types import SimpleNamespace

# Use a throwaway database and secret; must be set before importing the app.
_TMP = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")
os.environ["JWT_SECRET_KEY"] = "test-secret-key-for-pytest-only-1234567890"
os.environ["SEED_DEMO_DATA"] = "false"
os.environ["ADMIN_USERNAME"] = ""

import pytest
from fastapi.testclient import TestClient

import ai_service
from ai_service import check_reply, generate_support_response
from customer_tools import format_customer_context, get_customer_context
from database import get_connection, initialize_database
from knowledge_base import search_policies

GOOD = (
    "Hi Sam, thank you for contacting us about your billing concern. "
    "Our support team will review the transaction details and follow up."
)


class FakeChain:
    """Stands in for `prompt | gemini`; replies are returned in order."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def invoke(self, inputs):
        self.calls.append(inputs)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(content=reply)


def run(chain, monkeypatch, **overrides):
    monkeypatch.setattr(ai_service, "support_chain", chain)
    args = dict(
        customer_name="Sam",
        message="I was charged twice",
        category="billing",
        policy_title="Billing and Refund Policy",
        policy="Verify before refunds.",
    )
    args.update(overrides)
    return generate_support_response(**args)


# ---------- validation rules ----------

@pytest.mark.parametrize("text", [
    GOOD,
    "Hi Sam, please never share your password or authentication codes with anyone.",
    "Hi Sam, we do not ask for your PIN. Once eligibility is confirmed we can discuss a refund.",
])
def test_safe_replies_pass(text):
    assert check_reply(text) == []


@pytest.mark.parametrize("text", [
    "Hi Sam, please send me your password so I can check your account.",
    "Hi Sam, we will refund you in full within three days of today.",
    "Hi Sam, your refund has been approved and will arrive soon.",
    "Hi Sam, track your parcel here: https://example.com/track/123",
    "Hi [Customer Name], thanks for reaching out to our support team.",
    "Subject: Your ticket\nHi Sam, thanks for getting in touch with us today.",
    "Hi!",
])
def test_unsafe_replies_are_flagged(text):
    assert check_reply(text)


# ---------- the graph ----------

def test_good_draft_passes_first_time(monkeypatch):
    chain = FakeChain(GOOD)
    result = run(chain, monkeypatch)
    assert (result.status, result.source, result.response) == ("success", "gemini", GOOD)
    assert len(chain.calls) == 1


def test_rejected_draft_is_retried_with_feedback(monkeypatch):
    chain = FakeChain("Hi Sam, please send me your password to continue.", GOOD)
    result = run(chain, monkeypatch)
    assert result.status == "success" and result.response == GOOD
    assert len(chain.calls) == 2
    assert "password" in chain.calls[1]["revision_notes"]


def test_gives_up_after_repeated_validation_failures(monkeypatch):
    chain = FakeChain("Hi Sam, we will refund you in full right now, promise.")
    result = run(chain, monkeypatch)
    assert result.status == "validation_failed"
    assert result.response is None and result.source == "template"
    assert len(chain.calls) == ai_service.MAX_DRAFT_ATTEMPTS


@pytest.mark.parametrize("error,expected", [
    (Exception("429 RESOURCE_EXHAUSTED quota exceeded"), "quota_exceeded"),
    (Exception("request timed out"), "timeout"),
    (Exception("something odd"), "provider_error"),
])
def test_provider_errors_are_classified(monkeypatch, error, expected):
    result = run(FakeChain(error), monkeypatch)
    assert result.status == expected and result.response is None


def test_empty_response(monkeypatch):
    assert run(FakeChain("   "), monkeypatch).status == "empty_response"


def test_not_configured_without_model(monkeypatch):
    monkeypatch.setattr(ai_service, "support_chain", None)
    monkeypatch.setattr(ai_service, "api_key", None)
    result = generate_support_response("Sam", "hello there", "other", "t", "p")
    assert result.status == "not_configured"


# ---------- customer lookup tool ----------

def _add_user_with_tickets():
    initialize_database()
    connection = get_connection()
    try:
        connection.execute(
            "INSERT INTO users (username, email, password_hash, role) "
            "VALUES ('sam', 'sam@example.com', 'x', 'customer')"
        )
        user_id = connection.execute(
            "SELECT id FROM users WHERE username = 'sam'"
        ).fetchone()["id"]
        for category, status in (("billing", "resolved"), ("billing", "received")):
            connection.execute(
                "INSERT INTO tickets (customer_name, message, category, status, "
                "user_id, created_at, updated_at) VALUES "
                "('Sam', 'old message', ?, ?, ?, datetime('now'), datetime('now'))",
                (category, status, user_id),
            )
        connection.commit()
        return user_id
    finally:
        connection.close()


def test_customer_lookup_and_prompt_context(monkeypatch):
    user_id = _add_user_with_tickets()

    info = get_customer_context(user_id, "Sam", category="billing")
    assert info["total_tickets"] == 2
    assert len(info["open_tickets"]) == 1
    assert info["same_category_recent"] == 2

    text = format_customer_context(info)
    assert "Previous tickets: 2" in text and "SF-" in text
    assert "old message" not in text  # old customer text never reaches the prompt

    chain = FakeChain(GOOD)
    run(chain, monkeypatch, user_id=user_id)
    assert "Previous tickets: 2" in chain.calls[0]["customer_context"]


def test_unknown_customer_has_no_history(monkeypatch):
    initialize_database()
    chain = FakeChain(GOOD)
    run(chain, monkeypatch, customer_name="Nobody", user_id=None)
    assert "No previous tickets" in chain.calls[0]["customer_context"]


# ---------- end to end through the API ----------

def test_second_ticket_sees_the_first(monkeypatch):
    import main

    chain = FakeChain(GOOD)
    monkeypatch.setattr(ai_service, "support_chain", chain)

    with TestClient(main.app) as client:
        client.post("/auth/register", json={
            "username": "casey", "email": "casey@example.com",
            "password": "password123",
        })
        token = client.post("/auth/login", json={
            "username": "casey", "password": "password123",
        }).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        first = client.post("/tickets", json={"message": "I was charged twice"}, headers=headers)
        second = client.post("/tickets", json={"message": "Still charged twice"}, headers=headers)

    assert first.status_code == second.status_code == 201
    assert "No previous tickets" in chain.calls[0]["customer_context"]
    assert first.json()["ticket_ref"] in chain.calls[1]["customer_context"]


# ---------- policy search ----------

def test_policy_search_prefers_matching_category():
    assert search_policies("billing: I was charged twice", k=1)[0].metadata["category"] == "billing"
    assert search_policies("account_access: locked out", k=1)[0].metadata["category"] == "account_access"
    assert len(search_policies("technical: app crashes", k=2)) == 2
