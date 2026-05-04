"""Tiny BFF for the RAG web UI.

The browser talks only to this service; it forwards calls to the internal
ingestion and query services with `x-api-key` attached so secrets never
ship to the browser.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("web")

INGESTION_URL = os.environ.get("INGESTION_URL", "http://ingestion:8001").rstrip("/")
QUERY_URL = os.environ.get("QUERY_URL", "http://query:8002").rstrip("/")
SERVICE_API_KEY = os.environ.get("SERVICE_API_KEY", "")
HTTP_TIMEOUT = float(os.environ.get("WEB_HTTP_TIMEOUT", "120"))

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="RAG Web UI", version="0.1.0")


def _auth_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"x-api-key": SERVICE_API_KEY}
    if extra:
        headers.update(extra)
    return headers


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/upload")
async def api_upload(
    file: UploadFile = File(...),
    user_id: str | None = Form(default=None),
    collection: str | None = Form(default=None),
) -> JSONResponse:
    content = await file.read()
    files = {"file": (file.filename or "upload.bin", content, file.content_type or "application/octet-stream")}
    data: dict[str, str] = {}
    if user_id:
        data["user_id"] = user_id
    if collection:
        data["collection"] = collection

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        try:
            resp = await client.post(
                f"{INGESTION_URL}/ingest",
                headers=_auth_headers(),
                files=files,
                data=data,
            )
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"ingestion service unreachable: {exc}") from exc

    return JSONResponse(status_code=resp.status_code, content=_safe_json(resp))


@app.get("/api/documents/{document_id}")
async def api_document(document_id: UUID) -> JSONResponse:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        try:
            resp = await client.get(
                f"{INGESTION_URL}/documents/{document_id}",
                headers=_auth_headers(),
            )
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"ingestion service unreachable: {exc}") from exc
    return JSONResponse(status_code=resp.status_code, content=_safe_json(resp))


class QueryBody(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    document_id: str | None = None
    collection: str | None = None
    user_id: str | None = None
    top_k: int | None = Field(default=None, ge=1, le=50)


@app.post("/api/query")
async def api_query(body: QueryBody) -> JSONResponse:
    payload = body.model_dump(exclude_none=True)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        try:
            resp = await client.post(
                f"{QUERY_URL}/query",
                headers=_auth_headers({"content-type": "application/json"}),
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"query service unreachable: {exc}") from exc
    return JSONResponse(status_code=resp.status_code, content=_safe_json(resp))


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return {"error": resp.text or f"upstream returned status {resp.status_code}"}


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
