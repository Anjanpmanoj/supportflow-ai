
import requests
import pandas as pd
import streamlit as st

# --------------------------------------------------
# Configuration
# --------------------------------------------------

API_URL = "http://127.0.0.1:8000"

st.set_page_config(
    page_title="SupportFlow AI",
    page_icon="🎫",
    layout="wide",
)

# --------------------------------------------------
# Helper functions
# --------------------------------------------------

def get_tickets():
    """Fetch saved tickets from the FastAPI backend."""
    try:
        response = requests.get(
            f"{API_URL}/tickets",
            timeout=5,
        )
        response.raise_for_status()
        data = response.json()

        # Support APIs that return either a list or a wrapper object.
        if isinstance(data, list):
            return data

        if isinstance(data, dict):
            return data.get("tickets", [])

        return []

    except requests.RequestException as error:
        st.error(f"Could not fetch tickets: {error}")
        return []


def get_priority(ticket):
    """Read priority, using medium when the API doesn't provide it."""
    return str(ticket.get("priority") or "medium").lower()


def is_escalated(ticket):
    """Handle boolean and integer escalation values."""
    value = ticket.get("escalated", False)
    return value is True or value == 1 or str(value).lower() == "true"


# --------------------------------------------------
# Header
# --------------------------------------------------

st.title("🎫 SupportFlow AI")
st.caption("Customer support ticket management dashboard")

# --------------------------------------------------
# Backend connection
# --------------------------------------------------

try:
    health_response = requests.get(
        f"{API_URL}/health",
        timeout=3,
    )
    health_response.raise_for_status()
    backend_connected = True

except requests.RequestException:
    backend_connected = False

if backend_connected:
    st.success("Backend connected")
else:
    st.error(
        "Backend is not connected. Start FastAPI in another terminal "
        "using: python -m uvicorn main:app --reload"
    )

# --------------------------------------------------
# Fetch tickets
# --------------------------------------------------

tickets_data = get_tickets()

# --------------------------------------------------
# Dashboard overview
# --------------------------------------------------

st.header("Dashboard Overview")

total_tickets = len(tickets_data)

high_priority_count = sum(
    1
    for ticket in tickets_data
    if get_priority(ticket) in ("high", "urgent")
)

escalated_count = sum(
    1
    for ticket in tickets_data
    if is_escalated(ticket)
)

col1, col2, col3 = st.columns(3)

col1.metric("Total Tickets", total_tickets)
col2.metric("High Priority", high_priority_count)
col3.metric("Escalated Tickets", escalated_count)

st.divider()

# --------------------------------------------------
# Create a support ticket
# --------------------------------------------------

st.header("Create a support ticket")

with st.form("create_ticket_form", clear_on_submit=False):
    customer_name = st.text_input("Customer name")

    message = st.text_area(
        "Describe the customer's issue",
        placeholder="Example: I was charged twice for my subscription.",
        height=120,
    )

    submitted = st.form_submit_button("Submit ticket")

    if submitted:
        if not customer_name.strip() or not message.strip():
            st.warning("Please enter both the customer name and issue.")
        elif not backend_connected:
            st.error("The backend is offline. Start FastAPI and try again.")
        else:
            try:
                response = requests.post(
                    f"{API_URL}/tickets",
                    json={
                        "customer_name": customer_name.strip(),
                        "message": message.strip(),
                    },
                    timeout=15,
                )
                response.raise_for_status()

                st.session_state["latest_ticket"] = response.json()
                st.success("Ticket created successfully!")

                # Reload the dashboard so its metrics and table update.
                st.rerun()

            except requests.RequestException as error:
                st.error(f"Could not create ticket: {error}")

# --------------------------------------------------
# Latest ticket result
# --------------------------------------------------

latest_ticket = st.session_state.get("latest_ticket")

if latest_ticket:
    st.header("Latest Ticket Result")

    result_col1, result_col2, result_col3 = st.columns(3)

    result_col1.metric(
        "Ticket ID",
        latest_ticket.get("id", "N/A"),
    )

    result_col2.metric(
        "Category",
        latest_ticket.get("category", "N/A"),
    )

    result_col3.metric(
        "Priority",
        latest_ticket.get("priority", "N/A"),
    )

    if latest_ticket.get("escalated"):
        st.warning(
            "Escalation required: "
            + str(
                latest_ticket.get(
                    "escalation_reason",
                    "Needs review",
                )
            )
        )

    st.markdown("**Draft response**")
    st.write(
        latest_ticket.get(
            "draft_response",
            "No draft response available.",
        )
    )

st.divider()

# --------------------------------------------------
# Saved support tickets
# --------------------------------------------------

st.header("Saved support tickets")

# Refresh button
if st.button("Refresh tickets"):
    st.rerun()

if tickets_data:
    # Category filter
    category_options = ["All"] + sorted(
        {
            str(ticket.get("category", "other"))
            for ticket in tickets_data
        }
    )

    selected_category = st.selectbox(
        "Filter by category",
        category_options,
    )

    # Priority filter
    priority_options = ["All", "medium", "high", "urgent"]

    selected_priority = st.selectbox(
        "Filter by priority",
        priority_options,
    )

    # Apply both filters
    filtered_tickets = tickets_data

    if selected_category != "All":
        filtered_tickets = [
            ticket
            for ticket in filtered_tickets
            if ticket.get("category") == selected_category
        ]

    if selected_priority != "All":
        filtered_tickets = [
            ticket
            for ticket in filtered_tickets
            if get_priority(ticket) == selected_priority
        ]

    # Display the filtered table
    if filtered_tickets:
        display_columns = [
            "id",
            "customer_name",
            "message",
            "category",
            "policy_title",
            "policy",
            "draft_response",
        ]

        # Include these fields when supplied by the API.
        optional_columns = [
            "priority",
            "status",
            "escalated",
            "escalation_reason",
        ]

        available_columns = list(filtered_tickets[0].keys())

        columns_to_show = [
            column
            for column in display_columns + optional_columns
            if column in available_columns
        ]

        tickets_df = pd.DataFrame(filtered_tickets)
        tickets_df = tickets_df.reindex(columns=columns_to_show)

        st.dataframe(
            tickets_df,
            use_container_width=True,
            hide_index=True,
        )

        st.caption(f"Showing {len(filtered_tickets)} ticket(s)")

        # --------------------------------------------------
        # Ticket details viewer
        # --------------------------------------------------

        st.subheader("Ticket Details")

        ticket_options = {
            (
                f"Ticket #{ticket.get('id', 'N/A')} — "
                f"{ticket.get('customer_name', 'Unknown')}"
            ): ticket
            for ticket in filtered_tickets
        }

        selected_ticket_label = st.selectbox(
            "Select a ticket to inspect",
            list(ticket_options.keys()),
        )

        selected_ticket = ticket_options[selected_ticket_label]

        st.markdown(
            f"**Customer:** "
            f"{selected_ticket.get('customer_name', 'N/A')}"
        )

        st.markdown(
            f"**Category:** "
            f"{selected_ticket.get('category', 'N/A')}"
        )

        st.markdown(
            f"**Priority:** {get_priority(selected_ticket)}"
        )

        st.markdown(
            f"**Status:** {selected_ticket.get('status', 'N/A')}"
        )

        st.markdown("**Customer message**")
        st.write(
            selected_ticket.get(
                "message",
                "No message available.",
            )
        )

        st.markdown("**Support policy**")
        st.write(
            selected_ticket.get(
                "policy",
                "No policy available.",
            )
        )

        st.markdown("**Draft response**")
        st.write(
            selected_ticket.get(
                "draft_response",
                "No draft available.",
            )
        )

        if is_escalated(selected_ticket):
            st.warning(
                "Escalation: "
                + str(
                    selected_ticket.get(
                        "escalation_reason"
                    )
                    or "Needs review"
                )
            )
        else:
            st.info("No escalation flagged.")

    else:
        st.info("No tickets match the selected filters.")

else:
    st.info("No saved tickets found. Create your first ticket above.")
