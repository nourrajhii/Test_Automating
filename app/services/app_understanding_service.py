"""
app_understanding_service.py
------------------------------
Application Understanding Agent : "Quel est le rôle de cette interface ?"

PROBLÈME RÉSOLU
----------------
Avant ce module, `page_purpose` était simplement récupéré du PREMIER lot
de `_extract_features_batch` qui en renvoyait un (scenario_service.py,
`_extract_features`, ligne ~124 : `if not page_purpose and data.get(...)`).
Autrement dit, la compréhension de l'application entière dépendait d'un
sous-ensemble arbitraire de FEATURE_EXTRACT_BATCH_SIZE (20) éléments — la
compréhension était un SOUS-PRODUIT accidentel de l'extraction de
features, jamais une étape à part entière avec sa propre vue d'ensemble.

CE MODULE fait UN SEUL appel LLM, sur le RÉSUMÉ du UI Semantic Graph
(ui_graph_service.summary_for_llm — quelques centaines de tokens, jamais
tronqué même pour une page à 90 éléments), avant toute extraction de
features. Sa sortie (app_type, app_purpose, zone_roles) est ensuite
injectée dans le prompt du Feature Discovery Agent (scenario_service),
qui n'a donc plus besoin de deviner le rôle de la page lot par lot.

Volontairement pas de fallback complexe ici : si le LLM échoue, on
retombe sur une description minimale déterministe (page_type déjà connu)
plutôt que de bloquer le pipeline — même philosophie défensive que le
reste du projet (voir generate_all_scenarios._fallback_from_elements).
"""
from __future__ import annotations

import httpx
import json
import logging
import re
from dataclasses import dataclass

from app.config import OLLAMA_BASE_URL, TEXT_MODEL

logger = logging.getLogger("app_understanding")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[app_understanding] %(message)s"))
    logger.addHandler(_h)
from app.services.ui_graph_service import UISemanticGraph


APP_UNDERSTANDING_PROMPT = """You are a senior QA architect. Below is a
STRUCTURAL SUMMARY of a web/app interface (zones, element counts, sample
labels) — NOT the raw HTML, a condensed overview so you can reason about
the application AS A WHOLE rather than element by element.

=== STRUCTURAL SUMMARY ===
{summary}

YOUR TASK:
Identify what kind of application this is and what each zone is FOR, so
that a QA engineer downstream can decide which categories of tests make
sense (a login form does not need the same tests as a public navigation
menu).

Respond ONLY with valid JSON, no markdown, no explanation:
{{
  "app_type": "one of: public_website, dashboard, erp, crm, ecommerce, saas_app, admin_panel, form_heavy_app, other",
  "app_purpose": "one sentence, EN FRANCAIS, describing what this application/page is for",
  "zone_roles": {{
    "<zone name from the summary>": "one short phrase, EN FRANCAIS, describing what this zone is for (ex: 'menu de navigation principal', 'formulaire de connexion', 'liens institutionnels de bas de page')"
  }}
}}
"""


@dataclass
class AppUnderstanding:
    app_type: str
    app_purpose: str
    zone_roles: dict[str, str]


def _fallback_understanding(graph: UISemanticGraph) -> AppUnderstanding:
    return AppUnderstanding(
        app_type="other",
        app_purpose=f"Interface de type '{graph.page_type}' ({graph.element_count} éléments détectés).",
        zone_roles={z: "" for z in graph.zones},
    )


async def understand_application(graph: UISemanticGraph) -> AppUnderstanding:
    """
    Point d'entrée. Appelé UNE fois par run de pipeline, avant
    l'extraction de features (voir scenario_service._extract_features_with_coverage).
    """
    prompt = APP_UNDERSTANDING_PROMPT.format(summary=graph.summary_for_llm())

    payload = {
        "model": TEXT_MODEL,
        "prompt": prompt,
        "stream": True,
        "keep_alive": "5m",
        "format": "json",
        "options": {"temperature": 0.1, "num_predict": 700, "num_ctx": 2048},
    }

    try:
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
            data = json.loads(full.strip())
        except Exception:
            match = re.search(r"\{[\s\S]*\}", full)
            data = json.loads(match.group()) if match else {}

        if not data:
            logger.warning(
                "Réponse LLM vide ou JSON invalide -> compréhension neutre par "
                "défaut. Réponse brute reçue (500 premiers caractères) : %r",
                full[:500],
            )
            return _fallback_understanding(graph)

        return AppUnderstanding(
            app_type=data.get("app_type") or "other",
            app_purpose=data.get("app_purpose") or "",
            zone_roles=data.get("zone_roles") or {},
        )

    except Exception as e:
        logger.warning(
            "Application Understanding Agent indisponible (%s: %s) -> "
            "compréhension neutre par défaut, le pipeline continue.",
            type(e).__name__, e,
        )
        return _fallback_understanding(graph)
