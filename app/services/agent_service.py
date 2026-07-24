"""
agent_service.py
------------------
Agent ⑥+⑦+⑧ (Selenium Generation + Execution + Failure Analysis), version
agentique : au lieu d'un simple retry déterministe (failure_analysis_service),
c'est le LLM AGENT_MODEL (llama3.1:8b, tool-calling natif) qui DÉCIDE, après
chaque échec, s'il faut réessayer avec le fallback XPath-par-texte ou
abandonner le scénario — dans la limite de AGENT_MAX_RETRIES_PER_SCENARIO
par scénario et AGENT_MAX_TURNS au total (garde-fou anti-boucle-infinie).

CE FICHIER NE CONTIENT AUCUNE ROUTE FASTAPI. Il expose une seule fonction :

    run_agent(analysis, target_url, events: asyncio.Queue) -> None

qui pousse des tuples (event_type, scenario_index, payload) dans `events`
au fur et à mesure, et termine TOUJOURS par un événement "agent_done"
(sinon la boucle `while True` côté main.py ne se termine jamais).

Outils exposés au LLM (function-calling Ollama, format OpenAI-like) :
  - retry_with_text_fallback : régénère le script en forçant le XPath par
    texte visible sur tous les éléments (le fallback déjà éprouvé de
    script_service), puis ré-exécute.
  - give_up_on_scenario : abandonne proprement ce scénario avec une raison.

Si le modèle ne renvoie pas un tool_call exploitable (JSON invalide, modèle
non installé...), on abandonne le scénario par défaut plutôt que de boucler
indéfiniment — un agent qui échoue à décider ne doit jamais bloquer le pipeline.
"""
import asyncio
import json

import httpx

from app.config import OLLAMA_BASE_URL, AGENT_MODEL, AGENT_MAX_TURNS, AGENT_MAX_RETRIES_PER_SCENARIO
from app.models.schemas import UIAnalysisResult, TestScenario
from app.services.scenario_service import generate_all_scenarios
from app.services.script_service import generate_selenium_script
from app.services.executor_service import execute_script
from app.services.failure_analysis_service import force_text_fallback


# ─────────────────────────────────────────────────────────────────────────────
# Définition des outils (function-calling) exposés au LLM
# ─────────────────────────────────────────────────────────────────────────────

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "retry_with_text_fallback",
            "description": (
                "Regenerate the Selenium script forcing all elements to use "
                "XPath-by-visible-text selectors instead of CSS selectors, "
                "then re-execute it. Use this when the failure looks like a "
                "dead/wrong CSS selector (NoSuchElementException, "
                "TimeoutException while waiting for an element)."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "give_up_on_scenario",
            "description": (
                "Abandon this scenario and move on. Use this when the "
                "failure is not selector-related (e.g. assertion failure, "
                "unexpected page behavior) or after a retry already failed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "Short reason for giving up"}
                },
                "required": ["reason"],
            },
        },
    },
]


def _decision_prompt(scenario: TestScenario, error: str, attempt: int) -> list[dict]:
    return [
        {
            "role": "system",
            "content": (
                "You are a test-automation triage agent. A Selenium test just "
                "failed. Decide the next action by calling exactly one tool. "
                "Prefer retry_with_text_fallback only if the error suggests a "
                "broken selector. Otherwise call give_up_on_scenario."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Scenario: {scenario.title}\n"
                f"Attempt number: {attempt}\n"
                f"Error:\n{error[:800]}"
            ),
        },
    ]


async def _ask_agent_decision(scenario: TestScenario, error: str, attempt: int) -> tuple[str, dict]:
    """
    Appelle AGENT_MODEL avec tool-calling pour décider de l'action suivante.
    Retourne (tool_name, arguments). Par défaut : ("give_up_on_scenario", {...})
    si le modèle ne répond pas correctement (jamais de boucle infinie silencieuse).
    """
    payload = {
        "model": AGENT_MODEL,
        "messages": _decision_prompt(scenario, error, attempt),
        "tools": _TOOLS,
        "stream": False,
        "options": {"temperature": 0.1},
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            resp = await client.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()

        tool_calls = (data.get("message") or {}).get("tool_calls") or []
        if not tool_calls:
            return "give_up_on_scenario", {"reason": "Agent n'a proposé aucun outil (réponse texte libre)."}

        call = tool_calls[0]["function"]
        name = call.get("name", "give_up_on_scenario")
        args = call.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        return name, args

    except Exception as e:
        return "give_up_on_scenario", {"reason": f"Agent injoignable ({type(e).__name__}: {e})"}


# ─────────────────────────────────────────────────────────────────────────────
# Boucle par scénario
# ─────────────────────────────────────────────────────────────────────────────

async def _run_scenario_with_agent(
    idx: int,
    scenario: TestScenario,
    analysis: UIAnalysisResult,
    target_url: str,
    events: asyncio.Queue,
    turns_budget: list[int],   # mutable, partagé entre scénarios (AGENT_MAX_TURNS global)
) -> dict:
    loop = asyncio.get_event_loop()

    await events.put(("script_start", idx, {"scenario_title": scenario.title}))

    current_analysis = analysis
    attempt = 1
    script = await generate_selenium_script(scenario, current_analysis, target_url)
    report = await loop.run_in_executor(None, execute_script, script)

    await events.put(("script_done", idx, {"scenario_title": scenario.title, "script_code": script.code}))
    await events.put(("exec_done", idx, {"attempt": attempt, "execution_report": report.dict()}))

    while not report.success and attempt <= AGENT_MAX_RETRIES_PER_SCENARIO and turns_budget[0] > 0:
        turns_budget[0] -= 1

        tool_name, args = await _ask_agent_decision(scenario, report.error or "\n".join(report.logs[-10:]), attempt)
        await events.put(("agent_decision", idx, {"tool": tool_name, "arguments": args}))

        if tool_name != "retry_with_text_fallback":
            await events.put(("give_up", idx, {"reason": args.get("reason", "Agent a choisi d'abandonner.")}))
            break

        attempt += 1
        current_analysis = force_text_fallback(analysis)
        script = await generate_selenium_script(scenario, current_analysis, target_url)
        report = await loop.run_in_executor(None, execute_script, script)

        await events.put(("script_done", idx, {"scenario_title": scenario.title, "script_code": script.code}))
        await events.put(("exec_done", idx, {"attempt": attempt, "execution_report": report.dict()}))

    return {
        "scenario": scenario.dict(),
        "script_code": script.code,
        "execution_report": report.dict(),
        "attempts": attempt,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Point d'entrée
# ─────────────────────────────────────────────────────────────────────────────

async def run_agent(analysis: UIAnalysisResult, target_url: str, events: asyncio.Queue) -> None:
    """
    Génère les scénarios puis exécute chacun avec la boucle agentique
    ci-dessus. Termine TOUJOURS par un événement ("agent_done", -1, payload),
    même en cas d'erreur — sinon main.py reste bloqué indéfiniment sur
    `await events.get()`.
    """
    try:
        all_scenarios = await generate_all_scenarios(analysis)
        scenarios = all_scenarios.scenarios

        if not scenarios:
            await events.put(("agent_done", -1, {"total": 0, "passed": 0, "failed": 0}))
            return

        turns_budget = [AGENT_MAX_TURNS]
        results = []

        for idx, scenario in enumerate(scenarios):
            result = await _run_scenario_with_agent(idx, scenario, analysis, target_url, events, turns_budget)
            results.append(result)

            if turns_budget[0] <= 0:
                await events.put(("agent_warning", idx, {"message": "AGENT_MAX_TURNS atteint, arrêt anticipé."}))
                break

        passed = sum(1 for r in results if r["execution_report"]["success"])
        await events.put(("agent_done", -1, {
            "total": len(results), "passed": passed, "failed": len(results) - passed,
        }))

    except Exception as e:
        import traceback
        await events.put(("error", -1, {"message": str(e), "traceback": traceback.format_exc()}))
        await events.put(("agent_done", -1, {"total": 0, "passed": 0, "failed": 0}))