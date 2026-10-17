
import os
from dataclasses import dataclass
from typing import Literal

from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import ChatPromptTemplate

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
- Return only the draft reply, without a subject line.
""",
    ),
    (
        "human",
        """Customer name: {customer_name}
Ticket category: {category}
Customer message: {message}

Retrieved knowledge-base policies:
{policy_context}

Existing application policy:
{policy_title}
{policy}
""",
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


def generate_support_response(
    customer_name: str,
    message: str,
    category: str,
    policy_title: str,
    policy: str,
) -> DraftGeneration:
    """Generate a draft and report its source and outcome."""

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

    policy_context = "No additional policies retrieved."

    try:
        documents = search_policies(
            query=f"{category}: {message}",
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

    try:
        result = support_chain.invoke({
            "customer_name": customer_name,
            "message": message,
            "category": category,
            "policy_title": policy_title,
            "policy": policy,
            "policy_context": policy_context,
        })

        content = result.content

        if isinstance(content, str) and content.strip():
            return DraftGeneration(
                response=content.strip(),
                source="gemini",
                status="success",
            )

        return DraftGeneration(
            response=None,
            source="template",
            status="empty_response",
            error_message="The AI provider returned an empty response.",
        )

    except Exception as error:
        status = classify_generation_error(error)

        # Keep the detailed exception out of customer-facing responses.
        print(f"AI generation failed: {status} ({type(error).__name__})")

        return DraftGeneration(
            response=None,
            source="template",
            status=status,
            error_message=f"AI generation unavailable: {status}.",
        )
