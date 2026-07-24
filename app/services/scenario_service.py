"""
scenario_service.py
-------------------
Pipeline en 2 étapes :
  1. Le LLM analyse le HTML et identifie les FONCTIONNALITÉS réelles de la page
     (pas les éléments HTML, les actions utilisateur : "traduire du texte", "changer de langue"...)
  2. Le LLM génère un scénario de test par fonctionnalité détectée
"""
import asyncio
import httpx
import json
import logging
import re

from app.config import (
    OLLAMA_BASE_URL,
    TEXT_MODEL,
    MAX_SCENARIOS,
    FEATURE_EXTRACT_BATCH_SIZE,
    SCENARIO_GEN_BATCH_SIZE,
    SCENARIO_GEN_CONCURRENCY,
)
from app.models.schemas import UIAnalysisResult, UIElement, TestScenario, AllScenarios
from app.services.test_planning_service import build_test_plan, generate_planned_scenarios
from app.services.ui_graph_service import build_ui_graph
from app.services.app_understanding_service import understand_application
from app.services.navigation_discovery_service import discover_navigation_categories, all_categorized_labels
from app.services.workflow_discovery_service import discover_workflows
from app.services.business_domain_service import understand_business_domain

logger = logging.getLogger("scenario_service")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[scenario_service] %(message)s"))
    logger.addHandler(_h)


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

=== CRITICAL ANTI-HALLUCINATION RULE ===
You may ONLY describe features that are grounded in the elements list above.
Every feature's "elements" array MUST contain at least one label copied
VERBATIM (exact text) from the list above. If you cannot point to a real
element from the list for a feature, DO NOT invent it — leave it out
entirely. This application is UNKNOWN to you: do not assume it has login,
search, social sign-in, job listings, or any other "typical" feature
unless the corresponding element is ACTUALLY present in the list above.
Never reuse the illustrative feature names shown further below — they are
format examples only, not content to copy.

CRITICAL RULE ABOUT SIMILAR-LOOKING ELEMENTS:
When you see several elements that look similar but target a DIFFERENT
provider, destination, or value, they are SEPARATE features — never merge
them into one generic feature, even if they share a common verb.
Example: if the elements list literally contains three distinct sign-in
buttons for three distinct providers, that is THREE separate features, not
one generic "Authenticate" feature. Same logic for multiple distinct share
buttons or multiple distinct category filters — each distinct
target/provider/value actually present becomes its own feature.
(Note: language-switcher links are handled separately and will not appear
in this list — do not worry about them.)

FORMAT EXAMPLE ONLY (do not reuse these names — they illustrate structure,
not content; your real output must come only from the elements list above):
{{
  "page_purpose": "Une phrase decrivant a quoi sert cette page (EN FRANCAIS)",
  "features": [
    {{
      "name": "Nom court de la fonctionnalite (3-6 mots, EN FRANCAIS)",
      "description": "Ce que fait l'utilisateur et ce qui se passe (EN FRANCAIS)",
      "elements": ["libelle exact de l'element 1", "libelle exact de l'element 2"],
      "priority": "high|medium|low",
      "business_capability": "nom exact d'une capacite metier connue, ou null"
    }}
  ]
}}

Examples of BAD features (too generic, skip these):
- "Click a link"
- "Navigate to footer"
- "See legal pages"
- "Authenticate" (too vague when several distinct providers/buttons exist — list each one instead)

LANGUAGE: "page_purpose", "name" and "description" must be written in
FRENCH. Keep "elements" values as the exact verbatim labels from the list
above (do not translate them).

Respond ONLY with valid JSON, no markdown, no explanation, matching the
structure shown above.
"""


async def _extract_features_bottom_up(
    analysis: UIAnalysisResult, capabilities: list[str] | None = None,
) -> dict:
    """
    Passe ASCENDANTE (éléments -> features) du Feature Discovery Agent —
    comportement HISTORIQUE, inchangé. Le LLM lit un lot d'éléments et
    identifie librement les fonctionnalités qu'il y voit ; `capabilities`
    n'y est qu'une étiquette optionnelle proposée après coup (voir
    FEATURE_EXTRACTION_PROMPT / _extract_features_batch).

    Depuis la refonte capacité-first (voir _extract_features), cette
    fonction n'est plus le point d'entrée principal : elle sert de PASSE
    RÉSIDUELLE, appelée uniquement sur les éléments qu'aucune capacité
    métier connue n'a permis de rattacher (voir _extract_features), et de
    seul mode utilisé quand le Business Domain Understanding Agent n'a
    produit aucune capacité (LLM en échec) — comportement alors identique
    à 100% à avant cette refonte, aucune régression possible.

    Découpé en LOTS de FEATURE_EXTRACT_BATCH_SIZE éléments : envoyer les 30+
    éléments d'une page riche (Google Traduction avec ses onglets Traduire /
    Documents / Dictionnaire / Correcteur / Vocabulaire / Contexte...) en une
    seule requête à un petit modèle (llama3.2:3b) produit une liste de
    "features" qui dépasse souvent num_predict -> le JSON est tronqué ->
    les fonctionnalités listées EN FIN de réponse (souvent les onglets
    secondaires, lus après la zone principale) disparaissent silencieusement.
    En petits lots, chaque réponse est courte et complète ; on fusionne
    ensuite les features de tous les lots (dédupliquées par nom).

    `capabilities` (voir business_domain_service.py) : transmis tel quel à
    chaque lot, pour que le rattachement business_capability reste cohérent
    d'un lot à l'autre — même référentiel partagé, quel que soit le lot.
    """
    elements = analysis.elements
    page_purpose = ""
    merged_features: list[dict] = []
    seen_names: set[str] = set()

    for i in range(0, len(elements), FEATURE_EXTRACT_BATCH_SIZE):
        batch = elements[i:i + FEATURE_EXTRACT_BATCH_SIZE]
        data = await _extract_features_batch(analysis.page_type, batch, capabilities=capabilities)

        if not page_purpose and data.get("page_purpose"):
            page_purpose = data["page_purpose"]

        for f in data.get("features", []) or []:
            name_key = (f.get("name") or "").strip().lower()
            if name_key and name_key not in seen_names:
                seen_names.add(name_key)
                merged_features.append(f)

    return {"page_purpose": page_purpose, "features": merged_features}


CAPABILITY_GROUNDED_FEATURE_PROMPT = """You are a senior QA/product analyst. This application's BUSINESS CAPABILITIES have ALREADY been identified by a prior analysis step — your job now is to find which UI elements, among the batch given below, actually REALIZE each of these capabilities.

Page type: {page_type}

=== KNOWN BUSINESS CAPABILITIES (already established for this whole application — do NOT invent new ones, do NOT use any name outside this list) ===
{capabilities_list}

=== UI ELEMENTS IN THIS BATCH (only part of the page — other elements are covered by other batches or by a residual pass; this is normal, do not worry about completeness) ===
{elements_list}

YOUR TASK:
For EACH capability above, check whether one or more elements in THIS BATCH
clearly realize it. If yes, report it as ONE feature grounded in those exact
elements. Several elements can belong together to the same capability (e.g.
a "Traduire" button plus source/target text areas -> one "Traduction"
feature). If NO element in this batch corresponds to a given capability,
say nothing about it here — it may be found in another batch, or simply
absent from this page.

=== CRITICAL ANTI-HALLUCINATION RULE ===
Every feature's "elements" array MUST contain at least one label copied
VERBATIM (exact text) from the batch above. If you cannot point to a real
element from THIS batch for a capability, DO NOT report it.

CRITICAL RULE ABOUT SIMILAR-LOOKING ELEMENTS:
Elements that look similar but target a DIFFERENT provider, destination, or
value are SEPARATE features, even under the same capability, when the
capability naturally covers distinct choices (e.g. three distinct OAuth
buttons under "Authentification" = three features, never one generic one).

"business_capability" MUST be one of the EXACT capability names listed
above, copied verbatim — never null in this pass, never invented, never a
paraphrase.

FORMAT (do not reuse this example's content — it only shows the structure):
{{
  "page_purpose": "Une phrase decrivant a quoi sert cette page (EN FRANCAIS)",
  "features": [
    {{
      "name": "Nom court de la fonctionnalite (3-6 mots, EN FRANCAIS)",
      "description": "Ce que fait l'utilisateur et ce qui se passe (EN FRANCAIS)",
      "elements": ["libelle exact de l'element 1", "libelle exact de l'element 2"],
      "priority": "high|medium|low",
      "business_capability": "nom exact d'une capacite ci-dessus"
    }}
  ]
}}

LANGUAGE: "page_purpose", "name" and "description" must be written in
FRENCH. Keep "elements" values as the exact verbatim labels from the batch
above (do not translate them).

Respond ONLY with valid JSON, no markdown, no explanation.
"""


async def _extract_features_capability_batch(
    page_type: str,
    elements: list[UIElement],
    capabilities: list[str],
    capability_descriptions: dict[str, str],
) -> dict:
    """
    Un lot d'éléments, interrogé au regard du référentiel COMPLET de
    capacités métier (Business Domain Understanding Agent). Contrairement à
    _extract_features_batch (mode ascendant, où `capabilities` n'est qu'une
    étiquette optionnelle ajoutée en fin de prompt), ICI le référentiel EST
    la structure organisatrice de la question posée au LLM : pour CHAQUE
    capacité connue, trouver les éléments de CE lot qui la réalisent. Le
    découpage en lots reste nécessaire pour les mêmes raisons de troncature
    JSON que _extract_features_batch (voir _extract_features_bottom_up),
    mais le lot n'est plus le point de départ du raisonnement — la capacité
    l'est.
    """
    elements_list = "\n".join(
        f"  [{el.type.upper()}] {el.label}"
        + (f"  →  {el.possible_destination}" if el.possible_destination else "")
        for el in elements
    )
    capabilities_list = "\n".join(
        f"  - {c}" + (f" : {capability_descriptions[c]}" if capability_descriptions.get(c) else "")
        for c in capabilities
    )

    prompt = CAPABILITY_GROUNDED_FEATURE_PROMPT.format(
        page_type=page_type or "general",
        capabilities_list=capabilities_list,
        elements_list=elements_list,
    )

    num_predict = min(2200, 700 + len(elements) * 70)

    payload = {
        "model": TEXT_MODEL,
        "prompt": prompt,
        "stream": True,
        "keep_alive": "5m",
        "format": "json",
        "options": {
            "temperature": 0.2,
            "num_predict": num_predict,
            "num_ctx": 2048,
        },
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
        data = json.loads(full.strip())
        if not data:
            logger.warning(
                "Feature Discovery (passe capacité-first) : réponse LLM vide "
                "sur ce lot (%d élément(s)) -> 0 feature pour ce lot. Réponse "
                "brute reçue (500 premiers caractères) : %r", len(elements), full[:500],
            )
        return data
    except Exception:
        match = re.search(r'\{[\s\S]*\}', full)
        if match:
            try:
                return json.loads(match.group())
            except Exception:
                pass
        logger.warning(
            "Feature Discovery (passe capacité-first) : JSON invalide/introuvable "
            "sur ce lot (%d élément(s)) -> 0 feature pour ce lot. Réponse brute "
            "reçue (500 premiers caractères) : %r", len(elements), full[:500],
        )
    return {}


async def _extract_features_capability_grounded(
    analysis: UIAnalysisResult, capabilities: list[str], capability_descriptions: dict[str, str],
) -> dict:
    """
    Passe DESCENDANTE (capacité -> éléments) du Feature Discovery Agent.
    Toujours appelée en lots (FEATURE_EXTRACT_BATCH_SIZE éléments, même
    contrainte de contexte/troncature que le mode ascendant), mais avec le
    référentiel de capacités COMPLET envoyé à CHAQUE lot, pour que le
    rattachement reste cohérent d'un lot à l'autre.
    """
    elements = analysis.elements
    page_purpose = ""
    merged_features: list[dict] = []
    seen_names: set[str] = set()

    for i in range(0, len(elements), FEATURE_EXTRACT_BATCH_SIZE):
        batch = elements[i:i + FEATURE_EXTRACT_BATCH_SIZE]
        data = await _extract_features_capability_batch(
            analysis.page_type, batch, capabilities, capability_descriptions,
        )

        if not page_purpose and data.get("page_purpose"):
            page_purpose = data["page_purpose"]

        for f in data.get("features", []) or []:
            name_key = (f.get("name") or "").strip().lower()
            if name_key and name_key not in seen_names:
                seen_names.add(name_key)
                merged_features.append(f)

    return {"page_purpose": page_purpose, "features": merged_features}


async def _extract_features(
    analysis: UIAnalysisResult,
    capabilities: list[str] | None = None,
    capability_descriptions: dict[str, str] | None = None,
) -> dict:
    """
    Point d'entrée du Feature Discovery Agent — CŒUR de la refonte du flux
    de connaissance (cause racine identifiée : Business Domain produisait
    déjà un référentiel de capacités métier, mais Feature Discovery ne
    l'utilisait que comme étiquette optionnelle après coup ; son substrat
    de raisonnement restait des lots d'éléments HTML découpés par position,
    jamais le référentiel lui-même). Deux passes complémentaires :

    1) Passe DESCENDANTE (capacité -> éléments),
       _extract_features_capability_grounded : SEULEMENT si le Business
       Domain Understanding Agent a produit un référentiel non vide. Pour
       chaque capacité connue, le LLM cherche quels éléments du lot la
       réalisent — le référentiel devient la structure organisatrice de la
       question posée, pas une suggestion annexe.

    2) Passe résiduelle ASCENDANTE (éléments -> features),
       _extract_features_bottom_up : appliquée UNIQUEMENT aux éléments que
       la passe 1 n'a rattachés à AUCUNE capacité (référentiel incomplet —
       cas fréquent, une capacité métier réelle peut ne pas avoir été
       anticipée par Business Domain). Comportement strictement identique
       à celui d'avant cette refonte. Si `capabilities` est vide (Business
       Domain a échoué), on saute directement à cette passe pour 100% des
       éléments — comportement alors 100% inchangé, aucune régression.
    """
    if not capabilities:
        return await _extract_features_bottom_up(analysis, capabilities=None)

    capability_result = await _extract_features_capability_grounded(
        analysis, capabilities, capability_descriptions or {},
    )
    cap_features = capability_result.get("features", [])

    # Éléments déjà couverts par la passe descendante — même correspondance
    # floue (sous-chaîne, insensible à la casse) que _filter_ungrounded_features
    # / _ensure_element_coverage plus haut dans ce fichier, pour rester
    # cohérent avec le reste du filtrage déterministe du module.
    covered_cores = {
        _label_core_text(el_label)
        for f in cap_features
        for el_label in (f.get("elements") or [])
        if _label_core_text(el_label)
    }

    def _is_covered(el: UIElement) -> bool:
        core = _label_core_text(el.label)
        if not core:
            return False
        return any(core in c or c in core for c in covered_cores)

    residual_elements = [el for el in analysis.elements if not _is_covered(el)]

    residual_result: dict = {"page_purpose": "", "features": []}
    if residual_elements:
        residual_analysis = analysis.copy(update={"elements": residual_elements})
        # `capabilities` reste transmis ici : si le LLM du mode ascendant
        # reconnaît malgré tout une capacité connue sur un élément non
        # rattaché par la passe 1, il peut encore l'étiqueter (comportement
        # historique) — mais ce n'est plus lui qui PILOTE la découverte.
        residual_result = await _extract_features_bottom_up(
            residual_analysis, capabilities=capabilities,
        )

    page_purpose = capability_result.get("page_purpose") or residual_result.get("page_purpose") or ""

    merged_features = list(cap_features)
    seen_names = {
        (f.get("name") or "").strip().lower()
        for f in merged_features if f.get("name")
    }
    for f in residual_result.get("features", []):
        name_key = (f.get("name") or "").strip().lower()
        if name_key and name_key not in seen_names:
            seen_names.add(name_key)
            merged_features.append(f)

    return {"page_purpose": page_purpose, "features": merged_features}


async def _extract_features_batch(
    page_type: str, elements: list[UIElement], capabilities: list[str] | None = None,
) -> dict:
    """
    Appelle le LLM sur UN lot d'éléments et retourne le JSON brut.

    `capabilities` (Business Domain Understanding Agent, voir
    business_domain_service.py) : quand fourni, ajoute au prompt le
    référentiel de capacités métier déjà identifiées pour l'application
    entière (ex: "traduction", "favoris", "historique" pour un
    traducteur). Le LLM devient alors un agent de RATTACHEMENT (quel
    élément réalise quelle capacité déjà connue ?) plutôt que d'INVENTION
    libre par lot — une feature reste quand même rapportée si aucune
    capacité connue ne correspond (business_capability=null), rien n'est
    perdu si le référentiel est incomplet.
    """
    elements_list = "\n".join(
        f"  [{el.type.upper()}] {el.label}"
        + (f"  →  {el.possible_destination}" if el.possible_destination else "")
        for el in elements
    )

    prompt = FEATURE_EXTRACTION_PROMPT.format(
        page_type=page_type or "general",
        elements_list=elements_list,
    )

    if capabilities:
        prompt += (
            "\n=== KNOWN BUSINESS CAPABILITIES (Business Domain Understanding Agent) ===\n"
            + "\n".join(f"  - {c}" for c in capabilities)
            + "\nFor each feature you report, if it clearly realizes one of the capabilities "
              "above, set \"business_capability\" to that EXACT name (copied verbatim). If none "
              "matches, set it to null — do NOT force a bad match just to use one of these names.\n"
        )

    # num_predict proportionnel à la taille du lot, avec large marge.
    num_predict = min(2200, 700 + len(elements) * 70)

    payload = {
        "model": TEXT_MODEL,
        "prompt": prompt,
        "stream": True,
        # FIX PERF (régression du bug déjà connu du projet) : keep_alive=0
        # forçait Ollama à DÉCHARGER le modèle de la mémoire après CET appel
        # -> le lot suivant devait le recharger depuis le disque (30-90s de
        # perdu par appel, en plus de la génération elle-même). "5m" garde
        # le modèle chargé entre les lots de ce même pipeline.
        "keep_alive": "5m",
        "format": "json",
        "options": {
            "temperature": 0.2,
            "num_predict": num_predict,
            # Contexte réduit : nos prompts/réponses tiennent largement
            # sous 2048 tokens ; sur un GPU à VRAM limitée ça libère de la
            # place pour offloader plus de couches sur GPU au lieu du CPU.
            "num_ctx": 2048,
        },
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
        data = json.loads(full.strip())
        if not data:
            logger.warning(
                "Feature Discovery : réponse LLM vide sur ce lot (%d élément(s)) "
                "-> 0 feature pour ce lot. Réponse brute reçue (500 premiers "
                "caractères) : %r", len(elements), full[:500],
            )
        return data
    except Exception:
        match = re.search(r'\{[\s\S]*\}', full)
        if match:
            try:
                return json.loads(match.group())
            except Exception:
                pass
        logger.warning(
            "Feature Discovery : JSON invalide/introuvable sur ce lot (%d "
            "élément(s)) -> 0 feature pour ce lot. Réponse brute reçue (500 "
            "premiers caractères) : %r", len(elements), full[:500],
        )
    return {}


def _label_core_text(label: str | None) -> str:
    """Retire le préfixe [contexte] éventuel (ajouté par vision_parser_service)
    et met en minuscule, pour comparer des labels indépendamment de leur
    provenance (HTML direct ou capture d'écran)."""
    raw = label or ""
    text = raw.split("]", 1)[-1].strip() if "]" in raw else raw.strip()
    return text.lower().strip()


def _filter_ungrounded_features(features: list[dict], elements: list[UIElement]) -> list[dict]:
    """
    Filet de sécurité DÉTERMINISTE (pas de LLM), même principe que le
    filtre anti-hallucination de vision_parser_service._to_ui_elements :
    rejette toute feature dont AUCUN des labels déclarés dans "elements"
    ne correspond à un élément réellement détecté sur la page.

    Pourquoi c'est nécessaire même avec un bon prompt : un petit modèle
    local (llama3.2:1b/3b) peut malgré tout "reconnaître" un pattern
    familier (login Google, recherche d'emploi...) et l'halluciner même
    si rien dans la page ne le justifie — surtout sur une page qu'il n'a
    jamais vue (ex: un site de banque centrale). Le prompt seul ne
    garantit rien à 100% ; ce filtre côté code, oui.
    """
    real_cores = [_label_core_text(el.label) for el in elements if el.label]

    grounded: list[dict] = []
    rejected: list[str] = []

    for f in features:
        declared = f.get("elements") or []
        is_grounded = any(
            any(
                dc and rc and (dc in rc or rc in dc)
                for rc in real_cores
            )
            for dc in (_label_core_text(d) for d in declared)
        )
        if is_grounded:
            grounded.append(f)
        else:
            rejected.append(f.get("name", "?"))

    if rejected:
        logger.warning(
            "%d feature(s) rejetée(s) par le filet anti-hallucination "
            "(aucun élément déclaré ne correspond à un élément réellement "
            "détecté sur la page — probable invention du LLM) : %s",
            len(rejected), rejected,
        )

    return grounded


def _ensure_element_coverage(features: list[dict], elements: list[UIElement]) -> list[dict]:
    """
    Filet de sécurité DÉTERMINISTE (pas de LLM) : garantit qu'aucun élément
    actionnable détecté (bouton/lien/select/checkbox/radio) n'est laissé
    sans scénario.

    Pourquoi c'est nécessaire même avec un bon prompt : un petit modèle
    local (llama3.2:3b) peut malgré tout choisir de fusionner "Se connecter
    avec Google" / "Se connecter avec Microsoft" / "Continuer avec email" en
    UNE seule feature générique "S'authentifier" — c'est un comportement de
    généralisation du modèle, pas une troncature, donc aucun prompt ni
    batching ne le garantit à 100%.

    On vérifie donc par CODE si chaque élément actionnable est référencé
    dans la liste "elements" d'au moins une feature déjà extraite. S'il ne
    l'est nulle part, on lui crée sa propre feature de secours -> il aura
    donc forcément son propre scénario + son propre script Selenium, quoi
    que le LLM ait décidé.
    """
    COVERABLE_TYPES = {"button", "link", "select", "checkbox", "radio"}

    covered_texts: set[str] = set()
    for f in features:
        for el_label in (f.get("elements") or []):
            core = _label_core_text(el_label)
            if core:
                covered_texts.add(core)
        name_core = (f.get("name") or "").lower().strip()
        if name_core:
            covered_texts.add(name_core)

    extra_features: list[dict] = []
    seen_extra: set[str] = set()

    for el in elements:
        if el.type not in COVERABLE_TYPES:
            continue
        core = _label_core_text(el.label)
        if not core or core in seen_extra:
            continue

        # Couvert si le texte de l'élément apparaît dans un texte de feature
        # déjà couvert (ou l'inverse) — tolère les reformulations partielles
        # du LLM (ex: feature "elements" contient juste "Google" au lieu du
        # label complet "[header] Se connecter avec Google").
        is_covered = any(
            core in ct or ct in core
            for ct in covered_texts if ct and len(ct) > 2
        )
        if is_covered:
            continue

        seen_extra.add(core)
        extra_features.append({
            "name": (el.label or core).strip()[:60],
            "description": f"Interagir avec l'élément « {core} » détecté sur la page et vérifier le résultat.",
            "elements": [el.label],
            "priority": "medium",
            # Marqueur : cette feature est un filet de sécurité (élément
            # trivial non repéré par le LLM), pas une vraie fonctionnalité
            # analysée. generate_all_scenarios() lui génère un scénario
            # DÉTERMINISTE (sans appel LLM) plutôt que de la faire passer
            # par _generate_scenarios_from_features — voir _template_scenario_for_feature.
            # C'est le principal facteur de lenteur : sur une page avec un
            # footer multilingue (ex: Reverso), 18-23 de ces features de
            # secours étaient générées à CHAQUE run, et repartaient quand
            # même se faire écrire un scénario par un modèle CPU-bound.
            "_template": True,
            "_el_type": el.type,
        })

    if extra_features:
        logger.warning(
            "%d élément(s) actionnable(s) n'étaient couverts par AUCUNE "
            "feature extraite par le LLM -> ajout de feature(s) de secours "
            "pour garantir leur test : %s",
            len(extra_features), [f["name"] for f in extra_features],
        )

    return features + extra_features


def _build_hover_menu_features(hover_elements: list[UIElement]) -> list[dict]:
    """
    Construit une feature DÉTERMINISTE par lien de sous-menu (pas de LLM),
    même philosophie que _ensure_element_coverage pour les liens de footer.

    Pourquoi contourner totalement le LLM ici (pas juste un filet de
    sécurité après coup) : un petit modèle local (llama3.2:1b/3b) a
    tendance à regrouper plusieurs liens de sous-menu qui "se ressemblent"
    (même conteneur, même style de libellé) en UNE SEULE feature de
    navigation générique — la règle anti-fusion du prompt
    (FEATURE_EXTRACTION_PROMPT) aide mais n'est pas garantie à 100% sur un
    modèle aussi petit. En les retirant du lot envoyé au LLM et en leur
    générant directement une feature + un scénario ici, chaque sous-lien
    est GARANTI d'avoir son propre scénario hover→clic, quel que soit le
    comportement du modèle.
    """
    seen: set[str] = set()
    features: list[dict] = []
    for el in hover_elements:
        core = _label_core_text(el.label)
        if not core or core in seen:
            continue
        seen.add(core)
        hover_label = el.hover_target_label or "le menu"
        features.append({
            "name": (el.label or core).strip()[:60],
            "description": f"Survoler « {hover_label} » pour ouvrir le sous-menu, puis cliquer sur « {core} ».",
            "elements": [el.label],
            "priority": "medium",
            "_template": True,
            "_el_type": "link",
            "_requires_hover": True,
            "_hover_target_label": hover_label,
        })
    return features


_APOSTROPHES_RE = re.compile(r"[’‘`´]")


def _norm_apo(s: str) -> str:
    """Normalise les apostrophes typographiques (’) vers l'apostrophe
    droite ('), pour des comparaisons robustes quelle que soit la
    ponctuation utilisée par le site testé."""
    return _APOSTROPHES_RE.sub("'", s or "")


_REGISTER_HINTS = ("sign up", "creer un compte", "créer un compte", "create account")
_LOGIN_HINTS = ("connexion", "connecter", "log in", "sign in", "login")
_LOGOUT_HINTS = ("deconnex", "déconnex", "logout", "log out", "sign out")
_FORGOT_PASSWORD_HINTS = (
    "mot de passe oublie", "mot de passe oublié", "forgot password",
    "reset password", "mot de passe perdu", "identifiant oublie", "identifiant oublié",
)
_OAUTH_PROVIDERS = ("google", "microsoft", "apple", "github", "facebook", "linkedin")
_OAUTH_ACTION_HINTS = (
    "continuer", "connect", "sign in", "se connecter", "login",
    "s'identifier", "poursuivre", "continue",
)

# FIX faux positif observé (page Google Traduction) : "inscri" en simple
# sous-chaîne peut matcher un mot sans rapport, produisant un scénario
# "Inscription" fantôme sans aucun élément d'inscription réel sur la
# page. Frontière de mot exigée avant la racine (une apostrophe compte
# comme frontière, donc "s'inscrire"/"inscrivez"/"inscription" matchent
# toujours). Couvre aussi "identifi" : "S'identifier" (variante LinkedIn
# de "se connecter") n'était reconnu nulle part comme mot-clé de
# connexion à part entière.
_AUTH_ROOT_RE = re.compile(r"\b(inscri|identifi)")


def _classify_auth_label(core: str) -> str | None:
    text = _norm_apo(core).lower()
    if any(h in text for h in _FORGOT_PASSWORD_HINTS):
        return "forgot_password"
    if any(h in text for h in _LOGOUT_HINTS):
        return None
    if any(h in text for h in _REGISTER_HINTS):
        return "register"
    m = _AUTH_ROOT_RE.search(text)
    if m and m.group(1) == "inscri":
        return "register"
    if m and m.group(1) == "identifi":
        return "login"
    if any(h in text for h in _LOGIN_HINTS):
        return "login"
    return None


def _oauth_provider_in_label(core: str) -> str | None:
    text = _norm_apo(core).lower()
    if not any(h in text for h in _OAUTH_ACTION_HINTS):
        return None
    for p in _OAUTH_PROVIDERS:
        if p in text:
            return p.capitalize()
    return None


def _build_auth_features(elements: list[UIElement]) -> tuple[list[dict], set[str]]:
    """
    Détecte les liens/boutons d'authentification (inscription, connexion,
    mot de passe oublié, connexion via Google/Microsoft/...) et construit
    UNE feature déterministe et DÉTAILLÉE par intention — avec des étapes
    concrètes de remplissage de champs (email/mot de passe de test), même
    si les champs eux-mêmes n'existent pas encore sur la page actuelle (le
    lien mène vers une page d'inscription/connexion séparée — le clic,
    lui, reste réel et se résout normalement vers l'élément correspondant
    lors du grounding Selenium ; les étapes de remplissage qui ne
    correspondent à aucun champ visible sur CETTE page sont simplement
    ignorées par le grounding, sans faire échouer le script).

    Contourne le LLM pour la même raison que _build_hover_menu_features /
    _build_language_group_features : un lien isolé "S'inscrire" ne suffit
    pas à un petit modèle local pour en déduire un scénario de test
    complet et cohérent (remplissage de champs, options OAuth...).

    Retourne (features, labels_consommés) — les labels consommés sont
    retirés du lot envoyé au LLM pour éviter un doublon générique
    ("cliquer sur S'inscrire et vérifier changement de page").
    """
    register_els: list[UIElement] = []
    login_els: list[UIElement] = []
    forgot_els: list[UIElement] = []
    oauth_options: list[tuple[str, str]] = []
    seen_oauth: set[str] = set()
    consumed: set[str] = set()

    for el in elements:
        if el.type not in ("link", "button") or not el.label:
            continue
        core = _label_core_text(el.label)
        if not core:
            continue
        provider = _oauth_provider_in_label(core)
        if provider:
            consumed.add(el.label)
            if provider not in seen_oauth:
                seen_oauth.add(provider)
                oauth_options.append((provider, _display_label(el.label)))
            continue
        kind = _classify_auth_label(core)
        if kind == "register":
            register_els.append(el)
            consumed.add(el.label)
        elif kind == "login":
            login_els.append(el)
            consumed.add(el.label)
        elif kind == "forgot_password":
            forgot_els.append(el)
            consumed.add(el.label)

    features: list[dict] = []

    if register_els:
        features.append({
            "name": "Inscription",
            "description": f"Créer un nouveau compte via « {_display_label(register_els[0].label)} ».",
            "elements": [register_els[0].label],
            "priority": "high",
            "_template": True,
            "_el_type": "auth_register",
            "_auth_trigger_label": register_els[0].label,
        })

    if login_els or oauth_options:
        trigger_label = login_els[0].label if login_els else None
        provider_names = ", ".join(p for p, _ in oauth_options)
        features.append({
            "name": "Connexion",
            "description": (
                f"Se connecter via « {_display_label(trigger_label)} »" if trigger_label
                else "Se connecter via un fournisseur externe"
            ) + (f", avec les options {provider_names}" if oauth_options else "") + ".",
            "elements": [trigger_label] if trigger_label else [oauth_options[0][1]],
            "priority": "high",
            "_template": True,
            "_el_type": "auth_login",
            "_auth_trigger_label": trigger_label,
            "_oauth_options": oauth_options,
        })

    if forgot_els:
        features.append({
            "name": "Mot de passe oublié",
            "description": f"Réinitialiser un mot de passe oublié via « {_display_label(forgot_els[0].label)} ».",
            "elements": [forgot_els[0].label],
            "priority": "medium",
            "_template": True,
            "_el_type": "auth_forgot_password",
            "_auth_trigger_label": forgot_els[0].label,
        })

    return features, consumed


def _build_language_group_features(language_elements: list[UIElement]) -> list[dict]:
    """
    Regroupe TOUS les liens de langue détectés (nav_group="language_switcher")
    en UNE SEULE feature déterministe (pas de LLM), conformément au Problème
    2 : "toutes les langues doivent être regroupées dans un seul scénario".
    Contourne totalement le LLM (comme _build_hover_menu_features) : le
    prompt seul ne suffit pas à empêcher un petit modèle de scinder les
    langues en features séparées.
    """
    if not language_elements:
        return []

    seen: set[str] = set()
    labels: list[str] = []
    cores: list[str] = []
    for el in language_elements:
        core = _label_core_text(el.label)
        if not core or core in seen:
            continue
        seen.add(core)
        labels.append(el.label)
        cores.append(core)

    if not labels:
        return []

    return [{
        "name": "Sélection de la langue",
        "description": (
            "Sélectionner successivement chaque langue disponible et "
            "vérifier que l'interface change de langue."
        ),
        "elements": labels,
        "priority": "medium",
        "_template": True,
        "_el_type": "language_group",
        "_language_cores": cores,
    }]


async def _extract_features_with_coverage(
    analysis: UIAnalysisResult,
    capabilities: list[str] | None = None,
    capability_descriptions: dict[str, str] | None = None,
) -> dict:
    """
    Factorise la logique commune à generate_all_scenarios() et
    extract_page_features() : extraction LLM + filtres déterministes +
    garantie de couverture + garantie sous-menus. Utilisée par les DEUX
    pipelines (direct main.py ET orchestrateur LangGraph) pour que la
    détection de sous-menus bénéficie aux deux de la même façon.

    Les éléments taggés requires_hover (voir html_parser_service, pattern
    <a>Menu</a><ul class="sub">...</ul>) sont retirés AVANT l'extraction
    LLM (voir _build_hover_menu_features) : le LLM ne les voit jamais et
    ne peut donc pas les fusionner en une feature générique.

    Idem pour les liens de langue (nav_group="language_switcher", voir
    _build_language_group_features) : retirés avant l'extraction LLM et
    regroupés en UNE SEULE feature déterministe, plutôt que laissés au LLM
    qui les traiterait comme des features séparées (voir Problème 2).

    `capabilities` / `capability_descriptions` (voir business_domain_service.py) :
    transmis à _extract_features, qui les utilise désormais comme structure
    ORGANISATRICE de la découverte (passe capacité-first), et non plus
    comme simple étiquette optionnelle — voir _extract_features.
    """
    hover_elements_all = [el for el in analysis.elements if el.requires_hover]
    language_elements = [
        el for el in analysis.elements
        if not el.requires_hover and el.nav_group == "language_switcher"
    ]
    non_hover_non_lang = [
        el for el in analysis.elements
        if not el.requires_hover and el.nav_group != "language_switcher"
    ]
    # FIX (observé : boutons "Continuer avec Google/Microsoft" absents du
    # rapport) : un contrôle OAuth est très souvent caché dans un menu
    # déroulant ou une modale de connexion (survol/clic du bouton "Se
    # connecter" pour le révéler) -> il porte requires_hover=True. En ne
    # scannant que non_hover_non_lang, _build_auth_features ne le voyait
    # JAMAIS : il repartait vers _build_hover_menu_features (scénario
    # générique "Vérifier la navigation (sous-menu)"), voire disparaissait
    # silencieusement si son libellé n'était pas résolu. On scanne
    # maintenant TOUS les éléments (hover compris) pour l'auth, puis on
    # retire les labels consommés des deux lots (hover ET plain) avant de
    # les distribuer à leurs générateurs respectifs.
    auth_features, auth_consumed_labels = _build_auth_features(analysis.elements)
    hover_elements = [el for el in hover_elements_all if el.label not in auth_consumed_labels]
    plain_elements = [
        el for el in non_hover_non_lang if el.label not in auth_consumed_labels
    ]
    plain_analysis = analysis.copy(update={"elements": plain_elements})

    features_data = await _extract_features(
        plain_analysis, capabilities=capabilities, capability_descriptions=capability_descriptions,
    )

    features_data["features"] = _filter_ungrounded_features(
        features_data.get("features", []), plain_elements
    )
    features_data["features"] = _ensure_element_coverage(
        features_data.get("features", []), plain_elements
    )
    features_data["features"] += _build_hover_menu_features(hover_elements)
    features_data["features"] += _build_language_group_features(language_elements)
    features_data["features"] += auth_features

    return features_data


# ─────────────────────────────────────────────────────────────────────────────
# ÉTAPE 0 (nouveau) : UI Semantic Graph -> Application Understanding ->
# Navigation Discovery -> Workflow Discovery, PARTAGÉE par
# generate_all_scenarios() et extract_page_features().
#
# Pourquoi cette étape existe : avant elle, les éléments de navigation
# (navbar/footer/sous-menus) étaient envoyés au LLM de _extract_features
# comme n'importe quel autre élément, découpés en lots arbitraires de
# FEATURE_EXTRACT_BATCH_SIZE -> impossible pour le LLM de voir deux liens
# du même menu dans des lots différents, et le prompt
# FEATURE_EXTRACTION_PROMPT décourage explicitement la fusion d'éléments
# à destination différente (règle anti-fusion pensée pour les boutons
# OAuth). Résultat observé : un scénario par lien.
#
# navigation_discovery_service traite maintenant TOUTE la navigation de
# façon déterministe (comme _build_language_group_features le faisait
# déjà pour la langue, généralisé à toutes les zones structurelles) et
# produit directement des scénarios finaux (category + target_labels) —
# ces éléments ne sont donc PLUS envoyés au LLM du tout. Seuls les
# éléments réellement non-navigationnels (formulaires, boutons d'action,
# auth...) continuent d'alimenter _extract_features_with_coverage,
# INCHANGÉE ci-dessus : elle reçoit juste moins d'éléments, et plus aucun
# élément hover/langue (déjà retirés en amont par navigation_discovery_service,
# donc _build_hover_menu_features / _build_language_group_features
# deviennent des no-op silencieux — ils restent en place comme filet de
# sécurité si jamais un élément de nav passait entre les mailles).
# ─────────────────────────────────────────────────────────────────────────────

def _display_label(label: str | None) -> str:
    """Retire le préfixe [zone] éventuel d'un label, en gardant la casse
    d'origine (contrairement à _label_core_text, utilisé pour l'AFFICHAGE
    dans les étapes de scénario, pas pour la comparaison)."""
    raw = label or ""
    return raw.split("]", 1)[-1].strip() if "]" in raw else raw.strip()


def _scenario_dict_from_nav_category(cat: dict) -> dict:
    """
    Convertit une catégorie de navigation (100% déterministe, voir
    navigation_discovery_service.discover_navigation_categories) DIRECTEMENT
    en dict de TestScenario. CE SONT DES SCÉNARIOS FINAUX : ils ne repassent
    JAMAIS par le LLM de génération de scénarios (SCENARIO_GENERATION_PROMPT
    ci-dessous) — même statut que les features "_template" existantes (voir
    _template_scenario_for_feature un peu plus bas).

    `category` + `target_labels` portent l'ensemble des cibles ; c'est ce
    que script_service._generate_actions_for_category lit pour générer une
    boucle Selenium (clic + vérification de navigation) sur chaque cible,
    plutôt qu'un grounding step-par-step comme pour les scénarios "métier".

    FIX lisibilité : `steps` listait auparavant une seule ligne générique
    ("Pour chaque élément de « X », cliquer et vérifier...") — impossible
    de savoir, avant exécution, QUELLES options concrètes seront testées
    (ex: le sous-menu "Budget" contient "Finance" et "Comptabilité" : on
    veut les VOIR dans le scénario). On génère maintenant UNE étape lisible
    par cible réelle.
    """
    options = [_display_label(lbl) for lbl in cat["target_labels"]]
    options = [o for o in options if o]
    category = cat["category"]

    if category.startswith("submenu:"):
        trigger = category.split(":", 1)[1]
        steps = ["Ouvrir la page", f"Survoler « {trigger} » pour ouvrir le sous-menu"]
        steps += [f"Cliquer sur « {opt} » et vérifier le changement de page" for opt in options]
        result_tail = "est cliquable et déclenche un changement observable (URL ou titre différent) après le clic."
    elif category.startswith("downloads"):
        steps = ["Ouvrir la page"]
        steps += [f"Vérifier que le fichier « {opt} » est accessible (requête HTTP)" for opt in options]
        result_tail = "pointe vers un fichier réellement accessible (aucun clic n'est effectué : un téléchargement ne produit pas de navigation observable)."
    elif category == "contact":
        steps = ["Ouvrir la page"]
        steps += [f"Vérifier le canal « {opt} » (format valide / destination accessible)" for opt in options]
        result_tail = "a un format valide (téléphone/email) ou pointe vers une destination accessible."
    else:
        steps = ["Ouvrir la page"]
        steps += [f"Cliquer sur « {opt} » et vérifier le changement de page" for opt in options]
        result_tail = "est cliquable et déclenche un changement observable (URL ou titre différent) après le clic."

    return {
        "title": cat["name"],
        "steps": steps,
        "expected_result": (
            f"Chaque élément de « {cat['name']} » ({', '.join(options[:8])}"
            f"{'...' if len(options) > 8 else ''}) {result_tail}"
        ),
        "objective": cat.get("description", ""),
        "preconditions": [],
        "category": cat["category"],
        "target_labels": cat["target_labels"],
    }


async def _discover_structure(analysis: UIAnalysisResult) -> tuple[dict, list[dict]]:
    """
    Point d'entrée unique de la nouvelle étape amont. Retourne :
      - features_data : même format qu'avant (page_purpose + features),
        pour rester compatible avec tout code qui consomme déjà ce dict
        (report_service, test_planning_service...) — enrichi de deux clés
        additives ("app_type", "nav_categories") que le code existant peut
        ignorer sans risque.
      - nav_categories : la liste brute des catégories de navigation,
        utilisée par generate_all_scenarios() pour produire les scénarios
        finaux correspondants.
    """
    graph = build_ui_graph(analysis)
    understanding = await understand_application(graph)

    # Business Domain Understanding Agent : référentiel de capacités
    # métier (ex: "traduction", "favoris", "historique" pour un
    # traducteur), utilisé ci-dessous par Feature Discovery pour RATTACHER
    # les éléments plutôt que d'inventer des features lot par lot sans
    # référentiel partagé. En échec, business_domain.capabilities == []
    # -> Feature Discovery retombe exactement sur son comportement actuel.
    business_domain = await understand_business_domain(understanding, graph)

    nav_categories = discover_navigation_categories(graph)

    # Les éléments déjà couverts par une catégorie de navigation ne sont
    # JAMAIS envoyés au Feature Discovery Agent (LLM) — c'est ce qui évite
    # structurellement la fragmentation en un scénario par lien, plutôt que
    # de compter sur le prompt pour bien vouloir les regrouper.
    categorized_labels = all_categorized_labels(nav_categories)
    remaining_elements = [el for el in analysis.elements if el.label not in categorized_labels]
    remaining_analysis = analysis.copy(update={"elements": remaining_elements})

    features_data = await _extract_features_with_coverage(
        remaining_analysis,
        capabilities=business_domain.capabilities,
        capability_descriptions=business_domain.capability_descriptions,
    )

    # Le page_purpose de l'Application Understanding Agent (vue d'ensemble,
    # un seul appel) prime sur celui, potentiellement partiel, dérivé du
    # premier lot de features LLM.
    if understanding.app_purpose:
        features_data["page_purpose"] = understanding.app_purpose

    # Workflow Discovery : regroupe les features métier restantes en
    # parcours utilisateur (informationnel — n'entre pas dans le grounding
    # Selenium, mais est exposé au rapport et disponible pour
    # test_planning_service si besoin d'ordonnancement futur).
    features_data["features"] = await discover_workflows(
        features_data.get("features", []), features_data.get("page_purpose", ""),
    )

    features_data["app_type"] = understanding.app_type
    features_data["nav_categories"] = nav_categories
    features_data["domain_type"] = business_domain.domain_type
    features_data["business_capabilities"] = business_domain.capabilities
    return features_data, nav_categories


# ─────────────────────────────────────────────────────────────────────────────
# ÉTAPE 2 : Génération des scénarios depuis les fonctionnalités
# ─────────────────────────────────────────────────────────────────────────────

SCENARIO_GENERATION_PROMPT = """You are a senior QA automation engineer writing Selenium-ready test scenarios.

Application purpose: {page_purpose}

=== FEATURES TO TEST (ordered by priority) ===
{features_list}

=== ALL AVAILABLE UI ELEMENTS (use EXACT labels in your steps) ===
{elements_list}

=== WHAT MAKES A GOOD SCENARIO ===

1. ATOMIC — one scenario verifies ONE precise, narrow behavior. Do not
   chain unrelated actions into a single scenario just because they
   happen on the same page. If a feature naturally involves a short
   sequence (e.g. choose a file, then submit), that is fine as ONE
   scenario, but the "expected_result" must be about the immediate,
   direct consequence of the LAST action — never a downstream business
   outcome you cannot verify in the DOM.

2. EXPECTED RESULT MUST BE OBSERVABLE IN THE UI/DOM — something a
   Selenium script can literally check (an element became visible,
   enabled/disabled, a value/text changed, an attribute changed, a new
   element appeared, the URL changed). NEVER describe a semantic/business
   truth that cannot be verified automatically.
   ❌ BAD  : "La traduction est correcte" / "Le contenu est bien trié"
   ✅ GOOD : "Le bouton devient actif" / "Le sélecteur de fichiers s'ouvre"
             / "Le champ contient le texte saisi" / "Le clic est accepté
             (aucune erreur visible)"

3. BE SPECIFIC — name the exact element, never a generic reference.
   ❌ "Cliquer sur un bouton."
   ✅ "Cliquer sur le bouton « Traduire des documents »."

4. TYPICAL BEHAVIOR TO TEST, BY COMPONENT TYPE (use as guidance for what
   "observable result" to check for each element type you test):
     - button        -> click accepted / becomes active or disabled / stays visible
     - input (text)   -> typing fills the field / clearing empties it / max length is enforced
     - textarea       -> long text is accepted / line breaks are preserved
     - select         -> chosen value is reflected in the UI
     - checkbox       -> checked/unchecked state toggles visibly
     - radio          -> selection moves to the clicked option
     - file input     -> picker opens / chosen file name appears / wrong format is rejected
     - link           -> navigation occurs or the correct destination is reachable
     - form            -> submission is accepted or a validation state appears
     - table          -> sort/pagination/search changes the visible rows

5. POSITIVE AND NEGATIVE CASES — do not only write the nominal ("happy
   path") case if a feature clearly allows an obvious error case (e.g. a
   file upload also deserves a "wrong file type" case if the app matches
   this behavior). Do not invent negative cases that make no sense for
   the feature.

6. NO DUPLICATES — never produce two scenarios that test the same action
   on the same element.

7. Use the EXACT element labels from the elements list above (including
   the [section] prefix). Do not translate them.

8. Max 6 steps per scenario.

9. LANGUAGE: "title", each entry in "steps", and "expected_result" MUST be
   written in FRENCH — no exceptions, even if the feature name itself is
   in another language (e.g. a German or English link label). Translate
   your OWN sentences to French; only the element labels stay untranslated.

10. Do NOT copy the example below verbatim. It only shows the JSON
    structure — every value in it must be REPLACED by content specific to
    the feature you are testing.

Respond ONLY with valid JSON:
{{
  "scenarios": [
    {{
      "title": "Vérifier l'envoi d'un document PDF",
      "steps": [
        "Étape 1 : Cliquer sur « Choisir un fichier »",
        "Étape 2 : Sélectionner un fichier PDF valide",
        "Étape 3 : Cliquer sur « Traduire »"
      ],
      "expected_result": "Le nom du fichier apparaît dans la zone de sélection"
    }}
  ]
}}
"""


_TEMPLATE_LEAK_PREFIXES = ("feature name:", "feature:")
_TEMPLATE_LEAK_STEP_RE = re.compile(r"^(step \d+)\s*:\s*", re.IGNORECASE)

# Mots-clés signalant un "résultat attendu" subjectif / non-vérifiable
# automatiquement par Selenium (jugement de qualité ou de contenu métier),
# plutôt qu'un état d'interface observable. Ex: "la traduction est
# correcte" ne peut être vérifié par du code — "le bouton devient actif"
# oui. Détection best-effort (mots-clés), pas une preuve formelle.
_NON_OBSERVABLE_RESULT_RE = re.compile(
    r"\b("
    r"correcte?s?|juste|exact|pertinen\w*|fid[eè]le|de bonne qualit[ée]|"
    r"bien traduit\w*|bien fait\w*|fonctionne bien|comme (attendu|pr[ée]vu)|"
    r"appropri[ée]e?"
    r")\b",
    re.IGNORECASE,
)


def _sanitize_scenario_dict(raw: dict, fallback_title: str) -> dict:
    """
    Filet de sécurité déterministe : un petit modèle (llama3.2:1b) recopie
    parfois littéralement le gabarit du prompt au lieu de le remplacer
    (ex: title="Feature name: X", steps=["Step 1: Navigate to the page"]),
    ou écrit un "expected_result" subjectif/non-observable (ex: "la
    traduction est correcte") que Selenium ne peut pas vérifier. On
    nettoie ces artefacts ici plutôt que de compter uniquement sur le
    prompt, qui ne garantit jamais 100% du comportement d'un petit modèle.
    """
    cleaned = dict(raw)

    title = str(cleaned.get("title") or "").strip()
    title_low = title.lower()
    if any(title_low.startswith(p) for p in _TEMPLATE_LEAK_PREFIXES):
        # "Feature name: X" -> "X" (garde la partie utile après le préfixe)
        title = title.split(":", 1)[-1].strip() or fallback_title
    cleaned["title"] = title or fallback_title

    steps = cleaned.get("steps") or []
    cleaned["steps"] = [
        _TEMPLATE_LEAK_STEP_RE.sub("", str(s)).strip() or str(s)
        for s in steps
    ]

    expected = str(cleaned.get("expected_result") or "").strip()
    if expected and _NON_OBSERVABLE_RESULT_RE.search(expected):
        cleaned["expected_result"] = (
            "L'action s'exécute sans erreur visible et l'interface reflète "
            "le changement attendu (élément mis à jour/affiché/activé)."
        )

    return cleaned


def _template_scenario_for_feature(feature: dict) -> dict:
    """
    Construit un TestScenario directement en code, sans appel LLM, pour les
    features de secours (_ensure_element_coverage) : un simple clic sur un
    lien/bouton de footer n'a pas besoin d'un modèle de langage pour
    rédiger 3 lignes de scénario — et ça élimine à la fois le temps de
    génération ET les artefacts de langue/gabarit vus avec un petit modèle.

    Chaque type de composant a son propre résultat attendu OBSERVABLE
    (jamais un jugement de qualité/contenu — ex: "le clic est accepté",
    pas "la traduction est correcte"), conforme à la table de
    comportements par type de composant utilisée aussi dans le prompt LLM.
    """
    label = (feature.get("elements") or [feature.get("name", "")])[0]
    core = _label_core_text(label) or label
    el_type = feature.get("_el_type", "button")

    if feature.get("_requires_hover"):
        hover_label = feature.get("_hover_target_label") or "le menu"
        title = f"Vérifier la navigation (sous-menu) : {core}"
        return {
            "title": title[:120],
            "steps": [
                "Étape 1 : Ouvrir la page.",
                f"Étape 2 : Survoler « {hover_label} » pour révéler le sous-menu.",
                f"Étape 3 : Cliquer sur « {core} » dans le sous-menu.",
            ],
            "expected_result": (
                "Le sous-menu apparaît au survol et la navigation vers la "
                "destination du lien a lieu (changement d'URL, nouvel onglet, "
                "ou ouverture visible)."
            ),
        }

    if el_type == "language_group":
        cores = feature.get("_language_cores") or [core]
        steps = ["Étape 1 : Ouvrir la page."]
        for i, lang_core in enumerate(cores, start=2):
            steps.append(f"Étape {i} : Sélectionner « {lang_core} » et vérifier que l'interface change de langue.")
        return {
            "title": "Vérifier le changement de langue",
            "steps": steps[:12],
            "expected_result": (
                "Chaque sélection de langue met à jour le contenu affiché "
                "dans la langue correspondante (texte, direction du texte, "
                "ou URL selon l'implémentation)."
            ),
        }

    if el_type == "auth_register":
        trigger_display = _label_core_text(feature.get("_auth_trigger_label")) or "S'inscrire"
        return {
            "title": "Vérifier l'inscription",
            "steps": [
                "Étape 1 : Ouvrir la page.",
                f"Étape 2 : Cliquer sur « {trigger_display} ».",
                "Étape 3 : Remplir le champ email avec une adresse de test (ex : test.user@example.com).",
                "Étape 4 : Remplir le champ mot de passe avec un mot de passe valide (ex : TestPassword123!).",
                "Étape 5 : Valider le formulaire d'inscription.",
            ],
            "expected_result": (
                "Le formulaire d'inscription s'affiche après le clic, accepte les données "
                "de test, et confirme la création du compte (ou affiche un message de "
                "confirmation / vérification par email)."
            ),
        }

    if el_type == "auth_login":
        trigger_label = feature.get("_auth_trigger_label")
        trigger_display = _label_core_text(trigger_label) if trigger_label else None
        oauth_options = feature.get("_oauth_options") or []
        steps = ["Étape 1 : Ouvrir la page."]
        n = 2
        if trigger_display:
            steps.append(f"Étape {n} : Cliquer sur « {trigger_display} ».")
            n += 1
        steps.append(f"Étape {n} : Remplir le champ email/identifiant avec une adresse de test (ex : test.user@example.com).")
        n += 1
        steps.append(f"Étape {n} : Remplir le champ mot de passe avec un mot de passe de test.")
        n += 1
        steps.append(f"Étape {n} : Valider le formulaire de connexion.")
        n += 1
        for provider, display_label in oauth_options:
            steps.append(f"Étape {n} : Vérifier la présence et la cliquabilité du bouton « {display_label} » (connexion via {provider}).")
            n += 1
        title = "Vérifier la connexion"
        if oauth_options:
            title += f" (dont options : {', '.join(p for p, _ in oauth_options)})"
        return {
            "title": title,
            "steps": steps[:14],
            "expected_result": (
                "Le formulaire de connexion accepte les identifiants de test et redirige "
                "vers l'espace utilisateur (ou affiche un message d'erreur cohérent si le "
                "compte n'existe pas). Chaque option de connexion externe "
                "(Google/Microsoft/...) présente sur la page est visible et cliquable."
            ),
        }

    if el_type == "auth_forgot_password":
        trigger_display = _label_core_text(feature.get("_auth_trigger_label")) or "Mot de passe oublié"
        return {
            "title": "Vérifier la réinitialisation du mot de passe",
            "steps": [
                "Étape 1 : Ouvrir la page.",
                f"Étape 2 : Cliquer sur « {trigger_display} ».",
                "Étape 3 : Remplir le champ email avec l'adresse associée au compte (ex : test.user@example.com).",
                "Étape 4 : Valider la demande de réinitialisation.",
            ],
            "expected_result": (
                "Un message de confirmation s'affiche indiquant qu'un email de "
                "réinitialisation a été envoyé (sans révéler si le compte existe "
                "réellement, pour des raisons de sécurité)."
            ),
        }

    if el_type == "link":
        title = f"Vérifier la navigation : {core}"
        action = f"Cliquer sur le lien « {core} »."
        outcome = "La navigation vers la destination du lien a lieu (changement d'URL, nouvel onglet, ou ouverture visible)."
    elif el_type == "checkbox":
        title = f"Vérifier la case à cocher : {core}"
        action = f"Cliquer sur la case à cocher « {core} »."
        outcome = "L'état coché/décoché de la case change visiblement après le clic."
    elif el_type == "radio":
        title = f"Vérifier le bouton radio : {core}"
        action = f"Cliquer sur le bouton radio « {core} »."
        outcome = f"Le bouton radio « {core} » devient sélectionné."
    elif el_type == "select":
        title = f"Vérifier le sélecteur : {core}"
        action = f"Ouvrir le sélecteur « {core} » et choisir une option."
        outcome = "La valeur choisie est reflétée dans l'interface."
    else:  # button et types non spécifiquement gérés
        title = f"Vérifier le clic : {core}"
        action = f"Cliquer sur le bouton « {core} »."
        outcome = "Le clic est accepté (aucune erreur visible, l'élément réagit)."

    return {
        "title": title[:120],
        "steps": [
            "Étape 1 : Ouvrir la page.",
            f"Étape 2 : {action}",
        ],
        "expected_result": outcome,
    }


async def _generate_scenarios_from_features(
    features_data: dict,
    analysis: UIAnalysisResult,
) -> list[dict]:
    """
    Étape 2 : Génère un scénario par fonctionnalité identifiée.

    Découpé en LOTS de SCENARIO_GEN_BATCH_SIZE fonctionnalités : demander
    d'un coup 10+ scénarios détaillés (jusqu'à 6 étapes chacun) à un petit
    modèle dépasse vite num_predict -> le JSON de fin de liste est tronqué
    -> les DERNIERES fonctionnalités (souvent les onglets secondaires
    listés en fin de prompt, ex: Correcteur/Vocabulaire) n'obtiennent
    jamais leur scénario alors qu'elles avaient pourtant été détectées.
    """
    features = features_data.get("features", [])
    page_purpose = features_data.get("page_purpose", analysis.raw_description or "")

    # Les features marquées "_template" (filet de sécurité _ensure_element_
    # coverage : liens/boutons triviaux non repérés par le LLM d'extraction)
    # ne passent PAS par le LLM ici — voir generate_all_scenarios, qui leur
    # génère un scénario déterministe via _template_scenario_for_feature.
    # C'est le principal gain de temps : sur une page à footer riche, ça
    # peut retirer 15-20 appels LLM inutiles pour de simples clics de lien.
    features = [f for f in features if not f.get("_template")]

    if not features:
        return []

    # Trie par priorité : high → medium → low
    priority_order = {"high": 0, "medium": 1, "low": 2}
    features_sorted = sorted(
        features,
        key=lambda f: priority_order.get(f.get("priority", "medium"), 1)
    )

    # Limite au nombre de scénarios voulu (cap global de sécurité)
    features_to_test = features_sorted[:MAX_SCENARIOS]

    elements_list = "\n".join(
        f"  [{el.type.upper()}] {el.label}"
        + (f"  →  {el.possible_destination}" if el.possible_destination else "")
        for el in analysis.elements
    )

    # FIX PERF : ces lots étaient auparavant traités SÉQUENTIELLEMENT (boucle
    # for + await), donc le temps total = somme du temps de chaque lot. On
    # les lance maintenant en concurrence (borné par SCENARIO_GEN_CONCURRENCY,
    # même logique que test_planning_service._run_feature), ce qui divise le
    # temps total par ~SCENARIO_GEN_CONCURRENCY quand Ollama peut traiter
    # plusieurs requêtes en parallèle (OLLAMA_NUM_PARALLEL > 1).
    batches_input = [
        features_to_test[i:i + SCENARIO_GEN_BATCH_SIZE]
        for i in range(0, len(features_to_test), SCENARIO_GEN_BATCH_SIZE)
    ]
    semaphore = asyncio.Semaphore(SCENARIO_GEN_CONCURRENCY)

    async def _run_batch(batch: list[dict]) -> list[dict]:
        async with semaphore:
            return await _generate_scenarios_batch(batch, page_purpose, elements_list)

    results = await asyncio.gather(
        *(_run_batch(batch) for batch in batches_input),
        return_exceptions=True,
    )

    all_scenarios: list[dict] = []
    for r in results:
        if isinstance(r, Exception) or not r:
            continue
        all_scenarios.extend(r)

    return all_scenarios


async def _generate_scenarios_batch(
    features_batch: list[dict],
    page_purpose: str,
    elements_list: str,
) -> list[dict]:
    """Génère les scénarios pour UN lot de fonctionnalités."""
    features_list = "\n".join(
        f"  {i+1}. [{f.get('priority','medium').upper()}] {f['name']}: {f['description']}"
        + (f"\n     Elements: {f.get('elements', [])}" if f.get('elements') else "")
        for i, f in enumerate(features_batch)
    )

    prompt = SCENARIO_GENERATION_PROMPT.format(
        page_purpose=page_purpose,
        features_list=features_list,
        elements_list=elements_list,
    )

    # num_predict proportionnel au nombre de scénarios demandés dans ce lot
    num_predict = min(3500, 900 + len(features_batch) * 400)

    payload = {
        "model": TEXT_MODEL,
        "prompt": prompt,
        "stream": True,
        # FIX PERF : idem _extract_features_batch — même cause, même fix.
        "keep_alive": "5m",
        "format": "json",
        "options": {
            "temperature": 0.3,
            "num_predict": num_predict,
            "num_ctx": 2048,
        },
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
    Pipeline :
    1. LLM extrait les fonctionnalités réelles de la page
    2. Test Planning Agent (test_planning_service) décide, PAR FEATURE, quels
       cas tester (nominal + cas négatifs pertinents : champ vide,
       identifiants invalides, injection...) puis génère un scénario par cas
    3. Les features "triviales" (garantie de couverture) restent générées
       directement en code, sans appel LLM

    IMPORTANT : c'est CETTE fonction que main.py appelle pour les 3 modes
    (HTML / vision / DOM live) — avant ce changement, elle ne générait
    qu'UN scénario "nominal" par feature (_generate_scenarios_from_features),
    alors que test_planning_service.py (cas nominal + négatifs, injection,
    champs vides...) existait déjà mais n'était utilisé QUE par le pipeline
    LangGraph (/generate-stream-graph). Résultat : quiconque utilisait les
    routes "classiques" de main.py n'avait jamais les cas d'erreur/limite,
    même si le code pour les produire existait. On unifie les deux chemins
    ici pour que main.py en bénéficie aussi.
    """

    # ── Étape 1 : Comprendre ce que fait la page. Remplace l'ancien appel
    # direct à _extract_features_with_coverage(analysis) : la navigation
    # (navbar/footer/sous-menus/liens externes) est désormais traitée
    # DÉTERMINISTIQUEMENT par navigation_discovery_service AVANT que quoi
    # que ce soit ne parte au LLM (voir _discover_structure ci-dessus) —
    # c'est la correction du problème "un scénario par lien". ────────────
    features_data, nav_categories = await _discover_structure(analysis)
    nav_scenarios = [_scenario_dict_from_nav_category(c) for c in nav_categories]

    # ── Étape 2 : Générer les scénarios ───────────────────────────────────────
    # Les features "réelles" (extraites par le LLM) passent par le Test
    # Planning Agent, qui leur attribue nominal + cas négatifs selon des
    # règles déterministes (login -> mauvais mdp/champ vide/injection,
    # formulaire -> champ vide/texte trop long, etc.) puis génère UN appel
    # LLM par feature couvrant tous ses cas d'un coup (voir
    # test_planning_service.generate_planned_scenarios).
    # Les features "_template" (filet de sécurité, éléments triviaux comme
    # les liens de footer) restent générées directement en code — voir
    # _template_scenario_for_feature. Gros gain de temps : ces dernières
    # peuvent représenter la majorité des features sur une page à footer
    # riche, sans qu'un appel LLM ne soit jamais nécessaire pour elles.
    all_features = features_data.get("features", [])
    template_features = [f for f in all_features if f.get("_template")]
    llm_features = [f for f in all_features if not f.get("_template")]

    llm_scenarios: list[dict] = []
    if llm_features:
        plan = await build_test_plan({"features": llm_features})
        planned = await generate_planned_scenarios(
            plan, analysis,
            features_data.get("page_purpose", analysis.raw_description or ""),
            max_scenarios=MAX_SCENARIOS,
        )
        llm_scenarios = [s.dict() for s in planned]

    template_scenarios = [_template_scenario_for_feature(f) for f in template_features]

    # FIX Problème 7 : le vieux code faisait
    #   raw_scenarios = llm_scenarios + template_scenarios
    #   ... puis raw_scenarios[:MAX_SCENARIOS] plus bas.
    # Les scénarios "template" sont la GARANTIE DE COUVERTURE
    # (_ensure_element_coverage) — un scénario déterministe par élément
    # actionnable (lien, bouton, champ...) que le LLM n'a pas repris dans
    # ses features. Comme ils étaient ajoutés APRÈS les scénarios LLM dans
    # la liste, et que llm_scenarios pouvait déjà à lui seul atteindre
    # MAX_SCENARIOS (30) sur une page riche, le slice final coupait purement
    # et simplement TOUS les scénarios "template" — donc tous les liens de
    # navigation secondaires ("Mot de passe oublié", "Créer un compte",
    # "FAQ", "Aide", "Nous contacter", "Accueil"...) disparaissaient sans
    # aucune erreur visible. On ne cape désormais QUE la partie "libre"
    # générée par le LLM ; les scénarios de couverture garantie ne sont
    # JAMAIS tronqués.
    # Les scénarios de navigation (nav_scenarios), comme les scénarios
    # "_template" (garantie de couverture), sont déterministes et JAMAIS
    # tronqués par MAX_SCENARIOS — seule la partie "libre" issue du LLM
    # (llm_scenarios) est plafonnée. Voir le commentaire historique
    # juste en dessous sur le Problème 7 : même raisonnement, étendu à
    # ces nouveaux scénarios.
    raw_scenarios = llm_scenarios[:MAX_SCENARIOS] + template_scenarios + nav_scenarios

    # ── Fallback si le LLM échoue ─────────────────────────────────────────────
    if not raw_scenarios:
        raw_scenarios = _fallback_from_elements(analysis)

    scenarios = []
    for s in raw_scenarios:
        try:
            clean = _sanitize_scenario_dict(s, fallback_title=str(s.get("title") or "Scénario"))
            scenarios.append(TestScenario(**clean))
        except Exception:
            continue

    # Filet de sécurité anti-duplication : deux scénarios au titre identique
    # (normalisé) peuvent apparaître si le même élément est référencé à la
    # fois par une feature LLM et par la garantie de couverture avec un
    # libellé légèrement différent. On garde le premier rencontré.
    seen_titles: set[str] = set()
    deduped_scenarios = []
    for sc in scenarios:
        key = re.sub(r"\s+", " ", sc.title.strip().lower())
        if key in seen_titles:
            continue
        seen_titles.add(key)
        deduped_scenarios.append(sc)
    scenarios = deduped_scenarios

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
    Exposé pour main.py / orchestrator_service.py (pipeline LangGraph).
    Applique le même filtre anti-hallucination, la même garantie de
    couverture, ET la même garantie de détection de sous-menus que
    generate_all_scenarios(), pour que le Test Planning Agent
    (test_planning_service) ne construise jamais son plan sur des
    fonctionnalités inventées ni ne rate les liens de sous-menu.
    """
    features_data, _ = await _discover_structure(analysis)
    return features_data


async def extract_page_features_and_nav(analysis: UIAnalysisResult) -> tuple[dict, list[dict]]:
    """
    FIX PARITÉ orchestrateur LangGraph : extract_page_features() ci-dessus
    jetait silencieusement `nav_categories` (2e valeur de _discover_structure).
    generate_all_scenarios() (chemin main.py, celui utilisé par le frontend
    actuel) les transforme, lui, en scénarios de navigation déterministes
    via _scenario_dict_from_nav_category — jamais soumis au LLM, jamais
    tronqués par MAX_SCENARIOS. Le pipeline orchestré (orchestrator_service.py,
    routes /generate-stream-graph*) n'avait AUCUN moyen d'obtenir ces mêmes
    scénarios : il perdait donc toute la navigation (mega-menu, sous-menus)
    dès que le fonctionnel passait par cette variante. Cette fonction
    expose les deux valeurs pour que orchestrator_service.py puisse les
    récupérer.
    """
    return await _discover_structure(analysis)


def nav_scenario_dicts(nav_categories: list[dict]) -> list[dict]:
    """Expose _scenario_dict_from_nav_category() à orchestrator_service.py."""
    return [_scenario_dict_from_nav_category(c) for c in nav_categories]