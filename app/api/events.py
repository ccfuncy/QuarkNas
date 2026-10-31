from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ..runtime import Ctx, get_ctx

router = APIRouter(prefix="/api", tags=["events"])

HEARTBEAT = 15.0


@router.get("/events")
async def events(request: Request, ctx: Ctx = Depends(get_ctx)) -> StreamingResponse:
    queue = ctx.bus.subscribe()

    async def stream():
        try:
            yield "event: ready\ndata: {}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                payload = json.dumps(event.get("data", {}), ensure_ascii=False)
                yield f"event: {event.get('type', 'message')}\ndata: {payload}\n\n"
        finally:
            ctx.bus.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
