from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import os

app = FastAPI(
    title="SEBI Circular Q&A Assistant API",
    description="Educational API for querying official SEBI circulars with supersession awareness",
    version="0.1.0",
)

# Configure CORS for local development
frontend_origin = os.getenv("FRONTEND_ORIGIN", "http://localhost:5173")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[frontend_origin, "http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


from app.api.v1.query import router as query_router

app.include_router(query_router)


@app.get("/health")
def get_health() -> dict:
    """Basic health check endpoint."""
    return {"status": "ok"}
