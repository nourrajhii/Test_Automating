"""
script_service.py
-----------------
Genere un script Selenium Python pour chaque scenario.
Utilise UNIQUEMENT les sélecteurs réels extraits du HTML.

<<<<<<< HEAD
MÉTRIQUES DE RAPPORT (ajout) : le script généré mesure désormais lui-même,
à l'exécution, tout ce dont report_service a besoin pour un rapport "pro" :
temps d'exécution, étapes réussies/échouées, assertions réussies/échouées,
URL finale, titre de page, et une capture d'écran systématique (succès ET
échec, pas seulement en cas d'exception). Tout est imprimé en une seule
ligne `RESULT_JSON:{...}` en fin de script, que executor_service.py parse
de façon fiable (plus robuste que de re-parser des logs texte libres).

SCENARIO GROUNDING (ajout) : voir scenario_grounding_service.py. Avant ce
fix, `_generate_actions` (renommée `_generate_actions_legacy` ci-dessous)
routait les actions par MOT-CLÉ DU TITRE du scénario ("navigation" -> les
3 premiers liens de la page), en ignorant totalement le contenu réel de
`scenario.steps`. Un scénario "Cliquer sur ministère des finances" pouvait
donc exécuter un clic sur un tout autre lien. `_generate_actions_grounded`
résout maintenant CHAQUE step vers l'UIElement qu'il désigne réellement
(via scenario_grounding_service.resolve_scenario_steps) et ne génère une
action Selenium QUE pour ce qui a été résolu avec confiance suffisante.
`_generate_actions_legacy` reste utilisée comme filet de sécurité pour les
scénarios sans steps exploitables (ex: `_fallback_from_elements`).
"""
import logging
import re

from app.config import ELEMENT_WAIT_TIMEOUT, SCREENSHOTS_DIR
from app.models.schemas import TestScenario, UIAnalysisResult, GeneratedScript, UIElement
from app.services.driver_service import get_driver_path
from app.services.scenario_grounding_service import (
    ResolvedStep,
    resolve_scenario_steps,
    grounding_summary,
)

logger = logging.getLogger("script_service")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[script_service] %(message)s"))
    logger.addHandler(_h)

=======
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
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd
_INVALID_SELECTORS = {
    "NONE", "none",
    "CSS selector", "css selector", "CSS_selector",
    "selector", ".selector", "#selector",
    "button", "input", "a", "div", "span", "form",
    "", "null", "undefined",
}


<<<<<<< HEAD
def _assert_message_literal(el: "UIElement", selector: str) -> str:
    """
    Construit un message d'assertion lisible (ex: "Élément 'Connexion'
    (button) non visible via #login-btn"), échappé pour être inséré tel
    quel dans une f-string Python entre guillemets doubles générée.

    Avant ce fix, `assert element.is_displayed()` sans message produisait
    un AssertionError VIDE (str(e) == "") -> les logs affichaient
    "STEP_FAIL: AssertionError: [...] -- " sans aucune info exploitable,
    et le rapport HTML (report_service / failure_explainer_service)
    n'avait rien à montrer à l'utilisateur.
    """
    label = (el.label or "").strip()
    # Les labels peuvent contenir un préfixe de contexte "[zone] texte"
    # (voir vision_parser_service._to_ui_elements) -> on ne garde que le
    # texte utile pour rester court.
    if "]" in label:
        label = label.split("]", 1)[-1].strip()
    label = label or "élément"

    msg = f"Élément '{label}' ({el.type}) non visible via {selector}"
    # Échappement pour insertion dans une f-string Python entre guillemets
    # doubles : backslash d'abord, puis guillemets doubles.
    return msg.replace("\\", "\\\\").replace('"', '\\"')


=======
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd
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
<<<<<<< HEAD
import os
import sys
import time
import json
import re
import urllib.request
import urllib.error

# FIX Windows UnicodeEncodeError : la console Windows utilise par défaut
# l'encodage cp1252 (ou le code page actif), incapable d'encoder l'arabe,
# les emojis, ou même certains accents selon la config. On force stdout/
# stderr en UTF-8 AVANT le moindre print (titres de scénario non-ASCII
# possibles : "عربي", "Vérifier la navigation", etc.). errors="replace"
# évite un crash même si un caractère venait à ne pas être encodable.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass  # Python < 3.7, ne devrait pas arriver ici

=======
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
<<<<<<< HEAD
from selenium.webdriver.common.action_chains import ActionChains
from selenium.common.exceptions import TimeoutException, NoSuchElementException
=======
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from webdriver_manager.chrome import ChromeDriverManager
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd

options = Options()
options.add_argument("--headless")
options.add_argument("--no-sandbox")
options.add_argument("--disable-dev-shm-usage")

driver = None
passed_steps = 0
failed_steps = 0
<<<<<<< HEAD
assertions_total = 0
assertions_passed = 0
start_time = time.time()

os.makedirs(r"__SCREENSHOTS_DIR__", exist_ok=True)


def pick_visible(driver, wait, by, selector, clickable=False):
=======


def pick_visible(driver, wait, by, selector, clickable=False):
    \"\"\"
    Attend qu'AU MOINS UN des éléments matchant le sélecteur soit visible
    (et enabled si clickable=True), puis le retourne.
    Évite de tomber sur un doublon caché en première position du DOM
    (ex: bouton précédent/suivant d'un carrousel désactivé par défaut).
    \"\"\"
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd
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
<<<<<<< HEAD
=======
    \"\"\"
    Clique sur l'element en gerant le cas ElementClickInterceptedException
    (overlay, bandeau cookies, header sticky qui recouvre l'element).
    1. Scroll l'element au centre du viewport
    2. Tente le clic natif
    3. Fallback : clic JS (ignore les recouvrements visuels)
    \"\"\"
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd
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
<<<<<<< HEAD
        service=Service("__DRIVER_PATH__"),
        options=options,
    )
    driver.set_window_size(1280, 800)
    wait = WebDriverWait(driver, __WAIT_TIMEOUT__)
    # FIX diagnostic timeout global : marqueur flush=True imprimé AVANT
    # l'action qui peut bloquer (driver.get peut lui-même pendre si le
    # serveur ne répond pas). Si le script entier timeout (voir
    # executor_service._parse_result_json / TimeoutExpired), c'est le
    # DERNIER "STEP_START:" présent dans stdout qui dit sur quoi le
    # script était bloqué au moment du kill -> sans ça, un timeout global
    # ne donnait AUCUNE info scénario-spécifique (toujours le même
    # message générique côté failure_explainer_service).
    print("STEP_START: [page_load:__TARGET_URL__]", flush=True)
    driver.get("__TARGET_URL__")
"""

# NOTE indentation : tout ce bloc reste À L'INTÉRIEUR du "try:" ouvert dans
# BOILERPLATE_HEADER (donc indenté à 4 espaces), jusqu'au "except" qui le
# ferme. Le "finally" quitte toujours le driver en dernier.
BOILERPLATE_FOOTER_TEMPLATE = """\

    elapsed = time.time() - start_time
    try:
        final_url = driver.current_url
    except Exception:
        final_url = None
    try:
        page_title = driver.title
    except Exception:
        page_title = None

    # Screenshot systématique (succès ET échec partiel) — avant, on ne
    # capturait qu'en cas d'exception fatale (bloc except plus bas).
    screenshot_path = None
    try:
        screenshot_path = r"__SCREENSHOT_PATH__"
        driver.save_screenshot(screenshot_path)
    except Exception:
        screenshot_path = None

=======
        service=Service(ChromeDriverManager().install()),
        options=options,
    )
    driver.set_window_size(1280, 800)
    wait = WebDriverWait(driver, 15)
    driver.get("__TARGET_URL__")
"""

BOILERPLATE_FOOTER_TEMPLATE = """\

>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd
    if failed_steps == 0:
        print("PASS: __TITLE__")
    else:
        print(f"PARTIAL: __TITLE__ -- {failed_steps} etape(s) echouee(s), {passed_steps} reussie(s)")

<<<<<<< HEAD
    print("RESULT_JSON:" + json.dumps({
        "success": failed_steps == 0,
        "steps_total": passed_steps + failed_steps,
        "steps_passed": passed_steps,
        "assertions_total": assertions_total,
        "assertions_passed": assertions_passed,
        "execution_time": round(elapsed, 3),
        "final_url": final_url,
        "page_title": page_title,
        "screenshot_path": screenshot_path,
    }, ensure_ascii=False))

except Exception as e:
    elapsed = time.time() - start_time
    screenshot_path = None
    final_url = None
    page_title = None
    if driver:
        try:
            final_url = driver.current_url
            page_title = driver.title
        except Exception:
            pass
        try:
            screenshot_path = r"__SCREENSHOT_PATH__"
            driver.save_screenshot(screenshot_path)
        except Exception:
            screenshot_path = None

    print(f"FAIL: __TITLE__ -- {e}")
    print("RESULT_JSON:" + json.dumps({
        "success": False,
        "steps_total": passed_steps + failed_steps,
        "steps_passed": passed_steps,
        "assertions_total": assertions_total,
        "assertions_passed": assertions_passed,
        "execution_time": round(elapsed, 3),
        "final_url": final_url,
        "page_title": page_title,
        "screenshot_path": screenshot_path,
        "error": str(e),
    }, ensure_ascii=False))
=======
except Exception as e:
    print(f"FAIL: __TITLE__ -- {e}")
    if driver:
        driver.save_screenshot("error___SLUG__.png")
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd

finally:
    if driver:
        driver.quit()
"""


<<<<<<< HEAD
def _assemble(title: str, slug: str, body: str, target_url: str) -> str:
    driver_path_escaped = get_driver_path().replace("\\", "\\\\")
    # Chemin en slashes "/" (fonctionne aussi sous Windows avec save_screenshot)
    # -> évite les soucis d'échappement de backslash dans le code généré.
    screenshot_path = f"{SCREENSHOTS_DIR}/{slug}.png".replace("\\", "/")

    header = (
        BOILERPLATE_HEADER
        .replace("__TARGET_URL__", target_url)
        .replace("__DRIVER_PATH__", driver_path_escaped)
        .replace("__WAIT_TIMEOUT__", str(ELEMENT_WAIT_TIMEOUT))
        .replace("__SCREENSHOTS_DIR__", SCREENSHOTS_DIR)
    )
=======
# ── ASSEMBLE ──────────────────────────────────────────────────────────────────

def _assemble(title: str, slug: str, body: str, target_url: str) -> str:
    header = BOILERPLATE_HEADER.replace("__TARGET_URL__", target_url)
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd
    footer = (
        BOILERPLATE_FOOTER_TEMPLATE
        .replace("__TITLE__", title)
        .replace("__SLUG__", slug)
<<<<<<< HEAD
        .replace("__SCREENSHOT_PATH__", screenshot_path)
=======
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd
    )
    return header + body + footer


<<<<<<< HEAD
=======
# ── Génération principale ─────────────────────────────────────────────────────

>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd
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


<<<<<<< HEAD
# ─────────────────────────────────────────────────────────────────────────────
# Génération des actions — GROUNDÉE sur les steps réels (nouveau), avec
# fallback vers l'ancienne logique par type/titre.
# ─────────────────────────────────────────────────────────────────────────────

def _generate_actions(scenario: TestScenario, analysis: UIAnalysisResult) -> list[str]:
    """
    Point d'entrée unique utilisé par generate_selenium_script.

    1. Tente le grounding : résout chaque `scenario.steps[i]` vers
       l'UIElement réel qu'il désigne (scenario_grounding_service).
    2. Si au moins un step a été résolu avec confiance suffisante, génère
       les actions Selenium DANS L'ORDRE DES STEPS, une par step résolu.
    3. Si AUCUN step n'a pu être résolu (scénario sans libellés exacts,
       ou ancien scénario `_fallback_from_elements` avec des steps
       génériques type "Vérifier navigation"), retombe sur l'ancienne
       logique par mot-clé de titre (`_generate_actions_legacy`) plutôt
       que de produire un script vide.
    """
    if not analysis.elements:
        return ['assert driver.title is not None, "Page loaded"']

    # ── Scénarios de navigation (category + target_labels) ─────────────────
    # Produits directement par scenario_service._scenario_dict_from_nav_category
    # (voir navigation_discovery_service) : un même scénario doit exercer
    # TOUTE une catégorie de liens (navigation principale, sous-menu,
    # liens externes...), pas une cible unique. Ce n'est pas un cas que
    # resolve_scenario_steps peut représenter (il résout un step de texte
    # libre vers UN élément) — on le traite donc en amont, avant tout
    # grounding step-par-step.
    if getattr(scenario, "category", None) and getattr(scenario, "target_labels", None):
        category_lines = _generate_actions_for_category(scenario, analysis)
        if category_lines:
            return category_lines
        logger.warning(
            "Scénario de catégorie '%s' (%s) : aucune cible résolue parmi "
            "target_labels -> fallback sur le grounding step-par-step.",
            scenario.title, scenario.category,
        )

    resolved_steps = resolve_scenario_steps(scenario, analysis)
    summary = grounding_summary(resolved_steps)

    if summary["resolved"] == 0:
        logger.warning(
            "Scenario grounding: aucun step résolu pour '%s' (steps=%s) "
            "-> fallback sur la logique par type/titre.",
            scenario.title, scenario.steps,
        )
        return _generate_actions_legacy(scenario, analysis)

    if summary["unresolved_steps"]:
        logger.info(
            "Scenario grounding '%s' : %d/%d step(s) actionnable(s) résolu(s). "
            "Non résolus (ignorés, pas exécutés à l'aveugle) : %s",
            scenario.title, summary["resolved"], summary["total_actionable_steps"],
            summary["unresolved_steps"],
        )

    return _generate_actions_grounded(resolved_steps)


def _category_target_elements(scenario: TestScenario, analysis: UIAnalysisResult) -> list[UIElement]:
    """
    Résout chaque `target_labels[i]` vers l'UIElement réel par
    correspondance EXACTE du label. Contrairement au grounding "métier"
    (scenario_grounding_service, matching flou par texte libre), les
    labels ici viennent verbatim de navigation_discovery_service — qui
    les a copiés directement depuis les mêmes UIElement — donc une
    correspondance exacte est fiable et ne nécessite aucun score de
    confiance.
    """
    by_label = {el.label: el for el in analysis.elements}
    return [by_label[lbl] for lbl in scenario.target_labels if lbl in by_label]


def _click_and_verify_navigation_lines(el: UIElement) -> list[str]:
    """
    Clique sur `el` puis vérifie qu'une navigation a RÉELLEMENT eu lieu
    (URL ou titre de page différents) — c'est l'assertion "comportement
    après navigation" qui manquait jusqu'ici (_build_interaction_lines ne
    vérifie que la présence/visibilité de l'élément avant le clic, jamais
    son effet). Revient ensuite en arrière (driver.back()) pour permettre
    de tester la cible suivante sur la même page.

    NOTE indentation `_wrap_steps` : la ligne `element = ...` DOIT rester
    en première position du bloc retourné, c'est elle qui sert de
    marqueur de début de groupe/étape (voir _wrap_steps : "line.startswith
    ('element =')"). url_before/title_before sont donc insérés APRÈS elle,
    jamais avant, pour ne pas casser le découpage en étapes indépendantes
    — chaque cible doit rester une étape isolée (un lien cassé ne doit
    pas faire échouer les autres cibles de la même catégorie).
    """
    action_lines = _build_interaction_lines(el)
    if not action_lines or not action_lines[0].startswith("element ="):
        return action_lines  # élément non-cliquable résolu (rare) : laissé tel quel

    label = (el.label or "élément").split("]", 1)[-1].strip() or "élément"
    safe_label = label.replace("\\", "\\\\").replace('"', '\\"')

    head = [action_lines[0]]           # "element = ..."
    tail = action_lines[1:]            # ex: ['safe_click(driver, element)']

    return (
        head
        + ["url_before = driver.current_url", "title_before = driver.title"]
        + tail
        + [
            "time.sleep(0.6)",
            f'assert (driver.current_url != url_before or driver.title != title_before), '
            f'"Aucun changement de page détecté après clic sur \\"{safe_label}\\""',
            "if driver.current_url != url_before:",
            "    driver.back()",
            "    time.sleep(0.4)",
        ]
    )


def _download_link_verification_lines(el: UIElement) -> list[str]:
    """
    Vérifie un lien de TÉLÉCHARGEMENT (PDF, DOC, ZIP...) SANS cliquer
    dessus. Cliquer déclencherait un vrai téléchargement dans Chrome
    headless (fichier écrit sur disque, AUCUNE navigation observable côté
    Selenium) -> l'ancienne assertion générique "changement d'URL/titre"
    (_click_and_verify_navigation_lines, utilisée jusqu'ici pour TOUTES
    les catégories, y compris les téléchargements) échouait alors
    systématiquement, à tort, sur ce type de lien : ce n'est pas "juste
    un lien interne/externe cassé", c'est le mauvais test pour ce type de
    cible.
    On récupère le href réel de l'élément et on vérifie par une requête
    HTTP HEAD que le fichier est bien accessible (status < 400) — c'est
    la VRAIE intention du scénario ("ce document est téléchargeable"),
    pas une navigation qui n'a jamais lieu pour ce type de lien.
    """
    action_lines = _build_interaction_lines(el)
    if not action_lines or not action_lines[0].startswith("element ="):
        return action_lines  # élément non-cliquable résolu (rare) : laissé tel quel

    label = (el.label or "élément").split("]", 1)[-1].strip() or "élément"
    safe_label = label.replace("\\", "\\\\").replace('"', '\\"')

    return [
        action_lines[0],  # element = ...
        'href = element.get_attribute("href")',
        f'assert href, "Lien de telechargement sans destination (href) : \\"{safe_label}\\""',
        "_dl_req = urllib.request.Request(href, method='HEAD', headers={'User-Agent': 'Mozilla/5.0'})",
        "try:",
        "    with urllib.request.urlopen(_dl_req, timeout=10) as _dl_resp:",
        "        _dl_status = _dl_resp.status",
        "except urllib.error.HTTPError as _dl_err:",
        "    _dl_status = _dl_err.code",
        "except Exception as _dl_err:",
        "    _dl_status = None",
        f'assert _dl_status is not None and _dl_status < 400, '
        f'f"Fichier de telechargement inaccessible (HTTP {{_dl_status}}) pour \\"{safe_label}\\" : {{href}}"',
    ]


def _contact_link_verification_lines(el: UIElement) -> list[str]:
    """
    Vérifie un lien de CONTACT (tel:, mailto:) ou de RÉSEAU SOCIAL
    (Facebook, Instagram...) SANS s'appuyer sur l'assertion de navigation
    générique : un lien tel:/mailto: ouvre une application externe (pas de
    changement d'URL/titre observable côté Selenium) et un lien social
    ouvre souvent un nouvel onglet (le titre/URL de l'onglet ACTUEL ne
    change pas non plus) — dans les deux cas, _click_and_verify_navigation_
    lines échouerait à tort. On vérifie donc le format de la destination
    (numéro/email) ou son accessibilité HTTP (réseau social), sans cliquer.
    """
    action_lines = _build_interaction_lines(el)
    if not action_lines or not action_lines[0].startswith("element ="):
        return action_lines

    label = (el.label or "élément").split("]", 1)[-1].strip() or "élément"
    safe_label = label.replace("\\", "\\\\").replace('"', '\\"')

    return [
        action_lines[0],  # element = ...
        'href = element.get_attribute("href") or ""',
        f'assert href, "Lien de contact sans destination : \\"{safe_label}\\""',
        "if href.startswith('tel:'):",
        "    assert re.match(r'^tel:\\+?[0-9 ().-]{6,}$', href), "
        f'f"Numero de telephone invalide pour \\"{safe_label}\\" : {{href}}"',
        "elif href.startswith('mailto:'):",
        "    assert re.match(r'^mailto:[^@\\s]+@[^@\\s]+\\.[^@\\s]+', href), "
        f'f"Adresse email invalide pour \\"{safe_label}\\" : {{href}}"',
        "else:",
        "    _c_req = urllib.request.Request(href, method='HEAD', headers={'User-Agent': 'Mozilla/5.0'})",
        "    try:",
        "        with urllib.request.urlopen(_c_req, timeout=10) as _c_resp:",
        "            _c_status = _c_resp.status",
        "    except urllib.error.HTTPError as _c_err:",
        "        _c_status = _c_err.code",
        "    except Exception:",
        "        _c_status = None",
        "    assert _c_status is not None and _c_status < 400, "
        f'f"Lien de contact inaccessible (HTTP {{_c_status}}) pour \\"{safe_label}\\" : {{href}}"',
    ]


def _generate_actions_for_category(scenario: TestScenario, analysis: UIAnalysisResult) -> list[str]:
    """
    Génère UNE étape par cible de `scenario.target_labels` (donc UN
    scénario = N étapes = N clics vérifiés), plutôt qu'un scénario par
    lien. C'est le pendant, côté génération de code, du regroupement fait
    en amont par navigation_discovery_service : la fusion se voit dans le
    RAPPORT (un seul scénario "Navigation principale" avec un taux de
    réussite type 7/8), pas dans le nombre de vérifications réellement
    effectuées — la couverture par élément reste totale.
    """
    targets = _category_target_elements(scenario, analysis)
    if not targets:
        return []

    is_submenu = bool(scenario.category) and scenario.category.startswith("submenu:")
    is_downloads = bool(scenario.category) and scenario.category.startswith("downloads")
    is_contact = scenario.category == "contact"
    lines: list[str] = []
    for el in targets:
        if is_submenu and getattr(el, "requires_hover", False):
            lines += _hover_open_menu_lines(el)
        if is_downloads:
            lines += _download_link_verification_lines(el)
        elif is_contact:
            lines += _contact_link_verification_lines(el)
        else:
            lines += _click_and_verify_navigation_lines(el)

    return lines


def _generate_actions_grounded(resolved_steps: list[ResolvedStep]) -> list[str]:
    lines: list[str] = []
    # Dernier texte survolé explicitement par un step "hover" — évite un
    # double hover consécutif quand le LLM écrit "Survoler X" PUIS "Cliquer
    # sur Y" alors que Y a déjà `requires_hover=True` pointant sur X :
    # sans ça, _create_action_for_element(Y) reproduirait le même hover
    # juste après celui déjà généré pour le step explicite.
    last_hover_target: str | None = None

    for r in resolved_steps:
        if r.action in ("navigate", "assert"):
            # "Ouvrir la page" est déjà fait par le boilerplate (driver.get).
            # Les steps "Vérifier ..." sans cible DOM précise ne génèrent
            # pas d'action ici — l'assertion réelle (URL/titre/élément
            # visible) est déjà couverte par la ligne d'assertion générée
            # pour le step click/type correspondant (_build_interaction_lines).
            continue

        if r.action == "hover":
            if r.target_text:
                lines += _direct_hover_lines(r.target_text)
                last_hover_target = _normalize_for_dedup(r.target_text)
            continue

        # click / type / check : nécessite un élément résolu avec confiance
        if r.element is None:
            continue

        if (
            getattr(r.element, "requires_hover", False)
            and r.element.hover_target_label
            and _normalize_for_dedup(r.element.hover_target_label) == last_hover_target
        ):
            # Le hover a déjà été fait explicitement par le step précédent
            # -> ne génère que l'action sur l'élément cible, sans hover.
            action_lines = _build_interaction_lines(r.element)
            if action_lines:
                lines += action_lines
        else:
            lines += _create_action_for_element(r.element)

    if not lines:
        lines.append('assert driver.title is not None, "Page loaded"')

    return lines


def _normalize_for_dedup(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _direct_hover_lines(target_text: str) -> list[str]:
    """
    Survole l'élément désigné par `target_text` (step "Survoler 'X'"),
    en le localisant directement par XPath texte visible — indépendant du
    fait que ce déclencheur existe ou non comme UIElement à part entière
    (souvent, seul l'enfant du sous-menu est extrait comme UIElement, pas
    le déclencheur lui-même — voir html_parser_service._find_submenu_container).
    """
    xpath = f'//*[contains(normalize-space(.), {_xpath_escape(target_text)})]'
    xpath_py = xpath.replace('\\', '\\\\').replace('"', '\\"')
    return [
        f'trigger = pick_visible(driver, wait, By.XPATH, "{xpath_py}", clickable=False)',
        'ActionChains(driver).move_to_element(trigger).pause(0.3).perform()',
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Ancienne logique (conservée comme FALLBACK uniquement — voir _generate_actions)
# ─────────────────────────────────────────────────────────────────────────────

def _generate_actions_legacy(scenario: TestScenario, analysis: UIAnalysisResult) -> list[str]:
=======
# ── Sélection des éléments selon le type de scénario ─────────────────────────

def _generate_actions(scenario: TestScenario, analysis: UIAnalysisResult) -> list[str]:
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd
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
<<<<<<< HEAD
=======
        # Test général : prend les 3 premiers éléments
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd
        for el in elements[:3]:
            lines += _create_action_for_element(el)

    if not lines:
        lines.append('assert driver.title is not None, "Page loaded"')

    return lines


<<<<<<< HEAD
def _xpath_escape(text: str) -> str:
    if "'" not in text:
        return f"'{text}'"
    if '"' not in text:
        return f'"{text}"'
=======
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
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd
    parts = text.split("'")
    return "concat(" + ", \"'\", ".join(f"'{p}'" for p in parts) + ")"


def _label_to_xpath(el: UIElement) -> str | None:
<<<<<<< HEAD
=======
    """Extrait le texte visible du label (sans le préfixe [contexte])."""
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd
    raw = el.label or ""
    text = raw.split("]", 1)[-1].strip() if "]" in raw else raw.strip()
    if not text:
        return None
    escaped = _xpath_escape(text)
    return f'//*[contains(normalize-space(.), {escaped})]'


<<<<<<< HEAD
def _hover_open_menu_lines(el: UIElement) -> list[str]:
    """
    Génère le survol Selenium (ActionChains.move_to_element) sur le
    déclencheur du sous-menu, AVANT toute interaction avec l'élément cible
    (voir UIElement.requires_hover, renseigné par html_parser_service).
    Sans ce hover, l'élément cible reste display:none et pick_visible() /
    wait.until() timeoutent systématiquement — c'est exactement le bug
    d'origine (sous-menu totalement ignoré des scénarios générés).
    """
    trigger_sel = el.hover_target_hint

    if _is_valid_selector(trigger_sel):
        safe_trigger = trigger_sel.replace('"', '\\"')
        by = "By.XPATH" if trigger_sel.startswith("//") else "By.CSS_SELECTOR"
        return [
            f'trigger = wait.until(EC.presence_of_element_located(({by}, "{safe_trigger}")))',
            'ActionChains(driver).move_to_element(trigger).pause(0.3).perform()',
        ]

    if el.hover_target_label:
        xpath = f'//*[contains(normalize-space(.), {_xpath_escape(el.hover_target_label)})]'
        xpath_py = xpath.replace('\\', '\\\\').replace('"', '\\"')
        return [
            f'trigger = pick_visible(driver, wait, By.XPATH, "{xpath_py}", clickable=False)',
            'ActionChains(driver).move_to_element(trigger).pause(0.3).perform()',
        ]

    return []


def _create_action_for_element(el: UIElement) -> list[str]:
    hover_lines: list[str] = []
    if getattr(el, "requires_hover", False):
        hover_lines = _hover_open_menu_lines(el)

    action_lines = _build_interaction_lines(el)
    if not action_lines:
        return []
    return hover_lines + action_lines


def _build_interaction_lines(el: UIElement) -> list[str]:
    sel = el.selector_hint

=======
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
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd
    if not _is_valid_selector(sel):
        xpath = _label_to_xpath(el)
        if not xpath:
            return []
<<<<<<< HEAD
        xpath_py = xpath.replace('\\', '\\\\').replace('"', '\\"')
        clickable = "button" in el.type or "click" in el.type or el.type == "link"
        assert_msg = _assert_message_literal(el, xpath)
=======
        # Échapper les " intérieurs pour l'embarquement dans une chaîne Python
        xpath_py = xpath.replace('\\', '\\\\').replace('"', '\\"')
        clickable = "button" in el.type or "click" in el.type or el.type == "link"
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd
        if clickable:
            return [
                f'element = pick_visible(driver, wait, By.XPATH, "{xpath_py}", clickable=True)',
                'safe_click(driver, element)',
            ]
        return [
            f'element = pick_visible(driver, wait, By.XPATH, "{xpath_py}", clickable=False)',
<<<<<<< HEAD
            f'assert element.is_displayed(), "{assert_msg}"',
        ]

    if sel.startswith("#"):
        safe = sel.replace('"', '\\"')
        assert_msg = _assert_message_literal(el, sel)
        return [
            f'element = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "{safe}")))',
            f'assert element.is_displayed(), "{assert_msg}"',
        ]

    if sel.startswith("//"):
        safe = sel.replace('"', '\\"')
        assert_msg = _assert_message_literal(el, sel)
        return [
            f'element = pick_visible(driver, wait, By.XPATH, "{safe}", clickable=False)',
            f'assert element.is_displayed(), "{assert_msg}"',
        ]

=======
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
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd
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
<<<<<<< HEAD
        assert_msg = _assert_message_literal(el, sel)
        return [
            f'element = pick_visible(driver, wait, By.CSS_SELECTOR, "{safe}", clickable=False)',
            f'assert element.is_displayed(), "{assert_msg}"',
        ]


def _wrap_steps(flat_lines: list[str]) -> str:
    if not flat_lines:
        return (
            '    print("STEP_START: [page_load]", flush=True)\n'
            "    assertions_total += 1\n"
            "    try:\n"
            '        assert driver.title is not None, "Page did not load"\n'
            "        passed_steps += 1\n"
            "        assertions_passed += 1\n"
=======
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
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd
            "    except Exception as e:\n"
            '        print(f"STEP_FAIL: {type(e).__name__}: page_load -- {str(e)[:200]}")\n'
            "        failed_steps += 1\n"
        )

<<<<<<< HEAD
    groups: list[list[str]] = []
    current: list[str] = []
    for line in flat_lines:
        if line.startswith("element =") or line.startswith("trigger =") or line.startswith("assert driver.title"):
=======
    # Regrouper les lignes en actions (une action = commence par element = ...)
    groups: list[list[str]] = []
    current: list[str] = []
    for line in flat_lines:
        if line.startswith("element =") or line.startswith("assert driver.title"):
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd
            if current:
                groups.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        groups.append(current)

    result_lines = []
    for group in groups:
<<<<<<< HEAD
=======
        # Extraire le sélecteur pour l'afficher en cas d'échec
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd
        selector_repr = "unknown"
        for line in group:
            m = re.search(r'By\.\w+,\s*"(.+?)"', line)
            if m:
                selector_repr = m.group(1)[:80]
                break

<<<<<<< HEAD
        # Nombre d'assertions "réelles" (lignes assert) dans ce groupe.
        # Un groupe purement action (ex: click) contribue 0 assertion mais
        # compte quand même comme 1 étape -> steps et assertions peuvent
        # légitimement différer dans le rapport final.
        n_asserts = sum(1 for line in group if line.strip().startswith("assert"))
        safe_sel = selector_repr.replace('"', "'")

        # FIX diagnostic timeout global : imprimé et flushé AVANT le
        # try/except, donc visible dans stdout même si CE groupe est celui
        # qui bloque (pick_visible / wait.until peuvent pendre jusqu'au
        # timeout global du subprocess). executor_service lit le dernier
        # "STEP_START:" capturé pour savoir précisément sur quel élément
        # le script était bloqué au moment du kill.
        result_lines.append(f'    print("STEP_START: [{safe_sel}]", flush=True)')
        result_lines.append(f"    assertions_total += {n_asserts}")
=======
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd
        result_lines.append("    try:")
        for line in group:
            result_lines.append("        " + line)
        result_lines.append("        passed_steps += 1")
<<<<<<< HEAD
        result_lines.append(f"        assertions_passed += {n_asserts}")
        result_lines.append("    except Exception as e:")
=======
        result_lines.append("    except Exception as e:")
        # On remplace les " du sélecteur par ' pour ne pas casser le f-string
        safe_sel = selector_repr.replace('"', "'")
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd
        result_lines.append(
            f'        print(f"STEP_FAIL: {{type(e).__name__}}: [{safe_sel}] -- {{str(e)[:200]}}")'
        )
        result_lines.append("        failed_steps += 1")

    return "\n".join(result_lines)


<<<<<<< HEAD
def _fallback_from_real_selectors(analysis: UIAnalysisResult) -> list[str]:
    return ['assert driver.title is not None, "Page loaded"']
=======
# ── Fallback ──────────────────────────────────────────────────────────────────

def _fallback_from_real_selectors(analysis: UIAnalysisResult) -> list[str]:
    return ['assert driver.title is not None, "Page loaded"']
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd
