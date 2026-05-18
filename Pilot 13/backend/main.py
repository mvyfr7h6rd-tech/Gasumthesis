"""
GASUM Biogas Logistics Decision Support System - FastAPI Backend

This is the main entry point for the backend API server.
Run with: uvicorn backend.main:app --reload
"""

import json
import logging
import sys

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api.routes import router
from .api.routing_routes import routing_router

# Configure logging to show in console
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Create FastAPI app
app = FastAPI(
    title="GASUM Biogas Logistics API",
    description="""
    Human-in-the-loop decision support system for containerized biogas logistics.

    This API provides:
    - Site and container inventory management
    - Risk assessment and prioritization
    - Route recommendations with VRP optimization
    - Approval workflow for recommendations

    All operational data can be updated via JSON file uploads.
    """,
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# Configure CORS for frontend access
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:5173",
        "http://localhost:5174",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5174",
        "null",
    ],
    # Desktop shells often use a custom app://-style origin instead of localhost.
    allow_origin_regex=r"^(app|file|tauri|capacitor-electron|electron)://.*$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API routes
app.include_router(router, prefix="/api")
app.include_router(routing_router, prefix="/api")


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Log the raw request body when Pydantic validation fails (422)."""
    body = exc.body
    logger.error(f"[Pydantic 422] {request.method} {request.url.path}")
    logger.error(f"[Pydantic 422] Body: {json.dumps(body, indent=2, default=str) if body else '<empty>'}")
    logger.error(f"[Pydantic 422] Errors: {json.dumps(exc.errors(), indent=2, default=str)}")
    # exc.errors() may contain non-serializable objects (e.g. ValueError in ctx['error']).
    # Round-trip through json with default=str to coerce them to strings before JSONResponse.
    safe_errors = json.loads(json.dumps(exc.errors(), default=str))
    return JSONResponse(status_code=422, content={"detail": safe_errors})


@app.get("/")
async def root():
    """Root endpoint with API information."""
    return {
        "name": "GASUM Biogas Logistics API",
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
