
import pytest
import database
from fastapi.testclient import TestClient

from main import app, classify_ticket, assess_escalation


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Use a separate temporary database for each test.
    test_db = tmp_path / "test_supportflow.db"
    monkeypatch.setattr(database, "DATABASE_NAME", str(test_db))

    with TestClient(app) as test_client:
        yield test_client


def test_health_endpoint(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_classify_billing_ticket():
    assert classify_ticket(
        "I was charged twice for my subscription."
    ) == "billing"


def test_classify_account_access_ticket():
    assert classify_ticket(
        "I forgot my password and cannot sign in."
    ) == "account_access"


def test_classify_technical_ticket():
    assert classify_ticket(
        "The website keeps crashing with an error."
    ) == "technical"


def test_classify_general_ticket():
    assert classify_ticket(
        "Where is your office located?"
    ) == "other"


def test_urgent_account_escalation():
    result = assess_escalation(
        "Someone accessed my account without permission.",
        "account_access",
    )

    assert result[0] == "urgent"
    assert result[1] is True
    assert result[2] == "Possible account compromise"


def test_create_ticket(client):
    response = client.post(
        "/tickets",
        json={
            "customer_name": "Anjan",
            "message": "I was charged twice for my subscription.",
        },
    )

    assert response.status_code == 201

    ticket = response.json()
    assert ticket["id"] > 0
    assert ticket["category"] == "billing"
    assert ticket["priority"] == "high"
    assert ticket["escalated"] is True


def test_list_tickets(client):
    client.post(
        "/tickets",
        json={
            "customer_name": "Anjan",
            "message": "I forgot my password and cannot sign in.",
        },
    )

    response = client.get("/tickets")

    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["customer_name"] == "Anjan"


def test_get_ticket_by_id(client):
    created = client.post(
        "/tickets",
        json={
            "customer_name": "Anjan",
            "message": "The website keeps crashing with an error.",
        },
    ).json()

    response = client.get(f"/tickets/{created['id']}")

    assert response.status_code == 200
    assert response.json()["id"] == created["id"]
    assert response.json()["category"] == "technical"


def test_missing_ticket_returns_404(client):
    response = client.get("/tickets/9999")

    assert response.status_code == 404
    assert response.json()["detail"] == "Ticket not found"


def test_invalid_ticket_request(client):
    response = client.post(
        "/tickets",
        json={
            "customer_name": "",
            "message": "Hi",
        },
    )

    assert response.status_code == 422
