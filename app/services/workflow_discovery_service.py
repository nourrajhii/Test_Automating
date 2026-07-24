"""
workflow_discovery_service.py
--------------------------------
Workflow Discovery Agent : "Quels sont les parcours utilisateur possibles ?"

Se place APRÈS le Feature Discovery Agent (scenario_service._extract_features)
et AVANT le Test Planning Agent (test_planning_service.build_test_plan).

PÉRIMÈTRE VOLONTAIREMENT ÉTROIT
---------------------------------
Ce n'est PAS une nouvelle passe de compréhension de l'UI — ça, c'est le
rôle de app_understanding_service (une fois) et de scenario_service
(par lot, pour les features). Ce module prend la liste des features DÉJÀ
extraites (donc déjà courte : quelques dizaines maximum, jamais des
centaines d'éléments) et répond à une question différente : est-ce que
plusieurs features forment ensemble un parcours logique (ex: "Se
connecter" précède "Accéder au tableau de bord") ?

Un seul appel LLM, sur une liste déjà réduite -> pas de risque de
troncature qui a justifié le batching ailleurs dans le projet.

SORTIE : chaque feature reçoit un champ optionnel "_workflow" (nom du
parcours) et "_workflow_order" (position dans le parcours, 0 = premier).
Les features non rattachées à un parcours (features "autonomes", ex.
"Changer la langue") gardent _workflow=None. AUCUN champ existant n'est
retiré : test_planning_service.build_test_plan continue de fonctionner
à l'identique s'il ignore ces nouveaux champs (il ne lit que
feature["name"]/["description"]).
"""
from __future__ import annotations

import httpx
import json
import logging
import re

from app.config import OLLAMA_BASE_URL, TEXT_MODEL

logger = logging.getLogger("workflow_discovery")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[workflow_discovery] %(message)s"))
    logger.addHandler(_h)


WORKFLOW_DISCOVERY_PROMPT = """You are a senior QA engineer. Below is the
list of FEATURES already identified on this application (navigation links
are handled separately — ignore that they are absent here).

Application purpose: {app_purpose}

=== FEATURES ===
{features_list}

YOUR TASK:
Group features that together form a single coherent USER WORKFLOW (a
sequence a real user would follow, e.g. "Se connecter" -> "Accéder au
tableau de bord" -> "Se déconnecter"). A feature that stands alone (no
natural sequence with another) is its own workflow of one feature.

CRITICAL RULES:
- Only use feature names copied VERBATIM from the list above.
- Do not invent features that are not in the list.
- Preserve the logical order within each workflow (first step first).

Respond ONLY with valid JSON, no markdown, no explanation:
{{
  "workflows": [
    {{
      "name": "Nom court du parcours, EN FRANCAIS (ex: Authentification)",
      "features": ["nom exact de la feature 1", "nom exact de la feature 2"]
    }}
  ]
}}
"""


def _batch_num_predict(n_features: int) -> int:
    return min(1800, 500 + n_features * 60)


async def discover_workflows(features: list[dict], app_purpose: str) -> list[dict]:
    """
    Entrée : liste de features (dicts, format scenario_service._extract_features).
    Sortie : la MÊME liste, dans le même ordre logique, chaque dict enrichi
    de "_workflow" (str | None) et "_workflow_order" (int). En cas
    d'échec LLM (modèle indisponible, JSON invalide...), retombe sur
    "chaque feature est son propre workflow" plutôt que de bloquer le
    pipeline — même politique défensive que le reste du projet.
    """
    if not features:
        return features

    named_features = [f for f in features if (f.get("name") or "").strip()]
    if not named_features:
        return features

    features_list = "\n".join(
        f'  - "{f.get("name")}" : {f.get("description", "")}'
        for f in named_features
    )
    prompt = WORKFLOW_DISCOVERY_PROMPT.format(
        app_purpose=app_purpose or "inconnue",
        features_list=features_list,
    )

    payload = {
        "model": TEXT_MODEL,
        "prompt": prompt,
        "stream": True,
        "keep_alive": "5m",
        "format": "json",
        "options": {
            "temperature": 0.2,
            "num_predict": _batch_num_predict(len(named_features)),
            "num_ctx": 2048,
        },
    }

    workflows: list[dict] = []
    try:
        full = ""
        timeout = httpx.Timeout(timeout=None)
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
        except Exception:
            match = re.search(r"\{[\s\S]*\}", full)
            data = json.loads(match.group()) if match else {}
        workflows = data.get("workflows", []) or []
        if not workflows:
            logger.warning(
                "Réponse LLM vide ou sans 'workflows' exploitable -> 1 workflow "
                "par feature (fallback). Réponse brute reçue (500 premiers "
                "caractères) : %r",
                full[:500],
            )
    except Exception as e:
        logger.warning(
            "Workflow Discovery Agent indisponible (%s: %s) -> 1 workflow par "
            "feature (fallback), le pipeline continue.",
            type(e).__name__, e,
        )
        workflows = []

    # Index nom de feature -> (workflow_name, position). Matching exact
    # d'abord (cas normal), puis substring en secours pour tolérer une
    # reformulation légère du LLM.
    assignment: dict[str, tuple[str, int]] = {}
    for wf in workflows:
        wf_name = (wf.get("name") or "").strip() or "Parcours"
        for pos, feat_name in enumerate(wf.get("features") or []):
            key = (feat_name or "").strip().lower()
            if key:
                assignment[key] = (wf_name, pos)

    def _find_assignment(name: str) -> tuple[str, int] | None:
        key = name.strip().lower()
        if key in assignment:
            return assignment[key]
        for k, v in assignment.items():
            if key and (key in k or k in key):
                return v
        return None

    enriched: list[dict] = []
    for f in features:
        f = dict(f)  # copie superficielle, ne mute pas l'entrée
        name = (f.get("name") or "").strip()
        found = _find_assignment(name) if name else None
        if found:
            f["_workflow"], f["_workflow_order"] = found
        else:
            f["_workflow"], f["_workflow_order"] = (name or "Fonctionnalité isolée"), 0
        enriched.append(f)

    return enriched
