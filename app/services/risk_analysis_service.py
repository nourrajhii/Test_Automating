"""
risk_analysis_service.py
---------------------------
Risk Analysis Agent : enrichit les features pour lesquelles
test_planning_service._RULES ne connaît AUCUN pattern, en leur proposant
des cas de test à risque (LLM) — champs invalides, permissions, timeout,
session expirée, sécurité, limites...

POURQUOI CET AGENT EST SÉPARÉ DE _RULES, PAS UN REMPLACEMENT
-----------------------------------------------------------------
test_planning_service._RULES couvre déjà, gratuitement (sans appel LLM),
quelques patterns fréquents et fiables (login, register, search, upload,
payment, form). Cet agent ne les redemande JAMAIS à un LLM — inutile et
plus lent, la règle déterministe reste prioritaire et suffisante. Il
n'intervient QUE pour les features qui ne matchent aucune règle connue
(ex: "virement" dans une appli bancaire, "assigner un dossier" dans un
ERP) : sans lui, ces features n'auraient QUE le cas nominal, quel que
soit leur enjeu métier réel — c'est exactement le trou identifié dans
l'analyse d'architecture précédente.

Défensif : si le LLM échoue ou ne renvoie rien d'exploitable, la feature
garde simplement son cas nominal (comportement identique à AVANT cet
agent) — jamais de blocage du pipeline. Voir test_planning_service.py
pour l'appel (parallélisé via SCENARIO_GEN_CONCURRENCY, même politique
que le reste du projet).
"""
from __future__ import annotations

import httpx
import json
import logging
import re

logger = logging.getLogger("risk_analysis")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[risk_analysis] %(message)s"))
    logger.addHandler(_h)


RISK_CATEGORIES_HINT = (
    "champs vides, données invalides, caractères spéciaux, doublons, accès "
    "interdit, permissions, erreurs réseau, timeout, session expirée, "
    "liens cassés, sécurité (XSS, injection SQL), upload invalide, limites"
)

RISK_ANALYSIS_PROMPT = """You are a senior QA risk analyst. Below is a
feature already identified on this application. It does NOT match any
standard test pattern (it is not a login, registration, search, upload
or payment form) — you must reason about it specifically.

Feature: {feature_name}
Description: {feature_description}
Business capability: {business_capability}

YOUR TASK:
Propose the RISK-BASED test cases relevant to THIS SPECIFIC feature (not
generic boilerplate) — think about what could realistically go wrong for
a feature like this one, drawing from categories such as: {risk_hint}.
Propose 1 to 4 cases, ONLY those that genuinely make sense for this
feature — do not force irrelevant ones (e.g. no file-upload risk for a
feature with no file input).

Respond ONLY with valid JSON, no markdown, no explanation:
{{
  "test_cases": [
    {{"type": "<short_snake_case_type>", "focus": "<1 phrase, EN FRANCAIS>"}}
  ]
}}
"""


async def _call_llm(prompt: str) -> dict:
    from app.config import OLLAMA_BASE_URL, TEXT_MODEL

    payload = {
        "model": TEXT_MODEL,
        "prompt": prompt,
        "stream": True,
        "keep_alive": "5m",
        "format": "json",
        "options": {"temperature": 0.3, "num_predict": 500, "num_ctx": 2048},
    }
    full = ""
    timeout = httpx.Timeout(timeout=60.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", f"{OLLAMA_BASE_URL}/api/generate", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                try:
                    chunk = json.loads(line)
                    full += chunk.get("response", "")
                    if chunk.get("done"):
                        break
                except Exception:
                    continue
    try:
        return json.loads(full.strip())
    except Exception:
        match = re.search(r"\{[\s\S]*\}", full)
        return json.loads(match.group()) if match else {}


async def analyze_risks(feature: dict) -> list[dict]:
    """
    Point d'entrée, appelé par test_planning_service.build_test_plan
    UNIQUEMENT pour les features que _RULES n'a pas su classer (voir
    docstring ci-dessus). Retourne une liste de cas au MÊME FORMAT que
    _RULES (`[{"type": ..., "focus": ...}]`), directement fusionnable
    dans le plan de test.
    """
    name = (feature.get("name") or "").strip()
    if not name:
        return []

    prompt = RISK_ANALYSIS_PROMPT.format(
        feature_name=name,
        feature_description=feature.get("description", ""),
        business_capability=feature.get("business_capability") or "non rattachée",
        risk_hint=RISK_CATEGORIES_HINT,
    )

    try:
        data = await _call_llm(prompt)
    except Exception as e:
        logger.warning(
            "Risk Analysis Agent indisponible pour la feature '%s' (%s: %s) -> "
            "aucun cas de risque ajouté, seul le cas nominal est conservé.",
            name, type(e).__name__, e,
        )
        return []

    cases = data.get("test_cases") or []
    if not cases:
        logger.warning(
            "Le Risk Analysis Agent n'a renvoyé aucun 'test_cases' exploitable "
            "pour la feature '%s'. JSON reçu : %r", name, data,
        )
    return [
        {"type": (c.get("type") or "risque").strip(), "focus": c.get("focus", "").strip()}
        for c in cases
        if isinstance(c, dict) and (c.get("focus") or "").strip()
    ]
