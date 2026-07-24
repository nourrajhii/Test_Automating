"""
graph_routes.py
-----------------
Endpoints SSE pour le pipeline orchestré par LangGraph (orchestrator_service).

Intégration dans main.py (3 lignes à ajouter) :

    from app.graph_routes import router as graph_router
    app.include_router(graph_router)

Pont SSE : orchestrator_service.run_pipeline() attend un callback `emit`
async ; ici on lui donne un emit qui pousse dans une asyncio.Queue, et on
lance run_pipeline() en tâche de fond pendant que le générateur SSE lit
la queue — ça donne le même niveau de granularité que pipeline_stream()
dans main.py (un event par étape/scénario), piloté cette fois par le graphe.
"""
import asyncio
import json

from fastapi import APIRouter, Form, UploadFile, File
from fastapi.responses import StreamingResponse

from app.services.orchestrator_service import run_pipeline
from app.services.html_server_service import serve_html
from app.services.vision_parser_service import ImageTooLargeError

router = APIRouter()

_SENTINEL = object()


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _stream_from_queue(queue: "asyncio.Queue"):
    while True:
        item = await queue.get()
        if item is _SENTINEL:
            break
        event, data = item
        yield sse(event, data)


def _make_emit(queue: "asyncio.Queue"):
    async def emit(event: str, data: dict):
        await queue.put((event, data))
    return emit


@router.post("/generate-stream-graph")
async def generate_stream_graph(html_code: str = Form(...)):
    """Équivalent LangGraph de /generate-stream (mode HTML)."""
    queue: asyncio.Queue = asyncio.Queue()
    emit = _make_emit(queue)

    async def runner():
        try:
            async with serve_html(html_code) as target_url:
                await run_pipeline(
                    input_type="html",
                    html_code=html_code,
                    target_url=target_url,
                    emit=emit,
                )
        except Exception as e:
            import traceback
            await emit("error", {"message": str(e), "traceback": traceback.format_exc()})
        finally:
            await queue.put(_SENTINEL)

    asyncio.create_task(runner())

    return StreamingResponse(
        _stream_from_queue(queue),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/generate-stream-graph-vision")
async def generate_stream_graph_vision(
    image: UploadFile = File(...),
    target_url: str | None = Form(None),
):
    """Équivalent LangGraph de /generate-stream-vision (mode screenshot)."""
    image_bytes = await image.read()
    queue: asyncio.Queue = asyncio.Queue()
    emit = _make_emit(queue)

    async def runner():
        try:
            await run_pipeline(
                input_type="vision",
                image_bytes=image_bytes,
                target_url=target_url,
                emit=emit,
            )
        except ImageTooLargeError as e:
            await emit("error", {"message": str(e)})
        except Exception as e:
            import traceback
            await emit("error", {"message": str(e), "traceback": traceback.format_exc()})
        finally:
            await queue.put(_SENTINEL)

    asyncio.create_task(runner())

    return StreamingResponse(
        _stream_from_queue(queue),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )