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
import re
from bs4 import BeautifulSoup, Tag

from app.config import OLLAMA_BASE_URL, TEXT_MODEL, TEXT_TIMEOUT
from app.models.schemas import UIAnalysisResult, UIElement


# ── Constantes : valeurs de selector_hint invalides / placeholder ─────────────
# Ces valeurs ne doivent JAMAIS être transmises à Selenium comme sélecteur réel.
_INVALID_SELECTORS = {
    "NONE", "none",
    "CSS selector", "css selector", "CSS_selector",
    "selector", ".selector", "#selector",
    "button", "input", "a", "div", "span", "form",
    "", "null", "undefined",
}


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


def _is_hidden(tag: Tag, max_depth: int = 8) -> bool:
    """
    Vérifie si l'élément OU un de ses parents proches est masqué.
    Beaucoup de sites cachent un conteneur entier (form, div) plutôt que
    chaque champ individuellement — se limiter à `tag` seul rate ces cas.
    """
    node = tag
    depth = 0
    while node is not None and isinstance(node, Tag) and depth < max_depth:
        if _node_looks_hidden(node):
            return True
        node = node.parent
        depth += 1
    return False


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
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style", "svg", "noscript", "link", "meta"]):
        tag.decompose()
    return str(soup)[:40000]


# ── Détection du contexte (section parente) ───────────────────────────────────

def _get_context(tag: Tag) -> str:
    for parent in tag.parents:
        if not isinstance(parent, Tag):
            continue
        name = parent.name or ""
        pid = (parent.get("id") or "").lower()
        pclass = " ".join(parent.get("class") or []).lower()
        pdata = (parent.get("data-test-id") or "").lower()
        combined = f"{name} {pid} {pclass} {pdata}"

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
        if name == "form":
            return "form"
        if name == "header":
            return "header"
        if name in ("section", "main", "article", "aside"):
            label = pid or pdata or (pclass.split()[0] if pclass else name)
            return label[:30]
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

    for tag in soup.find_all("input"):
        input_type = tag.get("type", "text").lower()
        if input_type == "hidden" or _is_hidden(tag):
            continue
        if input_type == "submit" and _submit_input_is_redundant(tag):
            continue
        label_text = (
            tag.get("placeholder") or tag.get("aria-label")
            or tag.get("name") or tag.get("id") or f"input_{input_type}"
        )
        ctx = _get_context(tag)
        elements.append(UIElement(
            type=f"input_{input_type}", label=f"[{ctx}] {label_text}",
            selector_hint=_build_selector(tag),
        ))

    for tag in soup.find_all("button"):
        if _is_hidden(tag):
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
        ))

    for tag in soup.find_all("input", type="submit"):
        if _is_hidden(tag) or _submit_input_is_redundant(tag):
            continue
        ctx = _get_context(tag)
        elements.append(UIElement(
            type="button", label=f"[{ctx}] {tag.get('value', 'Submit')}",
            selector_hint=_build_selector(tag),
        ))

    for tag in soup.find_all("a"):
        if _is_hidden(tag):
            continue
        label_text = (
            tag.get("aria-label") or tag.get_text(strip=True)
            or tag.get("title") or tag.get("href", "#")
        )
        if not label_text or len(label_text) > 100:
            continue
        href = tag.get("href", "")
        ctx = _get_context(tag)
        elements.append(UIElement(
            type="link", label=f"[{ctx}] {label_text[:80]}",
            selector_hint=_build_selector(tag), is_link=True,
            possible_destination=href[:150] if href and href not in ("#", "/", "") else None,
        ))

    for tag in soup.find_all("select"):
        if _is_hidden(tag):
            continue
        label_text = tag.get("aria-label") or tag.get("name") or tag.get("id") or "select"
        ctx = _get_context(tag)
        elements.append(UIElement(
            type="select", label=f"[{ctx}] {label_text}",
            selector_hint=_build_selector(tag),
        ))

    for tag in soup.find_all("textarea"):
        if _is_hidden(tag):
            continue
        label_text = tag.get("placeholder") or tag.get("aria-label") or tag.get("name") or "textarea"
        ctx = _get_context(tag)
        elements.append(UIElement(
            type="textarea", label=f"[{ctx}] {label_text}",
            selector_hint=_build_selector(tag),
        ))

    for tag in soup.find_all("input", type=["checkbox", "radio"]):
        if _is_hidden(tag):
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
        ))

    for tag in soup.find_all(attrs={"role": ["button", "link", "menuitem", "tab", "option"]}):
        if tag.name in ("button", "a", "input"):
            continue
        if _is_hidden(tag):
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
        ))

    seen = set()
    deduped = []
    for el in elements:
        key = (el.type, (el.label or "").lower().strip())
        if key not in seen:
            seen.add(key)
            deduped.append(el)

    return deduped[:50]


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

    raw_desc = f"Page type: {page_type}. {description}. "
    raw_desc += f"{len(elements)} elements detected in contexts: "
    contexts = sorted(set(
        el.label.split("]")[0].replace("[", "").strip()
        for el in elements if "]" in (el.label or "")
    ))
    raw_desc += ", ".join(contexts)

    return UIAnalysisResult(
        elements=elements,
        raw_description=raw_desc,
        page_type=page_type,
    )