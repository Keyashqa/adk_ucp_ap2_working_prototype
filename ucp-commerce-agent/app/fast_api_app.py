import os
from pathlib import Path

from app.config import GEMINI_API_KEY, MODEL_NAME
from app.db import init_db

# Initialize DB at module-load time — must happen before get_fast_api_app()
# because ADK imports agent.py → keys.py which queries the DB.
# @app.on_event("startup") is NOT used because the ADK ASGI wrapper intercepts
# the lifespan scope and the handler never fires.
init_db()

# Set Gemini API key before ADK initialises — it reads GOOGLE_API_KEY at import
if GEMINI_API_KEY:
    os.environ["GOOGLE_API_KEY"] = GEMINI_API_KEY
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "False"

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse, RedirectResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from google.adk.cli.fast_api import get_fast_api_app  # noqa: E402

from app.auth import router as auth_router  # noqa: E402

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=True,
    allow_origins=["*"],
    default_llm_model=MODEL_NAME,
)

app.title = "ucp-commerce-agent"
app.description = "UCP/AP2 cinema booking agent with user auth and wallet"

# CORS — allow the React dev server (port 5173) and same origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Auth + wallet routes
app.include_router(auth_router)

# Serve React chat UI build if it exists
_DIST = Path(__file__).parent.parent / "frontend" / "dist"
if _DIST.exists():
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/ui", include_in_schema=False)
    @app.get("/login", include_in_schema=False)
    @app.get("/register", include_in_schema=False)
    @app.get("/wallet", include_in_schema=False)
    @app.get("/chat", include_in_schema=False)
    def serve_spa(_path: str = "") -> FileResponse:
        return FileResponse(_DIST / "index.html")

    @app.get("/", include_in_schema=False)
    def root_redirect() -> RedirectResponse:
        return RedirectResponse(url="/login")


@app.post("/feedback")
def collect_feedback(feedback: dict) -> dict:
    return {"status": "success"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
