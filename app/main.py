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

from fastapi import FastAPI, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from app.services.html_parser_service import analyze_html_code
from app.services.scenario_service import generate_all_scenarios
from app.services.script_service import generate_selenium_script
from app.services.executor_service import execute_script
from app.services.html_server_service import serve_html
from app.config import UPLOAD_DIR, OLLAMA_BASE_URL, REPORTS_DIR

app = FastAPI(title="Test_Auto — HTML → Selenium Pipeline")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(REPORTS_DIR, exist_ok=True)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:5174", "http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── SSE helper ────────────────────────────────────────────────────────────────

def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ── Pipeline SSE ──────────────────────────────────────────────────────────────

async def pipeline_stream(html_code: str):
    try:
        # ── Étape 1 : Parsing HTML ───────────────────────────────────────────
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

        # ── Étape 2 : Génération de TOUS les scénarios ───────────────────────
        yield sse("stage", {"stage": "scenario", "status": "active"})

        all_scenarios = await generate_all_scenarios(analysis)

        yield sse("scenarios_done", {
            "stage": "scenario",
            "status": "done",
            "scenarios": [s.dict() for s in all_scenarios.scenarios],
            "count": len(all_scenarios.scenarios),
        })

        # ── Étape 3 & 4 : Script + Exécution ─────────────────────────────────
        results = []

        async with serve_html(html_code) as target_url:
            yield sse("server_ready", {"url": target_url})

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

                yield sse("exec_done", {
                    "scenario_index": idx,
                    "scenario_title": scenario.title,
                    "execution_report": report.dict(),
                })

                results.append({
                    "scenario": scenario.dict(),
                    "script_code": script.code,
                    "execution_report": report.dict(),
                })

        passed = sum(1 for r in results if r["execution_report"]["success"])
        yield sse("complete", {
            "ok": True,
            "total": len(results),
            "passed": passed,
            "failed": len(results) - passed,
        })

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


@app.post("/analyze")
async def analyze(html_code: str = Form(...)):
    analysis = await analyze_html_code(html_code)
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
    return {"status": "ok", "ollama": OLLAMA_BASE_URL}


# ── Point d'entrée avec watchfiles configuré ──────────────────────────────────
# Lance avec : python -m app.main
# (Ne PAS utiliser uvicorn ... --reload directement si vous avez des problèmes
#  de rechargement ; utilisez plutôt la commande ci-dessous qui exclut reports/)

if __name__ == "__main__":
    import uvicorn

    # Dossiers à exclure du watcher pour éviter que l'écriture des scripts
    # de test dans reports/ redémarre le serveur en plein pipeline.
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