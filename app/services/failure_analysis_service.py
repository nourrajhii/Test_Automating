"""
failure_analysis_service.py
-----------------------------
Agent ⑧ Failure Analysis Agent (voir architecture.md).

Rôle : quand execute_script() échoue, comprendre pourquoi (element
introuvable / sélecteur mort / timing) et proposer une correction avant
d'abandonner définitivement le scénario.

Choix de conception : la cause de loin la plus fréquente d'échec dans ce
pipeline est un selector_hint CSS qui ne correspond plus à rien (page qui
a changé, classe générée dynamiquement, hint halluciné par le LLM de
parsing). La correction la plus fiable n'est PAS de redemander au LLM de
"deviner un meilleur sélecteur" (source même du problème initial), mais
de forcer le même mécanisme de secours déjà éprouvé dans script_service :
le XPath par texte visible (pick_visible + //*[contains(text(), ...)]).
On retente ensuite l'exécution. Si ça échoue encore, on abandonne avec un
message de diagnostic clair plutôt que de boucler indéfiniment.
"""
import asyncio

from app.config import AGENT_MAX_RETRIES_PER_SCENARIO
from app.models.schemas import TestScenario, UIAnalysisResult, GeneratedScript, ExecutionReport
from app.services.script_service import generate_selenium_script
from app.services.executor_service import execute_script


def force_text_fallback(analysis: UIAnalysisResult) -> UIAnalysisResult:
    """
    Retourne une COPIE de l'analyse où tous les selector_hint sont mis à
    "NONE" -> script_service bascule automatiquement sur le XPath par
    texte visible pour chaque élément (voir script_service._is_valid_selector).

    Public : réutilisé par agent_service.py (boucle agentique tool-calling).
    """
    forced_elements = [
        el.copy(update={"selector_hint": "NONE"}) for el in analysis.elements
    ]
    return analysis.copy(update={"elements": forced_elements})


async def run_with_retry(
    scenario: TestScenario,
    analysis: UIAnalysisResult,
    target_url: str,
    on_attempt=None,
) -> tuple[GeneratedScript, ExecutionReport, int]:
    """
    Exécute un scénario avec jusqu'à AGENT_MAX_RETRIES_PER_SCENARIO tentatives.
    Tentative 1 : sélecteurs normaux (CSS/data-* si dispo).
    Tentatives suivantes : fallback XPath-par-texte forcé sur TOUS les éléments.

    on_attempt(attempt_index, script, report) : callback optionnel pour SSE,
    appelé après CHAQUE tentative (même les échecs intermédiaires).

    Retourne (dernier_script, dernier_rapport, nombre_de_tentatives).
    """
    loop = asyncio.get_event_loop()
    last_script = None
    last_report = None

    for attempt in range(1, AGENT_MAX_RETRIES_PER_SCENARIO + 2):  # +2 = 1 essai normal + N retries
        current_analysis = analysis if attempt == 1 else force_text_fallback(analysis)

        script = await generate_selenium_script(scenario, current_analysis, target_url)
        report = await loop.run_in_executor(None, execute_script, script)

        last_script, last_report = script, report

        if on_attempt:
            await on_attempt(attempt, script, report)

        if report.success:
            return script, report, attempt

        if attempt > AGENT_MAX_RETRIES_PER_SCENARIO:
            break

    return last_script, last_report, AGENT_MAX_RETRIES_PER_SCENARIO + 1