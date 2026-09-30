import asyncio
import json
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from fastapi.responses import StreamingResponse

ProgressReporter = Callable[..., Awaitable[None]]
ProgressOperation = Callable[[ProgressReporter], Coroutine[Any, Any, Any]]


def progress_response(operation: ProgressOperation) -> StreamingResponse:
    """Stream verified operation milestones and a final result as server-sent events."""
    async def events():
        queue: asyncio.Queue[dict | None] = asyncio.Queue()
        active_step: dict[str, str] = {"id": "operation", "label": "Process request"}

        async def report(step_id: str, label: str, state: str, detail: str, **extra) -> None:
            if state == "running":
                active_step.update(id=step_id, label=label)
            await queue.put({"type": "step", "id": step_id, "label": label,
                             "state": state, "detail": detail, **extra})

        async def run() -> None:
            try:
                result = await operation(report)
                await queue.put({"type": "result", "result": result.model_dump(mode="json")})
            except Exception as exc:
                await queue.put({"type": "failure", "id": active_step["id"],
                                 "label": active_step["label"], "detail": str(exc)})
            finally:
                await queue.put(None)

        task = asyncio.create_task(run())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(events(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
    })
