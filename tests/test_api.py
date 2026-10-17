import re
import uuid

import pytest
from fastapi.testclient import TestClient

import auth
import database
from auth import hash_password
from main import app, assess_escalation, classify_ticket

PASSWORD = "TestPassword123!"
DRAFT = "Test draft response"
BILLING_MESSAGE = "I was charged twice for my subscription."


# --------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    # Use a separate temporary database for each test.
    test_db = tmp_path / "test_supportflow.db"
    monkeypatch.setattr(database, "DATABASE_NAME", str(test_db))
    monkeypatch.setattr(auth, "JWT_SECRET_KEY", "test-secret-key-for-tests")
    monkeypatch.setattr(
        "main.generate_support_response",
        lambda **kwargs: DRAFT,
    )

    with TestClient(app) as test_client:
        yield test_client


def make_account(client, role="customer"):
    """Create an account and log in. Returns (auth headers, user dict).

    Public registration only creates customers, so staff accounts are
    inserted directly, the same way create_admin.py does it.
    """
    username = f"{role}_{uuid.uuid4().hex[:8]}"
    email = f"{username}@example.com"

    if role == "customer":
        response = client.post(
            "/auth/register",
            json={"username": username, "email": email, "password": PASSWORD},
        )
        assert response.status_code == 201, response.text
    else:
        connection = database.get_connection()
        connection.execute(
            """
            INSERT INTO users (username, email, password_hash, role)
            VALUES (?, ?, ?, ?)
            """,
            (username, email, hash_password(PASSWORD), role),
        )
        connection.commit()
        connection.close()

    login = client.post(
        "/auth/login",
        json={"username": username, "password": PASSWORD},
    )
    assert login.status_code == 200, login.text

    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    me = client.get("/auth/me", headers=headers)
    assert me.status_code == 200

    return headers, me.json()


def make_ticket(client, headers, message=BILLING_MESSAGE, name=None):
    payload = {"message": message}
    if name:
        payload["customer_name"] = name

    response = client.post("/tickets", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def status_of(client, headers, ticket_id):
    response = client.get(f"/tickets/{ticket_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["status"]


def post_status(client, headers, ticket_id, action, note=None):
    payload = {"action": action}
    if note:
        payload["note"] = note
    return client.post(
        f"/tickets/{ticket_id}/status", json=payload, headers=headers
    )


# --------------------------------------------------
# Basics and pure logic (unchanged behaviour)
# --------------------------------------------------

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


# --------------------------------------------------
# Authentication
# --------------------------------------------------

def test_register_creates_a_customer(client):
    _, me = make_account(client, "customer")

    assert me["role"] == "customer"


def test_register_cannot_grant_admin(client):
    username = f"sneaky_{uuid.uuid4().hex[:8]}"

    response = client.post(
        "/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": PASSWORD,
            "role": "admin",  # must be ignored
        },
    )
    assert response.status_code == 201
    assert response.json()["role"] == "customer"

    login = client.post(
        "/auth/login", json={"username": username, "password": PASSWORD}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    assert client.get("/auth/me", headers=headers).json()["role"] == "customer"
    assert client.get("/admin/users", headers=headers).status_code == 403


def test_register_rejects_duplicates_and_weak_input(client):
    username = f"dup_{uuid.uuid4().hex[:8]}"
    body = {
        "username": username,
        "email": f"{username}@example.com",
        "password": PASSWORD,
    }

    assert client.post("/auth/register", json=body).status_code == 201
    assert client.post("/auth/register", json=body).status_code == 409

    short_password = dict(body, username="other_user", email="o@example.com",
                          password="short")
    assert client.post("/auth/register", json=short_password).status_code == 400

    short_username = dict(body, username="ab", email="ab@example.com")
    assert client.post("/auth/register", json=short_username).status_code == 422


def test_login_with_wrong_password_fails(client):
    username = f"wrongpw_{uuid.uuid4().hex[:8]}"
    client.post(
        "/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": PASSWORD,
        },
    )

    response = client.post(
        "/auth/login", json={"username": username, "password": "nope-nope-nope"}
    )
    assert response.status_code == 401


def test_endpoints_require_a_valid_token(client):
    assert client.get("/tickets").status_code in (401, 403)
    assert client.post(
        "/tickets", json={"message": BILLING_MESSAGE}
    ).status_code in (401, 403)

    bad = {"Authorization": "Bearer not-a-real-token"}
    assert client.get("/tickets", headers=bad).status_code == 401
    assert client.get("/auth/me", headers=bad).status_code == 401


# --------------------------------------------------
# Creating and reading tickets
# --------------------------------------------------

def test_staff_create_ticket_has_full_details(client):
    staff, _ = make_account(client, "agent")

    ticket = make_ticket(client, staff, name="Anjan")

    assert ticket["id"] > 0
    assert ticket["category"] == "billing"
    assert ticket["priority"] == "high"
    assert ticket["escalated"] is True
    assert ticket["draft_response"] == DRAFT
    assert ticket["status"] == "received"


def test_staff_must_provide_customer_name(client):
    staff, _ = make_account(client, "agent")

    response = client.post(
        "/tickets", json={"message": BILLING_MESSAGE}, headers=staff
    )
    assert response.status_code == 400


def test_customer_ticket_gets_readable_id_and_hides_internals(client):
    customer, me = make_account(client, "customer")

    ticket = make_ticket(client, customer)  # name defaults to username

    assert re.fullmatch(r"SF-\d{6}", ticket["ticket_ref"])
    assert ticket["customer_name"] == me["username"]
    assert ticket["status"] == "received"

    for hidden in (
        "draft_response", "approved_response", "policy", "policy_title",
        "priority", "escalated", "escalation_reason", "generation_source",
        "generation_status", "generation_error", "assigned_to", "user_id",
    ):
        assert hidden not in ticket


def test_list_tickets(client):
    customer, _ = make_account(client, "customer")
    make_ticket(client, customer, "I forgot my password and cannot sign in.")

    response = client.get("/tickets", headers=customer)

    assert response.status_code == 200
    assert len(response.json()) == 1


def test_get_ticket_by_id(client):
    customer, _ = make_account(client, "customer")
    created = make_ticket(
        client, customer, "The website keeps crashing with an error."
    )

    response = client.get(f"/tickets/{created['id']}", headers=customer)

    assert response.status_code == 200
    assert response.json()["ticket_ref"] == created["ticket_ref"]


def test_missing_ticket_returns_404(client):
    customer, _ = make_account(client, "customer")
    staff, _ = make_account(client, "agent")

    assert client.get("/tickets/9999", headers=customer).status_code == 404
    assert client.get("/tickets/9999", headers=staff).status_code == 404


def test_invalid_ticket_request(client):
    customer, _ = make_account(client, "customer")

    response = client.post(
        "/tickets",
        json={"customer_name": "", "message": "Hi"},
        headers=customer,
    )

    assert response.status_code == 422


# --------------------------------------------------
# Privacy between customers, and staff-only actions
# --------------------------------------------------

def test_customers_only_see_their_own_tickets(client):
    alice, _ = make_account(client, "customer")
    bob, _ = make_account(client, "customer")
    staff, _ = make_account(client, "agent")

    alice_ticket = make_ticket(client, alice)
    bob_ticket = make_ticket(client, bob, "The website keeps crashing.")

    # Each customer's list contains only their own ticket.
    alice_ids = [t["id"] for t in client.get("/tickets", headers=alice).json()]
    assert alice_ids == [alice_ticket["id"]]

    # Other people's tickets look like they don't exist.
    for path in (
        f"/tickets/{alice_ticket['id']}",
        f"/tickets/{alice_ticket['id']}/comments",
        f"/tickets/{alice_ticket['id']}/audit",
    ):
        assert client.get(path, headers=bob).status_code == 404

    assert client.post(
        f"/tickets/{alice_ticket['id']}/comments",
        json={"body": "hello"},
        headers=bob,
    ).status_code == 404
    assert post_status(client, bob, alice_ticket["id"], "close").status_code == 404

    # Staff see everything.
    staff_ids = {t["id"] for t in client.get("/tickets", headers=staff).json()}
    assert staff_ids == {alice_ticket["id"], bob_ticket["id"]}


def test_tickets_logged_by_staff_are_not_visible_to_customers(client):
    customer, _ = make_account(client, "customer")
    staff, _ = make_account(client, "agent")

    ticket = make_ticket(client, staff, name="Phone caller")

    assert client.get("/tickets", headers=customer).json() == []
    assert client.get(
        f"/tickets/{ticket['id']}", headers=customer
    ).status_code == 404


def test_customer_cannot_use_staff_actions(client):
    customer, _ = make_account(client, "customer")
    ticket = make_ticket(client, customer)
    tid = ticket["id"]

    assert client.post(f"/tickets/{tid}/approve", headers=customer).status_code == 403
    assert client.post(f"/tickets/{tid}/reject", headers=customer).status_code == 403
    assert client.patch(
        f"/tickets/{tid}/draft",
        json={"draft_response": "I wrote my own reply"},
        headers=customer,
    ).status_code == 403
    assert client.post(
        f"/tickets/{tid}/assign", json={"assignee_id": None}, headers=customer
    ).status_code == 403
    assert post_status(client, customer, tid, "start").status_code == 403
    assert post_status(client, customer, tid, "resolve").status_code == 403
    assert client.get("/staff", headers=customer).status_code == 403


def test_customer_audit_history_is_friendly_and_has_no_internal_details(client):
    customer, _ = make_account(client, "customer")
    ticket = make_ticket(client, customer)

    events = client.get(
        f"/tickets/{ticket['id']}/audit", headers=customer
    ).json()

    assert [e["label"] for e in events] == ["Ticket submitted"]
    assert "actor" not in events[0]
    assert "details" not in events[0]


# --------------------------------------------------
# Review workflow: approve / reject / edit
# --------------------------------------------------

def test_approve_sends_the_reply_to_the_customer(client):
    customer, _ = make_account(client, "customer")
    staff, staff_user = make_account(client, "agent")
    tid = make_ticket(client, customer)["id"]

    response = client.post(f"/tickets/{tid}/approve", headers=staff)
    assert response.status_code == 200
    assert response.json()["status"] == "approved"

    # Customer sees the new status and the reply in the conversation.
    assert status_of(client, customer, tid) == "approved"
    thread = client.get(f"/tickets/{tid}/comments", headers=customer).json()
    assert len(thread) == 1
    assert thread[0]["kind"] == "response"
    assert thread[0]["body"] == DRAFT
    assert thread[0]["author_name"] == "Support team"  # staff name hidden

    # Staff audit trail records who approved it.
    audit = client.get(f"/tickets/{tid}/audit", headers=staff).json()
    assert audit[0]["action"] == "response_approved"
    assert audit[0]["actor"] == staff_user["username"]

    # Approving claims an unassigned ticket.
    ticket = client.get(f"/tickets/{tid}", headers=staff).json()
    assert ticket["assignee_name"] == staff_user["username"]


def test_approved_response_cannot_be_edited_or_approved_twice(client):
    customer, _ = make_account(client, "customer")
    staff, _ = make_account(client, "agent")
    tid = make_ticket(client, customer)["id"]

    client.post(f"/tickets/{tid}/approve", headers=staff)

    assert client.patch(
        f"/tickets/{tid}/draft", json={"draft_response": "changed"}, headers=staff
    ).status_code == 409
    assert client.post(f"/tickets/{tid}/approve", headers=staff).status_code == 409
    assert client.post(f"/tickets/{tid}/reject", headers=staff).status_code == 409


def test_edited_draft_is_what_gets_sent(client):
    customer, _ = make_account(client, "customer")
    staff, _ = make_account(client, "agent")
    tid = make_ticket(client, customer)["id"]

    edited = "Hi, we are looking into the duplicate charge."
    assert client.patch(
        f"/tickets/{tid}/draft", json={"draft_response": edited}, headers=staff
    ).status_code == 200
    client.post(f"/tickets/{tid}/approve", headers=staff)

    thread = client.get(f"/tickets/{tid}/comments", headers=customer).json()
    assert thread[0]["body"] == edited


def test_rejected_draft_is_hidden_from_customer_and_can_return_to_review(client):
    customer, _ = make_account(client, "customer")
    staff, _ = make_account(client, "agent")
    tid = make_ticket(client, customer)["id"]

    assert client.post(f"/tickets/{tid}/reject", headers=staff).status_code == 200

    # Staff see the real status, the customer just sees "in progress".
    assert status_of(client, staff, tid) == "rejected"
    assert status_of(client, customer, tid) == "in_progress"

    # Editing the rejected draft puts it back in the review queue.
    response = client.patch(
        f"/tickets/{tid}/draft",
        json={"draft_response": "A better reply"},
        headers=staff,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "received"


# --------------------------------------------------
# Ticket lifecycle
# --------------------------------------------------

def test_full_ticket_lifecycle(client):
    customer, _ = make_account(client, "customer")
    staff, staff_user = make_account(client, "agent")
    tid = make_ticket(client, customer)["id"]

    # Staff start work -> in progress, ticket claimed.
    assert post_status(client, staff, tid, "start").status_code == 200
    ticket = client.get(f"/tickets/{tid}", headers=staff).json()
    assert ticket["status"] == "in_progress"
    assert ticket["assignee_name"] == staff_user["username"]

    # Approve -> response sent, then resolve.
    assert client.post(f"/tickets/{tid}/approve", headers=staff).status_code == 200
    assert post_status(client, staff, tid, "resolve").status_code == 200
    assert status_of(client, customer, tid) == "resolved"

    # Customer confirms and closes.
    assert post_status(client, customer, tid, "close").status_code == 200
    assert status_of(client, customer, tid) == "closed"

    # A closed ticket accepts no new customer messages...
    assert client.post(
        f"/tickets/{tid}/comments", json={"body": "one more thing"}, headers=customer
    ).status_code == 409

    # ...until the customer reopens it, which sends it back for review.
    assert post_status(client, customer, tid, "reopen").status_code == 200
    assert status_of(client, staff, tid) == "received"
    assert client.post(
        f"/tickets/{tid}/comments", json={"body": "still broken"}, headers=customer
    ).status_code == 201


def test_invalid_status_transitions_are_rejected(client):
    customer, _ = make_account(client, "customer")
    staff, _ = make_account(client, "agent")
    tid = make_ticket(client, customer)["id"]

    # Nothing was sent yet, so it can't be resolved, closed or reopened.
    assert post_status(client, staff, tid, "resolve").status_code == 409
    assert post_status(client, staff, tid, "close").status_code == 409
    assert post_status(client, staff, tid, "reopen").status_code == 409
    assert post_status(client, customer, tid, "close").status_code == 409
    assert post_status(client, customer, tid, "reopen").status_code == 409

    # Can't start work twice.
    assert post_status(client, staff, tid, "start").status_code == 200
    assert post_status(client, staff, tid, "start").status_code == 409


# --------------------------------------------------
# Conversation thread
# --------------------------------------------------

def test_internal_notes_are_staff_only(client):
    customer, _ = make_account(client, "customer")
    staff, _ = make_account(client, "agent")
    tid = make_ticket(client, customer)["id"]

    note = client.post(
        f"/tickets/{tid}/comments",
        json={"body": "Customer looks like a repeat offender", "internal": True},
        headers=staff,
    )
    assert note.status_code == 201

    # Customers can't write internal notes...
    assert client.post(
        f"/tickets/{tid}/comments",
        json={"body": "sneaky", "internal": True},
        headers=customer,
    ).status_code == 403

    # ...and can't see them.
    assert client.get(f"/tickets/{tid}/comments", headers=customer).json() == []

    staff_view = client.get(f"/tickets/{tid}/comments", headers=staff).json()
    assert [c["kind"] for c in staff_view] == ["note"]


def test_customer_and_staff_can_exchange_messages(client):
    customer, me = make_account(client, "customer")
    staff, staff_user = make_account(client, "agent")
    tid = make_ticket(client, customer)["id"]

    assert client.post(
        f"/tickets/{tid}/comments",
        json={"body": "Any update?"},
        headers=customer,
    ).status_code == 201
    assert client.post(
        f"/tickets/{tid}/comments",
        json={"body": "We are on it."},
        headers=staff,
    ).status_code == 201

    customer_view = client.get(f"/tickets/{tid}/comments", headers=customer).json()
    assert [c["body"] for c in customer_view] == ["Any update?", "We are on it."]
    assert customer_view[0]["author_name"] == me["username"]
    assert customer_view[1]["author_name"] == "Support team"

    staff_view = client.get(f"/tickets/{tid}/comments", headers=staff).json()
    assert staff_view[1]["author_name"] == staff_user["username"]


def test_empty_comment_is_rejected(client):
    customer, _ = make_account(client, "customer")
    tid = make_ticket(client, customer)["id"]

    assert client.post(
        f"/tickets/{tid}/comments", json={"body": ""}, headers=customer
    ).status_code == 422


# --------------------------------------------------
# Assignment
# --------------------------------------------------

def test_agent_can_claim_and_release_a_ticket(client):
    customer, _ = make_account(client, "customer")
    agent, agent_user = make_account(client, "agent")
    tid = make_ticket(client, customer)["id"]

    claim = client.post(
        f"/tickets/{tid}/assign",
        json={"assignee_id": agent_user["id"]},
        headers=agent,
    )
    assert claim.status_code == 200
    assert claim.json()["status"] == "in_progress"

    release = client.post(
        f"/tickets/{tid}/assign", json={"assignee_id": None}, headers=agent
    )
    assert release.status_code == 200
    assert client.get(f"/tickets/{tid}", headers=agent).json()["assigned_to"] is None


def test_agents_cannot_take_over_or_hand_off_tickets(client):
    customer, _ = make_account(client, "customer")
    agent1, agent1_user = make_account(client, "agent")
    agent2, agent2_user = make_account(client, "agent")
    tid = make_ticket(client, customer)["id"]

    client.post(
        f"/tickets/{tid}/assign",
        json={"assignee_id": agent1_user["id"]},
        headers=agent1,
    )

    # Agent 2 can't grab a ticket that is already taken.
    assert client.post(
        f"/tickets/{tid}/assign",
        json={"assignee_id": agent2_user["id"]},
        headers=agent2,
    ).status_code == 409

    # Agents can't assign work to somebody else, or release another's ticket.
    assert client.post(
        f"/tickets/{tid}/assign",
        json={"assignee_id": agent2_user["id"]},
        headers=agent1,
    ).status_code == 403
    assert client.post(
        f"/tickets/{tid}/assign", json={"assignee_id": None}, headers=agent2
    ).status_code == 403


def test_admin_can_assign_anyone_but_only_staff(client):
    customer, customer_user = make_account(client, "customer")
    agent, agent_user = make_account(client, "agent")
    admin, _ = make_account(client, "admin")
    tid = make_ticket(client, customer)["id"]

    response = client.post(
        f"/tickets/{tid}/assign",
        json={"assignee_id": agent_user["id"]},
        headers=admin,
    )
    assert response.status_code == 200
    ticket = client.get(f"/tickets/{tid}", headers=admin).json()
    assert ticket["assignee_name"] == agent_user["username"]

    # A customer can't be an assignee.
    assert client.post(
        f"/tickets/{tid}/assign",
        json={"assignee_id": customer_user["id"]},
        headers=admin,
    ).status_code == 400


def test_closed_ticket_cannot_be_reassigned(client):
    customer, _ = make_account(client, "customer")
    agent, agent_user = make_account(client, "agent")
    tid = make_ticket(client, customer)["id"]

    client.post(f"/tickets/{tid}/approve", headers=agent)
    assert post_status(client, agent, tid, "close").status_code == 200

    assert client.post(
        f"/tickets/{tid}/assign",
        json={"assignee_id": agent_user["id"]},
        headers=agent,
    ).status_code == 409


# --------------------------------------------------
# Admin: team management
# --------------------------------------------------

def test_only_admins_can_manage_users(client):
    customer, _ = make_account(client, "customer")
    agent, _ = make_account(client, "agent")
    admin, _ = make_account(client, "admin")

    assert client.get("/admin/users", headers=customer).status_code == 403
    assert client.get("/admin/users", headers=agent).status_code == 403
    assert client.get("/admin/users", headers=admin).status_code == 200

    new_user = {
        "username": "new_agent_1",
        "email": "new_agent_1@example.com",
        "password": PASSWORD,
        "role": "agent",
    }
    assert client.post("/admin/users", json=new_user, headers=customer).status_code == 403
    assert client.post("/admin/users", json=new_user, headers=agent).status_code == 403


def test_admin_can_create_staff_who_can_then_log_in(client):
    admin, _ = make_account(client, "admin")

    response = client.post(
        "/admin/users",
        json={
            "username": "new_agent_2",
            "email": "new_agent_2@example.com",
            "password": PASSWORD,
            "role": "agent",
        },
        headers=admin,
    )
    assert response.status_code == 201

    login = client.post(
        "/auth/login", json={"username": "new_agent_2", "password": PASSWORD}
    )
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert client.get("/auth/me", headers=headers).json()["role"] == "agent"
    assert client.get("/staff", headers=headers).status_code == 200


def test_admin_can_change_roles_but_not_their_own(client):
    admin, admin_user = make_account(client, "admin")
    customer, customer_user = make_account(client, "customer")

    assert client.patch(
        f"/admin/users/{admin_user['id']}/role",
        json={"role": "customer"},
        headers=admin,
    ).status_code == 400

    assert client.patch(
        f"/admin/users/{customer_user['id']}/role",
        json={"role": "agent"},
        headers=admin,
    ).status_code == 200

    # The role is read from the database on every request, so the
    # customer's existing token now has agent access.
    assert client.get("/auth/me", headers=customer).json()["role"] == "agent"

    assert client.patch(
        "/admin/users/99999/role", json={"role": "agent"}, headers=admin
    ).status_code == 404
