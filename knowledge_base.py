
from pathlib import Path

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_huggingface import HuggingFaceEmbeddings


BASE_DIR = Path(__file__).resolve().parent
DB_DIR = BASE_DIR / "chroma_db"

# This embedding model runs locally.
embeddings = HuggingFaceEmbeddings(
    model_name="sentence-transformers/all-MiniLM-L6-v2"
)

# Start with your existing support policies.
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


def build_knowledge_base():
    documents = [
        Document(
            page_content=policy["content"],
            metadata={
                "title": policy["title"],
                "category": policy["category"],
            },
        )
        for policy in POLICIES
    ]

    vector_store = Chroma(
        collection_name="support_policies",
        embedding_function=embeddings,
        persist_directory=str(DB_DIR),
    )

    # Replace the existing policy documents to avoid duplicates
    # whenever this setup function is run again.
    existing = vector_store.get()
    if existing["ids"]:
        vector_store.delete(ids=existing["ids"])

    vector_store.add_documents(documents)
    return vector_store


def search_policies(query: str, k: int = 2):
    vector_store = Chroma(
        collection_name="support_policies",
        embedding_function=embeddings,
        persist_directory=str(DB_DIR),
    )

    return vector_store.similarity_search(query, k=k)
