"""Password vault package for Cosmic.

Stores site credentials encrypted at rest (Fernet via the shared
CREDENTIAL_ENCRYPTION_KEY), enforces per-entry agent access policies, and
keeps an append-only audit log. The orchestrator is the only agent client;
the user manages entries from the desktop settings panel.
"""
