"""
content_category_service.py
----------------------------
Généralise navigation_discovery_service.py à TOUTES les zones de contenu
métier (news, publications, downloads, gallery, videos, social, contact,
cards, widget — voir html_parser_service.CONTENT_ZONE_NAMES), pas
seulement à la navigation structurelle (navbar/footer/sous-menus).

POURQUOI UN SERVICE DÉTERMINISTE ICI, PAS LE LLM GÉNÉRIQUE :
même raisonnement que navigation_discovery_service.py — une liste
d'actualités, une liste de rapports téléchargeables ou une liste de liens
réseaux sociaux n'ont pas besoin de "compréhension" pour savoir QUOI
tester : ce sont des composants récurrents avec des règles de test
connues d'avance (ouvrir chaque item / vérifier une image / vérifier une
date / télécharger chaque PDF / vérifier qu'un lien social pointe vers le
bon domaine...). Le LLM (FEATURE_EXTRACTION_PROMPT) ne sait pas qu'une
zone "news" mérite un scénario "vérifier la date" — il n'a que le libellé
brut des liens. La détection déterministe est donc plus fiable, plus
rapide (aucun appel LLM) et surtout jamais tronquée par num_predict.

SORTIE : liste de "category dicts" au même format que
navigation_discovery_service.discover_navigation_categories — consommée
de la même façon par scenario_service (via _scenario_dict_from_nav_category,
réutilisée telle quelle) pour produire des TestScenario FINAUX qui ne
repassent jamais par le LLM de génération de scénarios.
"""
from __future__ import annotations

from app.services.ui_graph_service import UISemanticGraph
from app.services.html_parser_service import CONTENT_ZONE_NAMES
from app.models.schemas import UIElement

# ── Règles de test par zone de contenu ──────────────────────────────────────
# Chaque règle produit UNE catégorie de test (donc UN scénario final,
# au même titre qu'une catégorie de navigation). `focus` sert de sous-titre
# humain dans le scénario généré ; `verify` décrit ce que l'action doit
# vérifier concrètement (utilisé tel quel dans l'expected_result).
_CATEGORY_TEST_RULES: dict[str, list[dict]] = {
    "news": [
        {
            "suffix": "ouverture de chaque actualité",
            "action": "Pour chaque actualité de « {name} », cliquer et vérifier qu'elle s'ouvre",
            "verify": "Chaque actualité s'ouvre et affiche un contenu (titre, texte) après le clic.",
        },
    ],
    "publications": [
        {
            "suffix": "ouverture de chaque rapport/publication",
            "action": "Pour chaque publication de « {name} », cliquer et vérifier qu'elle s'ouvre",
            "verify": "Chaque publication est cliquable et mène à un contenu ou un fichier accessible.",
        },
    ],
    "downloads": [
        {
            "suffix": "téléchargement de chaque document",
            "action": "Pour chaque document de « {name} », cliquer et vérifier que le téléchargement/l'ouverture démarre sans erreur",
            "verify": "Chaque lien de téléchargement s'ouvre ou télécharge un fichier, sans erreur 404/ouverture cassée.",
        },
    ],
    "gallery": [
        {
            "suffix": "ouverture de chaque image de la galerie",
            "action": "Pour chaque élément de « {name} », cliquer et vérifier que l'image s'affiche en grand",
            "verify": "Chaque image de la galerie s'ouvre correctement (aperçu ou changement de page).",
        },
    ],
    "videos": [
        {
            "suffix": "chargement du lecteur vidéo",
            "action": "Pour chaque élément de « {name} », vérifier que le lecteur vidéo se charge",
            "verify": "Le lecteur vidéo (ou l'iframe d'intégration) se charge sans erreur visible.",
        },
    ],
    "social": [
        {
            "suffix": "liens réseaux sociaux",
            "action": "Pour chaque lien de « {name} », cliquer et vérifier qu'il pointe vers le bon domaine externe",
            "verify": "Chaque lien réseau social ouvre un nouvel onglet ou redirige vers le domaine externe attendu, sans erreur.",
        },
    ],
    "contact": [
        {
            "suffix": "coordonnées de contact",
            "action": "Pour chaque élément de « {name} », vérifier que l'information (email, téléphone, adresse) est affichée et exploitable",
            "verify": "Les coordonnées de contact (lien mailto, numéro, adresse) sont présentes et correctement formées.",
        },
    ],
    "cards": [
        {
            "suffix": "ouverture de chaque carte",
            "action": "Pour chaque carte de « {name} », cliquer et vérifier qu'elle mène à un contenu",
            "verify": "Chaque carte est cliquable et mène à une page ou un contenu différent après le clic.",
        },
    ],
    "widget": [
        {
            "suffix": "fonctionnement du widget",
            "action": "Pour chaque élément de « {name} », vérifier qu'il répond à l'interaction",
            "verify": "Le widget répond à l'interaction sans erreur visible (état mis à jour ou contenu affiché).",
        },
    ],
}

# Titres FONCTIONNELS (jamais un nom de zone technique) — voir la règle
# produit : "Consultation des communiqués", "Consultation des chiffres et
# indicateurs", etc. plutôt qu'un simple nom de zone brut.
_ZONE_HUMAN_NAMES = {
    "news": "Consultation des actualités",
    "publications": "Consultation des publications et rapports",
    "downloads": "Téléchargement des documents",
    "gallery": "Consultation de la galerie",
    "videos": "Visionnage des vidéos",
    "social": "Consultation des réseaux sociaux",
    "contact": "Consultation des coordonnées de contact",
    "cards": "Consultation des cartes de contenu",
    "widget": "Utilisation du widget",
}

# Groupe nominal simple utilisé À L'INTÉRIEUR de la phrase de description
# (ex: "Pour chaque actualité de « Actualités », ...") — distinct du titre
# fonctionnel ci-dessus (_ZONE_HUMAN_NAMES), qui est déjà une phrase
# complète et ne doit pas être réinjecté tel quel dans la description.
_ZONE_DESCRIPTION_NOUNS = {
    "news": "Actualités",
    "publications": "Publications",
    "downloads": "Documents",
    "gallery": "Galerie",
    "videos": "Vidéos",
    "social": "Réseaux sociaux",
    "contact": "Contact",
    "cards": "Cartes",
    "widget": "Widget",
}


def _labels(elements: list[UIElement]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for el in elements:
        if not el.label or el.label in seen:
            continue
        seen.add(el.label)
        out.append(el.label)
    return out


def discover_content_categories(graph: UISemanticGraph) -> list[dict]:
    """
    Retourne une liste de catégories de test déterministes pour les zones de
    CONTENU métier (news/publications/downloads/gallery/videos/social/
    contact/cards/widget), sur le même modèle que
    navigation_discovery_service.discover_navigation_categories.

    Une zone peut produire PLUSIEURS catégories (ex: une liste de rapports
    pourrait avoir "ouverture" ET "vérification erreurs") — voir
    _CATEGORY_TEST_RULES, actuellement une règle par zone mais extensible
    sans toucher au reste du pipeline.

    Une catégorie n'est émise QUE si elle a au moins une cible dans le
    graphe (pas de scénario vide) — même garde-fou que
    navigation_discovery_service.
    """
    categories: list[dict] = []

    for zname in CONTENT_ZONE_NAMES:
        node = graph.zones.get(zname)
        if node is None or not node.elements:
            continue

        rules = _CATEGORY_TEST_RULES.get(zname)
        if not rules:
            continue

        human_name = _ZONE_HUMAN_NAMES.get(zname, zname.capitalize())
        description_noun = _ZONE_DESCRIPTION_NOUNS.get(zname, human_name)
        target_labels = _labels(node.elements)
        if not target_labels:
            continue

        for i, rule in enumerate(rules):
            suffix = f" — {rule['suffix']}" if len(rules) > 1 else ""
            categories.append({
                "category": f"content:{zname}:{i}",
                "name": f"{human_name}{suffix}",
                "description": rule["action"].format(name=description_noun),
                "target_labels": target_labels,
                "priority": "medium",
                # Repris tel quel par scenario_service pour construire le
                # TestScenario final (voir _scenario_dict_from_content_category) :
                # on garde le texte de vérification explicite plutôt que la
                # formule générique "changement de page" utilisée pour la
                # navigation pure, car "vérifier une image"/"vérifier une
                # date"/"télécharger" ne sont pas de simples changements d'URL.
                "verify": rule["verify"],
            })

    return categories


def all_categorized_labels(categories: list[dict]) -> set[str]:
    """Labels déjà couverts par une catégorie de contenu — à retirer du lot
    envoyé au Feature Discovery Agent (scenario_service), même principe que
    navigation_discovery_service.all_categorized_labels."""
    return {lbl for cat in categories for lbl in cat["target_labels"]}
