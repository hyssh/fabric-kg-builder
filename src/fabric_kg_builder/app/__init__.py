"""fabric_kg_builder.app — M8 reference application package.

FastAPI backend + auth helpers for the Fabric KG reference app.
"""

from fabric_kg_builder.app.models import ChatRequest, ChatResponse, CitationResponse
from fabric_kg_builder.app.auth import (
    OutboundAuthProvider,
    ManagedIdentityAuthProvider,
    InboundAuthVerifier,
    AllowAllVerifier,
)

__all__ = [
    "AllowAllVerifier",
    "ChatRequest",
    "ChatResponse",
    "CitationResponse",
    "InboundAuthVerifier",
    "ManagedIdentityAuthProvider",
    "OutboundAuthProvider",
]
