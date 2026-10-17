import os
import re
from dataclasses import dataclass
from typing import Literal, Optional, TypedDict

from dotenv import load_dotenv
from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import END, START, StateGraph

from customer_tools import NO_HISTORY, format_customer_context, get_customer_context
from knowledge_base import search_policies

load_dotenv()


@dataclass
class DraftGeneration:
    response: str | None
    source: Literal["gemini", "template"]
    status: Literal[
        "success",
        "quota_exceeded",
        "timeout",
        "network_error",
        "provider_error",
        "not_configured",
        "empty_response",
        "validation_failed",
    ]
    error_message: str | None = None


api_key = os.getenv("GEMINI_API_KEY")

try:
    llm = (
        ChatGoogleGenerativeAI(
            model="gemini-3.8-flash",
            google_api_key=api_key,
            temperature=0.3,
            timeout=20,
            max_retries=0,
        )
        if api_key
        else None
    )
except Exception as error:
    print(f"Gemini initialization failed: {type(error).__name__}")
    llm = None


support_prompt = ChatPromptTemplate.from_messages([
    (
        "system",
        """You are a professional customer support assistant.
Write a helpful, polite, concise reply to the customer.
Follow the supplied support policies strictly.

Rules:
- Address the customer by name.
- Respond to their actual issue.
- Never invent order statuses, tracking links, refund approvals,
  delivery dates, or facts not provided.
- Never ask for passwords or authentication codes.
- If human review is required, explain that the team will review
  the issue without promising an outcome.
- If information is insufficient, do not guess.
- Use the customer history only as background (for example, to
  acknowledge an open ticket). Never mention details not listed there.
- Return only the draft reply, without a subject line.
""",
    ),
    (
        "human",
        """Customer name: {customer_name}
Ticket category: {category}
Customer message: {message}

Customer history from our system:
{customer_context}

Retrieved knowledge-base policies:
{policy_context}

Existing application policy:
{policy_title}
{policy}

{revision_notes}""",
    ),
])

support_chain = support_prompt | llm if llm else None


def classify_generation_error(error: Exception) -> str:
    """Classify provider failures without exposing sensitive details."""
    text = str(error).lower()
    error_name = type(error).__name__.lower()

    if any(term in text for term in (
        "resource_exhausted",
        "quota exceeded",
        "quota_exceeded",
        "rate limit",
        "429",
    )):
        return "quota_exceeded"

    if any(term in text for term in (
        "timeout",
        "timed out",
        "deadline exceeded",
    )) or "timeout" in error_name:
        return "timeout"

    if any(term in text for term in (
        "connection refused",
        "connection reset",
        "name resolution",
        "network is unreachable",
        "temporary failure in name resolution",
    )):
        return "network_error"

    return "provider_error"


# --------------------------------------------------
# Reply validation (safety rules the draft must pass)
# --------------------------------------------------

_SENSITIVE_REQUEST = re.compile(
    r"\b(send|share|provide|give|tell|enter|reply with|email me)\b[^.!?\n]{0,40}"
    r"\b(password|passcode|otp|one[- ]time (?:code|password)|"
    r"authentication code|verification code|security code|pin)\b",
    re.IGNORECASE,
)
_NEGATION = re.compile(r"\b(never|do not|don't|not|won't|will not)\b", re.IGNORECASE)

_REFUND_PROMISES = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(?:we|i)(?:'ll| will)(?: be)? (?:issue|process|send|give|provide)"
        r"(?: you)?(?: a| the| your)? (?:full |partial )?refund",
        r"\b(?:we|i)(?:'ll| will) refund\b",
        r"\b(?:we|i)(?: have|'ve) (?:issued|processed|approved|refunded)\b",
        r"\byou(?:'ll| will) (?:receive|get) (?:a |your |the )?(?:full |partial )?refund",
        r"\byou(?:'ll| will) be refunded\b",
        r"\brefund (?:has been|is|was) (?:approved|issued|processed|confirmed)\b",
        r"\bguarantee[sd]?\b.{0,20}\brefund",
    )
]
_LINK = re.compile(r"https?://|www\.", re.IGNORECASE)
_PLACEHOLDER = re.compile(r"\[[^\]\n]{1,40}\]|\{\{|\}\}|<[^>\n]{1,40}>")
_SUBJECT_LINE = re.compile(r"\s*subject\s*:", re.IGNORECASE)


def check_reply(text: str) -> list[str]:
    """Return the problems found in a draft (empty list = safe to use)."""
    issues = []

    if len(text) < 20:
        issues.append("The reply is too short to be helpful.")
    if len(text) > 2000:
        issues.append("The reply is too long; keep it concise.")

    for match in _SENSITIVE_REQUEST.finditer(text):
        before = text[max(0, match.start() - 40):match.start()]
        if not _NEGATION.search(before):
            issues.append("It asks the customer for a password, PIN or code.")
            break

    if any(pattern.search(text) for pattern in _REFUND_PROMISES):
        issues.append("It promises a refund before eligibility is confirmed.")
    if _LINK.search(text):
        issues.append("It contains a link; links must not be invented.")
    if _PLACEHOLDER.search(text):
        issues.append("It contains an unfilled placeholder such as [Name].")
    if _SUBJECT_LINE.match(text):
        issues.append("It starts with a subject line; return only the reply.")

    return issues


# --------------------------------------------------
# LangGraph workflow
#
#   load_customer ─┐
#                  ├─> draft_reply -> validate_reply -> done
#   retrieve_policy┘        ^               |
#                           └── retry ──────┤ (up to MAX_DRAFT_ATTEMPTS)
#                                           └─> give_up (validation_failed)
# --------------------------------------------------

MAX_DRAFT_ATTEMPTS = 2


class SupportState(TypedDict, total=False):
    # inputs
    customer_name: str
    message: str
    category: str
    policy_title: str
    policy: str
    user_id: Optional[int]
    # filled in by the nodes
    customer_context: str
    policy_context: str
    draft: Optional[str]
    attempts: int
    issues: list[str]
    status: str
    error_message: Optional[str]


def _message_text(content) -> str:
    """Chat models return a string or a list of content blocks."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts).strip()
    return ""


def load_customer(state: SupportState) -> dict:
    """Tool node: look up the customer's history in the database."""
    try:
        info = get_customer_context(
            user_id=state.get("user_id"),
            customer_name=state["customer_name"],
            category=state["category"],
        )
        return {"customer_context": format_customer_context(info)}
    except Exception as error:
        print(f"Customer lookup failed: {type(error).__name__}")
        return {"customer_context": NO_HISTORY}


def retrieve_policy(state: SupportState) -> dict:
    """Retrieval node: find the most relevant support policy."""
    policy_context = "No additional policies retrieved."
    try:
        documents = search_policies(
            query=f"{state['category']}: {state['message']}",
            k=1,
        )
        if documents:
            document = documents[0]
            policy_context = (
                f"Policy title: "
                f"{document.metadata.get('title', 'Support Policy')}\n"
                f"Policy category: "
                f"{document.metadata.get('category', 'other')}\n"
                f"Policy content: {document.page_content}"
            )
    except Exception as error:
        # Retrieval failure should not prevent generation.
        print(f"Policy retrieval failed: {type(error).__name__}")
    return {"policy_context": policy_context}


def draft_reply(state: SupportState) -> dict:
    """LLM node: write the draft (again, with feedback, if it was rejected)."""
    attempts = state.get("attempts", 0) + 1

    revision_notes = ""
    if state.get("issues"):
        revision_notes = (
            "Your previous draft was rejected for these reasons:\n"
            + "\n".join(f"- {issue}" for issue in state["issues"])
            + "\nWrite a corrected reply."
        )

    try:
        result = support_chain.invoke({
            "customer_name": state["customer_name"],
            "message": state["message"],
            "category": state["category"],
            "policy_title": state["policy_title"],
            "policy": state["policy"],
            "policy_context": state.get("policy_context", ""),
            "customer_context": state.get("customer_context", NO_HISTORY),
            "revision_notes": revision_notes,
        })
    except Exception as error:
        status = classify_generation_error(error)
        # Keep the detailed exception out of customer-facing responses.
        print(f"AI generation failed: {status} ({type(error).__name__})")
        return {
            "attempts": attempts,
            "draft": None,
            "status": status,
            "error_message": f"AI generation unavailable: {status}.",
        }

    text = _message_text(result.content)
    if not text:
        return {
            "attempts": attempts,
            "draft": None,
            "status": "empty_response",
            "error_message": "The AI provider returned an empty response.",
        }

    return {
        "attempts": attempts,
        "draft": text,
        "status": "drafted",
        "error_message": None,
    }


def validate_reply(state: SupportState) -> dict:
    """Validation node: apply the safety rules to the draft."""
    issues = check_reply(state["draft"])
    return {"issues": issues, "status": "invalid" if issues else "success"}


def give_up(state: SupportState) -> dict:
    """The draft kept failing validation, so no AI reply is used."""
    return {
        "draft": None,
        "status": "validation_failed",
        "error_message": (
            "The AI draft failed validation: " + "; ".join(state["issues"])
        ),
    }


def route_after_draft(state: SupportState) -> str:
    return "validate_reply" if state["status"] == "drafted" else END


def route_after_validation(state: SupportState) -> str:
    if state["status"] == "success":
        return END
    if state["attempts"] < MAX_DRAFT_ATTEMPTS:
        return "draft_reply"
    return "give_up"


def build_support_graph():
    graph = StateGraph(SupportState)

    graph.add_node("load_customer", load_customer)
    graph.add_node("retrieve_policy", retrieve_policy)
    graph.add_node("draft_reply", draft_reply)
    graph.add_node("validate_reply", validate_reply)
    graph.add_node("give_up", give_up)

    # Customer lookup and policy retrieval run in parallel.
    graph.add_edge(START, "load_customer")
    graph.add_edge(START, "retrieve_policy")
    graph.add_edge(["load_customer", "retrieve_policy"], "draft_reply")

    graph.add_conditional_edges(
        "draft_reply",
        route_after_draft,
        {"validate_reply": "validate_reply", END: END},
    )
    graph.add_conditional_edges(
        "validate_reply",
        route_after_validation,
        {"draft_reply": "draft_reply", "give_up": "give_up", END: END},
    )
    graph.add_edge("give_up", END)

    return graph.compile()


support_graph = build_support_graph()


def generate_support_response(
    customer_name: str,
    message: str,
    category: str,
    policy_title: str,
    policy: str,
    user_id: Optional[int] = None,
) -> DraftGeneration:
    """Run the support workflow and report the draft's source and outcome."""

    if support_chain is None:
        status = "not_configured"
        if api_key:
            status = "provider_error"

        return DraftGeneration(
            response=None,
            source="template",
            status=status,
            error_message="AI service is not available.",
        )

    try:
        final = support_graph.invoke(
            {
                "customer_name": customer_name,
                "message": message,
                "category": category,
                "policy_title": policy_title,
                "policy": policy,
                "user_id": user_id,
                "attempts": 0,
                "issues": [],
            },
            # Shows up as a named run in LangSmith when tracing is enabled.
            config={
                "run_name": "support_ticket_workflow",
                "tags": ["supportflow"],
                "metadata": {"category": category},
            },
        )
    except Exception as error:
        print(f"Support workflow failed: {type(error).__name__}")
        return DraftGeneration(
            response=None,
            source="template",
            status="provider_error",
            error_message="The support workflow failed unexpectedly.",
        )

    status = final.get("status", "provider_error")

    if status == "success" and final.get("draft"):
        return DraftGeneration(
            response=final["draft"],
            source="gemini",
            status="success",
        )

    return DraftGeneration(
        response=None,
        source="template",
        status=status,
        error_message=final.get("error_message"),
    )
