"""Route registration for the FastAPI application."""

from fastapi import FastAPI

from routes.calls import router as calls_router
from routes.health import router as health_router


def register_routes(app: FastAPI) -> None:
    app.include_router(health_router)
    app.include_router(calls_router)
