"""Diagnostics endpoints. `/_diag/sse` verifies that streaming reaches the browser on Vercel (risk R2)."""
import asyncio
import json
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

router = APIRouter()


def sse_event(event: str, data: dict) -> str:
    """Format one Server-Sent Event frame."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@router.get("/_diag/sse")
async def sse_diag(request: Request) -> StreamingResponse:
    """Emit a few timed ticks so we can see whether events arrive incrementally.

    Hidden (404) in prod unless ENABLE_DIAG_ENDPOINTS is true. Settings are read from the
    runtime holder without opening the database: the endpoint never needs it.
    """
    settings = request.app.state.runtime_holder.settings
    if not settings.diag_endpoints_enabled:
        raise HTTPException(status_code=404)
    ticks, interval = settings.sse_diag_ticks, settings.sse_diag_interval_s

    async def gen() -> AsyncIterator[str]:
        """Yield tick events then a done event."""
        start = time.monotonic()
        for i in range(ticks):
            yield sse_event("tick", {"i": i, "t": round(time.monotonic() - start, 3)})
            await asyncio.sleep(interval)
        yield sse_event("done", {"elapsed": round(time.monotonic() - start, 3)})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
