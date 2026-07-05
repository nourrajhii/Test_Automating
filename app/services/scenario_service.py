"""
scenario_service.py
-------------------
Pipeline en 2 étapes :
  1. Le LLM analyse le HTML et identifie les FONCTIONNALITÉS réelles de la page
     (pas les éléments HTML, les actions utilisateur : "traduire du texte", "changer de langue"...)
  2. Le LLM génère un scénario de test par fonctionnalité détectée
"""
import httpx
import json
import re

from app.config import OLLAMA_BASE_URL, TEXT_MODEL, MAX_SCENARIOS
from app.models.schemas import UIAnalysisResult, UIElement, TestScenario, AllScenarios


# ─────────────────────────────────────────────────────────────────────────────
# ÉTAPE 1 : Extraction des fonctionnalités réelles via LLM
# ─────────────────────────────────────────────────────────────────────────────

FEATURE_EXTRACTION_PROMPT = """You are a product analyst. Read the UI elements below and identify the REAL FEATURES of this application.

Page type: {page_type}

UI elements detected (with their page section):
{elements_list}

YOUR TASK:
List every distinct FEATURE (user action / functionality) that a user can perform on this page.
Focus on what the USER CAN DO, not on what HTML elements exist.

Examples of good features:
- "Translate text between languages"
- "Sign in with Google account"
- "Search for a job by category"
- "Play a daily word game"
- "Switch the interface language"
- "Browse learning courses by topic"
- "Look up a word definition"
- "Check grammar and spelling"

Examples of BAD features (too generic, skip these):
- "Click a link"
- "Navigate to footer"
- "See legal pages"

Respond ONLY with valid JSON:
{{
  "page_purpose": "One sentence describing what this page/app is for",
  "features": [
    {{
      "name": "Short feature name (3-6 words)",
      "description": "What the user does and what happens",
      "elements": ["exact label of element 1", "exact label of element 2"],
      "priority": "high|medium|low"
    }}
  ]
}}
"""


async def _extract_features(analysis: UIAnalysisResult) -> dict:
    """
    Étape 1 : Le LLM lit les éléments et identifie les vraies fonctionnalités.
    """
    elements_list = "\n".join(
        f"  [{el.type.upper()}] {el.label}"
        + (f"  →  {el.possible_destination}" if el.possible_destination else "")
        for el in analysis.elements
    )

    prompt = FEATURE_EXTRACTION_PROMPT.format(
        page_type=analysis.page_type or "general",
        elements_list=elements_list,
    )

    payload = {
        "model": TEXT_MODEL,
        "prompt": prompt,
        "stream": True,
        "keep_alive": 0,
        "format": "json",
        "options": {"temperature": 0.2, "num_predict": 1500},
    }

    timeout = httpx.Timeout(timeout=None)
    full = ""
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
        match = re.search(r'\{[\s\S]*\}', full)
        if match:
            try:
                return json.loads(match.group())
            except Exception:
                pass
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# ÉTAPE 2 : Génération des scénarios depuis les fonctionnalités
# ─────────────────────────────────────────────────────────────────────────────

SCENARIO_GENERATION_PROMPT = """You are a senior QA engineer. Write test scenarios for the features listed below.

Application purpose: {page_purpose}

=== FEATURES TO TEST (ordered by priority) ===
{features_list}

=== ALL AVAILABLE UI ELEMENTS (use EXACT labels in your steps) ===
{elements_list}

=== RULES ===
1. Write exactly ONE scenario per feature listed above
2. Each scenario must test the REAL functionality described — not just clicking elements
3. Use the EXACT element labels from the elements list (including [section] prefix)
4. When an element has a destination URL (→ URL), include it in the verify step
5. Steps must be concrete: what to click, what to type, what to verify as outcome
6. Max 6 steps per scenario
7. DO NOT repeat scenarios — each must test something different
8. Think about the FULL user journey: setup → action → verification
9. For forms: include filling fields, submitting, and verifying the result
10. For navigation: include clicking AND verifying the correct page loaded

Respond ONLY with valid JSON:
{{
  "scenarios": [
    {{
      "title": "Feature name: specific action being tested",
      "steps": [
        "Step 1: Navigate to the page",
        "Step 2: [specific action on specific element]",
        "Step 3: [verify specific outcome]"
      ],
      "expected_result": "Specific, observable outcome"
    }}
  ]
}}
"""


async def _generate_scenarios_from_features(
    features_data: dict,
    analysis: UIAnalysisResult,
) -> list[dict]:
    """
    Étape 2 : Génère un scénario par fonctionnalité identifiée.
    """
    features = features_data.get("features", [])
    page_purpose = features_data.get("page_purpose", analysis.raw_description or "")

    if not features:
        return []

    # Trie par priorité : high → medium → low
    priority_order = {"high": 0, "medium": 1, "low": 2}
    features_sorted = sorted(
        features,
        key=lambda f: priority_order.get(f.get("priority", "medium"), 1)
    )

    # Limite au nombre de scénarios voulu
    features_to_test = features_sorted[:MAX_SCENARIOS]

    features_list = "\n".join(
        f"  {i+1}. [{f.get('priority','medium').upper()}] {f['name']}: {f['description']}"
        + (f"\n     Elements: {f.get('elements', [])}" if f.get('elements') else "")
        for i, f in enumerate(features_to_test)
    )

    elements_list = "\n".join(
        f"  [{el.type.upper()}] {el.label}"
        + (f"  →  {el.possible_destination}" if el.possible_destination else "")
        for el in analysis.elements
    )

    prompt = SCENARIO_GENERATION_PROMPT.format(
        page_purpose=page_purpose,
        features_list=features_list,
        elements_list=elements_list,
    )

    payload = {
        "model": TEXT_MODEL,
        "prompt": prompt,
        "stream": True,
        "keep_alive": 0,
        "format": "json",
        "options": {"temperature": 0.3, "num_predict": 3000},
    }

    timeout = httpx.Timeout(timeout=None)
    full = ""
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
        parsed = json.loads(full.strip())
        return parsed.get("scenarios", [])
    except Exception:
        match = re.search(r'\{[\s\S]*\}', full)
        if match:
            try:
                return json.loads(match.group()).get("scenarios", [])
            except Exception:
                pass
    return []


# ─────────────────────────────────────────────────────────────────────────────
# POINT D'ENTRÉE
# ─────────────────────────────────────────────────────────────────────────────

async def generate_all_scenarios(analysis: UIAnalysisResult) -> AllScenarios:
    """
    Pipeline 2 étapes :
    1. LLM extrait les fonctionnalités réelles de la page
    2. LLM génère un scénario de test par fonctionnalité
    """

    # ── Étape 1 : Comprendre ce que fait la page ──────────────────────────────
    features_data = await _extract_features(analysis)

    # ── Étape 2 : Générer les scénarios ───────────────────────────────────────
    raw_scenarios = []
    if features_data.get("features"):
        raw_scenarios = await _generate_scenarios_from_features(features_data, analysis)

    # ── Fallback si le LLM échoue ─────────────────────────────────────────────
    if not raw_scenarios:
        raw_scenarios = _fallback_from_elements(analysis)

    scenarios = []
    for s in raw_scenarios[:MAX_SCENARIOS]:
        try:
            scenarios.append(TestScenario(**s))
        except Exception:
            continue

    if not scenarios:
        scenarios = _fallback_from_elements(analysis)

    return AllScenarios(scenarios=scenarios)


# ─────────────────────────────────────────────────────────────────────────────
# FALLBACK
# ─────────────────────────────────────────────────────────────────────────────

def _fallback_from_elements(analysis: UIAnalysisResult) -> list[TestScenario]:
    """Fallback minimal basé sur les types d'éléments détectés."""
    steps = ["Navigate to the page"]
    for el in analysis.elements[:6]:
        if "input_" in el.type:
            steps.append(f"Fill in '{el.label}' with test data")
        elif el.type == "button":
            steps.append(f"Click '{el.label}' button")
        elif el.type == "link":
            steps.append(f"Click '{el.label}' link")
            if el.possible_destination:
                steps.append(f"Verify navigation to '{el.possible_destination}'")
    steps.append("Verify the page responds correctly")
    return [TestScenario(
        title="Basic page interaction",
        steps=steps[:6],
        expected_result="Page responds to all interactions correctly",
    )]


# ─────────────────────────────────────────────────────────────────────────────
# HELPER : expose les features détectées pour le SSE de main.py (optionnel)
# ─────────────────────────────────────────────────────────────────────────────

async def extract_page_features(analysis: UIAnalysisResult) -> dict:
    """
    Exposé pour main.py si tu veux afficher les features dans l'UI
    avant de lancer la génération de scénarios.
    Usage dans main.py :
        features = await extract_page_features(analysis)
        yield sse("features_detected", {"features": features})
        all_scenarios = await generate_all_scenarios(analysis)
    """
    return await _extract_features(analysis)