"""
ui_graph_service.py
--------------------
UI Semantic Graph — première brique du nouveau pipeline :

    HTML/Screenshot → Parser → **UI Semantic Graph** → Application
    Understanding Agent → Feature Discovery → Workflow Discovery →
    QA Scenario Generator → Scenario Grounding → Selenium → Exécution → Rapport

PROBLÈME RÉSOLU
----------------
html_parser_service._get_context(tag) calcule déjà, pour CHAQUE élément,
une zone sémantique ("navbar", "footer", "header", "form", "auth"...).
Mais cette info n'était utilisée QUE comme préfixe texte dans
UIElement.label ("[navbar] Contact") — illisible pour du code de
regroupement, seulement pour l'affichage humain. Résultat : aucune couche
en aval ne pouvait savoir que 15 liens appartenaient au même menu, donc
scenario_service traitait chaque lien comme un cas isolé (cf. UIElement
possède maintenant un champ structuré `zone`, voir schemas.py).

CE MODULE NE RE-PARSE RIEN. Il construit, à PARTIR des UIElement déjà
produits par html_parser_service / vision_parser_service, une structure
agrégée : autant un graphe (relations hover trigger -> enfants, groupes de
navigation) qu'un résumé condensé destiné à être lu par un LLM SANS lui
envoyer les 90 éléments un par un (c'est ce résumé, et non la liste brute,
qui alimente app_understanding_service — voir ce fichier).

Aucune modification d'un service existant n'est requise pour ajouter ce
module : il est purement additif, consommé par scenario_service.py.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from app.models.schemas import UIAnalysisResult, UIElement


# Zones métier (voir navigation_discovery_service._NON_NAVIGATIONAL_ZONES) :
# un bouton OAuth ("Se connecter avec Google") pointe presque toujours
# vers une URL absolue externe (accounts.google.com...) et serait donc
# classé "externe" par la même heuristique qu'un simple lien de footer —
# alors qu'il s'agit d'une action d'authentification, pas de navigation.
# On l'exclut ici pour qu'il reste dans zone_map["auth"/"oauth"] SEULEMENT
# (jamais dans external_links), afin que navigation_discovery_service ne
# le catégorise pas non plus comme lien externe générique, et qu'il
# atteigne le Feature Discovery Agent.
_NON_NAVIGATIONAL_ZONES = {"auth", "oauth", "search", "form"}


def _label_core(label: str | None) -> str:
    raw = label or ""
    return (raw.split("]", 1)[-1].strip() if "]" in raw else raw.strip())


@dataclass
class ZoneNode:
    """Un noeud du graphe = une zone structurelle (navbar, footer, form...)."""
    zone: str
    elements: list[UIElement] = field(default_factory=list)

    @property
    def link_count(self) -> int:
        return sum(1 for e in self.elements if e.type == "link")

    @property
    def interactive_count(self) -> int:
        return len(self.elements)

    def sample_labels(self, n: int = 8) -> list[str]:
        return [_label_core(e.label) for e in self.elements[:n] if _label_core(e.label)]


@dataclass
class SubmenuNode:
    """Relation hover_trigger -> liens enfants (déjà détectée par html_parser_service)."""
    trigger_label: str
    children: list[UIElement] = field(default_factory=list)


@dataclass
class UISemanticGraph:
    zones: dict[str, ZoneNode]
    submenus: list[SubmenuNode]
    language_group: list[UIElement]
    external_links: list[UIElement]
    page_type: str
    element_count: int

    def non_navigational_elements(self) -> list[UIElement]:
        """
        Éléments qui ne sont PAS déjà pris en charge par une structure
        déterministe du graphe (hover, langue). Ce sont les seuls encore
        envoyés au Feature Discovery Agent (LLM) — tout le reste
        (navigation) est traité déterministiquement par
        navigation_discovery_service, JAMAIS par le LLM.
        """
        hover_ids = {id(el) for sm in self.submenus for el in sm.children}
        lang_ids = {id(el) for el in self.language_group}
        out = []
        for zone in self.zones.values():
            for el in zone.elements:
                if id(el) in hover_ids or id(el) in lang_ids:
                    continue
                out.append(el)
        return out

    def summary_for_llm(self, max_samples: int = 6) -> str:
        """
        Résumé COMPACT et déterministe de toute l'interface, en UN seul
        bloc de texte — c'est ce résumé (jamais la liste brute des 90
        éléments) qui est envoyé à app_understanding_service. Il tient
        sous quelques centaines de tokens même pour une page riche, donc
        pas de troncature côté petit modèle local.
        """
        lines = [f"Page type détecté (heuristique) : {self.page_type}", ""]
        for zone_name, node in sorted(self.zones.items(), key=lambda kv: -kv[1].interactive_count):
            if not node.elements:
                continue
            samples = ", ".join(f'"{s}"' for s in node.sample_labels(max_samples))
            lines.append(
                f"- Zone « {zone_name} » : {node.interactive_count} élément(s) "
                f"({node.link_count} lien(s)). Exemples : {samples}"
            )
        if self.submenus:
            lines.append("")
            lines.append(f"- {len(self.submenus)} sous-menu(s) détecté(s) : "
                          + ", ".join(f'"{sm.trigger_label}" ({len(sm.children)} lien(s))' for sm in self.submenus[:8]))
        if self.language_group:
            lines.append(f"- Sélecteur de langue détecté ({len(self.language_group)} langues).")
        if self.external_links:
            lines.append(f"- {len(self.external_links)} lien(s) externe(s) détecté(s).")
        return "\n".join(lines)


def build_ui_graph(analysis: UIAnalysisResult) -> UISemanticGraph:
    """
    Construit le graphe à partir du résultat déjà produit par
    html_parser_service / vision_parser_service. Ne fait AUCUN appel LLM,
    AUCUN re-parsing HTML — uniquement de l'agrégation déterministe sur
    des champs déjà présents (zone, requires_hover, hover_target_label,
    nav_group, is_external).
    """
    zones: dict[str, ZoneNode] = defaultdict(lambda: None)
    zone_map: dict[str, ZoneNode] = {}

    hover_groups: dict[str, list[UIElement]] = defaultdict(list)
    language_group: list[UIElement] = []
    external_links: list[UIElement] = []

    for el in analysis.elements:
        # zone : champ structuré si présent (html_parser_service ≥ ce fix),
        # sinon on retombe sur le préfixe texte du label pour rester
        # compatible avec un ancien UIAnalysisResult (vision_parser_service
        # n'écrit que le préfixe, voir vision_parser_service._to_ui_elements).
        zone = el.zone or (el.label.split("]", 1)[0].lstrip("[").strip() if el.label and "]" in el.label else "page")
        zone_map.setdefault(zone, ZoneNode(zone=zone))
        zone_map[zone].elements.append(el)

        if el.requires_hover and el.hover_target_label:
            hover_groups[el.hover_target_label].append(el)
        elif el.nav_group == "language_switcher":
            language_group.append(el)
        elif el.type == "link" and getattr(el, "is_external", False) and zone not in _NON_NAVIGATIONAL_ZONES:
            external_links.append(el)

    submenus = [
        SubmenuNode(trigger_label=trigger, children=children)
        for trigger, children in hover_groups.items()
    ]

    return UISemanticGraph(
        zones=zone_map,
        submenus=submenus,
        language_group=language_group,
        external_links=external_links,
        page_type=analysis.page_type or "general",
        element_count=len(analysis.elements),
    )
