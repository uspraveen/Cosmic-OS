from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
import uvicorn

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
from .wishlist.routes import router as wishlist_router
from .tool_opportunities.routes import router as tool_opportunities_router

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
app.include_router(channel_router)
app.include_router(automation_router)
app.include_router(credential_router)
app.include_router(gmail_router)
app.include_router(github_router)
app.include_router(memory_router)
app.include_router(preferences_router)
app.include_router(usage_router)
app.include_router(wishlist_router)
app.include_router(tool_opportunities_router)


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
