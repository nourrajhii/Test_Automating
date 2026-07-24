"""
scenario_grounding_service.py
------------------------------
Scenario Grounding Engine (agent ⑤bis, entre Scenario Generation et
Selenium Generation).

PROBLÈME RÉSOLU
----------------
script_service._generate_actions() routait les actions Selenium par
MOT-CLÉ DU TITRE du scénario ("navigation" -> prends les 3 premiers liens
de la page), sans jamais lire le contenu réel de `scenario.steps`. Résultat
observé : un scénario "Cliquer sur ministère des finances" pouvait exécuter
un clic sur "Actualités & Evénements" à la place — n'importe quel élément
du même TYPE, pas celui réellement demandé.

Or `scenario.steps` contient déjà les libellés EXACTS des éléments
(scenario_service.FEATURE_EXTRACTION_PROMPT / test_planning_service.
BATCHED_SCENARIO_PROMPT demandent explicitement au LLM d'utiliser les
libellés verbatim de la liste d'éléments). L'information existait déjà,
elle n'était simplement jamais exploitée au moment de la génération
Selenium.

CE MODULE NE CHANGE NI TestScenario NI UIElement NI script_service dans
leur ensemble. Il ajoute une seule fonction pure :

    resolve_scenario_steps(scenario, analysis) -> list[ResolvedStep]

que script_service.py appelle pour savoir, PAR STEP, quel UIElement réel
manipuler (ou None si aucun match fiable — mieux vaut sauter un step que
deviner faux et cliquer sur autre chose).

STRATÉGIE DE MATCHING (du plus fiable au moins fiable)
--------------------------------------------------------
1. Détection de l'ACTION via mots-clés en tête de step (hover / click /
   type / check / assert / navigate).
2. Extraction du TEXTE CIBLE : en priorité le texte entre guillemets
   (le prompt LLM encourage `Cliquer sur "X"`), sinon le reste du step
   après retrait du verbe d'action et des mots vides usuels.
3. Comparaison du texte cible normalisé avec le label normalisé de chaque
   UIElement (égalité exacte > inclusion > similarité difflib), en gardant
   le meilleur score. Si le meilleur score est sous CONFIDENCE_THRESHOLD,
   le step est marqué "non résolu" plutôt que rattaché à un élément
   probablement faux.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from app.models.schemas import TestScenario, UIAnalysisResult, UIElement

# Sous ce score de similarité, on préfère ne PAS résoudre le step plutôt
# que de risquer un mauvais matching (mieux vaut un step ignoré qu'un clic
# sur le mauvais élément).
CONFIDENCE_THRESHOLD = 0.55

# Ordre important : "cliquer" doit être testé après "survoler" etc. mais
# comme chaque groupe est indépendant, l'ordre des groupes ci-dessous suit
# simplement la fréquence attendue dans les scénarios générés.
_ACTION_PATTERNS: list[tuple[set[str], str]] = [
    ({"ouvrir la page", "charger la page", "aller sur l'url", "naviguer vers l'url"}, "navigate"),
    ({"survoler", "survol", "hover", "passer la souris sur"}, "hover"),
    ({"cocher", "décocher", "decocher", "check"}, "check"),
    ({"saisir", "taper", "entrer", "remplir", "renseigner"}, "type"),
    ({"vérifier", "verifier", "verify", "s'assurer", "constater", "contrôler", "controler"}, "assert"),
    ({"cliquer", "clique", "click", "appuyer sur", "sélectionner", "selectionner", "choisir"}, "click"),
]


def _detect_action(step_text: str) -> str:
    t = step_text.lower()
    for keywords, action in _ACTION_PATTERNS:
        if any(k in t for k in keywords):
            return action
    return "click"  # action par défaut la plus fréquente dans les steps


_QUOTE_RE = re.compile(r'["“«]([^"”»]{1,80})["”»]')


def _extract_target_text(step_text: str) -> str | None:
    m = _QUOTE_RE.search(step_text)
    if m:
        return m.group(1).strip()

    t = step_text
    for keywords, _ in _ACTION_PATTERNS:
        for k in keywords:
            t = re.sub(rf"\b{re.escape(k)}\b", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"^\s*(sur|le|la|les|un|une|des|du|de|l['’])\s+", "", t.strip(), flags=re.IGNORECASE)
    t = t.strip(" :.-'\"()")
    return t or None


def _normalize(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"[^a-z0-9à-ÿ ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _label_core(label: str | None) -> str:
    """Retire le préfixe [contexte] (vision_parser_service) avant matching."""
    raw = label or ""
    text = raw.split("]", 1)[-1].strip() if "]" in raw else raw.strip()
    return _normalize(text)


@dataclass
class ResolvedStep:
    step_text: str
    action: str                 # navigate | hover | click | type | check | assert
    target_text: str | None
    element: UIElement | None
    confidence: float


def _best_match(target_norm: str, elements: list[UIElement]) -> tuple[UIElement | None, float]:
    if not target_norm:
        return None, 0.0
    best_el: UIElement | None = None
    best_score = 0.0
    for el in elements:
        core = _label_core(el.label)
        if not core:
            continue
        if core == target_norm:
            score = 1.0
        elif target_norm in core or core in target_norm:
            shorter, longer = sorted((target_norm, core), key=len)
            score = 0.85 * (len(shorter) / len(longer))
        else:
            score = difflib.SequenceMatcher(None, target_norm, core).ratio()
        if score > best_score:
            best_el, best_score = el, score
    return best_el, best_score


def resolve_scenario_steps(scenario: TestScenario, analysis: UIAnalysisResult) -> list[ResolvedStep]:
    """
    Résout chaque `scenario.steps[i]` (texte libre) vers l'UIElement réel
    qu'il désigne, dans l'ordre. Ne modifie ni `scenario` ni `analysis`.
    """
    resolved: list[ResolvedStep] = []
    for step in scenario.steps:
        action = _detect_action(step)

        if action in ("navigate", "assert"):
            # "Ouvrir la page" est déjà géré par le boilerplate Selenium
            # (driver.get). "Vérifier ..." n'a pas de cible DOM à cliquer/
            # remplir — hors scope de ce resolver (assertions génériques
            # déjà couvertes par _build_interaction_lines / report_service).
            resolved.append(ResolvedStep(step, action, None, None, 1.0))
            continue

        target_text = _extract_target_text(step)
        target_norm = _normalize(target_text) if target_text else ""
        element, score = _best_match(target_norm, analysis.elements)
        if score < CONFIDENCE_THRESHOLD:
            element = None

        resolved.append(ResolvedStep(step, action, target_text, element, score))

    return resolved


def _step_is_resolved(r: ResolvedStep) -> bool:
    # "hover" est traduit directement en XPath texte à partir de
    # target_text côté script_service._direct_hover_lines : il n'a pas
    # besoin d'un UIElement matché (le déclencheur de menu n'est souvent
    # PAS extrait comme UIElement à part entière — voir
    # html_parser_service._find_submenu_container). Ne pas le compter
    # comme "non résolu" tant que target_text a pu être extrait du step.
    if r.action == "hover":
        return r.target_text is not None
    return r.element is not None


def grounding_summary(resolved_steps: list[ResolvedStep]) -> dict:
    """Résumé exploitable en log/SSE : combien de steps résolus vs manqués."""
    actionable = [r for r in resolved_steps if r.action not in ("navigate", "assert")]
    unresolved = [r for r in actionable if not _step_is_resolved(r)]
    return {
        "total_actionable_steps": len(actionable),
        "resolved": len(actionable) - len(unresolved),
        "unresolved_steps": [r.step_text for r in unresolved],
    }
