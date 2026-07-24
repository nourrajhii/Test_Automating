"""
main.py
-------
FastAPI — pipeline de test automatisé pour interfaces web.
Entrée : code HTML / JS / JSX (texte brut)
Sortie : tous les scénarios + scripts Selenium via SSE

FIX WatchFiles : uvicorn --reload surveille tout le projet par défaut,
y compris le dossier reports/ où les scripts Selenium sont écrits pendant
l'exécution → rechargement du serveur en plein milieu du pipeline →
mini-serveur HTTP coupé → TimeoutException sur Selenium.
On configure watchfiles pour ignorer reports/ et uploads/.
"""
import asyncio
import json
import os

from fastapi import FastAPI, Form, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.services.html_parser_service import analyze_html_code
from app.services.vision_parser_service import analyze_screenshot, ImageTooLargeError
from app.services.live_dom_service import fetch_rendered_page, LiveFetchError
from app.services.scenario_service import generate_all_scenarios
from app.services.script_service import generate_selenium_script
from app.services.executor_service import execute_script
from app.services.failure_explainer_service import explain_failure, build_agent_report
from app.services.html_server_service import serve_html
from app.services.agent_service import run_agent
from app.config import UPLOAD_DIR, OLLAMA_BASE_URL, REPORTS_DIR, VISION_MODEL, SCREENSHOTS_DIR

# ── App ──────────────────────────────────────────────────────────────────────
app = FastAPI(title="Test_Auto — HTML → Selenium Pipeline")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(REPORTS_DIR, exist_ok=True)
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

# Sert les captures d'écran prises par les scripts Selenium générés
# (script_service.SCREENSHOTS_DIR) pour que le frontend puisse les afficher
# via <img src={`${API_BASE}/screenshots/xxx.png`}>.
app.mount("/screenshots", StaticFiles(directory=SCREENSHOTS_DIR), name="screenshots")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:5174", "http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routeur du pipeline orchestré LangGraph ──────────────────────────────────
# DOIT être importé/inclus APRÈS la création de `app` ci-dessus, sinon
# "app is not defined". graph_routes.py expose /generate-stream-graph et
# /generate-stream-graph-vision.
from app.graph_routes import router as graph_router
app.include_router(graph_router)


# ── SSE helper ────────────────────────────────────────────────────────────────

def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _execution_summary(results: list[dict]) -> dict:
    """
    Résumé global façon "EXECUTION SUMMARY" (voir report_service.py),
    calculé à partir de la liste de résultats accumulés pendant le stream
    et renvoyé dans l'événement `complete` pour que le frontend puisse
    afficher le même résumé sans dupliquer la logique.
    """
    total = len(results)
    passed = sum(1 for r in results if r and r["execution_report"]["success"])
    failed = total - passed
    coverage = round((passed / total * 100), 1) if total else 0.0
    total_time = sum((r["execution_report"].get("execution_time") or 0) for r in results if r)
    screenshots = sum(
        1 for r in results
        if r and r["execution_report"].get("screenshot_path")
        and os.path.isfile(r["execution_report"]["screenshot_path"])
    )
    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "coverage": coverage,
        "total_time": round(total_time, 2),
        "screenshots": screenshots,
    }


# ── Pipeline SSE (mode HTML, sans agent) ───────────────────────────────────────

async def pipeline_stream(html_code: str):
    try:
        yield sse("stage", {"stage": "parse", "status": "active"})

        analysis = await analyze_html_code(html_code)

        if not analysis.elements:
            yield sse("error", {
                "message": "Aucun élément interactif détecté dans le code.",
                "hint": "Vérifiez que votre code contient des balises input, button, a, select, etc.",
            })
            return

        yield sse("parse_done", {
            "stage": "parse",
            "status": "done",
            "analysis": {
                "elements": [e.dict() for e in analysis.elements],
                "raw_description": analysis.raw_description,
                "page_type": analysis.page_type,
            },
        })

        yield sse("stage", {"stage": "scenario", "status": "active"})

        all_scenarios = await generate_all_scenarios(analysis)

        yield sse("scenarios_done", {
            "stage": "scenario",
            "status": "done",
            "scenarios": [s.dict() for s in all_scenarios.scenarios],
            "count": len(all_scenarios.scenarios),
        })

        scenarios = all_scenarios.scenarios
        results: list[dict | None] = [None] * len(scenarios)
        event_queue: asyncio.Queue = asyncio.Queue()
        sem = asyncio.Semaphore(3)  # 3 Chrome en parallèle max — ajuste selon ton PC

        async with serve_html(html_code) as target_url:
            yield sse("server_ready", {"url": target_url})

            async def run_one(idx: int, scenario):
                async with sem:
                    await event_queue.put(("script_start", {
                        "scenario_index": idx, "scenario_title": scenario.title,
                    }))

                    script = await generate_selenium_script(scenario, analysis, target_url)

                    await event_queue.put(("script_done", {
                        "scenario_index": idx, "scenario_title": scenario.title,
                        "script_code": script.code,
                    }))
                    await event_queue.put(("exec_start", {"scenario_index": idx}))

                    loop = asyncio.get_event_loop()
                    report = await loop.run_in_executor(None, execute_script, script)

                    # Diagnostic IA (cause probable + patch de code suggéré),
                    # voir failure_explainer_service.py. Toujours calculé —
                    # explain_failure() renvoie has_failures=False si le
                    # scénario a réussi, donc rien ne s'affiche côté frontend
                    # dans ce cas (voir App.jsx).
                    diagnosis = explain_failure(scenario.title, report.dict())
                    agent_report = build_agent_report(scenario.title, report.dict(), diagnosis)

                    await event_queue.put(("exec_done", {
                        "scenario_index": idx, "scenario_title": scenario.title,
                        "execution_report": report.dict(),
                        "diagnosis": diagnosis,
                        "agent_report": agent_report,
                    }))

                    results[idx] = {
                        "scenario": scenario.dict(),
                        "script_code": script.code,
                        "execution_report": report.dict(),
                    }

            tasks = [asyncio.create_task(run_one(i, s)) for i, s in enumerate(scenarios)]

            async def _wait_all_then_close():
                await asyncio.gather(*tasks)
                await event_queue.put(None)

            closer = asyncio.create_task(_wait_all_then_close())

            while True:
                item = await event_queue.get()
                if item is None:
                    break
                event_name, data = item
                yield sse(event_name, data)

            await closer

        yield sse("complete", {"ok": True, **_execution_summary(results)})

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(tb)
        yield sse("error", {"message": str(e), "traceback": tb})


# ── Pipeline SSE — mode capture d'écran ───────────────────────────────────────

async def pipeline_stream_vision(image_bytes: bytes, target_url: str | None):
    try:
        yield sse("stage", {"stage": "parse", "status": "active"})

        try:
            analysis = await analyze_screenshot(image_bytes)
        except ImageTooLargeError as e:
            yield sse("error", {"message": str(e)})
            return

        if not analysis.elements:
            yield sse("error", {
                "message": "Aucun élément interactif détecté dans la capture d'écran.",
                "hint": (
                    f"Vérifiez la qualité/résolution de l'image, ou que le modèle "
                    f"vision '{VISION_MODEL}' est bien installé (ollama pull {VISION_MODEL}). "
                    "Un modèle plus grand (llava:13b, llama3.2-vision) lit mieux le texte "
                    "qu'un petit modèle 7b."
                ),
            })
            return

        yield sse("parse_done", {
            "stage": "parse",
            "status": "done",
            "source": "vision",
            "analysis": {
                "elements": [e.dict() for e in analysis.elements],
                "raw_description": analysis.raw_description,
                "page_type": analysis.page_type,
            },
        })

        yield sse("stage", {"stage": "scenario", "status": "active"})
        all_scenarios = await generate_all_scenarios(analysis)

        yield sse("scenarios_done", {
            "stage": "scenario",
            "status": "done",
            "scenarios": [s.dict() for s in all_scenarios.scenarios],
            "count": len(all_scenarios.scenarios),
        })

        if not target_url or not target_url.strip():
            yield sse("complete", {
                "ok": True,
                "total": len(all_scenarios.scenarios),
                "passed": 0,
                "failed": 0,
                "note": "Scénarios générés depuis la capture d'écran (aucune URL fournie, pas d'exécution).",
            })
            return

        yield sse("server_ready", {"url": target_url})

        results = []
        for idx, scenario in enumerate(all_scenarios.scenarios):
            yield sse("script_start", {
                "scenario_index": idx,
                "scenario_title": scenario.title,
            })

            script = await generate_selenium_script(scenario, analysis, target_url)

            yield sse("script_done", {
                "scenario_index": idx,
                "scenario_title": scenario.title,
                "script_code": script.code,
            })

            yield sse("exec_start", {"scenario_index": idx})

            loop = asyncio.get_event_loop()
            report = await loop.run_in_executor(None, execute_script, script)

            diagnosis = explain_failure(scenario.title, report.dict())
            agent_report = build_agent_report(scenario.title, report.dict(), diagnosis)

            yield sse("exec_done", {
                "scenario_index": idx,
                "scenario_title": scenario.title,
                "execution_report": report.dict(),
                "diagnosis": diagnosis,
                "agent_report": agent_report,
            })

            results.append({
                "scenario": scenario.dict(),
                "script_code": script.code,
                "execution_report": report.dict(),
            })

        yield sse("complete", {"ok": True, **_execution_summary(results)})

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(tb)
        yield sse("error", {"message": str(e), "traceback": tb})


# ── Pipeline SSE — mode AGENT (tool-calling llama3.1:8b) ──────────────────────
# Nécessite qu'une page réelle soit servie (serve_html) puisque l'agent
# exécute et ré-exécute les scripts Selenium contre une vraie URL.

async def agent_stream(html_code: str):
    try:
        yield sse("stage", {"stage": "parse", "status": "active"})

        analysis = await analyze_html_code(html_code)
        if not analysis.elements:
            yield sse("error", {"message": "Aucun élément interactif détecté."})
            return

        yield sse("parse_done", {
            "stage": "parse",
            "status": "done",
            "analysis": {
                "elements": [e.dict() for e in analysis.elements],
                "page_type": analysis.page_type,
            },
        })

        events: asyncio.Queue = asyncio.Queue()

        async with serve_html(html_code) as target_url:
            yield sse("server_ready", {"url": target_url})

            agent_task = asyncio.create_task(run_agent(analysis, target_url, events))

            while True:
                event_type, idx, payload = await events.get()
                yield sse(event_type, {"scenario_index": idx, "payload": payload})
                if event_type == "agent_done":
                    break

            await agent_task

        yield sse("complete", {"ok": True})

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(tb)
        yield sse("error", {"message": str(e), "traceback": tb})


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/generate-stream")
async def generate_stream(html_code: str = Form(...)):
    return StreamingResponse(
        pipeline_stream(html_code),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/generate-stream-agent")
async def generate_stream_agent(html_code: str = Form(...)):
    return StreamingResponse(
        agent_stream(html_code),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/analyze")
async def analyze(html_code: str = Form(...)):
    analysis = await analyze_html_code(html_code)
    return analysis.dict()


@app.post("/generate-stream-vision")
async def generate_stream_vision(
    image: UploadFile = File(...),
    target_url: str | None = Form(None),
):
    image_bytes = await image.read()
    return StreamingResponse(
        pipeline_stream_vision(image_bytes, target_url),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/analyze-vision")
async def analyze_vision(image: UploadFile = File(...)):
    image_bytes = await image.read()
    try:
        analysis = await analyze_screenshot(image_bytes)
    except ImageTooLargeError as e:
        return JSONResponse({"error": str(e)}, status_code=413)
    return analysis.dict()


@app.post("/analyze-url")
async def analyze_url(target_url: str = Form(...)):
    try:
        html, _ = await fetch_rendered_page(target_url)
    except LiveFetchError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    analysis = await analyze_html_code(html)
    return analysis.dict()


@app.post("/generate-all")
async def generate_all(html_code: str = Form(...)):
    try:
        analysis = await analyze_html_code(html_code)
        if not analysis.elements:
            return JSONResponse({"error": "Aucun élément UI détecté"}, status_code=422)

        all_scenarios = await generate_all_scenarios(analysis)
        results = []

        async with serve_html(html_code) as target_url:
            for scenario in all_scenarios.scenarios:
                script = await generate_selenium_script(scenario, analysis, target_url)
                report = execute_script(script)
                results.append({
                    "scenario": scenario.dict(),
                    "script_code": script.code,
                    "execution_report": report.dict(),
                })

        return JSONResponse({
            "analysis": analysis.dict(),
            "results": results,
        })

    except Exception as e:
        import traceback
        return JSONResponse(
            {"error": str(e), "traceback": traceback.format_exc()},
            status_code=500,
        )


@app.get("/health")
async def health():
    return {"status": "ok", "ollama": OLLAMA_BASE_URL, "vision_model": VISION_MODEL}


# ── Point d'entrée avec watchfiles configuré ──────────────────────────────────
# Lance avec : python -m app.main

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        reload_excludes=[
            f"{REPORTS_DIR}/*",
            f"{UPLOAD_DIR}/*",
            "*.pyc",
            "__pycache__/*",
        ],
    )