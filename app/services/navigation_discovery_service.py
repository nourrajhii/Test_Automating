"""
navigation_discovery_service.py
---------------------------------
Traite la partie "navigation" du Feature Discovery / Workflow Discovery de
façon 100% déterministe, à partir du UI Semantic Graph (ui_graph_service).

POURQUOI CONTOURNER TOTALEMENT LE LLM ICI (pas juste un filet de sécurité
après coup, comme scenario_service._ensure_element_coverage) :

  - Aucun prompt ne garantit à 100% qu'un LLM regroupe "Accueil / Contact /
    Budget" en une feature "Navigation" plutôt que trois features séparées
    (voir l'analyse de FEATURE_EXTRACTION_PROMPT : sa règle anti-fusion,
    nécessaire pour ne pas fusionner à tort plusieurs boutons OAuth
    distincts, a l'effet de bord de décourager aussi la fusion de liens de
    navigation qui ont chacun une destination différente).
  - Le batching (FEATURE_EXTRACT_BATCH_SIZE) empêche STRUCTURELLEMENT deux
    liens de nav situés dans des lots différents d'être vus ensemble par
    un même appel LLM — aucun prompt ne peut réparer ça après coup.

Un menu de navigation n'a de toute façon pas besoin de "compréhension" :
c'est une STRUCTURE (zone, hover, domaine de destination), pas une
fonctionnalité métier ambiguë. La détection déterministe est donc plus
fiable ET moins chère que l'extraction LLM pour ce cas précis — exactement
le même raisonnement qui a déjà justifié _build_language_group_features
et _build_hover_menu_features dans scenario_service.py. Ce module
généralise ce principe à TOUTES les zones de navigation plutôt qu'au seul
cas de la langue, et CORRIGE le regroupement des sous-menus (une feature
par SOUS-MENU, pas une feature par LIEN DE SOUS-MENU — voir
_submenu_category, à comparer à l'ancien scenario_service._build_hover_menu_features).

SORTIE : liste de "category dicts", consommée par scenario_service pour
produire directement des TestScenario avec `category` + `target_labels`
renseignés (voir schemas.TestScenario) — CE SONT DES SCÉNARIOS FINAUX,
pas des features à repasser au LLM.
"""
from __future__ import annotations

import re
from collections import Counter

from app.services.ui_graph_service import UISemanticGraph
from app.models.schemas import UIElement
from app.config import DOWNLOAD_EXTENSIONS, CONTENT_ZONE_KEYWORDS

_NAV_ZONES = {"navbar", "header"}
_FOOTER_ZONES = {"footer"}

# Zones produites par html_parser_service._get_context qui portent une
# intention MÉTIER, pas structurelle — même si elles ne contiennent que
# des <a> (ex: boutons OAuth "Se connecter avec Google" souvent rendus en
# lien, "Mot de passe oublié", "Créer un compte"). Le filet de secours
# structurel ci-dessous (2+ liens hors nav/footer = navigation) les
# excluait pas jusqu'ici, ce qui les faisait passer directement en
# scénario "cliquer + vérifier changement de page" SANS jamais atteindre
# le Feature Discovery Agent -> aucune feature "Authentification"
# détectée, aucun cas négatif/risque généré (voir échange précédent :
# une page de login n'obtenait QUE des scénarios de navigation).
_NON_NAVIGATIONAL_ZONES = {"auth", "oauth", "search", "form"}


def _is_download_link(el: UIElement) -> bool:
    """
    True si ce lien pointe vers un fichier téléchargeable (PDF, DOC,
    ZIP...) d'après son extension — voir config.DOWNLOAD_EXTENSIONS.
    Ces liens ne doivent JAMAIS finir dans une catégorie de navigation
    générique ("cliquer + vérifier changement de page") : un clic sur un
    PDF déclenche un téléchargement, PAS une navigation observable par
    Selenium, donc l'assertion de navigation échouerait à tort dessus.
    Ils reçoivent leur propre catégorie ("downloads", voir plus bas) avec
    la bonne vérification (accessibilité HTTP du fichier).
    """
    if el.type != "link" or not el.possible_destination:
        return False
    dest = el.possible_destination.strip().lower().split("?", 1)[0].split("#", 1)[0]
    return any(dest.endswith(ext) for ext in DOWNLOAD_EXTENSIONS)


def _is_content_zone(zone_name: str) -> bool:
    """
    True si `zone_name` (calculé par html_parser_service._get_context)
    désigne une zone de CONTENU métier (actualités, rapports,
    publications, galeries...) plutôt qu'une zone STRUCTURELLE (menu,
    footer boilerplate) — voir config.CONTENT_ZONE_KEYWORDS.

    Sans cette distinction, le filet de secours structurel plus bas
    ("2+ liens internes hors nav/footer dans la même zone = navigation
    secondaire") absorbait AUSSI les blocs d'actualités/rapports/
    téléchargements/galeries dès qu'ils contenaient 2 liens ou plus —
    exactement le cas d'une page riche (portail ministériel). Ces liens
    étaient alors retirés du lot envoyé au Feature Discovery Agent
    (voir all_categorized_labels, utilisé par scenario_service) AVANT
    même d'avoir eu une chance d'être compris comme de vraies
    fonctionnalités ("lire une actualité", "consulter un rapport") : ils
    finissaient noyés dans un unique scénario générique "Liens internes —
    actualites" avec un clic + vérif de navigation, au lieu de N
    scénarios métier distincts. C'est la cause directe du problème
    "il ne génère que des liens internes/externes, jamais de vrais tests".
    """
    name = (zone_name or "").lower()
    return any(kw in name for kw in CONTENT_ZONE_KEYWORDS)


_SOCIAL_DOMAIN_HINTS = (
    "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com",
    "youtube.com", "tiktok.com", "wa.me", "whatsapp.com", "telegram.org", "t.me",
)

# Liens d'accessibilité ("skip links") : toujours en tout début de page,
# invisibles à l'écran, servant à sauter la navigation au clavier/lecteur
# d'écran. Ce ne sont JAMAIS de vrais déclencheurs de sous-menu. On les
# exclut ici de toute catégorie de nav (voir aussi html_parser_service.
# _is_skip_link, qui empêche en plus le tag requires_hover d'être posé à
# la source sur les liens du "vrai" menu qui suit).
_SKIP_LINK_HINTS = (
    "aller au contenu", "aller directement au contenu", "skip to content",
    "skip to main", "passer au contenu", "aller au menu", "skip navigation",
    "skip to navigation",
)

# Libellés courts qui, LUS SUR LE LIEN LUI-MÊME (pas juste son domaine),
# indiquent sans ambiguïté "suivez-nous sur / contactez-nous via" une
# plateforme. Nécessaire car matcher uniquement le DOMAINE de destination
# est dangereux : si le site TESTÉ est lui-même Facebook/Instagram/etc.,
# absolument tous ses liens internes (Se connecter, S'inscrire, Messenger,
# Meta Pay, Boutique Meta...) pointent vers un domaine "social" et se
# retrouvaient à tort tous classés "Contact & réseaux sociaux" — y compris
# des fonctionnalités d'authentification qui doivent au contraire atteindre
# le Feature Discovery Agent (voir _AUTH_KEYWORDS ci-dessous).
_SOCIAL_LABEL_NAMES = {
    "facebook", "instagram", "linkedin", "twitter", "x", "youtube",
    "tiktok", "whatsapp", "telegram",
}

# Jamais classé "contact", même si le href pointe vers un domaine social
# (cas Facebook/Instagram testant leur propre site) : ce sont des
# fonctionnalités d'authentification, pas des canaux de contact.
import re as _re

# FIX faux positif observé (page Google Traduction) : un simple test de
# sous-chaîne "inscri in texte" peut matcher par coïncidence un mot sans
# aucun rapport avec l'inscription, produisant un scénario "Inscription"
# fantôme alors qu'aucun élément d'inscription n'existe réellement sur la
# page. On exige une frontière de mot AVANT la racine (regex \b) : ça
# matche toujours "inscription"/"inscrivez"/"s'inscrire" (une apostrophe
# est une frontière de mot pour \b) mais plus un fragment interne à un mot
# non lié.
_AUTH_ROOT_RE = _re.compile(r"\b(inscri|identifi)", _re.I)

_AUTH_KEYWORDS = (
    # Expressions qui n'ont pas de "racine" isolable fiable (pas de risque
    # de faux positif par sous-chaîne, donc gardées en simple substring) :
    "creer un compte", "créer un compte", "create account",
    "connexion", "connecter", "sign in", "sign up", "log in", "login",
    "register", "mot de passe", "password",
)


def _normalize_apostrophes(text: str) -> str:
    """Unifie les variantes d'apostrophe (’ ‘ ` ´) vers ' — sans ça, un
    label HTML utilisant l'apostrophe typographique "S’inscrire" (U+2019,
    très courante dans le contenu web réel) ne matchait JAMAIS le mot-clé
    "s'inscrire" (apostrophe droite U+0027) codé dans _AUTH_KEYWORDS : le
    lien repartait donc, à tort, dans "Liens externes" au lieu d'être
    exclu et envoyé vers le Feature Discovery Agent."""
    return (text or "").replace("’", "'").replace("‘", "'").replace("`", "'").replace("´", "'")


_OAUTH_PROVIDERS = ("google", "microsoft", "apple", "github", "facebook", "linkedin")
_OAUTH_ACTION_HINTS = (
    "continuer", "connect", "sign in", "se connecter", "login",
    "s'identifier", "poursuivre", "continue",
)


def _is_auth_link(el: UIElement) -> bool:
    """
    True si ce lien est une action d'authentification (inscription,
    connexion, mot de passe oublié, connexion via Google/Microsoft/...).
    Ces liens ne doivent JAMAIS finir dans une catégorie de navigation
    générique ("cliquer + vérifier changement de page") : ce sont des
    FONCTIONNALITÉS métier qui doivent atteindre le Feature Discovery
    Agent, où scenario_service._build_auth_features leur construit un
    scénario détaillé dédié (remplissage email/mot de passe, options
    OAuth nommées) — voir scenario_service.py.

    Couvre aussi les boutons OAuth phrasés sans "connexion" explicite
    (ex: "Continuer avec Google") : sans ce second test, un tel bouton
    échappait à la fois à ce filtre ET risquait de ne jamais atteindre
    scenario_service._oauth_provider_in_label (capté avant, en amont, par
    une catégorie de navigation générique).
    """
    if el.type not in ("link", "button"):
        return False
    label_core = _normalize_apostrophes((el.label or "").split("]", 1)[-1].strip().lower())
    if _AUTH_ROOT_RE.search(label_core):
        return True
    if any(kw in label_core for kw in _AUTH_KEYWORDS):
        return True
    if any(p in label_core for p in _OAUTH_PROVIDERS) and any(h in label_core for h in _OAUTH_ACTION_HINTS):
        return True
    return False


def _is_skip_link(label: str | None) -> bool:
    text = (label or "").split("]", 1)[-1].strip().lower()
    return any(hint in text for hint in _SKIP_LINK_HINTS)


def _is_contact_link(el: UIElement) -> bool:
    """
    True si ce lien est un canal de contact/réseau social : téléphone
    (tel:), email (mailto:), ou lien EXPLICITEMENT labellisé comme un
    réseau social connu (voir _SOCIAL_LABEL_NAMES — on se base sur le
    libellé visible, pas seulement le domaine de destination, pour ne pas
    happer toute la navigation d'un site qui EST lui-même ce réseau social).
    Ces liens ne déclenchent PAS une navigation observable de la même façon
    qu'un lien classique (tel:/mailto: ouvrent une appli externe, un réseau
    social ouvre souvent un nouvel onglet) — ils méritent leur propre
    catégorie avec leur propre vérification (voir script_service._contact_
    link_verification_lines) plutôt que d'être noyés dans "Liens externes"
    ou "Liens de bas de page" avec une assertion de navigation inadaptée.
    """
    if el.type != "link":
        return False
    label_core = (el.label or "").split("]", 1)[-1].strip().lower()
    if any(kw in label_core for kw in _AUTH_KEYWORDS):
        return False
    dest = (el.possible_destination or "").strip().lower()
    if dest.startswith("tel:") or dest.startswith("mailto:"):
        return True
    if label_core in _SOCIAL_LABEL_NAMES:
        return any(domain in dest for domain in _SOCIAL_DOMAIN_HINTS)
    return False


_GENERIC_ZONE_TOKENS = {
    "page", "main", "home", "container", "wrapper", "section", "content",
    "div", "root", "app", "layout", "block", "inner", "outer", "body",
}


def _humanize_zone(zname: str) -> str:
    """
    html_parser_service._get_context renvoie maintenant, pour la plupart
    des conteneurs, un titre DÉJÀ humain (le premier h1/h2/h3/legend/
    aria-label/label/texte trouvé DANS le conteneur — voir
    _container_display_title). Dans ce cas on le retourne tel quel, sans
    le retoucher (le re-découper par "-"/"_" et le capitaliser mot par mot
    casserait des expressions comme "chiffres clés").

    Seul un identifiant encore techniquement brut (slug CSS/ID, ex:
    "translation-home-page__transla", faute de heading trouvable) passe
    par le nettoyage par tokens ci-dessous — dernier filet de sécurité
    pour ne jamais afficher une classe CSS telle quelle dans un titre.
    """
    zname = (zname or "").strip()
    if not zname:
        return "page"
    looks_like_slug = bool(re.fullmatch(r"[a-z0-9]+([-_][a-z0-9]+)+", zname))
    if not looks_like_slug:
        return zname[:1].upper() + zname[1:]

    parts = re.split(r"[-_]+", zname)
    parts = [p for p in parts if p and p.lower() not in _GENERIC_ZONE_TOKENS and len(p) > 2]
    if not parts:
        return "page"
    return " ".join(p.capitalize() for p in parts[:2])


def _common_theme(target_labels: list[str]) -> str | None:
    """
    Si la majorité des libellés d'un groupe commencent par le même
    premier mot (ex: "Traduction Anglais", "Traduction Espagnol",
    "Traduction Portugais"...), ce mot décrit mieux le contenu réel du
    groupe qu'un nom de zone technique — on l'utilise comme titre.
    """
    first_words = []
    for lbl in target_labels:
        core = lbl.split("]", 1)[-1].strip() if lbl and "]" in lbl else (lbl or "").strip()
        w = core.split(" ", 1)[0].strip(":,.-–—").lower()
        if len(w) > 2:
            first_words.append(w)
    if len(first_words) < 2:
        return None
    common, count = Counter(first_words).most_common(1)[0]
    if count >= max(2, len(first_words) * 0.5):
        return common.capitalize()
    return None


def _professional_group_name(kind: str, zname: str, target_labels: list[str]) -> str:
    """
    Construit un titre de scénario FONCTIONNEL pour un groupe de liens,
    compréhensible par un utilisateur — jamais "Liens internes"/"Liens
    externes" + un nom de zone technique (règle produit explicite : le
    titre ne doit jamais exposer la mécanique interne interne/externe, ni
    une classe CSS/ID brute).

    On utilise en priorité un thème de contenu détecté à partir des
    libellés eux-mêmes (ex: plusieurs liens commençant par "Traduction").
    À défaut, on s'appuie sur le titre du conteneur DOM (déjà humain dans
    la grande majorité des cas — voir html_parser_service.
    _container_display_title, qui lit h1/h2/h3/legend/aria-label/label/
    texte avant de retomber sur une classe CSS nettoyée).
    """
    theme = _common_theme(target_labels)
    zone_h = _humanize_zone(zname)
    subject = theme or zone_h
    return f"Consultation de « {subject} »"


def _labels(elements: list[UIElement]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for el in elements:
        if not el.label or el.label in seen:
            continue
        seen.add(el.label)
        out.append(el.label)
    return out


def discover_navigation_categories(graph: UISemanticGraph) -> list[dict]:
    """
    Retourne une liste de catégories de test structurelles, chacune de la
    forme :
      {
        "category": "navigation_primary" | "navigation_footer" |
                     "submenu:<trigger>" | "external_links" |
                     "internal_links:<zone>",
        "name": titre humain court,
        "description": description humaine,
        "target_labels": [labels EXACTS des UIElement concernés],
        "priority": "high" | "medium",
      }
    Une catégorie n'est émise QUE si elle a au moins une cible — pas de
    scénario vide.
    """
    categories: list[dict] = []

    # ── Téléchargements (PDF, DOC, ZIP...) — UN SCÉNARIO PAR DOCUMENT ───────
    # Avant : une seule catégorie "downloads" regroupait TOUS les documents
    # en un seul scénario générique. On veut au contraire pouvoir identifier
    # chaque document par son propre nom réel (celui affiché sur la page),
    # donc une catégorie déterministe PAR lien de téléchargement — même
    # logique que les sous-menus (une catégorie par groupe cohérent), mais
    # ici le groupe est réduit à 1 élément par design.
    download_links = [
        el for node in graph.zones.values()
        for el in node.elements
        if _is_download_link(el)
    ]
    seen_download_labels: set[str] = set()
    for el in download_links:
        if not el.label or el.label in seen_download_labels:
            continue
        seen_download_labels.add(el.label)
        doc_name = el.label.split("]", 1)[-1].strip() if el.label and "]" in el.label else (el.label or "document")
        categories.append({
            "category": f"downloads:{el.label}",
            "name": f"Téléchargement — {doc_name}",
            "description": f"Vérifier que le document « {doc_name} » est réellement accessible au téléchargement.",
            "target_labels": [el.label],
            "priority": "high",
        })
    download_labels = seen_download_labels

    # ── Contact / réseaux sociaux (tel:, mailto:, Facebook, Instagram...) ──
    # Regroupés en UN seul scénario, quelle que soit leur zone d'origine
    # (souvent dispersés entre header et footer) — mais chaque canal reste
    # listé individuellement dans les étapes du scénario (voir
    # scenario_service._scenario_dict_from_nav_category), donc "Facebook",
    # "Instagram" et le numéro de téléphone apparaissent chacun comme une
    # étape distincte, jamais fondus dans "Liens externes" génériques.
    contact_links = [
        el for node in graph.zones.values()
        for el in node.elements
        if _is_contact_link(el) and el.label not in download_labels
    ]
    if contact_links:
        categories.append({
            "category": "contact",
            "name": "Contact & réseaux sociaux",
            "description": (
                "Vérifier que chaque canal de contact (téléphone, email, "
                "réseaux sociaux) est valide et accessible."
            ),
            "target_labels": _labels(contact_links),
            "priority": "medium",
        })
    contact_labels = {lbl for lbl in _labels(contact_links)}

    def _excluded(el: UIElement) -> bool:
        """Labels déjà pris en charge ailleurs : téléchargements, contact,
        sélecteur de langue (voir scenario_service._build_language_group_
        features — la langue a besoin de son propre regroupement dédié,
        pas d'être mélangée à la navigation générique), et liens
        d'accessibilité ("Aller au contenu principal")."""
        return (
            el.label in download_labels
            or el.label in contact_labels
            or getattr(el, "nav_group", None) == "language_switcher"
            or _is_skip_link(el.label)
            or _is_auth_link(el)
        )

    # ── Navigation principale (navbar / header) ─────────────────────────────
    primary_links = [
        el for zname in _NAV_ZONES
        for el in graph.zones.get(zname, type("_", (), {"elements": []})()).elements
        if el.type == "link" and not el.requires_hover and not getattr(el, "is_external", False)
        and not _excluded(el)
    ]
    if primary_links:
        categories.append({
            "category": "navigation_primary",
            "name": "Navigation via la barre de navigation",
            "description": (
                "Vérifier que chaque lien du menu de navigation principal "
                "est cliquable et déclenche un changement de page (URL ou "
                "titre différent après le clic)."
            ),
            "target_labels": _labels(primary_links),
            "priority": "high",
        })

    # ── Liens de pied de page ────────────────────────────────────────────────
    footer_links = [
        el for el in graph.zones.get("footer", type("_", (), {"elements": []})()).elements
        if el.type == "link" and not el.requires_hover and not getattr(el, "is_external", False)
        and not _excluded(el)
    ]
    if footer_links:
        categories.append({
            "category": "navigation_footer",
            "name": "Navigation depuis le pied de page",
            "description": (
                "Vérifier que chaque lien du pied de page est cliquable et "
                "mène bien à une page différente."
            ),
            "target_labels": _labels(footer_links),
            "priority": "medium",
        })

    # ── Sous-menus : UNE catégorie PAR SOUS-MENU (regroupée), pas par lien ──
    for submenu in graph.submenus:
        if not submenu.children:
            continue
        # FIX : un lien d'accessibilité ("Aller au contenu principal") est
        # parfois suivi dans le DOM par le vrai <nav> du menu -> confondu à
        # tort avec un déclencheur de sous-menu par la détection statique
        # (html_parser_service._find_submenu_container). Ce n'est jamais un
        # VRAI sous-menu : on l'ignore ici plutôt que de générer un
        # scénario absurde "Sous-menu « Aller au contenu principal »".
        if _is_skip_link(submenu.trigger_label):
            continue
        # FIX (observé sur LinkedIn) : un item d'authentification
        # ("S'identifier", "S'inscrire"...) peut se retrouver "enfant" d'un
        # sous-menu détecté (à tort ou à raison — ex: menu profil/logo
        # confondu avec un dropdown), et échappait alors À TOUS les filtres
        # (auth/contact/téléchargement/langue), qui n'étaient appliqués
        # qu'aux liens de nav classiques. On applique désormais le même
        # filtre ici : ces éléments repartent vers le Feature Discovery
        # Agent / le générateur d'authentification dédié, où qu'ils soient
        # trouvés dans le DOM.
        real_children = [el for el in submenu.children if not _excluded(el)]
        if not real_children:
            continue
        categories.append({
            "category": f"submenu:{submenu.trigger_label}",
            "name": f"Ouverture et navigation du sous-menu « {submenu.trigger_label} »",
            "description": (
                f"Survoler « {submenu.trigger_label} » pour ouvrir le "
                f"sous-menu, puis vérifier que chaque lien du sous-menu "
                f"est cliquable et mène à une page différente."
            ),
            "target_labels": _labels(real_children),
            "priority": "medium",
        })

    # ── Liens externes — REGROUPÉS PAR ZONE, pas en un seul bloc ────────────
    # FIX (observé sur LinkedIn) : un unique scénario "Liens externes" avec
    # 60+ éléments mélangeait des choses totalement sans rapport (jeux
    # LinkedIn, logiciels partenaires, réseaux "Top Companies", liens
    # légaux du pied de page...) — illisible et inexploitable comme "un
    # seul test". On regroupe maintenant par zone d'origine (même logique
    # que le filet de secours interne juste en dessous) : chaque section
    # cohérente de la page devient son propre scénario, avec un nom qui
    # reflète la zone plutôt qu'un fourre-tout générique.
    external_by_zone: dict[str, list[UIElement]] = {}
    for el in graph.external_links:
        if _excluded(el):
            continue
        zname = getattr(el, "zone", None) or "page"
        external_by_zone.setdefault(zname, []).append(el)
    for zname, els in external_by_zone.items():
        pretty_name = _professional_group_name("external", zname, _labels(els))
        categories.append({
            "category": f"external_links:{zname}",
            "name": pretty_name,
            "description": (
                f"Vérifier que chaque lien externe de cette section "
                "s'ouvre correctement (nouvel onglet ou changement de "
                "domaine) sans erreur."
            ),
            "target_labels": _labels(els),
            "priority": "medium",
        })

    # ── Filet de secours structurel : liens internes dans une zone qui n'est
    # NI navbar/header NI footer NI un sous-menu NI de la langue (ex: liens
    # dans un "hero", une sidebar...). Sans ce filet, ces liens repartiraient
    # vers le Feature Discovery LLM et retomberaient sur le problème qu'on
    # cherche justement à éviter. Un seuil de 2 : un lien isolé dans une
    # zone ("hero") est plus probablement une vraie feature (call-to-action
    # métier) qu'un élément de navigation — on le laisse au LLM. Deux liens
    # ou plus dans la même zone hors nav/footer sont traités comme un
    # groupe de navigation secondaire.
    already_categorized_labels = {lbl for cat in categories for lbl in cat["target_labels"]}
    for zname, node in graph.zones.items():
        if zname in _NAV_ZONES or zname in _FOOTER_ZONES or zname in _NON_NAVIGATIONAL_ZONES:
            continue
        # FIX couverture/qualité : une zone de CONTENU (actualités, rapports,
        # publications, galeries, vidéos...) n'est PAS de la navigation
        # structurelle, même si elle contient 2+ liens. La laisser passer
        # ici la faisait absorber dans un unique scénario générique
        # "Liens internes — <zone>" (clic + vérif de changement de page),
        # ce qui (a) l'empêchait d'atteindre le Feature Discovery Agent
        # pour être comprise comme une vraie fonctionnalité métier
        # ("lire une actualité", "consulter un rapport"...), et (b)
        # produisait exactement le symptôme "il ne génère que des liens
        # internes/externes, jamais de vrais tests". On la laisse donc
        # repartir NON catégorisée ici : all_categorized_labels() ne la
        # retirera pas du lot, et scenario_service._extract_features la
        # recevra normalement.
        if _is_content_zone(zname):
            continue
        remaining_links = [
            el for el in node.elements
            if el.type == "link"
            and not el.requires_hover
            and not getattr(el, "is_external", False)
            and el.label not in already_categorized_labels
            and not _excluded(el)
        ]
        if len(remaining_links) >= 2:
            labels = _labels(remaining_links)
            pretty_name = _professional_group_name("internal", zname, labels)
            categories.append({
                "category": f"internal_links:{zname}",
                "name": pretty_name,
                "description": (
                    f"Vérifier que chaque lien interne de cette section "
                    f"est cliquable et mène à une page différente."
                ),
                "target_labels": labels,
                "priority": "low",
            })

    return categories


def all_categorized_labels(categories: list[dict]) -> set[str]:
    """Labels déjà couverts par une catégorie de navigation — à retirer du
    lot envoyé au Feature Discovery Agent (scenario_service)."""
    return {lbl for cat in categories for lbl in cat["target_labels"]}
