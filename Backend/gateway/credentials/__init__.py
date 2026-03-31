"""Gateway Credential Manager — Google OAuth token lifecycle and secure credential resolution."""

from __future__ import annotations

from .manager import CredentialManager
from .routes import router as credential_router

__all__ = ["CredentialManager", "credential_router"]
