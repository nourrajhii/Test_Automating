"""
fix_suggester_service.py
--------------------------
Agent complémentaire à failure_explainer_service.py : génère un PATCH DE
CODE concret (avant / après) pour un step Selenium en échec, plutôt qu'une
simple explication en langage naturel.

Choix de conception (aligné sur le reste du projet, voir
failure_explainer_service.py) : 100% déterministe, PAS d'appel LLM.
Raisons identiques : rapidité, fiabilité, et surtout cohérence — ce
projet sait déjà, en interne, quel est le remède le plus fiable à un
sélecteur mort (le XPath par texte visible, voir
failure_analysis_service.force_text_fallback et
script_service._label_to_xpath / _create_action_for_element). Ce module
ne fait que reformuler ce savoir-faire déjà éprouvé sous forme de patch
de code lisible, affiché à l'utilisateur final dans le rapport HTML.

Entrée : le type d'exception (tel que classifié par
failure_explainer_service._parse_step_failures /
_guess_exception_from_message) + le sélecteur brut loggé, + optionnellement
le texte visible de l'élément si on l'a (permet une suggestion XPath par
texte directement utilisable).

Sortie : dict prêt à afficher :
{
    "code_before": "...",
    "code_after": ["...", "..."],   # 1+ alternatives, la 1ère = recommandée
    "html_suggestion": "..." | None,
    "explanation": "...",
}
"""
from __future__ import annotations
import re


def _selector_kind(selector: str | None) -> str:
    sel = (selector or "").strip()
    if not sel or sel in ("unknown", "None", "null"):
        return "unknown"
    if sel.startswith("#"):
        return "id"
    if sel.startswith("//") or sel.startswith("contains("):
        return "xpath"
    if sel.startswith("."):
        return "class"
    if re.match(r"^[a-zA-Z][a-zA-Z0-9]*(\.[\w-]+)*$", sel):
        return "css_tag_class"
    return "css_generic"


def _lit(sel: str) -> str:
    """Échappe un sélecteur pour l'insérer dans une chaîne Python entre guillemets doubles."""
    return (sel or "").replace('"', '\\"')


def _slugify(text: str | None) -> str:
    """
    Convertit un texte visible (ex: "Vérifier la navigation") en identifiant
    data-testid plausible (ex: "verifier-la-navigation"). Retourne un
    placeholder neutre si aucun texte n'est disponible.

    Remplace l'ancien exemple codé en dur "login-button", qui s'affichait
    littéralement pour N'IMPORTE QUEL scénario en échec (navigation,
    formulaire, etc.) dès que le sélecteur réel était inconnu — donnant
    l'impression trompeuse que le projet ne détecte que des boutons de
    connexion.
    """
    if not text:
        return "ELEMENT_ID"
    ascii_text = (
        text.lower()
        .replace("é", "e").replace("è", "e").replace("ê", "e")
        .replace("à", "a").replace("â", "a")
        .replace("ô", "o").replace("î", "i").replace("ù", "u")
        .replace("ç", "c")
    )
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
    return slug[:40] or "ELEMENT_ID"


def _esc_html_text(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _reconstruct_before_code(selector: str | None, kind: str) -> str:
    if kind == "xpath":
        return f'driver.find_element(By.XPATH, "{_lit(selector)}")'
    if kind == "unknown":
        return 'driver.find_element(By.CSS_SELECTOR, "...")  # sélecteur non identifié dans les logs'
    return f'driver.find_element(By.CSS_SELECTOR, "{_lit(selector)}")'


def suggest_code_patch(
    exception_type: str,
    selector: str | None,
    visible_text: str | None = None,
) -> dict:
    kind = _selector_kind(selector)
    before = _reconstruct_before_code(selector, kind)
    after: list[str] = []
    html_suggestion = None
    explanation = ""

    if exception_type in ("NoSuchElementException", "TimeoutException"):
        # Cause la + fréquente dans ce projet : selector_hint CSS mort/instable.
        if visible_text:
            after.append(
                f'driver.find_element(By.XPATH, "//*[contains(text(), \'{visible_text}\')]")'
            )
        if kind != "unknown":
            after.append(
                "WebDriverWait(driver, 10).until(\n"
                f'    EC.presence_of_element_located((By.CSS_SELECTOR, "{_lit(selector)}"))\n'
                ")"
            )
        # FIX : "login-button" était codé en dur ici et s'affichait pour
        # TOUT scénario en échec sans sélecteur connu (navigation, champ de
        # recherche, etc.), donnant l'impression que seul un bouton de
        # connexion était testé. On dérive maintenant l'exemple du texte
        # réel de l'élément (visible_text) quand on l'a, sinon un
        # placeholder neutre.
        testid_hint = _slugify(visible_text)
        display_text = _esc_html_text(visible_text) if visible_text else "Texte de l'élément"
        after.append(
            f'driver.find_element(By.CSS_SELECTOR, \'[data-testid="{testid_hint}"]\')  '
            "# nécessite un attribut data-testid côté HTML (voir suggestion ci-dessous)"
        )
        html_suggestion = (
            "Ajouter un attribut stable, indépendant des classes CSS/du style :\n"
            f'<button class="..." data-testid="{testid_hint}">{display_text}</button>'
        )
        explanation = (
            "Le sélecteur actuel dépend probablement de classes CSS générées ou "
            "instables (ex: framework de style, classes utilitaires). Un sélecteur "
            "basé sur le texte visible ou un data-testid dédié résiste bien mieux "
            "aux changements de mise en page."
        )

    elif exception_type == "ElementClickInterceptedException":
        after.append(
            "driver.execute_script(\"arguments[0].scrollIntoView({block: 'center'});\", element)\n"
            "try:\n"
            "    element.click()\n"
            "except Exception:\n"
            "    driver.execute_script(\"arguments[0].click();\", element)"
        )
        explanation = (
            "Un autre élément (popup, bandeau cookies, overlay) recouvre "
            "probablement la cible visuellement. Un scroll explicite suivi d'un "
            "clic JavaScript de secours contourne l'interception."
        )

    elif exception_type == "ElementNotInteractableException":
        sel_for_wait = selector if kind != "unknown" else "SELECTOR"
        after.append(
            "WebDriverWait(driver, 10).until(\n"
            f'    EC.element_to_be_clickable((By.CSS_SELECTOR, "{_lit(sel_for_wait)}"))\n'
            ").click()"
        )
        explanation = (
            "L'élément existe dans le DOM mais n'était pas encore interactif "
            "(invisible, désactivé, ou animation en cours). Attendre explicitement "
            "qu'il devienne cliquable plutôt que de cliquer dès sa présence dans "
            "le DOM résout ce cas."
        )

    elif exception_type == "StaleElementReferenceException":
        sel_for_relocate = selector if kind != "unknown" else "SELECTOR"
        after.append(
            "# Relocaliser l'élément juste avant l'action plutôt que de réutiliser\n"
            "# une référence obtenue plus tôt dans le script :\n"
            f'element = driver.find_element(By.CSS_SELECTOR, "{_lit(sel_for_relocate)}")\n'
            "element.click()"
        )
        explanation = (
            "Le DOM a été re-rendu (framework JS type React/Vue) entre la "
            "localisation de l'élément et l'action. Relocaliser l'élément au "
            "dernier moment, juste avant de l'utiliser, évite ce décalage."
        )

    elif exception_type == "AssertionError":
        sel_for_wait = selector if kind != "unknown" else "SELECTOR"
        after.append(
            "WebDriverWait(driver, 10).until(\n"
            f'    EC.visibility_of_element_located((By.CSS_SELECTOR, "{_lit(sel_for_wait)}"))\n'
            ")\n"
            "assert element.is_displayed()"
        )
        explanation = (
            "L'assertion a probablement été évaluée avant que la page n'ait fini "
            "de se mettre à jour. Attendre explicitement l'état attendu avant de "
            "vérifier réduit les faux échecs (flakiness)."
        )

    else:
        after.append(before)
        explanation = (
            "Erreur non catégorisée automatiquement : consulter les logs "
            "détaillés et le screenshot pour identifier la cause exacte."
        )

    return {
        "code_before": before,
        "code_after": after,
        "html_suggestion": html_suggestion,
        "explanation": explanation,
    }
