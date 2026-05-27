"""
NeoMarket B2C API — FastAPI application entry-point.

Global error handlers unify 4xx/5xx to {"code", "message", "details?"}.
Canon: b2c-catalog-flows.md | Spec: b2c/openapi.yaml (neomarket-protocols)
"""
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.modules.catalog.router import router as catalog_router
from backend.modules.favorites.router import router as favorites_router
from backend.modules.subscriptions.router import router as subscriptions_router
from backend.modules.cart.router import router as cart_router
from backend.modules.banners.router import router as banners_router
from backend.modules.collections.router import router as collections_router

app = FastAPI(
    title="NeoMarket B2C API",
    description="Buyer-facing API for NeoMarket B2C module",
    version="1.0.0",
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────── Unified error handling ───────────────────────
# spec b2c/openapi.yaml#Error: every 4xx must be {code, message, details?}

_STATUS_CODE_NAMES = {
    400: "INVALID_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
}


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    from fastapi.encoders import jsonable_encoder
    return JSONResponse(
        status_code=422,
        content={
            "code": "VALIDATION_ERROR",
            "message": "Request validation failed",
            "details": {"errors": jsonable_encoder(exc.errors())},
        },
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    detail = exc.detail
    if isinstance(detail, dict) and "code" in detail and "message" in detail:
        body = {"code": detail["code"], "message": detail["message"]}
        if detail.get("details") is not None:
            body["details"] = detail["details"]
    else:
        body = {
            "code": _STATUS_CODE_NAMES.get(exc.status_code, "ERROR"),
            "message": detail if isinstance(detail, str) else str(detail),
        }
    return JSONResponse(
        status_code=exc.status_code,
        content=body,
        headers=getattr(exc, "headers", None),
    )


# ─────────────────────── Routers ───────────────────────
app.include_router(catalog_router)
app.include_router(favorites_router)
app.include_router(subscriptions_router)
app.include_router(cart_router)
app.include_router(banners_router)
app.include_router(collections_router)


# ─────────────────────── Health ───────────────────────
@app.get("/")
async def root():
    return {"message": "NeoMarket B2C API", "version": "1.0.0"}


@app.get("/health")
async def health():
    return {"status": "ok"}
