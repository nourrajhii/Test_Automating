"""
business_domain_service.py
-----------------------------
Business Domain Understanding Agent : "Quelles sont les capacités métier
de cette application ?"

Se place APRÈS Application Understanding Agent (app_understanding_service,
qui répond à "quel TYPE d'application est-ce ?") et AVANT Feature
Discovery (scenario_service._extract_features_with_coverage).

POURQUOI CET AGENT EXISTE SÉPARÉMENT DE FEATURE DISCOVERY
------------------------------------------------------------
Avant lui, Feature Discovery était ASCENDANT : le LLM regardait un lot
d'éléments et inventait des features à partir de ce qu'il voyait, lot par
lot, sans référentiel commun. Deux lots pouvaient nommer différemment la
même fonctionnalité réelle, et rien ne garantissait qu'une fonctionnalité
métier attendue (ex: "favoris" sur un traducteur) soit reconnue si ses
éléments UI étaient répartis entre plusieurs lots ou peu explicites.

Cet agent répond D'ABORD, à partir du type d'application déjà identifié
(pas des éléments bruts), à la question "quelles capacités métier ce type
d'application propose-t-il typiquement ?" (ex: pour un traducteur :
traduction, dictionnaire, synonymes, prononciation, favoris, historique).
Cette liste sert ensuite de RÉFÉRENTIEL à Feature Discovery
(scenario_service._extract_features_batch, paramètre `capabilities`) :
au lieu d'inventer librement, il rattache les éléments détectés aux
capacités déjà nommées via le champ "business_capability". Une feature
peut rester non rattachée (business_capability=null) si aucune capacité
connue ne correspond — elle n'est JAMAIS perdue pour autant, elle est
juste non classée métier.

Défensif par construction : si le LLM échoue, `capabilities` est vide ->
Feature Discovery retombe EXACTEMENT sur son comportement actuel
(extraction libre, sans référentiel) — aucune régression possible.
"""
from __future__ import annotations

import httpx
import json
import logging
import re
from dataclasses import dataclass

from app.config import OLLAMA_BASE_URL, TEXT_MODEL
from app.services.app_understanding_service import AppUnderstanding
from app.services.ui_graph_service import UISemanticGraph

logger = logging.getLogger("business_domain")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[business_domain] %(message)s"))
    logger.addHandler(_h)


BUSINESS_DOMAIN_PROMPT = """You are a senior QA/product analyst. This
application has already been identified as:

Application type : {app_type}
Application purpose : {app_purpose}

STRUCTURAL SUMMARY (for grounding — do not invent capabilities that
clearly contradict what's actually present below):
{summary}

YOUR TASK:
List the BUSINESS CAPABILITIES this application typically offers, given
its type and purpose. Think business-level functions, NEVER UI elements
(never "bouton", "lien", "menu" — think "traduction", "virement",
"gestion des commandes").

Example — a translation site (Reverso-like): traduction, dictionnaire,
synonymes, prononciation, favoris, historique.
Example — a retail bank app: virement, paiement, consultation de solde,
historique des opérations, gestion du compte.
Example — an ERP: gestion des commandes, gestion des clients, gestion
des utilisateurs, reporting.

Respond ONLY with valid JSON, no markdown, no explanation:
{{
  "domain_type": "<type de domaine metier court, EN FRANCAIS>",
  "capabilities": ["<capacite 1, EN FRANCAIS, 2-4 mots>", "..."],
  "capability_descriptions": {{
    "<capacite>": "<1 phrase, EN FRANCAIS>"
  }}
}}
"""


@dataclass
class BusinessDomainUnderstanding:
    domain_type: str
    capabilities: list[str]
    capability_descriptions: dict[str, str]


def _fallback_domain() -> BusinessDomainUnderstanding:
    # Aucune capacité connue -> Feature Discovery retombe sur son
    # comportement actuel (extraction libre par zone, sans référentiel).
    return BusinessDomainUnderstanding(domain_type="inconnu", capabilities=[], capability_descriptions={})


async def understand_business_domain(
    app_understanding: AppUnderstanding, graph: UISemanticGraph,
) -> BusinessDomainUnderstanding:
    """
    Point d'entrée. Appelé UNE fois par run de pipeline, juste après
    app_understanding_service.understand_application() (voir
    scenario_service._discover_structure).
    """
    prompt = BUSINESS_DOMAIN_PROMPT.format(
        app_type=app_understanding.app_type,
        app_purpose=app_understanding.app_purpose or "inconnu",
        summary=graph.summary_for_llm(),
    )

    payload = {
        "model": TEXT_MODEL,
        "prompt": prompt,
        "stream": True,
        "keep_alive": "5m",
        "format": "json",
        "options": {"temperature": 0.25, "num_predict": 700, "num_ctx": 2048},
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
                "Réponse LLM vide ou JSON invalide -> compréhension métier neutre "
                "(aucune capacité connue). Réponse brute reçue (500 premiers "
                "caractères) : %r",
                full[:500],
            )
            return _fallback_domain()

        capabilities = [
            c.strip() for c in (data.get("capabilities") or [])
            if isinstance(c, str) and c.strip()
        ]
        if not capabilities:
            logger.warning(
                "Le LLM a répondu mais sans 'capabilities' exploitable -> "
                "référentiel métier vide. JSON reçu : %r", data,
            )
        return BusinessDomainUnderstanding(
            domain_type=data.get("domain_type") or "inconnu",
            capabilities=capabilities,
            capability_descriptions=data.get("capability_descriptions") or {},
        )
    except Exception as e:
        logger.warning(
            "Business Domain Understanding Agent indisponible (%s: %s) -> "
            "compréhension métier neutre, le pipeline continue.",
            type(e).__name__, e,
        )
        return _fallback_domain()
