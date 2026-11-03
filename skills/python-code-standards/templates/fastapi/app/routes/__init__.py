from fastapi import APIRouter


def build_api_router(prefix: str) -> APIRouter:
    """Mount every domain router here; each declares a domain-relative prefix (`/orders`)."""
    return APIRouter(prefix=prefix)
