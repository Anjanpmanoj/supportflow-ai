"""Support policy knowledge base.

Two interchangeable retrieval modes, chosen with the KNOWLEDGE_BASE setting:

  simple (default)  plain in-memory lookup. No vector database and no model
                    download, so it fits on a free host with 512 MB of RAM.
  vector            semantic search with Chroma and local HuggingFace
                    embeddings. Install requirements-vector.txt first.

If vector mode fails for any reason, search falls back to simple mode.
"""

import os
import re
from pathlib import Path

from langchain_core.documents import Document


BASE_DIR = Path(__file__).resolve().parent
DB_DIR = BASE_DIR / "chroma_db"

POLICIES = [
    {
        "title": "Billing and Refund Policy",
        "category": "billing",
        "content": (
            "For billing questions, verify transaction details and "
            "subscription status before advising the customer. "
            "Duplicate charges and refund requests must be reviewed "
            "by a support agent. Do not promise a refund before "
            "eligibility is confirmed."
        ),
    },
    {
        "title": "Technical Support Policy",
        "category": "technical",
        "content": (
            "For technical issues, ask for relevant troubleshooting "
            "details and suggest safe, basic troubleshooting steps. "
            "Escalate unresolved or complex issues to a human support agent."
        ),
    },
    {
        "title": "Account Access Policy",
        "category": "account_access",
        "content": (
            "For account access issues, guide the customer to the "
            "official password recovery process. Never ask the customer "
            "to share passwords or authentication codes. Escalate "
            "suspected account compromise to a human support agent."
        ),
    },
    {
        "title": "General Support Policy",
        "category": "other",
        "content": (
            "Respond politely and address the customer's actual issue. "
            "Do not invent facts or promise outcomes that have not "
            "been confirmed. Escalate issues that require human review."
        ),
    },
]

DOCUMENTS = [
    Document(
        page_content=policy["content"],
        metadata={"title": policy["title"], "category": policy["category"]},
    )
    for policy in POLICIES
]

CATEGORIES = {policy["category"] for policy in POLICIES}

USE_VECTOR_SEARCH = (
    os.getenv("KNOWLEDGE_BASE", "simple").strip().lower() == "vector"
)


# --------------------------------------------------
# Vector mode (Chroma + local embeddings)
# --------------------------------------------------

_vector_store = None


def _get_embeddings():
    from langchain_huggingface import HuggingFaceEmbeddings

    # This embedding model runs locally.
    return HuggingFaceEmbeddings(
        model_name="sentence-transformers/all-MiniLM-L6-v2"
    )


def _get_vector_store():
    """Build the Chroma store once per process (only four short policies)."""
    global _vector_store

    if _vector_store is None:
        from langchain_chroma import Chroma

        store = Chroma(
            collection_name="support_policies",
            embedding_function=_get_embeddings(),
            persist_directory=str(DB_DIR),
        )

        # Replace existing documents so restarts never create duplicates.
        existing = store.get()
        if existing["ids"]:
            store.delete(ids=existing["ids"])

        store.add_documents(DOCUMENTS)
        _vector_store = store

    return _vector_store


# --------------------------------------------------
# Simple mode (no dependencies)
# --------------------------------------------------

def _words(text: str) -> set:
    return set(re.findall(r"[a-z]+", text.lower()))


def _simple_search(query: str, k: int):
    """The caller sends "<category>: <customer message>".

    A policy whose category matches comes first; the rest are ranked by
    word overlap with the message.
    """
    prefix, separator, rest = query.partition(":")
    category = prefix.strip().lower() if separator else ""
    text = rest if category in CATEGORIES else query

    matching = [d for d in DOCUMENTS if d.metadata["category"] == category]
    others = [d for d in DOCUMENTS if d not in matching]

    query_words = _words(text)
    others.sort(
        key=lambda d: len(
            query_words & _words(d.metadata["title"] + " " + d.page_content)
        ),
        reverse=True,
    )

    return (matching + others)[:k]


# --------------------------------------------------
# Public API
# --------------------------------------------------

def build_knowledge_base():
    """Prepare retrieval. Kept so older scripts that call it still work."""
    return _get_vector_store() if USE_VECTOR_SEARCH else DOCUMENTS


def search_policies(query: str, k: int = 2):
    if USE_VECTOR_SEARCH:
        try:
            return _get_vector_store().similarity_search(query, k=k)
        except Exception as error:
            print(
                "Vector search unavailable, using simple lookup: "
                f"{type(error).__name__}"
            )

    return _simple_search(query, k)
