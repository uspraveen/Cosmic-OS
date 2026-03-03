from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
import uvicorn

from .channels.routes import router as channel_router
from .config import GatewayConfig
from .runtime import GatewayRuntime


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
