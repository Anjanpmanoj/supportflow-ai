import inspect
import os
import re
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
import streamlit as st

# --------------------------------------------------
# Configuration
# --------------------------------------------------

API_URL = os.getenv("SUPPORTFLOW_API_URL", "http://127.0.0.1:8000")

# Timestamps are stored in UTC by the backend; show them in IST.
LOCAL_TZ = timezone(timedelta(hours=5, minutes=30))

STAFF_ROLES = {"agent", "admin"}

st.set_page_config(
    page_title="SupportFlow AI",
    page_icon="🎫",
    layout="wide",
)


def _stretch(func):
    """Full-width kwargs that work on old and new Streamlit versions."""
    try:
        params = inspect.signature(func).parameters
        if "width" in params and isinstance(params["width"].default, str):
            return {"width": "stretch"}
    except (TypeError, ValueError):
        pass
    return {"use_container_width": True}


BTN = _stretch(st.button)
DF = _stretch(st.dataframe)

# --------------------------------------------------
# Styling
# --------------------------------------------------

st.markdown(
    """
    <style>
    .block-container { padding-top: 2rem; max-width: 1250px; }

    .sf-brand { font-size: 1.35rem; font-weight: 700; letter-spacing: -0.01em; }
    .sf-muted { opacity: 0.65; font-size: 0.9rem; }

    .sf-badge {
        display: inline-block; padding: 2px 10px; border-radius: 999px;
        font-size: 0.78rem; font-weight: 600; white-space: nowrap;
        vertical-align: middle;
    }
    .sf-ref {
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        font-weight: 700; font-size: 1.05rem;
    }
    .sf-title { font-size: 1.6rem; font-weight: 700; margin-bottom: 0.2rem; }

    .sf-hero { text-align: center; padding: 1.5rem 0 1rem 0; }
    .sf-hero h1 { font-size: 2.4rem; margin-bottom: 0.2rem; }

    .sf-tracker { display: flex; align-items: flex-start; margin: 0.6rem 0 1.2rem 0; }
    .sf-step { flex: 1; text-align: center; position: relative; }
    .sf-step .dot {
        width: 26px; height: 26px; border-radius: 50%; margin: 0 auto 6px auto;
        display: flex; align-items: center; justify-content: center;
        font-size: 0.8rem; font-weight: 700;
        background: rgba(128,128,128,0.25); color: inherit;
    }
    .sf-step .label { font-size: 0.8rem; opacity: 0.6; }
    .sf-step::before {
        content: ""; position: absolute; top: 12px; left: -50%; width: 100%;
        height: 3px; background: rgba(128,128,128,0.25); z-index: 0;
    }
    .sf-step:first-child::before { display: none; }
    .sf-step .dot { position: relative; z-index: 1; }
    .sf-step.done .dot, .sf-step.current .dot { background: #2563eb; color: #fff; }
    .sf-step.done::before, .sf-step.current::before { background: #2563eb; }
    .sf-step.current .label { opacity: 1; font-weight: 700; }
    .sf-step.done .label { opacity: 0.9; }

    div[data-testid="stMetric"] {
        background: rgba(128,128,128,0.08); border-radius: 12px;
        padding: 14px 16px; border: 1px solid rgba(128,128,128,0.15);
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# --------------------------------------------------
# Display helpers
# --------------------------------------------------

STATUS_META = {
    "received": ("Needs review", "#2563eb"),
    "ai_replied": ("AI replied", "#7c3aed"),
    "in_progress": ("In progress", "#d97706"),
    "approved": ("Human replied", "#059669"),
    "rejected": ("Draft rejected", "#dc2626"),
    "resolved": ("Resolved", "#0d9488"),
    "closed": ("Closed", "#6b7280"),
}

# Wording for customers (they never see "draft rejected" or "needs review").
CUSTOMER_STATUS_LABEL = {
    "received": "Received",
    "ai_replied": "AI replied",
    "in_progress": "With support team",
    "approved": "Support replied",
    "resolved": "Resolved",
    "closed": "Closed",
}

PRIORITY_META = {
    "urgent": "#dc2626",
    "high": "#ea580c",
    "medium": "#2563eb",
    "low": "#6b7280",
}
PRIORITY_RANK = {"urgent": 0, "high": 1, "medium": 2, "low": 3}

DONE_STATUSES = {"resolved", "closed"}

CUSTOMER_STEPS = [
    ("received", "Received"),
    ("ai_replied", "AI replied"),
    ("in_progress", "With support"),
    ("approved", "Support replied"),
    ("resolved", "Resolved"),
    ("closed", "Closed"),
]


def badge(text, color):
    return (
        f'<span class="sf-badge" style="background:{color}22;color:{color};'
        f'border:1px solid {color}55">{text}</span>'
    )


def status_badge(status, customer=False):
    label, color = STATUS_META.get(status, (status, "#6b7280"))
    if customer:
        label = CUSTOMER_STATUS_LABEL.get(status, label)
    return badge(label, color)


def priority_badge(priority):
    priority = (priority or "medium").lower()
    return badge(priority.capitalize(), PRIORITY_META.get(priority, "#6b7280"))


def pretty(value):
    return str(value or "other").replace("_", " ").capitalize()


def fmt_time(value):
    """Turn the backend's UTC timestamp into a readable local time."""
    if not value:
        return "—"
    try:
        parsed = datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
        local = parsed.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ)
        return local.strftime("%d %b %Y, %I:%M %p").lstrip("0")
    except ValueError:
        return str(value)


_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~$])")


def safe_md(text):
    """Show user-written text without letting it be interpreted as markdown."""
    escaped = _MD_SPECIAL.sub(r"\\\1", str(text or ""))
    return escaped.replace("\n", "  \n")


def snippet(text, limit=110):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def tracker_html(status):
    keys = [key for key, _ in CUSTOMER_STEPS]
    current = keys.index(status) if status in keys else 0
    parts = []
    for index, (_, label) in enumerate(CUSTOMER_STEPS):
        css = "done" if index < current else "current" if index == current else ""
        mark = "✓" if index < current else str(index + 1)
        parts.append(
            f'<div class="sf-step {css}"><div class="dot">{mark}</div>'
            f'<div class="label">{label}</div></div>'
        )
    return f'<div class="sf-tracker">{"".join(parts)}</div>'


# --------------------------------------------------
# Session, navigation and API
# --------------------------------------------------

SESSION_KEYS = (
    "token", "user", "open_ticket_id", "nav_target",
    "nav_customer", "nav_staff", "staff_selected_ticket", "select_ticket",
)


def flash(level, text):
    st.session_state["flash"] = (level, text)


def show_flash():
    item = st.session_state.pop("flash", None)
    if item:
        level, text = item
        {"success": st.success, "error": st.error, "warning": st.warning}.get(
            level, st.info
        )(text)


def logout(message=None):
    for key in SESSION_KEYS:
        st.session_state.pop(key, None)
    if message:
        flash("warning", message)
    st.rerun()


def error_text(body, status_code):
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):  # FastAPI validation errors
        messages = []
        for item in detail:
            field = str(item.get("loc", [""])[-1])
            messages.append(f"{field}: {item.get('msg', 'invalid value')}")
        return "; ".join(messages)
    return f"Request failed (HTTP {status_code})."


def api(method, path, payload=None, auth=True, timeout=20):
    """Call the backend, attaching the JWT. Returns (ok, data_or_message)."""
    headers = {}
    token = st.session_state.get("token")
    if auth and token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        response = requests.request(
            method,
            f"{API_URL}{path}",
            json=payload,
            headers=headers,
            timeout=timeout,
        )
    except requests.RequestException:
        return False, "Cannot reach the server. Make sure the backend is running."

    if response.status_code == 401 and auth and token:
        logout("Your session expired. Please sign in again.")

    try:
        body = response.json()
    except ValueError:
        body = None

    if response.ok:
        return True, body
    return False, error_text(body, response.status_code)


def go(page=None, ticket_id=None, select=None):
    """Navigate (applied on the next run, before widgets are created)."""
    if page:
        st.session_state["nav_target"] = page
    if ticket_id is not None:
        st.session_state["open_ticket_id"] = ticket_id
    if select is not None:
        st.session_state["select_ticket"] = select
    st.rerun()


def render_nav(options, key):
    target = st.session_state.pop("nav_target", None)
    if target in options:
        st.session_state[key] = target
    if st.session_state.get(key) not in options:
        st.session_state.pop(key, None)
    return st.sidebar.radio(
        "Navigate", options, key=key, label_visibility="collapsed"
    )


def act(method, path, payload=None, success=None, level="success"):
    """Run a write action, then refresh the page with a message."""
    ok, data = api(method, path, payload)
    if ok:
        message = success or (data or {}).get("message", "Done")
        flash(level, message)
        st.rerun()
    else:
        st.error(data)
    return ok


def get_list(path, what):
    ok, data = api("GET", path)
    if not ok:
        st.error(f"Could not load {what}: {data}")
        return []
    return data if isinstance(data, list) else []


def get_tickets():
    return get_list("/tickets", "tickets")


def backend_online():
    try:
        requests.get(f"{API_URL}/health", timeout=3).raise_for_status()
        return True
    except requests.RequestException:
        return False


# --------------------------------------------------
# Login / registration
# --------------------------------------------------

def sign_in(username, password):
    ok, data = api(
        "POST",
        "/auth/login",
        {"username": username.strip(), "password": password},
        auth=False,
    )
    if not ok:
        return False, data

    st.session_state["token"] = data["access_token"]

    ok, me = api("GET", "/auth/me")
    if not ok:
        st.session_state.pop("token", None)
        return False, me

    st.session_state["user"] = me
    return True, None


def auth_screen():
    st.markdown(
        '<div class="sf-hero"><h1>🎫 SupportFlow AI</h1>'
        '<div class="sf-muted">Fast, policy-aware customer support</div></div>',
        unsafe_allow_html=True,
    )

    show_flash()

    if not backend_online():
        st.error(
            "Backend is not connected. Start it in another terminal with: "
            "python -m uvicorn main:app --reload"
        )

    _, centre, _ = st.columns([1, 1.5, 1])

    with centre:
        sign_in_tab, register_tab = st.tabs(["Sign in", "Create account"])

        with sign_in_tab:
            with st.form("login_form"):
                username = st.text_input("Username")
                password = st.text_input("Password", type="password")
                submitted = st.form_submit_button(
                    "Sign in", type="primary", **BTN
                )

            if submitted:
                if not username.strip() or not password:
                    st.warning("Enter your username and password.")
                else:
                    ok, error = sign_in(username, password)
                    if ok:
                        st.rerun()
                    else:
                        st.error(error)

        with register_tab:
            st.caption(
                "New accounts are customer accounts. Support staff "
                "accounts are created by an administrator."
            )
            with st.form("register_form"):
                new_username = st.text_input("Username", key="reg_username")
                new_email = st.text_input("Email", key="reg_email")
                new_password = st.text_input(
                    "Password (min. 8 characters)",
                    type="password",
                    key="reg_password",
                )
                confirm = st.text_input(
                    "Confirm password", type="password", key="reg_confirm"
                )
                created = st.form_submit_button(
                    "Create account", type="primary", **BTN
                )

            if created:
                if not (new_username.strip() and new_email.strip() and new_password):
                    st.warning("Please fill in every field.")
                elif new_password != confirm:
                    st.error("The passwords do not match.")
                else:
                    ok, data = api(
                        "POST",
                        "/auth/register",
                        {
                            "username": new_username.strip(),
                            "email": new_email.strip(),
                            "password": new_password,
                        },
                        auth=False,
                    )
                    if not ok:
                        st.error(data)
                    else:
                        ok, error = sign_in(new_username, new_password)
                        if ok:
                            flash("success", "Welcome! Your account is ready.")
                            st.rerun()
                        else:
                            st.success("Account created. You can sign in now.")


# --------------------------------------------------
# Shared pieces
# --------------------------------------------------

def render_thread(comments, viewer_is_staff):
    """Conversation between customer and support."""
    if not comments:
        st.caption("No messages yet.")
        return

    for item in comments:
        role = item.get("author_role")
        kind = item.get("kind")
        name = item.get("author_name", "")
        stamp = fmt_time(item.get("created_at"))

        if kind == "note":
            with st.chat_message("assistant", avatar="🔒"):
                st.caption(f"Internal note · {name} · {stamp}")
                st.markdown(safe_md(item["body"]))
            continue

        is_customer = role == "customer"
        is_ai = role == "ai"
        avatar = "🙂" if is_customer else "🤖" if is_ai else "🎧"

        if is_ai:
            who = "AI Assistant (automatic reply)"
        elif viewer_is_staff:
            who = f"{name} (customer)" if is_customer else f"{name} ({role})"
            if kind == "response":
                who += " · reply sent"
        else:
            who = "You" if is_customer else "Support team"

        with st.chat_message("user" if is_customer else "assistant", avatar=avatar):
            st.caption(f"{who} · {stamp}")
            st.markdown(safe_md(item["body"]))


def post_comment_form(ticket_id, allow_internal=False, key_prefix=""):
    with st.form(f"{key_prefix}comment_form_{ticket_id}", clear_on_submit=True):
        body = st.text_area(
            "Add a message" if not allow_internal else "Add a message or note",
            height=90,
            key=f"{key_prefix}comment_body_{ticket_id}",
        )
        internal = False
        if allow_internal:
            internal = st.checkbox(
                "Internal note (hidden from the customer)",
                key=f"{key_prefix}comment_internal_{ticket_id}",
            )
        sent = st.form_submit_button("Send", type="primary")

    if sent:
        if not body.strip():
            st.warning("Write something first.")
        else:
            act(
                "POST",
                f"/tickets/{ticket_id}/comments",
                {"body": body.strip(), "internal": internal},
            )


def new_ticket_form(is_staff_user, key="new_ticket"):
    """Create-ticket form. Returns nothing; navigates on success."""
    with st.form(f"{key}_form"):
        customer_name = ""
        if is_staff_user:
            customer_name = st.text_input("Customer name")

        message = st.text_area(
            "How can we help?",
            placeholder="Example: I was charged twice for my subscription.",
            height=160,
        )
        submitted = st.form_submit_button("Submit ticket", type="primary")

    if not submitted:
        return

    if is_staff_user and not customer_name.strip():
        st.warning("Please enter the customer's name.")
        return
    if len(message.strip()) < 5:
        st.warning("Please describe the issue (at least a few words).")
        return

    payload = {"message": message.strip()}
    if is_staff_user:
        payload["customer_name"] = customer_name.strip()

    with st.spinner("Creating your ticket…"):
        ok, data = api("POST", "/tickets", payload, timeout=90)

    if not ok:
        st.error(data)
        return

    ref = data.get("ticket_ref", f"#{data.get('id')}")
    if is_staff_user:
        flash("success", f"Ticket {ref} created.")
        go("Queue", ticket_id=data["id"], select=data["id"])
    else:
        flash(
            "success",
            f"Ticket {ref} submitted. Keep this ID to track your request.",
        )
        go("My tickets", ticket_id=data["id"])


# --------------------------------------------------
# Customer experience
# --------------------------------------------------

def customer_page(user):
    page = render_nav(["My tickets", "New ticket"], "nav_customer")
    show_flash()

    if page == "New ticket":
        st.markdown('<div class="sf-title">Submit a request</div>', unsafe_allow_html=True)
        st.caption(
            "Describe your issue and our team will get back to you. "
            "You'll receive a ticket ID so you can follow progress here."
        )
        new_ticket_form(is_staff_user=False, key="customer_new")
        return

    tickets = get_tickets()

    open_id = st.session_state.get("open_ticket_id")
    if open_id:
        ticket = next((t for t in tickets if t["id"] == open_id), None)
        if ticket:
            customer_ticket_detail(ticket)
            return
        st.session_state.pop("open_ticket_id", None)

    st.markdown(
        f'<div class="sf-title">Hello, {user["username"]} 👋</div>',
        unsafe_allow_html=True,
    )
    st.caption("Track your support requests below.")

    active = sum(1 for t in tickets if t["status"] not in DONE_STATUSES)
    m1, m2, m3 = st.columns(3)
    m1.metric("Your tickets", len(tickets))
    m2.metric("In progress", active)
    m3.metric("Resolved", len(tickets) - active)

    with st.form("track_form", clear_on_submit=True):
        c1, c2 = st.columns([4, 1])
        lookup = c1.text_input(
            "Find a ticket by ID",
            placeholder="SF-000123",
            label_visibility="collapsed",
        )
        search = c2.form_submit_button("Find ticket", **BTN)

    if search:
        digits = re.sub(r"\D", "", lookup)
        match = next(
            (t for t in tickets if digits and t["id"] == int(digits)), None
        )
        if match:
            go(ticket_id=match["id"])
        else:
            st.error("No ticket with that ID on your account.")

    if not tickets:
        st.info("You haven't submitted any tickets yet. Use **New ticket** to get started.")
        return

    for ticket in tickets:
        with st.container(border=True):
            c1, c2, c3, c4 = st.columns([1.5, 4, 1.8, 1])
            c1.markdown(
                f'<span class="sf-ref">{ticket["ticket_ref"]}</span><br>'
                f'{status_badge(ticket["status"], customer=True)}',
                unsafe_allow_html=True,
            )
            c2.markdown(safe_md(snippet(ticket["message"])))
            c3.caption(f"Updated\n\n{fmt_time(ticket.get('updated_at'))}")
            if c4.button("View", key=f"view_{ticket['id']}", **BTN):
                go(ticket_id=ticket["id"])


def customer_ticket_detail(ticket):
    tid = ticket["id"]
    status = ticket["status"]

    if st.button("← Back to my tickets"):
        st.session_state.pop("open_ticket_id", None)
        st.rerun()

    st.markdown(
        f'<span class="sf-ref" style="font-size:1.5rem">{ticket["ticket_ref"]}</span> '
        f'&nbsp;{status_badge(status, customer=True)}',
        unsafe_allow_html=True,
    )
    st.caption(
        f"{pretty(ticket.get('category'))} · Opened {fmt_time(ticket.get('created_at'))}"
    )

    st.markdown(tracker_html(status), unsafe_allow_html=True)

    comments = get_list(f"/tickets/{tid}/comments", "messages")
    has_reply = any(c["author_role"] != "customer" for c in comments)

    st.markdown("##### Your request")
    with st.container(border=True):
        st.markdown(safe_md(ticket["message"]))

    st.markdown("##### Conversation")
    if not has_reply and status in ("received", "in_progress"):
        st.info("Our team is reviewing your ticket. Replies will appear here.")
    if status == "ai_replied":
        st.info(
            "Our AI assistant has answered your question. A member of our "
            "support team will also review this ticket and follow up if needed."
        )
    render_thread(comments, viewer_is_staff=False)

    if status == "closed":
        st.caption("This ticket is closed.")
        if st.button("Reopen this ticket", key=f"reopen_{tid}"):
            act("POST", f"/tickets/{tid}/status", {"action": "reopen"})
    else:
        post_comment_form(tid)

        if status == "ai_replied":
            st.markdown("##### Did the AI answer help?")
            a, b, _ = st.columns([1.2, 1.4, 1.6])
            if a.button("Yes, close ticket", key=f"close_{tid}", type="primary", **BTN):
                act("POST", f"/tickets/{tid}/status", {"action": "close"})
            if b.button("Talk to a human", key=f"human_{tid}", **BTN):
                act(
                    "POST",
                    f"/tickets/{tid}/status",
                    {"action": "human"},
                    success="We've asked a human agent to take over your ticket.",
                )
        elif status in ("approved", "resolved"):
            st.markdown("##### Did this solve your problem?")
            a, b, _ = st.columns([1.2, 1.2, 2])
            if a.button("Yes, close ticket", key=f"close_{tid}", type="primary", **BTN):
                act("POST", f"/tickets/{tid}/status", {"action": "close"})
            if b.button("No, I still need help", key=f"reopen_{tid}", **BTN):
                act("POST", f"/tickets/{tid}/status", {"action": "reopen"})

    with st.expander("Ticket history"):
        events = get_list(f"/tickets/{tid}/audit", "history")
        for event in events:
            st.markdown(f"**{event['label']}** · {fmt_time(event['created_at'])}")


# --------------------------------------------------
# Staff experience
# --------------------------------------------------

STATUS_FILTERS = {
    "All": None,
    "Needs review": "received",
    "AI replied": "ai_replied",
    "In progress": "in_progress",
    "Human replied": "approved",
    "Draft rejected": "rejected",
    "Resolved": "resolved",
    "Closed": "closed",
}


def staff_page(user):
    options = ["Queue", "Create ticket"]
    if user["role"] == "admin":
        options.append("Team")

    page = render_nav(options, "nav_staff")
    show_flash()

    if page == "Create ticket":
        st.markdown('<div class="sf-title">Create a ticket</div>', unsafe_allow_html=True)
        st.caption("Log a request on behalf of a customer.")
        new_ticket_form(is_staff_user=True, key="staff_new")
    elif page == "Team":
        team_page(user)
    else:
        staff_queue(user)


def staff_queue(user):
    st.markdown('<div class="sf-title">Support queue</div>', unsafe_allow_html=True)

    tickets = get_tickets()
    me = user["username"]
    active = [t for t in tickets if t["status"] not in DONE_STATUSES]

    cols = st.columns(5)
    cols[0].metric("Needs review", sum(1 for t in tickets if t["status"] == "received"))
    cols[1].metric("AI replied", sum(1 for t in tickets if t["status"] == "ai_replied"))
    cols[2].metric("Escalated (open)", sum(1 for t in active if t.get("escalated")))
    cols[3].metric("Unassigned (open)", sum(1 for t in active if not t.get("assigned_to")))
    cols[4].metric("Assigned to me", sum(1 for t in active if t.get("assignee_name") == me))

    with st.expander("Filters", expanded=True):
        f1, f2, f3, f4, f5 = st.columns([2, 1.3, 1.2, 1.3, 1.4])
        search = f1.text_input("Search", placeholder="ID, customer or message")
        status_label = f2.selectbox("Status", list(STATUS_FILTERS))
        priority = f3.selectbox("Priority", ["All", "urgent", "high", "medium", "low"])
        categories = ["All"] + sorted({t.get("category", "other") for t in tickets})
        category = f4.selectbox(
            "Category",
            categories,
            format_func=lambda c: c if c == "All" else pretty(c),
        )
        scope = f5.selectbox("Assignment", ["All", "Mine", "Unassigned"])

    filtered = tickets

    if search.strip():
        needle = search.strip().lower()
        digits = re.sub(r"\D", "", needle)
        filtered = [
            t for t in filtered
            if needle in t["ticket_ref"].lower()
            or (digits and str(t["id"]) == digits.lstrip("0"))
            or needle in str(t.get("customer_name", "")).lower()
            or needle in str(t.get("message", "")).lower()
        ]
    wanted = STATUS_FILTERS[status_label]
    if wanted:
        filtered = [t for t in filtered if t["status"] == wanted]
    if priority != "All":
        filtered = [t for t in filtered if (t.get("priority") or "medium") == priority]
    if category != "All":
        filtered = [t for t in filtered if t.get("category") == category]
    if scope == "Mine":
        filtered = [t for t in filtered if t.get("assignee_name") == me]
    elif scope == "Unassigned":
        filtered = [t for t in filtered if not t.get("assigned_to")]

    # Open tickets first, most urgent first, newest first.
    filtered = sorted(
        filtered,
        key=lambda t: (
            t["status"] in DONE_STATUSES,
            PRIORITY_RANK.get(t.get("priority") or "medium", 2),
            -t["id"],
        ),
    )

    if not filtered:
        st.info("No tickets match the current filters.")
        return

    table = pd.DataFrame(
        [
            {
                "Ticket": t["ticket_ref"],
                "Customer": t.get("customer_name"),
                "Category": pretty(t.get("category")),
                "Priority": pretty(t.get("priority") or "medium"),
                "Status": STATUS_META.get(t["status"], (t["status"],))[0],
                "Assigned to": t.get("assignee_name") or "—",
                "Escalated": "⚠️ Yes" if t.get("escalated") else "",
                "Created": fmt_time(t.get("created_at")),
            }
            for t in filtered
        ]
    )
    st.dataframe(table, hide_index=True, **DF)
    st.caption(f"Showing {len(filtered)} of {len(tickets)} ticket(s)")

    # Ticket picker
    options = [None] + [t["id"] for t in filtered]
    by_id = {t["id"]: t for t in filtered}

    # NOTE: "pending is not None" matters. None is also in `options` (the
    # "Select a ticket" placeholder), so without this check every rerun
    # would reset the selection and the opened ticket would vanish.
    pending = st.session_state.pop("select_ticket", None)
    if pending is not None and pending in options:
        st.session_state["staff_selected_ticket"] = pending
    if st.session_state.get("staff_selected_ticket") not in options:
        st.session_state["staff_selected_ticket"] = None

    selected = st.selectbox(
        "Open a ticket",
        options,
        key="staff_selected_ticket",
        format_func=lambda i: (
            "— Select a ticket —"
            if i is None
            else f"{by_id[i]['ticket_ref']} · {by_id[i].get('customer_name')} · "
                 f"{snippet(by_id[i].get('message'), 60)}"
        ),
    )

    if selected is None:
        return

    st.divider()
    staff_workspace(by_id[selected], user)


def human_reply_form(tid):
    """A human agent's own reply to the customer (POST /tickets/{id}/reply)."""
    with st.form(f"reply_form_{tid}", clear_on_submit=True):
        body = st.text_area(
            "Your reply to the customer",
            height=130,
            key=f"reply_body_{tid}",
        )
        sent = st.form_submit_button("Send reply", type="primary")

    if sent:
        if not body.strip():
            st.warning("Write a reply first.")
        else:
            act("POST", f"/tickets/{tid}/reply", {"body": body.strip()})


def staff_workspace(ticket, user):
    tid = ticket["id"]
    status = ticket["status"]
    is_admin = user["role"] == "admin"

    comments = get_list(f"/tickets/{tid}/comments", "messages")

    left, right = st.columns([3, 2], gap="large")

    # ---------------- Left: the conversation ----------------
    with left:
        st.markdown(
            f'<span class="sf-ref" style="font-size:1.5rem">{ticket["ticket_ref"]}</span> '
            f'&nbsp;{status_badge(status)} &nbsp;{priority_badge(ticket.get("priority"))}',
            unsafe_allow_html=True,
        )
        st.caption(
            f"{ticket.get('customer_name')} · {pretty(ticket.get('category'))} · "
            f"Opened {fmt_time(ticket.get('created_at'))}"
        )

        if ticket.get("escalated"):
            st.warning(f"Escalation: {ticket.get('escalation_reason') or 'Needs review'}")

        st.markdown("##### Customer message")
        with st.container(border=True):
            st.markdown(safe_md(ticket["message"]))

        st.markdown("##### AI draft response")

        gen_status = ticket.get("generation_status")
        if gen_status and gen_status not in ("success",):
            st.info(
                f"No AI draft was generated ({gen_status.replace('_', ' ')}); "
                "a standard template was used."
            )
        elif ticket.get("generation_source"):
            st.caption(f"Drafted by: {ticket['generation_source']}")

        if status == "ai_replied":
            st.success(
                "The AI assistant already sent this reply to the customer. "
                "You can follow up with your own reply below."
            )
            with st.container(border=True):
                st.markdown(
                    safe_md(
                        ticket.get("approved_response")
                        or ticket.get("draft_response")
                        or ""
                    )
                )
            human_reply_form(tid)
        elif status in ("received", "in_progress", "rejected"):
            if ticket.get("escalated"):
                st.caption(
                    "Held for human review, so the AI draft was not sent automatically."
                )
            if status == "rejected":
                st.warning(
                    "This draft was rejected. Edit it and save to return the "
                    "ticket to the review queue."
                )

            edited = st.text_area(
                "Review and edit the response",
                value=ticket.get("draft_response") or "",
                height=190,
                key=f"draft_editor_{tid}_{status}",
            )

            can_review = status in ("received", "in_progress")
            b1, b2, b3 = st.columns(3)

            if b1.button("Save draft", key=f"save_{tid}", **BTN):
                if not edited.strip():
                    st.warning("The draft cannot be empty.")
                else:
                    act("PATCH", f"/tickets/{tid}/draft", {"draft_response": edited})

            if can_review:
                if b2.button(
                    "Approve & send", key=f"approve_{tid}", type="primary", **BTN
                ):
                    if not edited.strip():
                        st.warning("The draft cannot be empty.")
                    else:
                        saved = True
                        # Approve exactly what is on screen.
                        if edited.strip() != (ticket.get("draft_response") or "").strip():
                            ok, data = api(
                                "PATCH",
                                f"/tickets/{tid}/draft",
                                {"draft_response": edited},
                            )
                            if not ok:
                                st.error(data)
                                saved = False
                        if saved:
                            act("POST", f"/tickets/{tid}/approve")

                if b3.button("Reject draft", key=f"reject_{tid}", **BTN):
                    act("POST", f"/tickets/{tid}/reject", level="warning")

            already_replied = bool(ticket.get("approved_response"))
            if already_replied:
                st.info(
                    "A reply was already sent on this ticket earlier. Write a "
                    "new reply instead of approving the old draft again."
                )
            with st.expander("Write your own reply instead", expanded=already_replied):
                human_reply_form(tid)
        else:
            st.success("A response has been sent to the customer.")
            with st.container(border=True):
                st.markdown(
                    safe_md(
                        ticket.get("approved_response")
                        or ticket.get("draft_response")
                        or ""
                    )
                )

        st.markdown("##### Conversation")
        render_thread(comments, viewer_is_staff=True)
        post_comment_form(tid, allow_internal=True, key_prefix="staff_")

    # ---------------- Right: workflow & details ----------------
    with right:
        st.markdown("##### Workflow")
        with st.container(border=True):
            st.markdown(
                f"**Status** &nbsp;{status_badge(status)}  \n"
                f"**Assigned to** &nbsp;{ticket.get('assignee_name') or 'Unassigned'}  \n"
                f"**Requested by** &nbsp;{ticket.get('owner_username') or '(logged by staff)'}  \n"
                f"**Last updated** &nbsp;{fmt_time(ticket.get('updated_at'))}",
                unsafe_allow_html=True,
            )

            if status != "closed":
                if is_admin:
                    staff_list = get_list("/staff", "team members")
                    names = {s["id"]: s["username"] for s in staff_list}
                    choices = [None] + list(names)
                    current = ticket.get("assigned_to")
                    assignee = st.selectbox(
                        "Assign to",
                        choices,
                        index=choices.index(current) if current in choices else 0,
                        format_func=lambda i: "Unassigned" if i is None else names[i],
                        key=f"assignee_{tid}",
                    )
                    if st.button("Update assignment", key=f"assign_{tid}", **BTN):
                        act("POST", f"/tickets/{tid}/assign", {"assignee_id": assignee})
                else:
                    if ticket.get("assigned_to") == user["id"]:
                        if st.button("Release ticket", key=f"release_{tid}", **BTN):
                            act("POST", f"/tickets/{tid}/assign", {"assignee_id": None})
                    elif not ticket.get("assigned_to"):
                        if st.button(
                            "Assign to me", key=f"claim_{tid}", type="primary", **BTN
                        ):
                            act("POST", f"/tickets/{tid}/assign", {"assignee_id": user["id"]})
                    else:
                        st.caption("Assigned to another agent.")

            st.markdown("**Move ticket forward**")
            if status in ("received", "rejected", "ai_replied"):
                if st.button("Start working", key=f"start_{tid}", **BTN):
                    act("POST", f"/tickets/{tid}/status", {"action": "start"})
            if status in ("approved", "ai_replied"):
                if st.button("Mark resolved", key=f"resolve_{tid}", type="primary", **BTN):
                    act("POST", f"/tickets/{tid}/status", {"action": "resolve"})
            if status in ("approved", "resolved", "ai_replied"):
                if st.button("Close ticket", key=f"close_{tid}", **BTN):
                    act("POST", f"/tickets/{tid}/status", {"action": "close"})
            if status in ("approved", "resolved", "closed"):
                if st.button("Reopen", key=f"reopen_{tid}", **BTN):
                    act("POST", f"/tickets/{tid}/status", {"action": "reopen"})
            if status in ("in_progress",):
                st.caption("Review the AI draft, then approve or reject it.")

        with st.expander("Support policy used"):
            st.markdown(f"**{ticket.get('policy_title') or 'Policy'}**")
            st.write(ticket.get("policy") or "No policy available.")

        with st.expander("Audit history"):
            events = get_list(f"/tickets/{tid}/audit", "audit history")
            if events:
                frame = pd.DataFrame(events).reindex(
                    columns=["created_at", "actor", "action", "details"]
                )
                frame["created_at"] = frame["created_at"].map(fmt_time)
                frame.columns = ["When", "Who", "Action", "Details"]
                st.dataframe(frame, hide_index=True, **DF)
            else:
                st.info("No audit events recorded yet.")


# --------------------------------------------------
# Admin: team
# --------------------------------------------------

def team_page(user):
    st.markdown('<div class="sf-title">Team & access</div>', unsafe_allow_html=True)
    st.caption("Only administrators can create staff accounts or change roles.")

    users = get_list("/admin/users", "users")
    if users:
        frame = pd.DataFrame(users)[["id", "username", "email", "role", "created_at"]]
        frame["created_at"] = frame["created_at"].map(fmt_time)
        frame.columns = ["ID", "Username", "Email", "Role", "Joined"]
        st.dataframe(frame, hide_index=True, **DF)

    c1, c2 = st.columns(2, gap="large")

    with c1:
        st.markdown("##### Create an account")
        with st.form("admin_create_user", clear_on_submit=True):
            username = st.text_input("Username")
            email = st.text_input("Email")
            password = st.text_input("Temporary password (min. 8 characters)", type="password")
            role = st.selectbox("Role", ["agent", "admin", "customer"])
            created = st.form_submit_button("Create account", type="primary")

        if created:
            if not (username.strip() and email.strip() and password):
                st.warning("Please fill in every field.")
            else:
                act(
                    "POST",
                    "/admin/users",
                    {
                        "username": username.strip(),
                        "email": email.strip(),
                        "password": password,
                        "role": role,
                    },
                )

    with c2:
        st.markdown("##### Change a role")
        others = [u for u in users if u["id"] != user["id"]]
        if not others:
            st.info("There are no other users yet.")
        else:
            with st.form("admin_change_role"):
                target = st.selectbox(
                    "User",
                    others,
                    format_func=lambda u: f"{u['username']} ({u['role']})",
                )
                new_role = st.selectbox("New role", ["customer", "agent", "admin"])
                changed = st.form_submit_button("Update role")

            if changed:
                act("PATCH", f"/admin/users/{target['id']}/role", {"role": new_role})


# --------------------------------------------------
# App shell
# --------------------------------------------------

def sidebar(user):
    with st.sidebar:
        st.markdown('<div class="sf-brand">🎫 SupportFlow AI</div>', unsafe_allow_html=True)
        role = user["role"]
        st.markdown(
            f'**{user["username"]}**<br>'
            f'{badge(role.capitalize(), "#2563eb" if role == "customer" else "#7c3aed")}',
            unsafe_allow_html=True,
        )
        st.divider()


def sidebar_footer():
    with st.sidebar:
        st.divider()
        if st.button("Refresh", **BTN):
            st.rerun()
        if st.button("Sign out", **BTN):
            logout()
        if backend_online():
            st.caption("🟢 Backend connected")
        else:
            st.caption("🔴 Backend offline")


def main():
    user = st.session_state.get("user")

    if not st.session_state.get("token") or not user:
        auth_screen()
        return

    sidebar(user)

    if user["role"] in STAFF_ROLES:
        staff_page(user)
    else:
        customer_page(user)

    sidebar_footer()


main()
