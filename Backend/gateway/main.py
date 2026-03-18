from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
import uvicorn

from .channels.routes import router as channel_router
from .config import GatewayConfig
from .memory.routes import router as memory_router
from .runtime import GatewayRuntime
from .usage.routes import router as usage_router
from .wishlist.routes import router as wishlist_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    runtime = GatewayRuntime(GatewayConfig.from_env())
    app.state.gateway_runtime = runtime
    await runtime.start()
    try:
        yield
    finally:
        await runtime.stop()


app = FastAPI(
    title="COSMIC Gateway",
    description="Single FastAPI door for COSMIC channel ingress and control-plane routes",
    version="1.0.0",
    lifespan=lifespan,
)
app.include_router(channel_router)
app.include_router(memory_router)
app.include_router(usage_router)
app.include_router(wishlist_router)


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
