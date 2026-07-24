"""
orchestrator_service.py
-------------------------
Orchestrateur LangGraph du pipeline multi-agent Test_Auto.

Relie les agents existants (déjà écrits comme services indépendants,
inchangés) + les 3 nouveaux (Test Planning, Failure Analysis, Report) :

  Input Agent            -> html_parser_service / vision_parser_service
  UI Understanding Agent -> (même modules, extraction déjà faite dedans)
  Functional Analysis    -> scenario_service.extract_page_features
  Test Planning Agent     -> test_planning_service.build_test_plan
  Scenario Generation      -> test_planning_service.generate_planned_scenarios
  Selenium Generation +
  Execution + Failure     -> failure_analysis_service.run_with_retry
  Report Agent             -> report_service.build_report

Design de streaming : LangGraph n'expose nativement les mises à jour
qu'après CHAQUE NODE (pas à l'intérieur d'une boucle interne comme
l'exécution scénario par scénario). Pour garder le SSE granulaire déjà
présent dans main.py (script_start, exec_done, etc.), chaque node reçoit
un callback `emit(event, data)` injecté dans le state au démarrage — ce
callback pousse dans une asyncio.Queue lue en parallèle par main.py.
Le graphe reste donc la source de vérité de l'ORDRE des agents, sans
sacrifier la granularité du streaming existant.

Installation : pip install langgraph
"""
from __future__ import annotations
import asyncio
from app.config import MAX_SCENARIOS, EXECUTION_CONCURRENCY
from typing import TypedDict, Callable, Awaitable, Any, Optional

from langgraph.graph import StateGraph, START, END

from app.models.schemas import UIAnalysisResult, TestScenario
from app.services.html_parser_service import analyze_html_code
from app.services.vision_parser_service import analyze_screenshot
from app.services.scenario_service import (
    extract_page_features_and_nav, generate_all_scenarios, nav_scenario_dicts,
)
from app.services.test_planning_service import build_test_plan, plan_summary, generate_planned_scenarios
from app.services.failure_analysis_service import run_with_retry
from app.services.failure_explainer_service import explain_failure, build_agent_report
from app.services.report_service import build_report
from app.config import MAX_SCENARIOS


EmitFn = Callable[[str, dict], Awaitable[None]]


class PipelineState(TypedDict, total=False):
    # ── entrée ──
    input_type: str                 # "html" | "vision"
    html_code: Optional[str]
    image_bytes: Optional[bytes]
    target_url: Optional[str]       # requis pour exécuter Selenium
    emit: EmitFn

    # ── produit par les agents ──
    analysis: UIAnalysisResult
    features_data: dict
    nav_categories: list[dict]
    test_plan: list[dict]
    plan_summary: dict
    scenarios: list[TestScenario]
    results: list[dict]
    report_path: Optional[str]
    error: Optional[str]


async def _noop_emit(event: str, data: dict) -> None:
    return None


# ─────────────────────────────────────────────────────────────────────────────
# ① + ② Input Agent + UI Understanding Agent
# ─────────────────────────────────────────────────────────────────────────────

async def node_input_and_ui_understanding(state: PipelineState) -> PipelineState:
    emit = state.get("emit", _noop_emit)
    await emit("stage", {"stage": "parse", "status": "active"})

    if state["input_type"] == "html":
        analysis = await analyze_html_code(state["html_code"])
    else:
        analysis = await analyze_screenshot(state["image_bytes"])

    if not analysis.elements:
        await emit("error", {"message": "Aucun élément interactif détecté."})
        return {**state, "analysis": analysis, "error": "no_elements"}

    await emit("parse_done", {
        "stage": "parse", "status": "done",
        "analysis": {
            "elements": [e.dict() for e in analysis.elements],
            "raw_description": analysis.raw_description,
            "page_type": analysis.page_type,
        },
    })
    return {**state, "analysis": analysis}


# ─────────────────────────────────────────────────────────────────────────────
# ③ Functional Analysis Agent
# ─────────────────────────────────────────────────────────────────────────────

async def node_functional_analysis(state: PipelineState) -> PipelineState:
    if state.get("error"):
        return state
    emit = state.get("emit", _noop_emit)
    await emit("stage", {"stage": "functional_analysis", "status": "active"})

    features_data, nav_categories = await extract_page_features_and_nav(state["analysis"])

    await emit("functional_analysis_done", {
        "stage": "functional_analysis", "status": "done",
        "page_purpose": features_data.get("page_purpose", ""),
        "features": features_data.get("features", []),
        "domain_type": features_data.get("domain_type", ""),
        "business_capabilities": features_data.get("business_capabilities", []),
        "nav_categories": [c.get("name") for c in nav_categories],
    })
    return {**state, "features_data": features_data, "nav_categories": nav_categories}


# ─────────────────────────────────────────────────────────────────────────────
# ④ Test Planning Agent
# ─────────────────────────────────────────────────────────────────────────────

async def node_test_planning(state: PipelineState) -> PipelineState:
    if state.get("error"):
        return state
    emit = state.get("emit", _noop_emit)
    await emit("stage", {"stage": "test_planning", "status": "active"})

    plan = await build_test_plan(state["features_data"])
    summary = plan_summary(plan)

    await emit("test_planning_done", {
        "stage": "test_planning", "status": "done",
        "summary": summary,
    })
    return {**state, "test_plan": plan, "plan_summary": summary}


# ─────────────────────────────────────────────────────────────────────────────
# ⑤ Scenario Generation Agent (piloté par le plan)
# ─────────────────────────────────────────────────────────────────────────────

async def node_scenario_generation(state: PipelineState) -> PipelineState:
    if state.get("error"):
        return state
    emit = state.get("emit", _noop_emit)
    await emit("stage", {"stage": "scenario", "status": "active"})

    async def on_progress(done: int, total: int):
        await emit("scenario_progress", {"done": done, "total": total})

    scenarios = await generate_planned_scenarios(
        state["test_plan"], state["analysis"],
        state["features_data"].get("page_purpose", ""),
        max_scenarios=MAX_SCENARIOS,
        on_progress=on_progress,
    )

    # Filet de sécurité : si le plan n'a rien produit (LLM en panne...),
    # on retombe sur le générateur simple existant plutôt que de finir vide.
    if not scenarios:
        fallback = await generate_all_scenarios(state["analysis"])
        scenarios = fallback.scenarios

    # FIX PARITÉ (voir extract_page_features_and_nav / nav_scenario_dicts
    # dans scenario_service.py) : les catégories de navigation (mega-menu,
    # sous-menus...) détectées par navigation_discovery_service sont
    # DÉTERMINISTES et ne passent jamais par le LLM de scénarios — sans ce
    # bloc, elles étaient calculées (state["nav_categories"]) puis jamais
    # rattachées à aucun scénario final dans le pipeline LangGraph, alors
    # que generate_all_scenarios() (chemin main.py) les inclut, lui,
    # systématiquement. On les ajoute ici, sans jamais les tronquer par
    # MAX_SCENARIOS (même règle que côté main.py), et on déduplique par
    # titre au cas où le fallback ci-dessus les aurait déjà générées.
    nav_categories = state.get("nav_categories", [])
    if nav_categories:
        existing_titles = {s.title.strip().lower() for s in scenarios}
        for raw in nav_scenario_dicts(nav_categories):
            try:
                nav_scenario = TestScenario(**raw)
            except Exception:
                continue
            if nav_scenario.title.strip().lower() in existing_titles:
                continue
            scenarios.append(nav_scenario)
            existing_titles.add(nav_scenario.title.strip().lower())

    await emit("scenarios_done", {
        "stage": "scenario", "status": "done",
        "scenarios": [s.dict() for s in scenarios],
        "count": len(scenarios),
    })
    return {**state, "scenarios": scenarios}


# ─────────────────────────────────────────────────────────────────────────────
# ⑥ + ⑦ + ⑧ Selenium Generation + Execution + Failure Analysis (avec retry)
# ─────────────────────────────────────────────────────────────────────────────

async def node_execution(state: PipelineState) -> PipelineState:
    if state.get("error"):
        return state
    emit = state.get("emit", _noop_emit)
    target_url = state.get("target_url")

    if not target_url:
        return {**state, "results": []}

    await emit("server_ready", {"url": target_url})

    # FIX PERF : les scénarios étaient exécutés un par un (boucle for
    # séquentielle), chacun avec jusqu'à AGENT_MAX_RETRIES_PER_SCENARIO+1
    # tentatives Selenium (~120s de timeout max par tentative) -> avec
    # MAX_SCENARIOS élevé c'était la plus grosse partie des 40+ minutes.
    # On lance maintenant EXECUTION_CONCURRENCY navigateurs Chrome headless
    # en parallèle (Semaphore) SANS réduire le nombre de scénarios exécutés.
    # execute_script() tourne déjà dans un thread (run_in_executor), donc
    # plusieurs instances Chrome peuvent tourner en même temps sans se
    # bloquer les unes les autres.
    semaphore = asyncio.Semaphore(EXECUTION_CONCURRENCY)
    results: list[dict | None] = [None] * len(state["scenarios"])

    async def _run_one(idx: int, scenario) -> None:
        async with semaphore:
            await emit("script_start", {"scenario_index": idx, "scenario_title": scenario.title})

            async def on_attempt(attempt, script, report, _idx=idx, _title=scenario.title):
                await emit("exec_attempt", {
                    "scenario_index": _idx, "scenario_title": _title,
                    "attempt": attempt, "success": report.success,
                })

            script, report, attempts = await run_with_retry(
                scenario, state["analysis"], target_url, on_attempt=on_attempt,
            )

            diagnosis = explain_failure(scenario.title, report.dict())
            agent_report = build_agent_report(scenario.title, report.dict(), diagnosis)

            await emit("script_done", {
                "scenario_index": idx, "scenario_title": scenario.title, "script_code": script.code,
            })
            await emit("exec_done", {
                "scenario_index": idx, "scenario_title": scenario.title,
                "execution_report": report.dict(), "attempts": attempts,
                "diagnosis": diagnosis,
                "agent_report": agent_report,
            })

        results[idx] = {
            "scenario": scenario.dict(),
            "script_code": script.code,
            "execution_report": report.dict(),
            "attempts": attempts,
        }

    await asyncio.gather(*(
        _run_one(idx, scenario) for idx, scenario in enumerate(state["scenarios"])
    ))

    return {**state, "results": [r for r in results if r is not None]}

# ─────────────────────────────────────────────────────────────────────────────
# ⑨ Report Agent
# ─────────────────────────────────────────────────────────────────────────────

async def node_report(state: PipelineState) -> PipelineState:
    emit = state.get("emit", _noop_emit)
    if state.get("error"):
        return state

    report_path = build_report(
        state["features_data"].get("page_purpose", ""),
        state.get("plan_summary", {}),
        state.get("results", []),
    )

    results = state.get("results", [])
    passed = sum(1 for r in results if r["execution_report"]["success"])

    await emit("complete", {
        "ok": True,
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "report_path": report_path,
    })
    return {**state, "report_path": report_path}


# ─────────────────────────────────────────────────────────────────────────────
# Construction du graphe
# ─────────────────────────────────────────────────────────────────────────────

def build_graph():
    graph = StateGraph(PipelineState)

    graph.add_node("ui_understanding", node_input_and_ui_understanding)
    graph.add_node("functional_analysis", node_functional_analysis)
    graph.add_node("test_planning", node_test_planning)
    graph.add_node("scenario_generation", node_scenario_generation)
    graph.add_node("execution", node_execution)
    graph.add_node("report", node_report)

    graph.add_edge(START, "ui_understanding")
    graph.add_edge("ui_understanding", "functional_analysis")
    graph.add_edge("functional_analysis", "test_planning")
    graph.add_edge("test_planning", "scenario_generation")
    graph.add_edge("scenario_generation", "execution")
    graph.add_edge("execution", "report")
    graph.add_edge("report", END)

    return graph.compile()


_compiled_graph = None


def get_graph():
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = build_graph()
    return _compiled_graph


async def run_pipeline(
    *,
    input_type: str,
    html_code: str | None = None,
    image_bytes: bytes | None = None,
    target_url: str | None = None,
    emit: EmitFn,
) -> PipelineState:
    """
    Point d'entrée unique appelé par main.py. `emit` est le callback SSE
    (voir orchestrator_stream dans main.py pour l'implémentation avec
    asyncio.Queue).
    """
    graph = get_graph()
    initial_state: PipelineState = {
        "input_type": input_type,
        "html_code": html_code,
        "image_bytes": image_bytes,
        "target_url": target_url,
        "emit": emit,
    }
    final_state = await graph.ainvoke(initial_state)
    return final_state