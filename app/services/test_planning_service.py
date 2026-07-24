"""
test_planning_service.py
-------------------------
Agent ④ Test Planning Agent (voir architecture.md).

Rôle : pour chaque feature détectée par le Functional Analysis Agent
(scenario_service._extract_features), décider QUELS TYPES DE CAS tester
— pas juste le cas nominal, mais aussi les cas négatifs pertinents
(champ vide, mauvais mot de passe, injection SQL, XSS, limites...).

Choix de conception : la décision "quels types de test pour quelle feature"
est faite par des RÈGLES DÉTERMINISTES (mots-clés), pas par un appel LLM
supplémentaire. Un appel LLM ici ajouterait une couche de latence + un
risque de troncature JSON pour un problème qui est en réalité un simple
classifieur par mots-clés (login -> injection/XSS, search -> requête
vide, upload -> fichier invalide...). Le LLM garde son rôle là où il
apporte une vraie valeur : rédiger le scénario en langage naturel.

Sortie : un TestPlan = liste de {feature, test_cases:[{type, focus}]}
consommé par generate_planned_scenarios() pour produire un TestScenario
par (feature, type de cas).

FIX PERF (v2 — diagnostic ollama ps : 80% CPU / 20% GPU, inférence lente) :
--------------------------------------------------------------------------
Sur ce type de setup (majoritairement CPU-bound), paralléliser les appels
LLM n'apporte quasiment rien car les requêtes concurrentes se partagent
les mêmes cœurs. Le vrai gain vient de la RÉDUCTION DU NOMBRE D'APPELS :

  AVANT : 1 appel LLM par (feature, cas de test) -> ex. une feature login
          avec 5 cas de test = 5 appels séparés, chacun ré-envoyant tout
          le prompt (règles + liste complète des éléments UI) qui doit
          être re-traité par le modèle à chaque fois.

  APRÈS : 1 appel LLM par FEATURE, qui génère TOUS ses cas de test en une
          seule réponse JSON (tableau de scénarios). Pour une page avec
          4-6 features, ça passe de ~15 appels à ~5 appels, sans réduire
          le nombre de scénarios produits.

On garde aussi la concurrence (SCENARIO_GEN_CONCURRENCY) pour les cas où
le GPU est plus disponible, mais elle compte maintenant moins puisqu'il y
a mécaniquement moins d'appels à paralléliser.
"""
import asyncio
import httpx
import json
import re

from app.config import OLLAMA_BASE_URL, TEXT_MODEL, SCENARIO_GEN_CONCURRENCY
from app.models.schemas import UIAnalysisResult, TestScenario
from app.services.risk_analysis_service import analyze_risks


# ─────────────────────────────────────────────────────────────────────────────
# Règles : mots-clés (feature name + description) -> cas de test additionnels
# ─────────────────────────────────────────────────────────────────────────────

_RULES: list[tuple[set[str], list[dict]]] = [
    (
        {"login", "connexion", "connecter", "sign in", "authentif"},
        [
            {"type": "wrong_credentials", "focus": "identifiants invalides (mauvais mot de passe)"},
            {"type": "empty_field", "focus": "champ email/mot de passe laissé vide"},
            {"type": "sql_injection", "focus": "tentative d'injection SQL dans le champ identifiant (ex: ' OR '1'='1)"},
            {"type": "xss", "focus": "tentative d'injection XSS dans un champ texte (ex: <script>alert(1)</script>)"},
        ],
    ),
    (
        {"register", "inscri", "sign up", "creer un compte", "créer un compte"},
        [
            {"type": "empty_field", "focus": "champ obligatoire laissé vide"},
            {"type": "invalid_format", "focus": "format email invalide"},
            {"type": "duplicate", "focus": "inscription avec un email déjà utilisé"},
        ],
    ),
    (
        {"search", "recherch", "chercher"},
        [
            {"type": "empty_query", "focus": "recherche avec un champ vide"},
            {"type": "special_chars", "focus": "recherche avec caractères spéciaux/injection"},
        ],
    ),
    (
        {"upload", "téléverser", "importer un fichier", "joindre un fichier"},
        [
            {"type": "invalid_file", "focus": "upload d'un type de fichier non supporté ou trop volumineux"},
        ],
    ),
    (
        {"payment", "paiement", "checkout", "carte bancaire"},
        [
            {"type": "invalid_card", "focus": "numéro de carte invalide"},
            {"type": "empty_field", "focus": "champ de paiement obligatoire laissé vide"},
        ],
    ),
    (
        {"form", "formulaire", "envoyer", "submit", "soumettre"},
        [
            {"type": "empty_field", "focus": "soumission avec champ(s) obligatoire(s) vide(s)"},
            {"type": "boundary_length", "focus": "texte anormalement long dans un champ"},
        ],
    ),
]


def _matches(text: str, keywords: set[str]) -> bool:
    t = text.lower()
    return any(k in t for k in keywords)


async def build_test_plan(features_data: dict) -> list[dict]:
    """
    Entrée : le dict {"page_purpose": ..., "features": [...]} produit par
    scenario_service.extract_page_features (Functional Analysis Agent).

    Sortie : liste de plans par feature :
      [{"feature": <feature dict>, "test_cases": [{"type": "nominal", "focus": "..."}, ...]}]

    Passée async (elle ne l'était pas avant) : les features qui ne
    matchent AUCUNE règle connue dans _RULES (ex: "virement" pour une
    appli bancaire) sont désormais soumises au Risk Analysis Agent
    (risk_analysis_service.analyze_risks, 1 appel LLM par feature
    concernée) plutôt que de rester avec pour seul cas le scénario
    nominal. Les features "_template" (filet de couverture, éléments
    triviaux) et celles qui matchent déjà une règle _RULES ne déclenchent
    JAMAIS cet appel — comportement et performance identiques à avant
    pour tout ce qui était déjà couvert.
    """
    features = features_data.get("features", [])
    plan: list[dict] = []
    to_analyze: list[tuple[int, dict]] = []

    for feature in features:
        text = f"{feature.get('name','')} {feature.get('description','')}"
        cases = [{"type": "nominal", "focus": "cas d'usage normal / succès"}]

        matched_known_pattern = False
        for keywords, extra_cases in _RULES:
            if _matches(text, keywords):
                matched_known_pattern = True
                for c in extra_cases:
                    if c["type"] not in {existing["type"] for existing in cases}:
                        cases.append(c)

        plan.append({"feature": feature, "test_cases": cases})
        if not matched_known_pattern and not feature.get("_template"):
            to_analyze.append((len(plan) - 1, feature))

    if to_analyze:
        semaphore = asyncio.Semaphore(SCENARIO_GEN_CONCURRENCY)

        async def _bounded_analyze(plan_idx: int, feature: dict) -> tuple[int, list[dict]]:
            async with semaphore:
                risk_cases = await analyze_risks(feature)
            return plan_idx, risk_cases

        results = await asyncio.gather(
            *(_bounded_analyze(idx, feature) for idx, feature in to_analyze),
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                continue
            plan_idx, risk_cases = r
            existing_types = {c["type"] for c in plan[plan_idx]["test_cases"]}
            for c in risk_cases:
                if c["type"] not in existing_types:
                    plan[plan_idx]["test_cases"].append(c)
                    existing_types.add(c["type"])

    return plan


def plan_summary(plan: list[dict]) -> dict:
    """Petit résumé exposable en SSE (compte de cas par type)."""
    total_cases = sum(len(p["test_cases"]) for p in plan)
    by_type: dict[str, int] = {}
    for p in plan:
        for c in p["test_cases"]:
            by_type[c["type"]] = by_type.get(c["type"], 0) + 1
    return {"features": len(plan), "total_test_cases": total_cases, "by_type": by_type}


# ─────────────────────────────────────────────────────────────────────────────
# Génération des scénarios à partir du plan — BATCHÉE PAR FEATURE
# ─────────────────────────────────────────────────────────────────────────────

# Un seul appel LLM génère TOUS les cas de test d'UNE feature d'un coup.
BATCHED_SCENARIO_PROMPT = """You are a senior QA engineer. Write ONE test scenario for EACH test case listed below, for the same feature.

Application purpose: {page_purpose}

Feature: {feature_name}
Feature description: {feature_description}
Relevant UI elements: {feature_elements}

TEST CASES TO WRITE (one scenario per case, same order):
{test_cases_list}

=== ALL AVAILABLE UI ELEMENTS (use EXACT labels in your steps) ===
{elements_list}

Rules:
- Each scenario must specifically exercise its own test case (not a generic nominal flow if a negative case was requested)
- "objective" states the INTENT of the test in one sentence — what business
  behavior it verifies, not a restatement of the steps
  (e.g. "Vérifier qu'un utilisateur peut accéder à son espace personnel avec des identifiants valides.")
- "preconditions" lists the state required BEFORE the scenario starts
  (e.g. "L'utilisateur est sur la page de connexion.", "Le formulaire est disponible.")
  — 1 to 3 short items, or an empty list if truly none apply
- Use EXACT element labels (including [section] prefix) from the elements list
- Steps must be concrete: what to click, what to type, what to verify
- Max 6 steps per scenario
- expected_result must describe what SHOULD happen for that specific case (e.g. for a negative case: an error message should appear, the action should be rejected)
- Return EXACTLY {n_cases} scenario(s), in the SAME ORDER as the test cases listed above
- LANGUAGE: "title", "objective", each entry in "preconditions" and "steps",
  and "expected_result" must be written in FRENCH. Keep element labels
  exactly as given in the list above (do not translate them, they must
  match the real UI).

Respond ONLY with valid JSON, no markdown, no explanation:
{{
  "scenarios": [
    {{
      "title": "Nom de la fonctionnalite — cas de test (ex: 'Connexion : mauvais mot de passe')",
      "objective": "Une phrase decrivant l'intention du test",
      "preconditions": ["Etat requis avant de commencer"],
      "steps": ["Etape 1 : ...", "Etape 2 : ..."],
      "expected_result": "..."
    }}
  ]
}}
"""


def _num_predict_for(n_cases: int) -> int:
    # ~180-250 tokens par scénario (title + steps + expected_result) + marge
    # +60-80 tokens/scénario pour les nouveaux champs objective/preconditions.
    return min(3500, 350 + n_cases * 400)


def _elements_list_for_feature(feature: dict, all_elements_list: str, all_elements_lines: list[str]) -> str:
    """
    FIX PERF : auparavant, TOUS les éléments UI de la page étaient renvoyés
    dans CHAQUE prompt (un par feature), même quand une feature n'en
    concernait que 2-3. Plus il y a d'éléments détectés (pages riches),
    plus chaque appel LLM traite un prompt inutilement long -> ralentit
    directement l'inférence (le goulot n°1 identifié : setup CPU-bound,
    cf. commentaire plus haut). On ne garde ici que les lignes dont le
    libellé correspond à un élément listé par la feature ; si rien ne
    correspond (extraction imparfaite), on retombe sur la liste complète
    pour ne jamais priver le LLM de contexte utile.
    """
    feature_elements = [str(e).lower() for e in feature.get("elements", []) or []]
    if not feature_elements:
        return all_elements_list

    matched = [
        line for line in all_elements_lines
        if any(fe in line.lower() for fe in feature_elements)
    ]
    return "\n".join(matched) if matched else all_elements_list


async def _generate_batch_for_feature(
    feature: dict,
    test_cases: list[dict],
    page_purpose: str,
    elements_list: str,
) -> list[dict]:
    test_cases_list = "\n".join(
        f"  {i+1}. type={c['type']} — focus: {c['focus']}"
        for i, c in enumerate(test_cases)
    )
    prompt = BATCHED_SCENARIO_PROMPT.format(
        page_purpose=page_purpose,
        feature_name=feature.get("name", ""),
        feature_description=feature.get("description", ""),
        feature_elements=feature.get("elements", []),
        test_cases_list=test_cases_list,
        elements_list=elements_list,
        n_cases=len(test_cases),
    )
    payload = {
        "model": TEXT_MODEL,
        "prompt": prompt,
        "stream": True,
        "keep_alive": "5m",
        "format": "json",
        "options": {
            "temperature": 0.3,
            "num_predict": _num_predict_for(len(test_cases)),
            # FIX PERF : contexte réduit de 4096 -> 2048. Sur un GPU à VRAM
            # limitée (cf. `ollama ps` montrant 80% CPU / 20% GPU), la
            # taille du contexte alloué influence directement combien de
            # couches du modèle tiennent en VRAM. Nos prompts + réponses
            # tiennent largement sous 2048 tokens ; réduire ce budget
            # libère de la VRAM et permet à Ollama d'offloader plus de
            # couches sur GPU au lieu du CPU (plus lent).
            "num_ctx": 2048,
        },
    }
    timeout = httpx.Timeout(timeout=None)
    full = ""
    try:
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
        parsed = json.loads(full.strip())
    except Exception:
        match = re.search(r'\{[\s\S]*\}', full)
        parsed = None
        if match:
            try:
                parsed = json.loads(match.group())
            except Exception:
                parsed = None
        if parsed is None:
            return []

    scenarios = parsed.get("scenarios", []) if isinstance(parsed, dict) else []
    if not isinstance(scenarios, list):
        return []
    return scenarios


async def generate_planned_scenarios(
    plan: list[dict],
    analysis: UIAnalysisResult,
    page_purpose: str,
    max_scenarios: int,
    on_progress=None,
) -> list[TestScenario]:
    """
    Génère un TestScenario par (feature, test_case) du plan, dans la limite
    de max_scenarios (les cas "nominal" sont priorisés en premier par
    feature, pour ne jamais couper une feature avant son cas de base).

    FIX PERF v2 : UN SEUL appel LLM par feature (pas par cas de test),
    qui renvoie tous les scénarios de la feature d'un coup. Le nombre total
    de scénarios généré reste identique à `max_scenarios` près — seul le
    nombre d'ALLERS-RETOURS LLM change (ex: 15 cas de test répartis sur 5
    features = 5 appels au lieu de 15).

    on_progress(done, total) : callback optionnel pour SSE. `done`/`total`
    comptent maintenant en FEATURES traitées (pas en scénarios individuels),
    car c'est le niveau de granularité réel des appels LLM.
    """
    all_elements_lines = [
        f"  [{el.type.upper()}] {el.label}"
        + (f"  →  {el.possible_destination}" if el.possible_destination else "")
        for el in analysis.elements
    ]
    elements_list = "\n".join(all_elements_lines)

    # Répartit max_scenarios sur les features : priorité aux "nominal" de
    # chaque feature, puis on complète avec les cas négatifs tant qu'il
    # reste du budget.
    trimmed_plan: list[tuple[dict, list[dict]]] = []
    budget = max_scenarios
    # 1ère passe : garantir le nominal de chaque feature
    for p in plan:
        if budget <= 0:
            break
        nominal = [c for c in p["test_cases"] if c["type"] == "nominal"][:1]
        if nominal:
            trimmed_plan.append((p["feature"], nominal))
            budget -= 1
    # 2e passe : ajouter les cas négatifs restants tant qu'il y a du budget
    feature_index = {id(f): cases for f, cases in trimmed_plan}
    for p in plan:
        if budget <= 0:
            break
        others = [c for c in p["test_cases"] if c["type"] != "nominal"]
        for c in others:
            if budget <= 0:
                break
            key = id(p["feature"])
            if key in feature_index:
                feature_index[key].append(c)
            else:
                new_cases = [c]
                trimmed_plan.append((p["feature"], new_cases))
                feature_index[key] = new_cases
            budget -= 1

    if not trimmed_plan:
        return []

    semaphore = asyncio.Semaphore(SCENARIO_GEN_CONCURRENCY)
    progress_counter = {"done": 0}
    progress_lock = asyncio.Lock()
    total_features = len(trimmed_plan)

    async def _run_feature(feature: dict, test_cases: list[dict]) -> list[dict]:
        feature_elements_list = _elements_list_for_feature(feature, elements_list, all_elements_lines)
        async with semaphore:
            raw_scenarios = await _generate_batch_for_feature(
                feature, test_cases, page_purpose, feature_elements_list,
            )
        if on_progress:
            async with progress_lock:
                progress_counter["done"] += 1
                await on_progress(progress_counter["done"], total_features)
        return raw_scenarios

    batches = await asyncio.gather(
        *(_run_feature(feature, cases) for feature, cases in trimmed_plan),
        return_exceptions=True,
    )

    scenarios: list[TestScenario] = []
    for batch in batches:
        if not batch or isinstance(batch, Exception):
            continue
        for raw in batch:
            try:
                if not isinstance(raw.get("preconditions"), list):
                    pc = raw.get("preconditions")
                    raw["preconditions"] = [str(pc)] if pc else []
                if raw.get("objective") is not None:
                    raw["objective"] = str(raw["objective"])
                scenarios.append(TestScenario(**raw))
            except Exception:
                continue

    return scenarios[:max_scenarios]