from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


BACKEND_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(BACKEND_ROOT / "gateway.env")
load_dotenv(BACKEND_ROOT / ".env")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None:
        return default
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    return values or default


def _default_alpha_codex_home() -> Path:
    if os.name == "nt":
        return BACKEND_ROOT / "agents" / "alpha_agent" / "runtime" / "alpha" / "homes" / "codex"
    return _default_alpha_workspace_root() / "homes" / "codex"


def _default_alpha_cursor_home() -> Path:
    if os.name == "nt":
        return BACKEND_ROOT / "agents" / "alpha_agent" / "runtime" / "alpha" / "homes" / "cursor"
    return _default_alpha_workspace_root() / "homes" / "cursor"


def _default_alpha_opencode_home() -> Path:
    if os.name == "nt":
        return BACKEND_ROOT / "agents" / "alpha_agent" / "runtime" / "alpha" / "homes" / "opencode"
    return _default_alpha_workspace_root() / "homes" / "opencode"


def _default_alpha_workspace_root() -> Path:
    if os.name == "nt":
        return BACKEND_ROOT / "agents" / "alpha_agent" / "runtime" / "alpha"
    return Path(os.getenv("ALPHA_WORKSPACE_ROOT", "/var/lib/cosmic/alpha")).expanduser()


@dataclass(slots=True)
class GatewayConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    public_host: str = ""
    public_base_url: str = ""
    owner_user_id: str = ""
    local_api_token: str = ""
    internal_token: str = ""
    signing_secret: str = ""
    model_router_url: str = "http://127.0.0.1:8742"
    model_router_timeout_sec: float = 15.0
    orchestrator_url: str = "http://127.0.0.1:8743"
    orchestrator_timeout_sec: float = 300.0
    redis_url: str = ""
    agent_events_stream: str = "streams:events"
    agent_events_gateway_group: str = "gateway-specialist-events"
    orchestrator_task_ledger_db_path: Path = (
        BACKEND_ROOT / "agents" / "orchestrator" / "store" / "data" / "task_ledger.db"
    )
    heartbeat_notes_path: Path = (
        BACKEND_ROOT / "agents" / "orchestrator" / "store" / "heartbeat_notes.md"
    )
    task_input_requests_stream: str = "user_input:requests"
    task_input_replies_stream: str = "user_input:replies"
    task_input_gateway_group: str = "gateway"
    enable_whatsapp: bool = True
    enable_telegram: bool = False
    enable_agent_email: bool = False
    agent_email_integrations_db_path: Path = (
        BACKEND_ROOT / "gateway" / "agent_email_integrations.db"
    )
    alpha_workspace_root: Path = _default_alpha_workspace_root()
    alpha_codex_home: Path = _default_alpha_codex_home()
    alpha_cursor_home: Path = _default_alpha_cursor_home()
    alpha_opencode_home: Path = _default_alpha_opencode_home()
    preferences_db_path: Path = BACKEND_ROOT / "gateway" / "preferences.db"
    sessions_db_path: Path = BACKEND_ROOT / "gateway" / "sessions.db"
    mobile_devices_db_path: Path = BACKEND_ROOT / "gateway" / "mobile_devices.db"
    enable_push_notifications: bool = True
    expo_access_token: str = ""
    expo_push_url: str = "https://exp.host/--/api/v2/push/send"
    expo_push_timeout_sec: float = 8.0
    fcm_project_id: str = ""
    fcm_service_account_file: Path | None = None
    fcm_service_account_json: str = ""
    fcm_timeout_sec: float = 8.0
    mobile_presence_stale_sec: int = 120
    desktop_connection_stale_sec: int = 180
    usage_db_path: Path = BACKEND_ROOT / "gateway" / "usage.db"
    request_trace_db_path: Path = BACKEND_ROOT / "gateway" / "request_traces.db"
    routing_audit_db_path: Path = BACKEND_ROOT / "gateway" / "routing_audit.db"
    memory_write_audit_db_path: Path = (
        BACKEND_ROOT / "gateway" / "memory_write_audit.db"
    )
    capability_wishlist_db_path: Path = (
        BACKEND_ROOT / "gateway" / "cosmics_capability_wishlist.db"
    )
    tool_opportunities_db_path: Path = BACKEND_ROOT / "gateway" / "tool_opportunities.db"
    artifacts_db_path: Path = BACKEND_ROOT / "gateway" / "artifacts.db"
    artifacts_root: Path = BACKEND_ROOT / "runs" / "artifacts"
    delivery_queue_db_path: Path = BACKEND_ROOT / "gateway" / "delivery_queue.db"
    scheduler_db_path: Path = BACKEND_ROOT / "gateway" / "scheduler.db"
    session_transcript_dir: Path = BACKEND_ROOT / "logs" / "sessions"
    capability_wishlist_export_dir: Path = (
        BACKEND_ROOT / "logs" / "cosmics_capability_wishlist"
    )
    tool_opportunities_export_path: Path = (
        BACKEND_ROOT / "logs" / "tool_opportunities" / "tool_opportunities.json"
    )
    session_reset_hour: int = 4
    user_timezone_fallback: str = "America/Chicago"
    cosmic_mail_base_url: str = ""
    cosmic_mail_api_token: str = ""
    cosmic_mail_timeout_sec: float = 20.0
    cosmic_mail_primary_mailbox_address: str = ""
    cosmic_mail_webhook_secret: str = ""
    cosmic_mail_webhook_signature_header: str = "X-Cosmic-Mail-Signature"
    scheduler_poll_interval_sec: float = 30.0
    heartbeat_interval_sec: int = 1800
    heartbeat_calendar_digest_enabled: bool = True
    heartbeat_calendar_window_hours: int = 24
    heartbeat_calendar_max_accounts: int = 4
    heartbeat_calendar_max_events: int = 10
    heartbeat_calendar_agent_timeout_sec: float = 12.0
    heartbeat_gmail_digest_enabled: bool = True
    heartbeat_gmail_max_accounts: int = 4
    heartbeat_gmail_max_items: int = 6
    heartbeat_gmail_agent_timeout_sec: float = 12.0
    delivery_retry_base_sec: float = 1.0
    delivery_retry_max_sec: float = 120.0
    delivery_max_attempts: int = 12
    usage_queue_max_size: int = 1000
    usage_queue_flush_timeout_sec: float = 5.0
    desktop_system_metrics_cache_ttl_sec: float = 15.0
    artifact_download_timeout_sec: float = 60.0
    artifact_signed_url_ttl_sec: int = 300
    max_image_attachments_per_message: int = 20
    llm_image_max_edge_px: int = 1568
    llm_image_max_pixels: int = 1_150_000
    llm_image_jpeg_quality: int = 85
    docs_auto_parse_enabled: bool = True
    docs_parser_agent_id: str = "cosmic/docs-parser-agent:1.0.0"
    docs_upload_max_file_bytes: int = 20 * 1024 * 1024
    docs_parse_timeout_sec: float = 300.0
    docs_parse_reconcile_timeout_sec: float = 900.0
    docs_parse_poll_interval_sec: float = 0.25
    tabular_auto_parse_enabled: bool = True
    tabular_agent_id: str = "cosmic/tabular-agent:1.0.0"
    tabular_parse_timeout_sec: float = 300.0
    tabular_parse_reconcile_timeout_sec: float = 900.0
    tabular_parse_poll_interval_sec: float = 0.25
    email_agent_id: str = "cosmic/email-agent:1.0.0"
    gmail_agent_id: str = "cosmic/gmail-agent:1.0.0"
    gmail_context_db_path: Path = BACKEND_ROOT / "gateway" / "gmail_context.db"
    gmail_approvals_db_path: Path = BACKEND_ROOT / "gateway" / "gmail_approvals.db"
    sandbox_permissions_db_path: Path = BACKEND_ROOT / "gateway" / "sandbox_permissions.db"
    code_sandbox_timeout_sec: float = 45.0
    code_sandbox_allow_pip: bool = True
    code_sandbox_pip_timeout_sec: float = 120.0
    code_sandbox_venv_cache_root: Path | None = None
    code_sandbox_max_script_bytes: int = 256000
    code_sandbox_max_files: int = 12
    code_sandbox_max_file_bytes: int = 25 * 1024 * 1024
    event_automation_db_path: Path = BACKEND_ROOT / "gateway" / "event_automations.db"
    gmail_webhook_secret: str = ""
    gmail_process_inbound_timeout_sec: float = 180.0
    gmail_process_inbound_poll_interval_sec: float = 0.25
    gmail_watch_renewal_interval_sec: float = 21_600.0
    gmail_surface_backfill_interval_sec: float = 600.0
    gmail_surface_backfill_stale_after_sec: float = 1200.0
    gmail_surface_backfill_batch_limit: int = 3
    calendar_agent_id: str = "cosmic/calendar-agent:1.0.0"
    email_process_inbound_timeout_sec: float = 180.0
    email_process_inbound_poll_interval_sec: float = 0.25
    haiku_api_key: str = ""
    haiku_model: str = "claude-haiku-4-5"
    anthropic_version: str = "2023-06-01"
    haiku_max_tokens: int = 16000
    haiku_thinking_budget_tokens: int = 10000
    perplexity_api_key: str = ""
    perplexity_model: str = "sonar"
    capability_wishlist_embedding_model: str = "pplx-embed-v1-4b"
    capability_wishlist_embedding_dimensions: int = 1024
    xai_api_key: str = ""
    capability_wishlist_adjudicator_model: str = "grok-4-1-fast-reasoning"
    direct_llm_timeout_sec: float = 90.0
    cosmic_memory_url: str = ""
    cosmic_memory_timeout_sec: float = 12.0
    cosmic_memory_write_timeout_sec: float = 45.0
    cosmic_memory_core_fact_max_chars: int = 1500
    cosmic_memory_passive_max_results: int = 8
    cosmic_memory_passive_token_budget: int = 12000
    cosmic_memory_passive_kinds: tuple[str, ...] = (
        "session_summary",
        "task_summary",
        "agent_note",
        "user_data",
        # Raw per-turn records, including email threads (which never produce a
        # session summary of their own). Without this kind, every conversation
        # turn is written to shared memory but can never be recalled.
        "transcript",
    )
    cosmic_memory_ingest_transcripts: bool = True
    cosmic_memory_episode_extract_graph: bool = False
    # Summarize an email thread once it has been idle this long, so email
    # correspondence gets a recallable session_summary like day sessions do.
    email_thread_summary_idle_minutes: int = 360
    email_thread_summary_poll_sec: int = 900
    memory_write_max_per_hour: int = 50
    memory_write_dedup_ttl_sec: int = 86_400
    session_summary_max_output_tokens: int = 2500
    max_background_tasks_per_session: int = 5
    # OAuth providers / Credential Manager
    credentials_db_path: Path = BACKEND_ROOT / "gateway" / "credentials.db"
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8085/"
    # GitHub App (user-to-server). The redirect is loopback on the *user's*
    # machine, not this VM: the desktop bridge listens, catches the code, and
    # relays it to whichever gateway that desktop is paired with. A VM hostname
    # here would have to be pre-registered per user, which cannot work for a
    # shared App. Deliberately a different port from Google's 8085 so the two
    # listeners can never collide.
    github_client_id: str = ""
    github_client_secret: str = ""
    github_redirect_uri: str = "http://localhost:8086/"
    # Needed for the first-time install URL (github.com/apps/<slug>/installations/new),
    # which is not derivable from the client id.
    github_app_slug: str = ""

    @classmethod
    def from_env(cls) -> "GatewayConfig":
        return cls(
            host=os.getenv("GATEWAY_HOST", "0.0.0.0"),
            port=_env_int("GATEWAY_PORT", 8080),
            public_host=os.getenv("GATEWAY_PUBLIC_HOST", "").strip(),
            public_base_url=os.getenv("GATEWAY_PUBLIC_BASE_URL", "").strip(),
            owner_user_id=os.getenv("COSMIC_USER_ID", "").strip(),
            local_api_token=(
                os.getenv("GATEWAY_LOCAL_API_TOKEN")
                or os.getenv("LOCAL_API_TOKEN")
                or ""
            ),
            internal_token=os.getenv("GATEWAY_INTERNAL_TOKEN", ""),
            signing_secret=os.getenv("GATEWAY_SIGNING_SECRET", "").strip(),
            model_router_url=os.getenv(
                "MODEL_ROUTER_URL", "http://127.0.0.1:8742"
            ).rstrip("/"),
            model_router_timeout_sec=max(
                1.0,
                _env_float("MODEL_ROUTER_TIMEOUT_SEC", 15.0),
            ),
            orchestrator_url=os.getenv(
                "ORCHESTRATOR_URL", "http://127.0.0.1:8743"
            ).rstrip("/"),
            orchestrator_timeout_sec=max(
                10.0,
                _env_float("ORCHESTRATOR_TIMEOUT_SEC", 300.0),
            ),
            redis_url=os.getenv("REDIS_URL", "").strip(),
            agent_events_stream=os.getenv("AGENT_EVENTS_STREAM", "streams:events").strip()
            or "streams:events",
            agent_events_gateway_group=os.getenv(
                "GATEWAY_AGENT_EVENTS_GROUP", "gateway-specialist-events"
            ).strip()
            or "gateway-specialist-events",
            orchestrator_task_ledger_db_path=Path(
                os.getenv(
                    "ORCHESTRATOR_TASK_LEDGER_DB_PATH",
                    str(
                        BACKEND_ROOT
                        / "agents"
                        / "orchestrator"
                        / "store"
                        / "data"
                        / "task_ledger.db"
                    ),
                )
            ).expanduser(),
            heartbeat_notes_path=Path(
                os.getenv(
                    "COSMIC_HEARTBEAT_NOTES_PATH",
                    str(
                        BACKEND_ROOT
                        / "agents"
                        / "orchestrator"
                        / "store"
                        / "heartbeat_notes.md"
                    ),
                )
            ).expanduser(),
            task_input_requests_stream=os.getenv(
                "TASK_INPUT_REQUESTS_STREAM", "user_input:requests"
            ).strip()
            or "user_input:requests",
            task_input_replies_stream=os.getenv(
                "TASK_INPUT_REPLIES_STREAM", "user_input:replies"
            ).strip()
            or "user_input:replies",
            task_input_gateway_group=os.getenv(
                "TASK_INPUT_GATEWAY_GROUP", "gateway"
            ).strip()
            or "gateway",
            enable_whatsapp=_env_bool("WHATSAPP_ENABLED", True),
            enable_telegram=_env_bool("TELEGRAM_ENABLED", False),
            enable_agent_email=_env_bool("AGENT_EMAIL_ENABLED", False),
            agent_email_integrations_db_path=Path(
                os.getenv(
                    "GATEWAY_AGENT_EMAIL_INTEGRATIONS_DB_PATH",
                    str(BACKEND_ROOT / "gateway" / "agent_email_integrations.db"),
                )
            ).expanduser(),
            alpha_workspace_root=Path(
                os.getenv("ALPHA_WORKSPACE_ROOT", str(_default_alpha_workspace_root()))
            ).expanduser(),
            alpha_codex_home=Path(
                os.getenv(
                    "ALPHA_CODEX_HOME",
                    os.getenv("CODEX_HOME", str(_default_alpha_codex_home())),
                )
            ).expanduser(),
            alpha_cursor_home=Path(
                os.getenv(
                    "ALPHA_CURSOR_HOME",
                    os.getenv("CURSOR_HOME", str(_default_alpha_cursor_home())),
                )
            ).expanduser(),
            alpha_opencode_home=Path(
                os.getenv(
                    "ALPHA_OPENCODE_HOME",
                    str(_default_alpha_opencode_home()),
                )
            ).expanduser(),
            preferences_db_path=Path(
                os.getenv(
                    "GATEWAY_PREFERENCES_DB_PATH",
                    str(BACKEND_ROOT / "gateway" / "preferences.db"),
                )
            ).expanduser(),
            sessions_db_path=Path(
                os.getenv(
                    "GATEWAY_SESSIONS_DB_PATH",
                    str(BACKEND_ROOT / "gateway" / "sessions.db"),
                )
            ).expanduser(),
            mobile_devices_db_path=Path(
                os.getenv(
                    "GATEWAY_MOBILE_DEVICES_DB_PATH",
                    str(BACKEND_ROOT / "gateway" / "mobile_devices.db"),
                )
            ).expanduser(),
            enable_push_notifications=_env_bool("ENABLE_PUSH_NOTIFICATIONS", True),
            expo_access_token=os.getenv("EXPO_ACCESS_TOKEN", "").strip(),
            expo_push_url=(
                os.getenv("EXPO_PUSH_URL", "https://exp.host/--/api/v2/push/send").strip()
                or "https://exp.host/--/api/v2/push/send"
            ),
            expo_push_timeout_sec=max(
                1.0,
                _env_float("EXPO_PUSH_TIMEOUT_SEC", 8.0),
            ),
            fcm_project_id=os.getenv("FCM_PROJECT_ID", "").strip(),
            fcm_service_account_file=(
                Path(os.getenv("FCM_SERVICE_ACCOUNT_FILE", "")).expanduser()
                if os.getenv("FCM_SERVICE_ACCOUNT_FILE", "").strip()
                else None
            ),
            fcm_service_account_json=os.getenv("FCM_SERVICE_ACCOUNT_JSON", "").strip(),
            fcm_timeout_sec=max(1.0, _env_float("FCM_TIMEOUT_SEC", 8.0)),
            mobile_presence_stale_sec=max(
                15,
                _env_int("MOBILE_PRESENCE_STALE_SEC", 120),
            ),
            desktop_connection_stale_sec=max(
                30,
                _env_int("DESKTOP_CONNECTION_STALE_SEC", 180),
            ),
            usage_db_path=Path(
                os.getenv(
                    "GATEWAY_USAGE_DB_PATH",
                    str(BACKEND_ROOT / "gateway" / "usage.db"),
                )
            ).expanduser(),
            request_trace_db_path=Path(
                os.getenv(
                    "GATEWAY_REQUEST_TRACE_DB_PATH",
                    str(BACKEND_ROOT / "gateway" / "request_traces.db"),
                )
            ).expanduser(),
            routing_audit_db_path=Path(
                os.getenv(
                    "GATEWAY_ROUTING_AUDIT_DB_PATH",
                    str(BACKEND_ROOT / "gateway" / "routing_audit.db"),
                )
            ).expanduser(),
            memory_write_audit_db_path=Path(
                os.getenv(
                    "GATEWAY_MEMORY_WRITE_AUDIT_DB_PATH",
                    str(BACKEND_ROOT / "gateway" / "memory_write_audit.db"),
                )
            ).expanduser(),
            capability_wishlist_db_path=Path(
                os.getenv(
                    "GATEWAY_CAPABILITY_WISHLIST_DB_PATH",
                    str(BACKEND_ROOT / "gateway" / "cosmics_capability_wishlist.db"),
                )
            ).expanduser(),
            tool_opportunities_db_path=Path(
                os.getenv(
                    "GATEWAY_TOOL_OPPORTUNITIES_DB_PATH",
                    str(BACKEND_ROOT / "gateway" / "tool_opportunities.db"),
                )
            ).expanduser(),
            artifacts_db_path=Path(
                os.getenv(
                    "GATEWAY_ARTIFACTS_DB_PATH",
                    str(BACKEND_ROOT / "gateway" / "artifacts.db"),
                )
            ).expanduser(),
            artifacts_root=Path(
                os.getenv(
                    "GATEWAY_ARTIFACTS_ROOT",
                    str(BACKEND_ROOT / "runs" / "artifacts"),
                )
            ).expanduser(),
            delivery_queue_db_path=Path(
                os.getenv(
                    "GATEWAY_DELIVERY_QUEUE_DB_PATH",
                    str(BACKEND_ROOT / "gateway" / "delivery_queue.db"),
                )
            ).expanduser(),
            scheduler_db_path=Path(
                os.getenv(
                    "GATEWAY_SCHEDULER_DB_PATH",
                    str(BACKEND_ROOT / "gateway" / "scheduler.db"),
                )
            ).expanduser(),
            session_transcript_dir=Path(
                os.getenv(
                    "GATEWAY_SESSION_TRANSCRIPT_DIR",
                    str(BACKEND_ROOT / "logs" / "sessions"),
                )
            ).expanduser(),
            capability_wishlist_export_dir=Path(
                os.getenv(
                    "GATEWAY_CAPABILITY_WISHLIST_EXPORT_DIR",
                    str(BACKEND_ROOT / "logs" / "cosmics_capability_wishlist"),
                )
            ).expanduser(),
            tool_opportunities_export_path=Path(
                os.getenv(
                    "GATEWAY_TOOL_OPPORTUNITIES_EXPORT_PATH",
                    str(BACKEND_ROOT / "logs" / "tool_opportunities" / "tool_opportunities.json"),
                )
            ).expanduser(),
            session_reset_hour=min(
                23,
                max(0, _env_int("SESSION_RESET_HOUR", 4)),
            ),
            user_timezone_fallback=os.getenv(
                "USER_TIMEZONE_FALLBACK", "America/Chicago"
            ).strip()
            or "America/Chicago",
            cosmic_mail_base_url=os.getenv("COSMIC_MAIL_BASE_URL", "")
            .strip()
            .rstrip("/"),
            cosmic_mail_api_token=os.getenv("COSMIC_MAIL_API_TOKEN", "").strip(),
            cosmic_mail_timeout_sec=max(
                5.0,
                _env_float("COSMIC_MAIL_TIMEOUT_SEC", 20.0),
            ),
            cosmic_mail_primary_mailbox_address=os.getenv(
                "COSMIC_MAIL_PRIMARY_MAILBOX_ADDRESS", ""
            ).strip(),
            cosmic_mail_webhook_secret=os.getenv(
                "COSMIC_MAIL_WEBHOOK_SECRET", ""
            ).strip(),
            cosmic_mail_webhook_signature_header=(
                os.getenv(
                    "COSMIC_MAIL_WEBHOOK_SIGNATURE_HEADER", "X-Cosmic-Mail-Signature"
                ).strip()
                or "X-Cosmic-Mail-Signature"
            ),
            scheduler_poll_interval_sec=max(
                5.0,
                _env_float("GATEWAY_SCHEDULER_POLL_INTERVAL_SEC", 30.0),
            ),
            heartbeat_interval_sec=max(
                60,
                _env_int("GATEWAY_HEARTBEAT_INTERVAL_SEC", 1800),
            ),
            heartbeat_calendar_digest_enabled=_env_bool(
                "GATEWAY_HEARTBEAT_CALENDAR_DIGEST_ENABLED", True
            ),
            heartbeat_calendar_window_hours=max(
                1,
                _env_int("GATEWAY_HEARTBEAT_CALENDAR_WINDOW_HOURS", 24),
            ),
            heartbeat_calendar_max_accounts=max(
                1,
                _env_int("GATEWAY_HEARTBEAT_CALENDAR_MAX_ACCOUNTS", 4),
            ),
            heartbeat_calendar_max_events=max(
                1,
                _env_int("GATEWAY_HEARTBEAT_CALENDAR_MAX_EVENTS", 10),
            ),
            heartbeat_calendar_agent_timeout_sec=max(
                2.0,
                _env_float("GATEWAY_HEARTBEAT_CALENDAR_AGENT_TIMEOUT_SEC", 12.0),
            ),
            heartbeat_gmail_digest_enabled=_env_bool(
                "GATEWAY_HEARTBEAT_GMAIL_DIGEST_ENABLED",
                True,
            ),
            heartbeat_gmail_max_accounts=max(
                1,
                _env_int("GATEWAY_HEARTBEAT_GMAIL_MAX_ACCOUNTS", 4),
            ),
            heartbeat_gmail_max_items=max(
                1,
                _env_int("GATEWAY_HEARTBEAT_GMAIL_MAX_ITEMS", 6),
            ),
            heartbeat_gmail_agent_timeout_sec=max(
                2.0,
                _env_float("GATEWAY_HEARTBEAT_GMAIL_AGENT_TIMEOUT_SEC", 12.0),
            ),
            delivery_retry_base_sec=max(
                0.25,
                _env_float("GATEWAY_DELIVERY_RETRY_BASE_SEC", 1.0),
            ),
            delivery_retry_max_sec=max(
                1.0,
                _env_float("GATEWAY_DELIVERY_RETRY_MAX_SEC", 120.0),
            ),
            delivery_max_attempts=max(
                1,
                _env_int("GATEWAY_DELIVERY_MAX_ATTEMPTS", 12),
            ),
            usage_queue_max_size=max(
                16,
                _env_int("GATEWAY_USAGE_QUEUE_MAX_SIZE", 1000),
            ),
            usage_queue_flush_timeout_sec=max(
                0.25,
                _env_float("GATEWAY_USAGE_QUEUE_FLUSH_TIMEOUT_SEC", 5.0),
            ),
            desktop_system_metrics_cache_ttl_sec=max(
                2.0,
                _env_float("GATEWAY_DESKTOP_SYSTEM_METRICS_CACHE_TTL_SEC", 15.0),
            ),
            artifact_download_timeout_sec=max(
                5.0,
                _env_float("GATEWAY_ARTIFACT_DOWNLOAD_TIMEOUT_SEC", 60.0),
            ),
            artifact_signed_url_ttl_sec=max(
                30,
                _env_int("GATEWAY_ARTIFACT_SIGNED_URL_TTL_SEC", 300),
            ),
            max_image_attachments_per_message=max(
                1,
                _env_int("GATEWAY_MAX_IMAGE_ATTACHMENTS_PER_MESSAGE", 20),
            ),
            llm_image_max_edge_px=max(
                512,
                _env_int("GATEWAY_LLM_IMAGE_MAX_EDGE_PX", 1568),
            ),
            llm_image_max_pixels=max(
                262_144,
                _env_int("GATEWAY_LLM_IMAGE_MAX_PIXELS", 1_150_000),
            ),
            llm_image_jpeg_quality=max(
                40,
                min(95, _env_int("GATEWAY_LLM_IMAGE_JPEG_QUALITY", 85)),
            ),
            docs_auto_parse_enabled=_env_bool("GATEWAY_DOCS_AUTO_PARSE_ENABLED", True),
            docs_parser_agent_id=(
                os.getenv(
                    "GATEWAY_DOCS_PARSER_AGENT_ID", "cosmic/docs-parser-agent:1.0.0"
                ).strip()
                or "cosmic/docs-parser-agent:1.0.0"
            ),
            docs_upload_max_file_bytes=max(
                1024 * 1024,
                _env_int("GATEWAY_DOCS_UPLOAD_MAX_FILE_BYTES", 20 * 1024 * 1024),
            ),
            docs_parse_timeout_sec=max(
                5.0,
                _env_float("GATEWAY_DOCS_PARSE_TIMEOUT_SEC", 300.0),
            ),
            docs_parse_reconcile_timeout_sec=max(
                30.0,
                _env_float("GATEWAY_DOCS_PARSE_RECONCILE_TIMEOUT_SEC", 900.0),
            ),
            docs_parse_poll_interval_sec=max(
                0.05,
                _env_float("GATEWAY_DOCS_PARSE_POLL_INTERVAL_SEC", 0.25),
            ),
            tabular_auto_parse_enabled=_env_bool(
                "GATEWAY_TABULAR_AUTO_PARSE_ENABLED", True
            ),
            tabular_agent_id=(
                os.getenv(
                    "GATEWAY_TABULAR_AGENT_ID", "cosmic/tabular-agent:1.0.0"
                ).strip()
                or "cosmic/tabular-agent:1.0.0"
            ),
            tabular_parse_timeout_sec=max(
                5.0,
                _env_float("GATEWAY_TABULAR_PARSE_TIMEOUT_SEC", 300.0),
            ),
            tabular_parse_reconcile_timeout_sec=max(
                30.0,
                _env_float("GATEWAY_TABULAR_PARSE_RECONCILE_TIMEOUT_SEC", 900.0),
            ),
            tabular_parse_poll_interval_sec=max(
                0.05,
                _env_float("GATEWAY_TABULAR_PARSE_POLL_INTERVAL_SEC", 0.25),
            ),
            email_agent_id=(
                os.getenv("GATEWAY_EMAIL_AGENT_ID", "cosmic/email-agent:1.0.0").strip()
                or "cosmic/email-agent:1.0.0"
            ),
            gmail_agent_id=(
                os.getenv("GATEWAY_GMAIL_AGENT_ID", "cosmic/gmail-agent:1.0.0").strip()
                or "cosmic/gmail-agent:1.0.0"
            ),
            gmail_context_db_path=Path(
                os.getenv(
                    "GATEWAY_GMAIL_CONTEXT_DB_PATH",
                    str(BACKEND_ROOT / "gateway" / "gmail_context.db"),
                )
            ).expanduser(),
            gmail_approvals_db_path=Path(
                os.getenv(
                    "GATEWAY_GMAIL_APPROVALS_DB_PATH",
                    str(BACKEND_ROOT / "gateway" / "gmail_approvals.db"),
                )
            ).expanduser(),
            sandbox_permissions_db_path=Path(
                os.getenv(
                    "GATEWAY_SANDBOX_PERMISSIONS_DB_PATH",
                    str(BACKEND_ROOT / "gateway" / "sandbox_permissions.db"),
                )
            ).expanduser(),
            code_sandbox_timeout_sec=max(
                1.0,
                _env_float(
                    "ORCHESTRATOR_CODE_SANDBOX_TIMEOUT_SEC",
                    _env_float("GATEWAY_CODE_SANDBOX_TIMEOUT_SEC", 45.0),
                ),
            ),
            code_sandbox_allow_pip=_env_bool(
                "ORCHESTRATOR_CODE_SANDBOX_ALLOW_PIP",
                _env_bool("GATEWAY_CODE_SANDBOX_ALLOW_PIP", True),
            ),
            code_sandbox_pip_timeout_sec=max(
                5.0,
                _env_float(
                    "ORCHESTRATOR_CODE_SANDBOX_PIP_TIMEOUT_SEC",
                    _env_float("GATEWAY_CODE_SANDBOX_PIP_TIMEOUT_SEC", 120.0),
                ),
            ),
            code_sandbox_venv_cache_root=(
                Path(
                    os.getenv(
                        "ORCHESTRATOR_CODE_SANDBOX_VENV_CACHE_ROOT",
                        os.getenv("GATEWAY_CODE_SANDBOX_VENV_CACHE_ROOT", ""),
                    )
                ).expanduser()
                if os.getenv(
                    "ORCHESTRATOR_CODE_SANDBOX_VENV_CACHE_ROOT",
                    os.getenv("GATEWAY_CODE_SANDBOX_VENV_CACHE_ROOT", ""),
                ).strip()
                else None
            ),
            code_sandbox_max_script_bytes=max(
                1024,
                _env_int(
                    "ORCHESTRATOR_CODE_SANDBOX_MAX_SCRIPT_BYTES",
                    _env_int("GATEWAY_CODE_SANDBOX_MAX_SCRIPT_BYTES", 256000),
                ),
            ),
            code_sandbox_max_files=max(
                0,
                _env_int(
                    "ORCHESTRATOR_CODE_SANDBOX_MAX_FILES",
                    _env_int("GATEWAY_CODE_SANDBOX_MAX_FILES", 12),
                ),
            ),
            code_sandbox_max_file_bytes=max(
                1024,
                _env_int(
                    "ORCHESTRATOR_CODE_SANDBOX_MAX_FILE_BYTES",
                    _env_int("GATEWAY_CODE_SANDBOX_MAX_FILE_BYTES", 25 * 1024 * 1024),
                ),
            ),
            event_automation_db_path=Path(
                os.getenv(
                    "GATEWAY_EVENT_AUTOMATION_DB_PATH",
                    str(BACKEND_ROOT / "gateway" / "event_automations.db"),
                )
            ).expanduser(),
            gmail_webhook_secret=(
                os.getenv("GATEWAY_GMAIL_WEBHOOK_SECRET")
                or os.getenv("GMAIL_WEBHOOK_SECRET")
                or ""
            ).strip(),
            gmail_process_inbound_timeout_sec=max(
                5.0,
                _env_float("GATEWAY_GMAIL_PROCESS_INBOUND_TIMEOUT_SEC", 180.0),
            ),
            gmail_process_inbound_poll_interval_sec=max(
                0.05,
                _env_float("GATEWAY_GMAIL_PROCESS_INBOUND_POLL_INTERVAL_SEC", 0.25),
            ),
            gmail_watch_renewal_interval_sec=max(
                3600.0,
                _env_float("GATEWAY_GMAIL_WATCH_RENEWAL_INTERVAL_SEC", 21_600.0),
            ),
            gmail_surface_backfill_interval_sec=max(
                60.0,
                _env_float("GATEWAY_GMAIL_SURFACE_BACKFILL_INTERVAL_SEC", 600.0),
            ),
            gmail_surface_backfill_stale_after_sec=max(
                300.0,
                _env_float("GATEWAY_GMAIL_SURFACE_BACKFILL_STALE_AFTER_SEC", 1200.0),
            ),
            gmail_surface_backfill_batch_limit=max(
                1,
                _env_int("GATEWAY_GMAIL_SURFACE_BACKFILL_BATCH_LIMIT", 3),
            ),
            calendar_agent_id=(
                os.getenv(
                    "GATEWAY_CALENDAR_AGENT_ID",
                    "cosmic/calendar-agent:1.0.0",
                ).strip()
                or "cosmic/calendar-agent:1.0.0"
            ),
            email_process_inbound_timeout_sec=max(
                5.0,
                _env_float("GATEWAY_EMAIL_PROCESS_INBOUND_TIMEOUT_SEC", 180.0),
            ),
            email_process_inbound_poll_interval_sec=max(
                0.05,
                _env_float("GATEWAY_EMAIL_PROCESS_INBOUND_POLL_INTERVAL_SEC", 0.25),
            ),
            haiku_api_key=(
                os.getenv("HAIKU_API_KEY")
                or os.getenv("ANTHROPIC_API_KEY")
                or os.getenv("GEMINI_API_KEY")
                or ""
            ).strip(),
            haiku_model=(
                os.getenv("HAIKU_MODEL")
                or os.getenv("GEMINI_MODEL")
                or "claude-haiku-4-5"
            ).strip(),
            anthropic_version=os.getenv("ANTHROPIC_VERSION", "2023-06-01").strip(),
            haiku_max_tokens=max(1024, _env_int("HAIKU_MAX_TOKENS", 16000)),
            haiku_thinking_budget_tokens=max(
                0,
                _env_int("HAIKU_THINKING_BUDGET_TOKENS", 10000),
            ),
            perplexity_api_key=os.getenv("PERPLEXITY_API_KEY", "").strip(),
            perplexity_model=os.getenv("PERPLEXITY_MODEL", "sonar").strip(),
            capability_wishlist_embedding_model=(
                os.getenv(
                    "CAPABILITY_WISHLIST_EMBEDDING_MODEL", "pplx-embed-v1-4b"
                ).strip()
                or "pplx-embed-v1-4b"
            ),
            capability_wishlist_embedding_dimensions=max(
                128,
                _env_int("CAPABILITY_WISHLIST_EMBEDDING_DIMENSIONS", 1024),
            ),
            xai_api_key=os.getenv("XAI_API_KEY", "").strip(),
            capability_wishlist_adjudicator_model=(
                os.getenv(
                    "CAPABILITY_WISHLIST_ADJUDICATOR_MODEL", "grok-4-1-fast-reasoning"
                ).strip()
                or "grok-4-1-fast-reasoning"
            ),
            direct_llm_timeout_sec=max(
                5.0,
                _env_float("DIRECT_LLM_TIMEOUT_SEC", 90.0),
            ),
            cosmic_memory_url=os.getenv("COSMIC_MEMORY_URL", "").rstrip("/"),
            cosmic_memory_timeout_sec=max(
                1.0,
                _env_float("COSMIC_MEMORY_TIMEOUT_SEC", 12.0),
            ),
            cosmic_memory_write_timeout_sec=max(
                5.0,
                _env_float("COSMIC_MEMORY_WRITE_TIMEOUT_SEC", 45.0),
            ),
            cosmic_memory_core_fact_max_chars=max(
                250,
                _env_int("COSMIC_MEMORY_CORE_FACT_MAX_CHARS", 1500),
            ),
            cosmic_memory_passive_max_results=max(
                1,
                _env_int("COSMIC_MEMORY_PASSIVE_MAX_RESULTS", 8),
            ),
            cosmic_memory_passive_token_budget=max(
                256,
                _env_int("COSMIC_MEMORY_PASSIVE_TOKEN_BUDGET", 12000),
            ),
            cosmic_memory_passive_kinds=_env_csv(
                "COSMIC_MEMORY_PASSIVE_KINDS",
                (
                    "session_summary",
                    "task_summary",
                    "agent_note",
                    "user_data",
                    "transcript",
                ),
            ),
            cosmic_memory_ingest_transcripts=_env_bool(
                "COSMIC_MEMORY_INGEST_TRANSCRIPTS", True
            ),
            cosmic_memory_episode_extract_graph=_env_bool(
                "COSMIC_MEMORY_EPISODE_EXTRACT_GRAPH",
                False,
            ),
            email_thread_summary_idle_minutes=max(
                15,
                _env_int("EMAIL_THREAD_SUMMARY_IDLE_MINUTES", 360),
            ),
            email_thread_summary_poll_sec=max(
                60,
                _env_int("EMAIL_THREAD_SUMMARY_POLL_SEC", 900),
            ),
            memory_write_max_per_hour=max(
                1,
                _env_int("GATEWAY_MEMORY_WRITE_MAX_PER_HOUR", 50),
            ),
            memory_write_dedup_ttl_sec=max(
                60,
                _env_int("GATEWAY_MEMORY_WRITE_DEDUP_TTL_SEC", 86_400),
            ),
            session_summary_max_output_tokens=max(
                512,
                _env_int("GATEWAY_SESSION_SUMMARY_MAX_OUTPUT_TOKENS", 2500),
            ),
            max_background_tasks_per_session=max(
                1,
                _env_int("GATEWAY_MAX_BACKGROUND_TASKS_PER_SESSION", 5),
            ),
            credentials_db_path=Path(
                os.getenv(
                    "GATEWAY_CREDENTIALS_DB_PATH",
                    str(BACKEND_ROOT / "gateway" / "credentials.db"),
                )
            ).expanduser(),
            google_client_id=os.getenv("GOOGLE_CLIENT_ID", "").strip(),
            google_client_secret=os.getenv("GOOGLE_CLIENT_SECRET", "").strip(),
            google_redirect_uri=os.getenv(
                "GOOGLE_REDIRECT_URI", "http://localhost:8085/"
            ).strip()
            or "http://localhost:8085/",
            github_client_id=os.getenv("GITHUB_CLIENT_ID", "").strip(),
            github_client_secret=os.getenv("GITHUB_CLIENT_SECRET", "").strip(),
            github_redirect_uri=os.getenv(
                "GITHUB_REDIRECT_URI", "http://localhost:8086/"
            ).strip()
            or "http://localhost:8086/",
            github_app_slug=os.getenv("GITHUB_APP_SLUG", "").strip(),
        )
