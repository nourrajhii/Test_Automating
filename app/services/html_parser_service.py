"""
html_parser_service.py
----------------------
Analyse le code HTML/JS fourni et extrait les éléments UI interactifs.
Chaque élément est enrichi avec son contexte (section parente) pour que
le LLM de scénarios puisse les distinguer correctement.

FIX 1 : _build_selector échappe les caractères spéciaux CSS (Tailwind) et
        retourne "NONE" explicitement quand aucun sélecteur fiable n'est
        disponible.
FIX 2 : _llm_fallback_parse — le LLM copiait littéralement la valeur
        d'exemple "CSS selector" depuis le prompt. On lui demande maintenant
        de produire de VRAIS sélecteurs (ID, name, data-*, ou classe) ET on
        filtre toute valeur générique/placeholder avant de créer les UIElement.
FIX 3 : num_predict du fallback augmenté à 1500 pour ne pas tronquer la
        liste d'éléments sur des pages complexes.
"""
import httpx
import json
import logging
import re
from bs4 import BeautifulSoup, Tag

from app.config import OLLAMA_BASE_URL, TEXT_MODEL, TEXT_TIMEOUT, MAX_HTML_CHARS, MAX_UI_ELEMENTS
from app.models.schemas import UIAnalysisResult, UIElement

logger = logging.getLogger("html_parser_service")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[html_parser_service] %(message)s"))
    logger.addHandler(_h)


# ── Zones de contenu métier reconnues (voir content_category_service.py) ─────
# Détection déterministe : ces zones ont des règles de test connues
# d'avance (voir content_category_service._CATEGORY_TEST_RULES) donc pas
# besoin du LLM pour les repérer. CONTENT_ZONE_NAMES est le référentiel
# des clés valides, importé tel quel par content_category_service.
CONTENT_ZONE_NAMES = frozenset({
    "news", "publications", "downloads", "gallery",
    "videos", "social", "contact", "cards", "widget",
})

_CONTENT_ZONE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "news": ("news", "actualit"),
    "publications": ("publication", "rapport", "communique", "press-release", "presse"),
    "downloads": ("download", "telecharg", "documents"),
    "gallery": ("gallery", "galerie", "carousel", "diaporama"),
    "videos": ("video",),
    "social": ("social", "reseaux-sociaux", "reseaux_sociaux"),
    "contact": ("contact-block", "coordonnees", "coordonnées"),
    "cards": ("card-list", "cards-grid", "cartes"),
    "widget": ("widget",),
}


# ── Constantes : valeurs de selector_hint invalides / placeholder ─────────────
# Ces valeurs ne doivent JAMAIS être transmises à Selenium comme sélecteur réel.
_INVALID_SELECTORS = {
    "NONE", "none",
    "CSS selector", "css selector", "CSS_selector",
    "selector", ".selector", "#selector",
    "button", "input", "a", "div", "span", "form",
    "", "null", "undefined",
}


# ── Détection heuristique des liens externes ───────────────────────────────
# On ne connaît pas le domaine d'origine du HTML brut fourni (pas de vraie
# navigation, contrairement à live_dom_service qui, lui, a une vraie URL) —
# on retient donc comme heuristique : toute URL ABSOLUE http(s) est traitée
# comme externe. Un lien relatif ("/contact", "#section", "contact.html")
# est toujours interne. Imparfait (un site qui linke vers son propre nom de
# domaine en absolu serait compté "externe") mais volontairement simple et
# déterministe, cohérent avec le reste des heuristiques de ce fichier
# (_get_context, _is_junk_link...).
def _is_external_link(href: str) -> bool:
    return bool(href) and href.strip().lower().startswith(("http://", "https://"))


def _is_valid_selector(sel: str | None) -> bool:
    """Vérifie qu'un selector_hint est utilisable par Selenium."""
    if not sel:
        return False
    if sel.strip() in _INVALID_SELECTORS:
        return False
    if len(sel.strip()) < 2:
        return False
    return True


# ── Détection des éléments cachés statiquement ────────────────────────────────
# Facebook (et beaucoup d'autres sites) gardent des <input type="submit">
# fallback "no-JS" cachés en display:none, pendant que le vrai bouton visible
# est un <button> à part. Si on extrait ces éléments cachés, le scénario
# généré pointe vers un élément que Selenium ne rendra JAMAIS visible
# (TimeoutException garanti après 15s d'attente). On les filtre dès le
# parsing statique pour ne jamais générer de scénario dessus.

_HIDDEN_CLASS_TOKENS = {"hidden", "sr-only", "visually-hidden", "d-none", "invisible"}
_OFFSCREEN_STYLE_RE = re.compile(r'(left|top)\s*:\s*-\d{2,}px')


def _node_looks_hidden(node: Tag) -> bool:
    if node.get("hidden") is not None:
        return True
    if (node.get("aria-hidden") or "").lower() == "true":
        return True
    style = (node.get("style") or "").lower()
    style_compact = style.replace(" ", "")
    if "display:none" in style_compact or "visibility:hidden" in style_compact:
        return True
    if _OFFSCREEN_STYLE_RE.search(style):  # ex: left:-9999px (technique screen-reader-only)
        return True
    classes = set(node.get("class") or [])
    if classes & _HIDDEN_CLASS_TOKENS:
        return True
    return False


def _is_hidden(tag: Tag, max_depth: int = 8, dropdown_container_ids: set | None = None) -> bool:
    """
    Vérifie si l'élément OU un de ses parents proches est masqué.
    Beaucoup de sites cachent un conteneur entier (form, div) plutôt que
    chaque champ individuellement — se limiter à `tag` seul rate ces cas.

    FIX sous-menus : les menus déroulants (dropdown/flyout) sont presque
    toujours cachés par défaut via `style="display:none"` (ou
    visibility:hidden) sur leur conteneur, révélé au survol via JS/CSS.
    Sans exception, CE signal filtrait purement et simplement TOUS les
    sous-menus dès le parsing — l'agent ne voyait alors plus jamais leurs
    liens, quel que soit le prompt utilisé ensuite. Pour les conteneurs
    identifiés comme sous-menus (voir _find_submenu_container), on ignore
    donc spécifiquement display:none/visibility:hidden, tout en gardant
    les autres signaux de masquage permanent (hidden, aria-hidden="true",
    classes sr-only/d-none...) qui eux restent des cas à exclure.
    """
    node = tag
    depth = 0
    dropdown_container_ids = dropdown_container_ids or set()
    while node is not None and isinstance(node, Tag) and depth < max_depth:
        if id(node) in dropdown_container_ids:
            if node.get("hidden") is not None:
                return True
            if (node.get("aria-hidden") or "").lower() == "true":
                return True
            classes = set(node.get("class") or [])
            if classes & _HIDDEN_CLASS_TOKENS:
                return True
            # display:none / visibility:hidden volontairement ignorés ici :
            # c'est le mécanisme normal de révélation au survol.
        else:
            if _node_looks_hidden(node):
                return True
        node = node.parent
        depth += 1
    return False


# ── Détection des menus déroulants / flyout ───────────────────────────────────
# Pattern HTML classique pour un menu avec sous-options révélées au survol :
#   <a href="#" class="top_link">Réglementation</a>
#   <ul class="sub"> <li><a>...</a></li> ... </ul>
# ou parfois imbriqué dans le même <li> :
#   <li><a>Menu</a><ul class="sub">...</ul></li>
# Dans les deux cas, le conteneur de sous-menu est un FRÈRE (sibling) direct
# du lien déclencheur.

# ── Liens d'accessibilité ("skip links") ────────────────────────────────────
# <a href="#main-content">Aller au contenu principal</a> — toujours en tout
# début de page, invisible à l'écran, sert à sauter la navigation au
# clavier/lecteur d'écran. Il est presque toujours immédiatement suivi dans
# le DOM par le VRAI <nav>/<ul> du menu principal du site -> sans cette
# exclusion, _find_submenu_container le confond avec un déclencheur de
# sous-menu et tague TOUT le menu principal (parfois 50+ liens) comme
# "enfants de ce sous-menu" (requires_hover=True). Conséquence observée :
# ces liens ne sont alors JAMAIS regroupés (ni par navigation_discovery_
# service, dont la catégorie est explicitement ignorée pour un skip-link,
# ni par _build_language_group_features pour FR/AR puisqu'ils portent
# requires_hover=True) -> un scénario "Vérifier la navigation (sous-menu) :
# X" PAR LIEN, dupliquant massivement le menu entier.
_SKIP_LINK_HINTS = (
    "aller au contenu", "aller directement au contenu", "skip to content",
    "skip to main", "passer au contenu", "aller au menu", "skip navigation",
    "skip to navigation",
)


def _is_skip_link(tag: Tag) -> bool:
    text = _norm_text(tag.get_text(strip=True))
    if text and any(hint in text for hint in _SKIP_LINK_HINTS):
        return True
    href = (tag.get("href") or "").strip().lower()
    return href in ("#main", "#main-content", "#content", "#maincontent")


_JS_EVENT_ATTRS = (
    "onclick", "onchange", "onsubmit", "ondblclick",
    "onmousedown", "onmouseup", "onkeydown", "onkeypress",
)

# ── Détection du sélecteur de langue ───────────────────────────────────────
# Problème 2 / duplication : le prompt LLM (FEATURE_EXTRACTION_PROMPT) a une
# règle anti-fusion qui dit explicitement de NE PAS fusionner les liens de
# langue ("distinct language links... each becomes its own feature") — donc
# même avec un bon prompt, chaque langue ressort comme un scénario séparé.
# On détecte donc les liens de langue par CODE, comme les sous-menus hover,
# pour les regrouper en un seul scénario garanti, sans dépendre du LLM.
_LANGUAGE_CONTAINER_HINTS = {
    "lang", "language", "langue", "locale", "idioma", "sprache",
    "taal", "lingua", "language-selector", "lang-switch", "lang-switcher",
    "i18n", "translations",
}
_KNOWN_LANGUAGE_NAMES = {
    "english", "français", "francais", "deutsch", "español", "espanol",
    "italiano", "português", "portugues", "nederlands", "polski", "svenska",
    "norsk", "dansk", "suomi", "türkçe", "turkce", "čeština", "cestina",
    "magyar", "română", "romana", "українська", "ukrainska", "العربية",
    "русский", "中文", "日本語", "한국어", "ελληνικά", "עברית", "हिन्दी",
    "bahasa", "tiếng việt", "tieng viet", "autres langues",
    # Codes courts fréquemment utilisés comme libellé de sélecteur de
    # langue (ex: "Fr" / "Ar" sur les sites institutionnels tunisiens).
    # Match sur le texte EXACT et complet du lien (voir _looks_like_
    # language_link) : sans risque de faux positif sur un lien "FR" isolé
    # qui ne signifierait rien d'autre dans ce contexte.
    "fr", "ar", "en", "es", "de", "it", "pt", "nl", "ru", "tr",
    "zh", "ja", "ko", "ع", "fra", "eng", "ara",
}


def _norm_text(s: str) -> str:
    return (s or "").strip().lower()


def _looks_like_language_link(tag: Tag) -> bool:
    """Un <a> est considéré comme un lien de langue si son texte correspond
    à un nom de langue connu, s'il porte hreflang/lang, ou si un conteneur
    proche (3 niveaux) évoque un sélecteur de langue par sa classe/id."""
    if tag.get("hreflang"):
        return True
    text_norm = _norm_text(tag.get_text(strip=True))
    if text_norm in _KNOWN_LANGUAGE_NAMES:
        return True
    node = tag
    depth = 0
    while node is not None and isinstance(node, Tag) and depth < 3:
        combined = f"{node.get('id') or ''} {' '.join(node.get('class') or [])}".lower()
        if any(hint in combined for hint in _LANGUAGE_CONTAINER_HINTS):
            return True
        node = node.parent
        depth += 1
    return False


def _detect_language_links(soup: BeautifulSoup) -> set:
    """Retourne {id(tag), ...} pour tous les <a> identifiés comme faisant
    partie d'un sélecteur de langue. On n'active le regroupement QUE s'il y
    a au moins 2 liens détectés (sinon un simple lien "English" isolé
    resterait traité normalement)."""
    candidates = [a for a in soup.find_all("a") if _looks_like_language_link(a)]
    if len(candidates) < 2:
        return set()
    return {id(a) for a in candidates}


_SUBMENU_HINT_CLASSES = {
    "sub", "submenu", "sub-menu", "dropdown", "dropdown-menu",
    "dropdown-content", "flyout", "fly-out", "mega-menu",
    "subnav", "sub-nav", "nav-dropdown", "children", "sub-list",
}


def _find_submenu_container(anchor: Tag, soup: BeautifulSoup | None = None) -> Tag | None:
    """
    Retourne le conteneur de sous-menu associé à ce lien déclencheur, ou
    None. On accepte le frère suivant (ul/div/nav) s'il porte une classe
    évoquant un sous-menu OU s'il contient lui-même des <a> — ce deuxième
    critère permet de détecter un dropdown même sans convention de nommage
    de classe particulière.

    FIX BUG CRITIQUE (duplication massive observée) : un lien d'accessibilité
    ("Aller au contenu principal") est TOUJOURS suivi du vrai menu principal
    dans le DOM -> sans l'exclure ici, il est confondu avec un déclencheur
    et TOUT le menu (parfois 50+ liens) est tagué comme son "sous-menu".
    On l'exclut à la source, et on ajoute un garde-fou : un vrai sous-menu
    déroulant ne dépasse quasiment jamais ~25 liens ; un conteneur plus
    gros est presque certainement une confusion (toute la nav principale,
    pas un dropdown) -> on l'ignore plutôt que de produire un scénario par
    lien pour l'ensemble du site.
    """
    if _is_skip_link(anchor):
        return None

    for attr in ("aria-controls", "data-target", "data-bs-target"):
        ref = (anchor.get(attr) or "").strip().lstrip("#")
        if ref and soup is not None:
            target = soup.find(id=ref)
            if target is not None:
                return target if len(target.find_all("a")) <= 25 else None

    sibling = anchor.find_next_sibling(["ul", "div", "nav", "form"])
    if sibling is None:
        parent_li = anchor.find_parent("li")
        if parent_li is not None:
            sibling = parent_li.find_next_sibling(["ul", "div", "nav"])
    if sibling is None:
        return None
    if len(sibling.find_all("a")) > 25:
        return None
    classes = set(c.lower() for c in (sibling.get("class") or []))
    has_hint_class = bool(classes & _SUBMENU_HINT_CLASSES)
    has_nested_links = sibling.find("a") is not None
    # FIX : un sous-menu peut ne contenir AUCUN <a> (ex: une barre de
    # recherche <input> seule dans un dropdown) — se limiter à find("a")
    # faisait passer ce conteneur pour "pas un sous-menu", il retombait
    # alors sous le filtre display:none normal et l'input à l'intérieur
    # n'était plus jamais détecté, silencieusement.
    has_nested_fields = sibling.find(["input", "select", "textarea", "button"]) is not None
    if has_hint_class or has_nested_links or has_nested_fields:
        return sibling
    return None


def _detect_dropdown_menus(soup: BeautifulSoup) -> tuple[set, dict]:
    """
    Parcourt tous les <a> du document pour repérer les déclencheurs de
    sous-menu et leurs enfants.

    Retourne :
      - dropdown_container_ids : {id(conteneur), ...} pour _is_hidden
      - submenu_child_to_parent : {id(lien enfant): tag déclencheur, ...}
    """
    dropdown_container_ids: set = set()
    submenu_child_to_parent: dict = {}

    for anchor in soup.find_all("a"):
        container = _find_submenu_container(anchor, soup)
        if container is None:
            continue
        dropdown_container_ids.add(id(container))
        # Tague aussi les champs non-lien (input/select/textarea/button) du
        # sous-menu, pas seulement les <a> : eux aussi ont besoin d'un
        # hover préalable pour devenir interactifs côté Selenium.
        for child in container.find_all(["a", "input", "select", "textarea", "button"]):
            if child is not anchor:
                submenu_child_to_parent[id(child)] = anchor

    return dropdown_container_ids, submenu_child_to_parent


_JUNK_HREF_VALUES = {"#", "", "javascript:void(0)", "javascript:void(0);", "javascript:;"}


def _is_junk_link(label_text: str, href: str, has_real_text_signal: bool) -> bool:
    """
    Rejette les liens qui n'ont ni texte visible/aria-label/title réel, ni
    destination exploitable — typiquement <a href="#"><i class="icon"/></a>
    (icône seule, ex: RSS/social) où le code retombait sur le HREF LITTÉRAL
    comme label ("#"). Un tel élément n'a aucun libellé humain à montrer ni
    aucune destination vérifiable : il ne doit jamais devenir une
    "fonctionnalité" à part entière (scénario absurde "Vérifier la
    navigation : #"). On ne filtre PAS un lien qui a un vrai texte/label
    (ex: "RSS" écrit en toutes lettres) même si son href est "#" — seul le
    cas "aucun texte du tout, on affiche le href brut" est visé ici.
    """
    label = (label_text or "").strip()
    href_clean = (href or "").strip().lower()
    if has_real_text_signal:
        return False
    return label in _JUNK_HREF_VALUES or href_clean in _JUNK_HREF_VALUES or label == href_clean


def _submit_input_is_redundant(tag: Tag) -> bool:
    """
    Pattern très courant (Facebook compris) : un <input type="submit">
    invisible sert de fallback "no-JS" à côté d'un <button> stylé qui est
    le VRAI contrôle visible, dans le même <form>. Le style/la classe qui
    cache l'input vit souvent dans une feuille CSS externe qu'on ne peut
    pas analyser statiquement -> on ne peut pas le détecter via _is_hidden.
    Si le même form contient déjà un <button>, on considère l'input comme
    redondant et on l'ignore : générer un scénario dessus garantirait un
    TimeoutException puisqu'il ne deviendra jamais visible.
    """
    container = tag.find_parent("form") or tag.parent
    if container is None:
        return False
    return container.find("button") is not None


# ── Nettoyage HTML ────────────────────────────────────────────────────────────

def _clean_html(raw: str) -> str:
    """
    FIX BUG RACINE (couverture incomplète sur les grosses pages) :
    l'ancienne version tronquait le HTML nettoyé à 40 000 caractères, AVANT
    même que _parse_html_elements() ne parcoure le DOM. Sur une page riche
    (mega-menu + actualités + rapports + téléchargements + galeries +
    footer social...), le HTML nettoyé (script/style déjà retirés)
    dépassait très largement ce seuil : tout ce qui suit le
    header/mega-menu dans le document (donc la quasi-totalité du contenu
    métier) était invisible pour le reste du pipeline. Résultat observé :
    seuls les sous-menus de navigation (proches du début du document)
    généraient des scénarios ; actualités, PDF, galeries, réseaux sociaux,
    formulaires plus bas dans le DOM n'existaient plus du tout en amont.
    Ce n'est donc jamais allé jusqu'à un problème de "compréhension" LLM.

    On utilise maintenant MAX_HTML_CHARS (config.py, 300 000 par défaut,
    configurable via env) et on log un WARNING explicite si une troncature
    a effectivement lieu, pour que ce cas soit visible au lieu de silencieux.
    """
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style", "svg", "noscript", "link", "meta"]):
        tag.decompose()
    cleaned = str(soup)
    if len(cleaned) > MAX_HTML_CHARS:
        logger.warning(
            "HTML nettoyé tronqué : %d caractères > MAX_HTML_CHARS=%d. "
            "Du contenu (probablement en fin de page : footer, galeries, "
            "sections secondaires) ne sera PAS analysé. Augmentez "
            "MAX_HTML_CHARS si cette page doit être couverte entièrement.",
            len(cleaned), MAX_HTML_CHARS,
        )
    return cleaned[:MAX_HTML_CHARS]


# ── Détection du contexte (section parente) ───────────────────────────────────

def _humanize_zone_label(raw: str) -> str:
    """
    Filet de secours UNIQUEMENT : utilisé quand _container_display_title
    n'a trouvé AUCUN élément sémantique visible (h1/h2/h3/legend/
    aria-label/label/texte) dans le conteneur. Ne doit jamais être ce qui
    finit dans un titre de scénario en usage normal — voir
    _container_display_title, qui est toujours tenté en premier.
    """
    if not raw:
        return "page"
    block = raw.split("__", 1)[0]
    words = [w for w in re.split(r"[-_]+", block) if w and not w.isdigit()]
    if not words:
        return "page"
    return " ".join(w.capitalize() for w in words[:4])


# ── Titre fonctionnel d'un conteneur (raisonnement QA, pas parsing HTML) ──────
# RÈGLE : le titre d'un scénario ne doit JAMAIS être une classe CSS, un ID
# ou un nom technique. Il doit être basé sur le premier élément VISIBLE
# trouvé dans le conteneur, dans cet ordre : h1 → h2 → h3 → legend →
# aria-label → label → texte affiché. C'est ce que fait cette fonction ;
# _humanize_zone_label (classe/ID nettoyés) n'est qu'un DERNIER recours si
# rien de sémantique n'est trouvé.
_MAX_HEADING_LEN = 80


def _clean_heading_text(text: str | None) -> str | None:
    if not text:
        return None
    t = re.sub(r"\s+", " ", text).strip()
    if not t or len(t) > _MAX_HEADING_LEN:
        return None
    return t


def _container_display_title(container: Tag) -> str | None:
    """
    Cherche, DANS ce conteneur (pas dans ses ancêtres), le premier élément
    visible dans l'ordre : h1 → h2 → h3 → legend → aria-label → label →
    texte affiché. Retourne un titre humain prêt à l'emploi, ou None si
    rien de sémantique n'a été trouvé (auquel cas _get_context retombe sur
    _humanize_zone_label).
    """
    if not isinstance(container, Tag):
        return None

    for heading_tag in ("h1", "h2", "h3"):
        el = container.find(heading_tag)
        if el is not None and not _node_looks_hidden(el):
            cleaned = _clean_heading_text(el.get_text(" ", strip=True))
            if cleaned:
                return cleaned

    legend = container.find("legend")
    if legend is not None and not _node_looks_hidden(legend):
        cleaned = _clean_heading_text(legend.get_text(" ", strip=True))
        if cleaned:
            return cleaned

    aria_owner = container if container.get("aria-label") else container.find(attrs={"aria-label": True})
    if aria_owner is not None:
        cleaned = _clean_heading_text(aria_owner.get("aria-label"))
        if cleaned:
            return cleaned

    label = container.find("label")
    if label is not None and not _node_looks_hidden(label):
        cleaned = _clean_heading_text(label.get_text(" ", strip=True))
        if cleaned:
            return cleaned

    # Dernier recours sémantique : le premier texte visible directement
    # porté par le conteneur lui-même (pas celui, potentiellement énorme
    # et hétérogène, de tous ses descendants réunis).
    for child in container.children:
        if isinstance(child, Tag):
            if child.name in ("script", "style"):
                continue
            cleaned = _clean_heading_text(child.get_text(" ", strip=True))
        else:
            cleaned = _clean_heading_text(str(child))
        if cleaned:
            return cleaned

    return None


def _get_context(tag: Tag) -> str:
    for parent in tag.parents:
        if not isinstance(parent, Tag):
            continue
        name = parent.name or ""
        pid = (parent.get("id") or "").lower()
        pclass = " ".join(parent.get("class") or []).lower()
        pdata = (parent.get("data-test-id") or "").lower()
        combined = f"{name} {pid} {pclass} {pdata}"

        # Zones structurelles reconnues par leur RÔLE (pas par leur nom
        # technique) : elles restent des mots-clés internes utilisés par le
        # reste du pipeline pour appliquer des règles connues (navbar =
        # menu principal, footer = pied de page...). Ce ne sont jamais les
        # titres affichés à l'utilisateur final (voir navigation_discovery_
        # service, qui les traduit en phrases fonctionnelles).
        if "footer" in combined or name == "footer":
            return "footer"
        if "nav" in combined or name == "nav":
            return "navbar"
        if "hero" in combined:
            return "hero"
        if "search" in combined:
            return "search"
        if "google-auth" in combined or "microsoft-auth" in combined or "oauth" in combined:
            return "oauth"
        if "sign-in" in combined or "signin" in combined or "login" in combined:
            return "auth"
        if "directory" in combined:
            return "directory"
        if "game" in combined:
            return "games"
        if "learning" in combined:
            return "learning"
        if "talent" in combined or "job" in combined or "emploi" in combined:
            return "jobs"
        for zone_key, keywords in _CONTENT_ZONE_KEYWORDS.items():
            if any(kw in combined for kw in keywords):
                return zone_key
        if name == "form":
            return "form"
        if name == "header":
            return "header"
        if name in ("section", "main", "article", "aside", "div"):
            # On ne traite un <div> comme un conteneur fonctionnel à part
            # entière que s'il a un id/classe (sinon il est trop générique
            # pour être une "zone" — on continue à remonter le DOM).
            if name == "div" and not (pid or pclass or pdata):
                continue
            heading = _container_display_title(parent)
            if heading:
                return heading
            raw = pid or pdata or (pclass.split()[0] if pclass else name)
            return _humanize_zone_label(raw)
    return "page"


# ── Constructeur de sélecteur CSS ─────────────────────────────────────────────

_CSS_SPECIAL_CHARS = re.compile(r'([:./\[\]\(\)#,%])')


def _escape_css_class(class_name: str) -> str:
    return _CSS_SPECIAL_CHARS.sub(r'\\\1', class_name)


def _build_selector(tag: Tag) -> str:
    """
    Construit un sélecteur CSS fiable, ou retourne "NONE" si aucun attribut
    stable n'est disponible. "NONE" est traité par script_service via XPath.
    """
    tag_id = tag.get("id")
    if tag_id and len(tag_id) < 100:
        safe_id = _CSS_SPECIAL_CHARS.sub(r'\\\1', tag_id)
        return f"#{safe_id}"

    for attr in ("data-testid", "data-test-id", "data-test", "data-cy", "data-qa"):
        val = tag.get(attr)
        if val and len(val) < 100:
            return f"[{attr}='{val.replace(chr(39), chr(92)+chr(39))}']"

    name_attr = tag.get("name")
    if name_attr and len(name_attr) < 100:
        return f"[name='{name_attr.replace(chr(39), chr(92)+chr(39))}']"

    # Classes : on filtre celles avec "/" (Tailwind fraction) et on échappe
    classes = [c for c in (tag.get("class") or []) if c and "/" not in c]
    if classes:
        escaped = [_escape_css_class(c) for c in classes[:2]]
        return f"{tag.name}." + ".".join(escaped)

    # Dernier recours : pas d'id/data-*/name/classe exploitable.
    # On utilise tag[type='...'] (input/button) plutot que "NONE", qui
    # forcerait script_service a chercher un texte litteral inexistant
    # (ex: le label de secours "input_submit" n'est jamais du texte reel
    # affiche sur la page -> TimeoutException garanti).
    # pick_visible() cote script_service gere deja les doublons en
    # choisissant le premier match visible/actif.
    type_attr = tag.get("type")
    if type_attr and tag.name in ("input", "button"):
        safe_type = type_attr.replace("'", "\\'")
        return f"{tag.name}[type='{safe_type}']"

    return "NONE"


# ── Extraction statique ───────────────────────────────────────────────────────

def _parse_html_elements(html_code: str) -> list[UIElement]:
    elements: list[UIElement] = []
    soup = BeautifulSoup(html_code, "html.parser")

    dropdown_container_ids, submenu_child_to_parent = _detect_dropdown_menus(soup)
    language_link_ids = _detect_language_links(soup)

    for tag in soup.find_all("input"):
        input_type = tag.get("type", "text").lower()
        if input_type == "hidden" or _is_hidden(tag, dropdown_container_ids=dropdown_container_ids):
            continue
        if input_type == "submit" and _submit_input_is_redundant(tag):
            continue
        label_text = (
            tag.get("placeholder") or tag.get("aria-label")
            or tag.get("name") or tag.get("id") or f"input_{input_type}"
        )
        ctx = _get_context(tag)
        trigger = submenu_child_to_parent.get(id(tag))
        hover_selector, hover_label = None, None
        if trigger is not None:
            hover_selector = _build_selector(trigger)
            hover_label = (
                trigger.get("aria-label") or trigger.get_text(strip=True)
                or trigger.get("title") or "menu"
            )[:80]
        elements.append(UIElement(
            type=f"input_{input_type}", label=f"[{ctx}] {label_text}",
            selector_hint=_build_selector(tag),
            requires_hover=trigger is not None,
            hover_target_hint=hover_selector,
            hover_target_label=hover_label,
            zone=ctx,
        ))

    for tag in soup.find_all("button"):
        if _is_hidden(tag, dropdown_container_ids=dropdown_container_ids):
            continue
        label_text = (
            tag.get("aria-label") or tag.get_text(strip=True)
            or tag.get("data-tracking-control-name") or tag.get("id") or "button"
        )
        if not label_text or len(label_text) > 120:
            continue
        ctx = _get_context(tag)
        elements.append(UIElement(
            type="button", label=f"[{ctx}] {label_text[:100]}",
            selector_hint=_build_selector(tag),
            zone=ctx,
        ))

    for tag in soup.find_all("input", type="submit"):
        if _is_hidden(tag, dropdown_container_ids=dropdown_container_ids) or _submit_input_is_redundant(tag):
            continue
        ctx = _get_context(tag)
        elements.append(UIElement(
            type="button", label=f"[{ctx}] {tag.get('value', 'Submit')}",
            selector_hint=_build_selector(tag),
            zone=ctx,
        ))

    _ASSET_EXT_RE = re.compile(r'\.(jpe?g|png|gif|svg|webp|bmp|ico)(\?|$)', re.I)
    _THUMBNAIL_PATH_RE = re.compile(r'itok=|/styles/|/thumbnails?/', re.I)

    for tag in soup.find_all("a"):
        if _is_hidden(tag, dropdown_container_ids=dropdown_container_ids):
            continue
        real_text = tag.get("aria-label") or tag.get_text(strip=True) or tag.get("title")
        href = tag.get("href", "")
        if not real_text:
            # FIX titres illisibles ("//amendes.finances.gov.tn/jsp/...",
            # "/fr/sites/default/files/styles/mediatheque_big/...?itok=...") :
            # un lien SANS texte/aria-label/title visible est presque
            # toujours une image cliquable (miniature de galerie, logo
            # partenaire...). Avant : on retombait sur le href BRUT comme
            # label -> le titre du scénario affichait un chemin de fichier
            # ou une URL au lieu d'un nom compréhensible. On essaie d'abord
            # l'attribut alt de l'image contenue, qui porte le VRAI nom
            # humain ("Cérémonie officielle...") quand le site le fournit.
            img = tag.find("img")
            img_alt = (img.get("alt") or "").strip() if img else ""
            if img_alt:
                real_text = img_alt
            elif img is not None and (_ASSET_EXT_RE.search(href) or _THUMBNAIL_PATH_RE.search(href)):
                # Miniature décorative sans alt : aucun libellé humain
                # exploitable -> on l'ignore plutôt que d'inventer un
                # scénario "Vérifier ... : /fr/sites/default/files/...".
                continue
        label_text = real_text or href or "#"
        if not label_text or len(label_text) > 100:
            continue
        if _is_junk_link(label_text, href, has_real_text_signal=bool(real_text)):
            continue
        ctx = _get_context(tag)

        # FIX sous-menus : si ce lien est un enfant d'un menu déroulant
        # détecté, on le tague pour que scenario_service lui garantisse un
        # scénario dédié (hover + clic) SANS passer par le LLM, et pour que
        # script_service génère le ActionChains.move_to_element() requis
        # avant de pouvoir cliquer dessus (l'élément reste display:none
        # tant que le déclencheur n'a pas été survolé).
        trigger = submenu_child_to_parent.get(id(tag))
        requires_hover = trigger is not None
        hover_selector = None
        hover_label = None
        if requires_hover:
            hover_selector = _build_selector(trigger)
            hover_label = (
                trigger.get("aria-label") or trigger.get_text(strip=True)
                or trigger.get("title") or "menu"
            )[:80]

        elements.append(UIElement(
            type="link", label=f"[{ctx}] {label_text[:80]}",
            selector_hint=_build_selector(tag), is_link=True,
            possible_destination=href[:150] if href and href not in ("#", "/", "") else None,
            requires_hover=requires_hover,
            hover_target_hint=hover_selector,
            hover_target_label=hover_label,
            nav_group="language_switcher" if (not requires_hover and id(tag) in language_link_ids) else None,
            zone=ctx,
            is_external=_is_external_link(href),
        ))

    for tag in soup.find_all("select"):
        if _is_hidden(tag, dropdown_container_ids=dropdown_container_ids):
            continue
        label_text = tag.get("aria-label") or tag.get("name") or tag.get("id") or "select"
        ctx = _get_context(tag)
        elements.append(UIElement(
            type="select", label=f"[{ctx}] {label_text}",
            selector_hint=_build_selector(tag),
            zone=ctx,
        ))

    for tag in soup.find_all("textarea"):
        if _is_hidden(tag, dropdown_container_ids=dropdown_container_ids):
            continue
        label_text = tag.get("placeholder") or tag.get("aria-label") or tag.get("name") or "textarea"
        ctx = _get_context(tag)
        elements.append(UIElement(
            type="textarea", label=f"[{ctx}] {label_text}",
            selector_hint=_build_selector(tag),
            zone=ctx,
        ))

    for tag in soup.find_all("input", type=["checkbox", "radio"]):
        if _is_hidden(tag, dropdown_container_ids=dropdown_container_ids):
            continue
        label_el = soup.find("label", {"for": tag.get("id", "")})
        label_text = (
            (label_el.get_text(strip=True) if label_el else None)
            or tag.get("aria-label") or tag.get("name") or tag.get("id") or tag.get("type")
        )
        ctx = _get_context(tag)
        elements.append(UIElement(
            type=tag.get("type"), label=f"[{ctx}] {label_text}",
            selector_hint=_build_selector(tag),
            zone=ctx,
        ))

    for tag in soup.find_all(attrs={"role": ["button", "link", "menuitem", "tab", "option"]}):
        if tag.name in ("button", "a", "input"):
            continue
        if _is_hidden(tag, dropdown_container_ids=dropdown_container_ids):
            continue
        label_text = (
            tag.get("aria-label") or tag.get_text(strip=True)
            or tag.get("data-tracking-control-name") or tag.get("role")
        )
        if not label_text or len(label_text) > 100:
            continue
        ctx = _get_context(tag)
        role = tag.get("role", "button")
        elements.append(UIElement(
            type=f"role_{role}", label=f"[{ctx}] {label_text[:80]}",
            selector_hint=_build_selector(tag),
            zone=ctx,
        ))

    # ── Éléments cliquables via événement JS (onclick/onchange/...) ───────
    # Problème 7 : des éléments comme <div onclick="...">, <li onclick="...">
    # ou <span onchange="..."> (menus custom, "Nous contacter", "En savoir
    # plus" implémentés sans <a>/<button>) n'étaient détectés par AUCUNE
    # des boucles ci-dessus, qui ne regardent que les vraies balises
    # interactives natives ou role=. On les capture ici explicitement.
    for tag in soup.find_all(True):
        if tag.name in ("input", "button", "a", "select", "textarea", "form"):
            continue  # déjà couverts ci-dessus
        if tag.get("role") in ("button", "link", "menuitem", "tab", "option"):
            continue  # déjà couvert par la boucle role= ci-dessus
        event_attr = next((a for a in _JS_EVENT_ATTRS if tag.get(a)), None)
        if not event_attr:
            continue
        if _is_hidden(tag, dropdown_container_ids=dropdown_container_ids):
            continue
        label_text = tag.get("aria-label") or tag.get_text(strip=True) or tag.get("title")
        if not label_text or len(label_text) > 100:
            continue
        ctx = _get_context(tag)
        elements.append(UIElement(
            type=f"js_{tag.name}", label=f"[{ctx}] {label_text[:80]}",
            selector_hint=_build_selector(tag),
            zone=ctx,
        ))

    seen = set()
    deduped = []
    for el in elements:
        key = (el.type, (el.label or "").lower().strip())
        if key not in seen:
            seen.add(key)
            deduped.append(el)

    # FIX Problème 7 : ce cap était fixé à 50 alors que les éléments sont
    # accumulés type par type (input, button, submit, a, select, textarea,
    # checkbox/radio, role=..., js=...). Sur une page riche (formulaire
    # long + nav + footer), les inputs/boutons remplissaient déjà le quota
    # à eux seuls -> TOUS les liens (a) ajoutés ensuite dans la liste,
    # y compris "Mot de passe oublié", "Créer un compte", "FAQ", "Aide",
    # "Nous contacter"... étaient silencieusement coupés par ce slice,
    # AVANT même d'atteindre scenario_service (qui, lui, garantit pourtant
    # une couverture complète de tout ce qu'il reçoit). Relevé à 200 : le
    # vrai plafond du nombre de scénarios reste MAX_SCENARIOS côté
    # scenario_service/config.py, qui s'applique lui APRÈS coverage, donc
    # ce cap-ci n'a plus besoin d'être restrictif — il ne sert plus qu'à
    # éviter un cas pathologique (page générée avec des milliers de nœuds).
    # Relevé de 200 -> MAX_UI_ELEMENTS (600 par défaut, config.py) : une
    # page ministérielle riche (mega-menu + actualités + rapports +
    # téléchargements + galeries + footer) dépasse facilement 200 éléments
    # interactifs distincts une fois la troncature HTML ci-dessus corrigée.
    if len(deduped) > MAX_UI_ELEMENTS:
        logger.warning(
            "%d éléments UI détectés > MAX_UI_ELEMENTS=%d : les %d derniers "
            "sont tronqués. Augmentez MAX_UI_ELEMENTS si cette page doit "
            "être couverte entièrement.",
            len(deduped), MAX_UI_ELEMENTS, len(deduped) - MAX_UI_ELEMENTS,
        )
    return deduped[:MAX_UI_ELEMENTS]


# ── Détection du type de page ─────────────────────────────────────────────────

PAGE_TYPE_PROMPT = """Analyze this HTML and respond ONLY with JSON.

HTML (first 2000 chars):
{html_snippet}

Respond ONLY with this JSON:
{{
  "page_type": "login|registration|form|dashboard|profile|settings|search|product|checkout|homepage|other",
  "description": "one sentence describing ALL the main features visible on this page"
}}
"""


async def _detect_page_type(html_code: str) -> tuple[str, str]:
    snippet = html_code[:2000]
    prompt = PAGE_TYPE_PROMPT.format(html_snippet=snippet)
    payload = {
        "model": TEXT_MODEL,
        "prompt": prompt,
        "stream": True,
        "keep_alive": 0,
        "format": "json",
        "options": {"temperature": 0.1, "num_predict": 80},
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
        parsed = json.loads(full)
        return parsed.get("page_type", "general"), parsed.get("description", "")
    except Exception:
        return "general", ""


# ── Fallback LLM pour JSX/React/Vue ──────────────────────────────────────────
# AVANT : le prompt avait "CSS selector" comme valeur d'exemple →
#         le LLM copiait ce placeholder littéralement → Selenium cherchait
#         un élément avec le sélecteur "CSS selector" → TimeoutException.
# APRÈS : on demande un VRAI sélecteur (id, name, data-*, classe concrète)
#         et on filtre toute valeur générique/placeholder en sortie.

FALLBACK_PROMPT = """You are a UI parser. Extract every interactive element from the code below.

Code:
{code_snippet}

Rules:
- For selector_hint, provide a REAL CSS selector using the actual attributes you see in the code.
  Priority order: #id-value > [name='attr-value'] > [data-testid='value'] > tag.real-class-name
  Example good values: "#search-input", "[name='email']", "button.btn-primary", "[data-testid='submit']"
  NEVER write "CSS selector", "selector", ".selector" or any placeholder — only real values from the code.
  If you cannot determine a real selector, write "NONE".
- For label, use the visible text, placeholder, aria-label, or name attribute you see.
- For type, choose from: button, input_text, input_email, input_password, input_search,
  link, select, checkbox, radio, textarea.
- For is_link, set true only for <a> tags or elements that navigate.
- For possible_destination, use the href value if present, otherwise null.

Respond ONLY with valid JSON — no markdown, no explanation:
{{
  "elements": [
    {{
      "type": "input_email",
      "label": "[form] Email address",
      "selector_hint": "[name='email']",
      "is_link": false,
      "possible_destination": null
    }}
  ]
}}
"""


async def _llm_fallback_parse(code: str) -> list[UIElement]:
    prompt = FALLBACK_PROMPT.format(code_snippet=code[:6000])
    payload = {
        "model": TEXT_MODEL,
        "prompt": prompt,
        "stream": True,
        "keep_alive": 0,
        "format": "json",
        "options": {"temperature": 0.1, "num_predict": 1500},
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

        parsed = json.loads(full)
        elements = []
        for raw in parsed.get("elements", []):
            # Remplacer les selector_hint invalides/placeholder par "NONE"
            # pour que script_service utilise le fallback XPath par texte.
            sel = raw.get("selector_hint", "NONE") or "NONE"
            if not _is_valid_selector(sel):
                raw["selector_hint"] = "NONE"
            try:
                elements.append(UIElement(**raw))
            except Exception:
                continue
        return elements[:30]
    except Exception:
        return []


# ── Point d'entrée principal ──────────────────────────────────────────────────

async def analyze_html_code(html_code: str) -> UIAnalysisResult:
    cleaned = _clean_html(html_code)
    elements = _parse_html_elements(cleaned)

    if not elements:
        elements = await _llm_fallback_parse(html_code[:8000])

    page_type, description = await _detect_page_type(cleaned)

    # NOTE : raw_description est un résumé À USAGE INTERNE (logs, debug),
    # PAS un texte destiné à être affiché à l'utilisateur avant les
    # scénarios (voir règle produit : jamais de jargon technique du type
    # "Page type: X. N elements detected in contexts: navbar, footer,
    # block-..." dans le rapport). On garde donc une phrase neutre et
    # courte, sans énumérer les noms de zones/contextes bruts.
    raw_desc = (description or f"Interface de type {page_type}").strip()
    if not raw_desc.endswith("."):
        raw_desc += "."

    return UIAnalysisResult(
        elements=elements,
        raw_description=raw_desc,
        page_type=page_type,
    )