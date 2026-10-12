"""
API v1 package exports.
"""
from app.api.v1.query import router as query_router
from app.api.v1.admin import router as admin_router

__all__ = ["query_router", "admin_router"]
