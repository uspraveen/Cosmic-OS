from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

from . import maintenance

from .browser.routes import router as browser_router
from .channels.routes import router as channel_router
from .config import GatewayConfig
from .automations.routes import router as automation_router
from .credentials.routes import router as credential_router
from .gmail_routes import router as gmail_router
from .github_routes import router as github_router
from .memory.routes import router as memory_router
from .preferences.routes import router as preferences_router
from .runtime import GatewayRuntime
from .usage.routes import router as usage_router
from .vault.routes import router as vault_router
from .wishlist.routes import router as wishlist_router
from .tool_opportunities.routes import router as tool_opportunities_router
from .prophet.routes import router as prophet_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    runtime = GatewayRuntime(GatewayConfig.from_env())
    _warn_on_open_github_webhooks(runtime.config)
    app.state.gateway_runtime = runtime
    await runtime.start()
    try:
        yield
    finally:
        await runtime.stop()


def _warn_on_open_github_webhooks(config: GatewayConfig) -> None:
    """Surface an unauthenticated /webhooks/github at startup.

    Signature verification is only enforced when a secret is configured, so an
    unset secret on a publicly reachable gateway leaves the connected-repo
    registry writable by anyone who can reach the port. The endpoint stays
    usable for local development; this makes the trade-off visible instead of
    silent.
    """
    if config.github_webhook_secret:
        return
    if not (config.github_app_slug or config.github_client_id):
        return
    logger.warning(
        "gateway.github_webhook_secret_unset slug=%s — /webhooks/github accepts "
        "unsigned requests. Set GITHUB_WEBHOOK_SECRET and configure the same "
        "secret on the GitHub App before exposing this gateway publicly.",
        config.github_app_slug or "(unknown)",
    )


app = FastAPI(
    title="COSMIC Gateway",
    description="Single FastAPI door for COSMIC channel ingress and control-plane routes",
    version="1.0.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def maintenance_drain_middleware(request: Request, call_next):
    """Refuse auto-retried webhook intake while a graceful restart drains.

    Push senders (Gmail Pub/Sub, GitHub, Telegram, WhatsApp, agent-email)
    retry non-2xx on their own, so a 503 here loses nothing -- whereas
    accepting work seconds before a fleet restart would kill it mid-flight.
    Health stays open so the restart helper can wait on it; interactive
    desktop/mobile sends are not drained (no sender retry, seconds-long window).
    """
    if (
        request.method == "POST"
        and maintenance.drain_active()
        and maintenance.request_should_drain(request.url.path)
    ):
        status, body, headers = maintenance.drain_retry_response()
        logger.warning(
            "gateway.maintenance_drain_refused path=%s", request.url.path
        )
        return JSONResponse(status_code=status, content=body, headers=headers)
    return await call_next(request)


app.include_router(browser_router)
app.include_router(channel_router)
app.include_router(automation_router)
app.include_router(credential_router)
app.include_router(gmail_router)
app.include_router(github_router)
app.include_router(memory_router)
app.include_router(preferences_router)
app.include_router(usage_router)
app.include_router(vault_router)
app.include_router(wishlist_router)
app.include_router(tool_opportunities_router)
app.include_router(prophet_router)


@app.get("/health")
async def health(request: Request) -> dict[str, object]:
    runtime: GatewayRuntime = request.app.state.gateway_runtime
    return await runtime.health_payload()


@app.get("/health/ready")
async def health_ready(request: Request) -> dict[str, object]:
    runtime: GatewayRuntime = request.app.state.gateway_runtime
    return await runtime.readiness_payload()


def main() -> int:
    config = GatewayConfig.from_env()
    uvicorn.run(
        "gateway.main:app",
        host=config.host,
        port=config.port,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
