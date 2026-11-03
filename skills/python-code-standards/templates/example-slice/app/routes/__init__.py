from fastapi import APIRouter

from app.routes import orders


def build_api_router(prefix: str) -> APIRouter:
    """Mount every domain router here; each declares a domain-relative prefix (`/orders`)."""
    router = APIRouter(prefix=prefix)
    router.include_router(orders.router)
    return router
