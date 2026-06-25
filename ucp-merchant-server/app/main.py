"""Merchant server FastAPI application."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.catalog import seed_db
from app.db import init_db
from app.mandate_router import router as mandate_router
from app.mcp_router import router as mcp_router
from app.ucp_router import router as ucp_router

app = FastAPI(title="UCP Cinema Merchant Server", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ucp_router)
app.include_router(mcp_router)
app.include_router(mandate_router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "ucp-merchant-server"}


# Serve React dashboard build if it exists (must be registered last — catch-all)
_DIST = Path(__file__).parent.parent / "frontend" / "dist"
if _DIST.exists():
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_spa(full_path: str) -> FileResponse:
        return FileResponse(_DIST / "index.html")


@app.on_event("startup")
def startup() -> None:
    init_db()
    seed_db()
