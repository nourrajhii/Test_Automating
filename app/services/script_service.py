"""
script_service.py
-----------------
Genere un script Selenium Python pour chaque scenario.
Utilise UNIQUEMENT les sélecteurs réels extraits du HTML.

FIX 1 : pick_visible() - quand plusieurs éléments partagent le même sélecteur
        CSS (ex: boutons de carrousel répétés), on cherche parmi TOUS les
        matches celui qui est réellement visible, au lieu de prendre
        aveuglément le premier du DOM (souvent caché/désactivé).
FIX 2 : "CSS selector" et toutes les valeurs placeholder de selector_hint
        sont traitées comme "NONE" → fallback XPath par texte visible.
FIX 3 : les exceptions ne sont plus avalées — STEP_FAIL affiche le sélecteur
        et l'erreur réelle pour faciliter le diagnostic.
FIX 4 : timeout WebDriverWait porté à 15s pour réduire les faux positifs.
"""
import re

from app.models.schemas import TestScenario, UIAnalysisResult, GeneratedScript, UIElement


# Valeurs de selector_hint à traiter comme "NONE" (placeholder LLM, tags nus…)
_INVALID_SELECTORS = {
    "NONE", "none",
    "CSS selector", "css selector", "CSS_selector",
    "selector", ".selector", "#selector",
    "button", "input", "a", "div", "span", "form",
    "", "null", "undefined",
}


def _is_valid_selector(sel: str | None) -> bool:
    if not sel:
        return False
    if sel.strip() in _INVALID_SELECTORS:
        return False
    if len(sel.strip()) < 2:
        return False
    return True


# ── Boilerplate ──────────────────────────────────────────────────────────────

BOILERPLATE_HEADER = """\
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from webdriver_manager.chrome import ChromeDriverManager

options = Options()
options.add_argument("--headless")
options.add_argument("--no-sandbox")
options.add_argument("--disable-dev-shm-usage")

driver = None
passed_steps = 0
failed_steps = 0


def pick_visible(driver, wait, by, selector, clickable=False):
    \"\"\"
    Attend qu'AU MOINS UN des éléments matchant le sélecteur soit visible
    (et enabled si clickable=True), puis le retourne.
    Évite de tomber sur un doublon caché en première position du DOM
    (ex: bouton précédent/suivant d'un carrousel désactivé par défaut).
    \"\"\"
    def _condition(d):
        candidates = d.find_elements(by, selector)
        for c in candidates:
            try:
                if not c.is_displayed():
                    continue
                if clickable and not c.is_enabled():
                    continue
                return c
            except Exception:
                continue
        return False
    return wait.until(_condition)


def safe_click(driver, element):
    \"\"\"
    Clique sur l'element en gerant le cas ElementClickInterceptedException
    (overlay, bandeau cookies, header sticky qui recouvre l'element).
    1. Scroll l'element au centre du viewport
    2. Tente le clic natif
    3. Fallback : clic JS (ignore les recouvrements visuels)
    \"\"\"
    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
    except Exception:
        pass
    try:
        element.click()
    except Exception:
        driver.execute_script("arguments[0].click();", element)


try:
    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options,
    )
    driver.set_window_size(1280, 800)
    wait = WebDriverWait(driver, 15)
    driver.get("__TARGET_URL__")
"""

BOILERPLATE_FOOTER_TEMPLATE = """\

    if failed_steps == 0:
        print("PASS: __TITLE__")
    else:
        print(f"PARTIAL: __TITLE__ -- {failed_steps} etape(s) echouee(s), {passed_steps} reussie(s)")

except Exception as e:
    print(f"FAIL: __TITLE__ -- {e}")
    if driver:
        driver.save_screenshot("error___SLUG__.png")

finally:
    if driver:
        driver.quit()
"""


# ── ASSEMBLE ──────────────────────────────────────────────────────────────────

def _assemble(title: str, slug: str, body: str, target_url: str) -> str:
    header = BOILERPLATE_HEADER.replace("__TARGET_URL__", target_url)
    footer = (
        BOILERPLATE_FOOTER_TEMPLATE
        .replace("__TITLE__", title)
        .replace("__SLUG__", slug)
    )
    return header + body + footer


# ── Génération principale ─────────────────────────────────────────────────────

async def generate_selenium_script(
    scenario: TestScenario,
    analysis: UIAnalysisResult,
    target_url: str,
) -> GeneratedScript:
    flat_lines = _generate_actions(scenario, analysis)
    body = _wrap_steps(flat_lines)
    slug = re.sub(r"[^a-z0-9]", "_", scenario.title.lower())[:30]
    code = _assemble(scenario.title, slug, body, target_url)
    return GeneratedScript(scenario=scenario, code=code)


# ── Sélection des éléments selon le type de scénario ─────────────────────────

def _generate_actions(scenario: TestScenario, analysis: UIAnalysisResult) -> list[str]:
    lines = []
    title_lower = scenario.title.lower()
    elements = analysis.elements

    if not elements:
        lines.append('assert driver.title is not None, "Page loaded"')
        return lines

    if any(w in title_lower for w in ("bouton", "button", "cliquable")):
        targets = [el for el in elements if "button" in el.type or "click" in el.type]
        for el in targets[:3]:
            lines += _create_action_for_element(el)

    elif any(w in title_lower for w in ("lien", "link", "navigation")):
        targets = [el for el in elements if el.type == "link" or el.is_link]
        for el in targets[:3]:
            lines += _create_action_for_element(el)

    elif any(w in title_lower for w in ("saisie", "input", "champ", "search", "recherche")):
        targets = [
            el for el in elements
            if "input" in el.type and "submit" not in el.type and "button" not in el.type
        ]
        for el in targets[:3]:
            lines += _create_action_for_element(el)

    elif any(w in title_lower for w in ("texte", "textarea")):
        targets = [el for el in elements if el.type == "textarea"]
        for el in targets[:2]:
            lines += _create_action_for_element(el)

    elif any(w in title_lower for w in ("liste", "select")):
        targets = [el for el in elements if el.type == "select"]
        for el in targets[:2]:
            lines += _create_action_for_element(el)

    else:
        # Test général : prend les 3 premiers éléments
        for el in elements[:3]:
            lines += _create_action_for_element(el)

    if not lines:
        lines.append('assert driver.title is not None, "Page loaded"')

    return lines


# ── Construction XPath depuis le texte visible ────────────────────────────────

def _xpath_escape(text: str) -> str:
    """
    Retourne le texte entouré des bons guillemets pour une expression XPath,
    en préférant les guillemets SIMPLES (apostrophes) pour que la chaîne
    XPath puisse être embarquée sans conflit dans un string Python délimité
    par des guillemets doubles.
    """
    if "'" not in text:
        return f"'{text}'"   # cas normal : 'Sign in', 'Submit', …
    if '"' not in text:
        return f'"{text}"'   # le texte contient des apostrophes : "it's here"
    # Contient les deux : on découpe avec concat() XPath
    parts = text.split("'")
    return "concat(" + ", \"'\", ".join(f"'{p}'" for p in parts) + ")"


def _label_to_xpath(el: UIElement) -> str | None:
    """Extrait le texte visible du label (sans le préfixe [contexte])."""
    raw = el.label or ""
    text = raw.split("]", 1)[-1].strip() if "]" in raw else raw.strip()
    if not text:
        return None
    escaped = _xpath_escape(text)
    return f'//*[contains(normalize-space(.), {escaped})]'


# ── Génération d'une action pour un UIElement ─────────────────────────────────

def _create_action_for_element(el: UIElement) -> list[str]:
    """
    Génère les lignes Python Selenium pour interagir avec un élément.
    Stratégie :
      1. Si selector_hint est un #id → find unique par CSS (EC standard)
      2. Si selector_hint est valide mais pas #id → pick_visible() CSS
      3. Si selector_hint est invalide/NONE → pick_visible() XPath texte
    """
    sel = el.selector_hint

    # ── Cas 3 : pas de sélecteur fiable → fallback XPath par texte ───────────
    if not _is_valid_selector(sel):
        xpath = _label_to_xpath(el)
        if not xpath:
            return []
        # Échapper les " intérieurs pour l'embarquement dans une chaîne Python
        xpath_py = xpath.replace('\\', '\\\\').replace('"', '\\"')
        clickable = "button" in el.type or "click" in el.type or el.type == "link"
        if clickable:
            return [
                f'element = pick_visible(driver, wait, By.XPATH, "{xpath_py}", clickable=True)',
                'safe_click(driver, element)',
            ]
        return [
            f'element = pick_visible(driver, wait, By.XPATH, "{xpath_py}", clickable=False)',
            'assert element.is_displayed()',
        ]

    # ── Cas 1 : #id → unique dans la page, EC standard suffit ────────────────
    if sel.startswith("#"):
        safe = sel.replace('"', '\\"')
        return [
            f'element = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "{safe}")))',
            'assert element.is_displayed()',
        ]

    # ── Cas 2 : XPath explicite (commence par //) ─────────────────────────────
    if sel.startswith("//"):
        safe = sel.replace('"', '\\"')
        return [
            f'element = pick_visible(driver, wait, By.XPATH, "{safe}", clickable=False)',
            'assert element.is_displayed()',
        ]

    # ── Cas 2 : sélecteur CSS (classes, [attr=...]) → pick_visible ───────────
    safe = sel.replace('"', '\\"')
    clickable = "button" in el.type or "click" in el.type or el.type == "link"

    if clickable:
        return [
            f'element = pick_visible(driver, wait, By.CSS_SELECTOR, "{safe}", clickable=True)',
            'safe_click(driver, element)',
        ]
    elif "input" in el.type and "submit" not in el.type and "button" not in el.type:
        return [
            f'element = pick_visible(driver, wait, By.CSS_SELECTOR, "{safe}", clickable=False)',
            'element.clear()',
            'element.send_keys("test")',
        ]
    elif el.type == "textarea":
        return [
            f'element = pick_visible(driver, wait, By.CSS_SELECTOR, "{safe}", clickable=False)',
            'element.clear()',
            'element.send_keys("test text")',
        ]
    elif el.type == "select":
        return [
            f'element = pick_visible(driver, wait, By.CSS_SELECTOR, "{safe}", clickable=True)',
            'safe_click(driver, element)',
        ]
    else:
        return [
            f'element = pick_visible(driver, wait, By.CSS_SELECTOR, "{safe}", clickable=False)',
            'assert element.is_displayed()',
        ]


# ── Wrapping try/except ───────────────────────────────────────────────────────

def _wrap_steps(flat_lines: list[str]) -> str:
    """
    Regroupe les lignes en blocs try/except indépendants.
    Chaque bloc affiche selector + erreur réelle en cas d'échec.
    """
    if not flat_lines:
        return (
            "    try:\n"
            '        assert driver.title is not None, "Page did not load"\n'
            "        passed_steps += 1\n"
            "    except Exception as e:\n"
            '        print(f"STEP_FAIL: {type(e).__name__}: page_load -- {str(e)[:200]}")\n'
            "        failed_steps += 1\n"
        )

    # Regrouper les lignes en actions (une action = commence par element = ...)
    groups: list[list[str]] = []
    current: list[str] = []
    for line in flat_lines:
        if line.startswith("element =") or line.startswith("assert driver.title"):
            if current:
                groups.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        groups.append(current)

    result_lines = []
    for group in groups:
        # Extraire le sélecteur pour l'afficher en cas d'échec
        selector_repr = "unknown"
        for line in group:
            m = re.search(r'By\.\w+,\s*"(.+?)"', line)
            if m:
                selector_repr = m.group(1)[:80]
                break

        result_lines.append("    try:")
        for line in group:
            result_lines.append("        " + line)
        result_lines.append("        passed_steps += 1")
        result_lines.append("    except Exception as e:")
        # On remplace les " du sélecteur par ' pour ne pas casser le f-string
        safe_sel = selector_repr.replace('"', "'")
        result_lines.append(
            f'        print(f"STEP_FAIL: {{type(e).__name__}}: [{safe_sel}] -- {{str(e)[:200]}}")'
        )
        result_lines.append("        failed_steps += 1")

    return "\n".join(result_lines)


# ── Fallback ──────────────────────────────────────────────────────────────────

def _fallback_from_real_selectors(analysis: UIAnalysisResult) -> list[str]:
    return ['assert driver.title is not None, "Page loaded"']