"""``POST /client-errors`` — optional FE error beacon (docs/02 §7.1, 03 §5).

Logged with its trace id, never persisted (no PII, S-12). The FE drops the
beacon silently when the rate limiter answers 429.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from app.models.api import ClientErrorBody
from app.routers import no_store

log = logging.getLogger(__name__)

router = APIRouter(tags=["ops"])


@router.post("/client-errors")
async def client_errors(body: ClientErrorBody) -> object:
    log.warning(
        "client error beacon code=%s trace_id=%s url=%s message=%s",
        body.code,
        body.trace_id,
        body.url,
        body.message[:200],
    )
    return no_store({"accepted": True}, status_code=202)


__all__ = ["router"]
