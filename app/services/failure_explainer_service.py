"""
failure_explainer_service.py
------------------------------
Résumés intelligents des échecs (feature démo PFE).

Approche 100% déterministe (règles + regex), volontairement SANS appel LLM :
- rapide (pas de latence Ollama)
- fiable (le petit modèle local llama3.2:1b n'est pas assez robuste pour
  raisonner de façon cohérente sur des logs Selenium)
- cohérent avec le reste du projet (fallback XPath déterministe, OCR
  grounding...) : on préfère des règles explicables à un LLM qui hallucine.

Entrée : le dict execution_report (celui produit par executor_service /
ExecutionReport.dict()) + le titre du scénario.
Sortie : un dict structuré, prêt à être affiché dans le rapport HTML
(report_service.py) ou dans le frontend (SSE) si tu veux l'exposer en direct.
"""
import os
import re

from app.services.fix_suggester_service import suggest_code_patch

# ─────────────────────────────────────────────────────────────────────────
# Score de confiance (%) par type d'exception -- 100% déterministe, pas de
# LLM ici non plus. Plus le type d'exception est fréquent/bien identifié
# dans _EXCEPTION_INFO, plus le diagnostic (cause/hypothèses/suggestion)
# est fiable -> confiance plus haute. Un type inconnu ou deviné par
# mots-clés (voir _guess_exception_from_message) a une confiance plus
# basse car on ne dispose pas du vrai nom de l'exception Python.
# ─────────────────────────────────────────────────────────────────────────
_CONFIDENCE_BY_EXCEPTION = {
    "ElementClickInterceptedException": 90,
    "NoSuchElementException": 85,
    "ElementNotInteractableException": 88,
    "TimeoutException": 82,
    "StaleElementReferenceException": 80,
    "AssertionError": 75,
}
_CONFIDENCE_DEFAULT = 55          # type d'exception inconnu (bloc _DEFAULT_INFO)
_CONFIDENCE_GUESSED = 45          # exception devinée par mot-clé (report["error"] brut)

# Nos propres scripts (script_service._assert_message_literal) génèrent des
# messages d'assertion au format "Élément 'LABEL' (TYPE) non visible via
# SELECTOR" -> permet de retrouver le texte visible de l'élément pour un
# affichage plus lisible (rapport Agent IA notamment).
_LABEL_FROM_MESSAGE_RE = re.compile(r"^Élément '(.+?)' \((.+?)\) non visible via (.+)$")

# ─────────────────────────────────────────────────────────────────────────
# Base de connaissances : type d'exception Selenium -> explication FR
# ─────────────────────────────────────────────────────────────────────────

_EXCEPTION_INFO = {
    "TimeoutException": {
        "cause": "L'élément ciblé n'est pas devenu visible/interactif dans le délai imparti.",
        "hypotheses": [
            "Le sélecteur ne correspond plus à un élément existant (page modifiée depuis l'analyse).",
            "La page n'était pas encore totalement chargée (contenu chargé en différé via JavaScript).",
            "Un élément (popup, bandeau cookies, overlay) masque ou bloque l'élément ciblé.",
        ],
        "suggestion": "Augmenter le délai d'attente (ELEMENT_WAIT_TIMEOUT) ou attendre explicitement la visibilité de l'élément avant l'action, et vérifier qu'aucun élément ne le recouvre.",
    },
    "NoSuchElementException": {
        "cause": "L'élément ciblé est introuvable dans le DOM.",
        "hypotheses": [
            "Le sélecteur CSS/XPath est obsolète ou incorrect.",
            "L'élément est généré dynamiquement et n'existe pas encore au moment du test.",
            "La page chargée n'est pas celle attendue (redirection, erreur de chargement).",
        ],
        "suggestion": "Vérifier le sélecteur dans le DOM réel (inspecteur navigateur) ou basculer sur le fallback XPath par texte visible.",
    },
    "ElementClickInterceptedException": {
        "cause": "Le clic a été intercepté : un autre élément se trouve visuellement au-dessus de la cible.",
        "hypotheses": [
            "Un bandeau cookies, une popup ou un overlay est encore affiché.",
            "L'élément est partiellement hors de l'écran (nécessite un scroll).",
            "Une animation/transition CSS n'est pas terminée au moment du clic.",
        ],
        "suggestion": "Fermer/gérer les popups avant l'action, ou utiliser un clic JavaScript (déjà fait par safe_click) après un scroll explicite.",
    },
    "StaleElementReferenceException": {
        "cause": "La référence DOM de l'élément n'est plus valide : la page a changé sous le test.",
        "hypotheses": [
            "Le DOM a été re-rendu (framework JS type React) entre la localisation et l'action.",
            "Une navigation ou un rafraîchissement partiel de la page a eu lieu.",
        ],
        "suggestion": "Relocaliser l'élément juste avant l'action plutôt que de réutiliser une référence ancienne.",
    },
    "ElementNotInteractableException": {
        "cause": "L'élément existe mais n'est pas dans un état permettant l'interaction (invisible, désactivé, taille nulle).",
        "hypotheses": [
            "L'élément est présent dans le DOM mais caché par du CSS (display:none, visibility:hidden).",
            "L'élément est désactivé (attribut disabled) tant qu'une condition n'est pas remplie.",
        ],
        "suggestion": "Attendre que l'élément devienne visible/activé, ou vérifier une condition préalable (ex: champ requis rempli avant).",
    },
    "AssertionError": {
        "cause": "L'état réel de la page ne correspond pas à ce qui était attendu par le scénario.",
        "hypotheses": [
            "L'élément est présent mais pas visible au moment de la vérification.",
            "Le contenu/texte affiché diffère de ce qui était attendu.",
            "L'action précédente n'a pas produit l'effet attendu sur la page.",
        ],
        "suggestion": "Vérifier manuellement le comportement attendu sur la page réelle et ajuster l'assertion ou attendre un état intermédiaire.",
    },
}

_DEFAULT_INFO = {
    "cause": "Une erreur inattendue s'est produite pendant l'exécution de cette étape.",
    "hypotheses": [
        "Comportement de la page différent de celui observé lors de l'analyse initiale.",
        "Ralentissement réseau ou temps de réponse anormal du serveur.",
    ],
    "suggestion": "Consulter les logs détaillés et le screenshot pour identifier la cause exacte.",
}

# Mots-clés utiles quand on n'a que le message d'erreur global (pas de type
# d'exception explicite) — ex: crash fatal du scénario (bloc except global).
_KEYWORD_TO_EXCEPTION = [
    (r"no such element", "NoSuchElementException"),
    (r"timeout|timed out", "TimeoutException"),
    (r"click intercepted", "ElementClickInterceptedException"),
    (r"stale element", "StaleElementReferenceException"),
    (r"not interactable", "ElementNotInteractableException"),
]

_STEP_FAIL_RE = re.compile(r"^STEP_FAIL:\s*(\w+):\s*\[(.*?)\]\s*--\s*(.*)$")


def _humanize_selector(sel: str) -> str:
    """Traduit un sélecteur technique en description lisible pour le rapport."""
    sel = (sel or "").strip()
    if not sel or sel == "unknown":
        return "l'élément ciblé"

    if sel.startswith("#"):
        return f"l'élément avec l'ID « {sel[1:]} »"

    if sel.startswith("//") or sel.startswith("contains("):
        return f"l'élément identifié par son texte visible (XPath : {sel})"

    if sel.startswith("."):
        return f"l'élément avec la classe « {sel[1:]} »"

    m = re.match(r"^([a-zA-Z][a-zA-Z0-9]*)((?:\.[\w-]+)*)$", sel)
    if m:
        tag, classes = m.groups()
        cls_list = classes.lstrip(".").split(".") if classes else []
        if cls_list:
            return f"l'élément <{tag}> avec la/les classe(s) « {', '.join(cls_list)} »"
        return f"l'élément <{tag}>"

    return f"l'élément correspondant au sélecteur « {sel} »"


def _extract_label(message: str) -> str | None:
    """
    Si le message d'assertion vient de nos scripts générés (voir
    script_service._assert_message_literal), retrouve le texte visible de
    l'élément (ex: "Inline content") pour un affichage plus parlant que le
    sélecteur brut. Retourne None si le format ne correspond pas (ancien
    script, message custom, etc.).
    """
    m = _LABEL_FROM_MESSAGE_RE.match((message or "").strip())
    return m.group(1) if m else None


def _parse_step_failures(logs: list[str]) -> list[dict]:
    """Extrait toutes les lignes STEP_FAIL des logs d'exécution, dans l'ordre."""
    failures = []
    for line in logs or []:
        m = _STEP_FAIL_RE.match(line.strip())
        if m:
            exc_type, selector, message = m.groups()
            # Filet de sécurité : un AssertionError sans message explicite
            # (ex: anciens scripts générés avant l'ajout des messages
            # d'assertion dans script_service) donne str(e) == "" -> on
            # évite d'afficher "Message brut : " vide dans le rapport.
            clean_message = message.strip() or "(Aucun message retourné par l'assertion — vérifier le screenshot et le sélecteur ci-dessus.)"
            failures.append({
                "exception_type": exc_type,
                "selector": selector,
                "selector_human": _humanize_selector(selector),
                "raw_message": clean_message,
                "label": _extract_label(message),
            })
    return failures


def _guess_exception_from_message(message: str) -> str | None:
    low = (message or "").lower()
    for pattern, exc_type in _KEYWORD_TO_EXCEPTION:
        if re.search(pattern, low):
            return exc_type
    return None


def explain_failure(scenario_title: str, report: dict) -> dict:
    """
    Construit une explication structurée à partir d'un execution_report
    (dict, tel que ExecutionReport.dict()) et du titre du scénario.

    Retourne :
    {
        "has_failures": bool,
        "summary": "phrase courte pour le jury",
        "steps": [
            {
                "step_number": 1,
                "exception_type": "TimeoutException",
                "selector_human": "...",
                "cause": "...",
                "hypotheses": [...],
                "suggestion": "...",
                "raw_message": "...",
            },
            ...
        ],
    }
    """
    if report.get("success"):
        return {"has_failures": False, "summary": "", "steps": []}

    logs = report.get("logs", [])
    step_failures = _parse_step_failures(logs)

    steps_out = []
    for i, sf in enumerate(step_failures, start=1):
        info = _EXCEPTION_INFO.get(sf["exception_type"], _DEFAULT_INFO)
        # FIX : visible_text n'était jamais transmis -> l'alternative
        # "XPath par texte visible" (déjà codée dans fix_suggester_service)
        # n'apparaissait jamais dans le patch suggéré.
        patch = suggest_code_patch(sf["exception_type"], sf["selector"], visible_text=sf["label"])
        steps_out.append({
            "step_number": i,
            "exception_type": sf["exception_type"],
            "selector": sf["selector"],
            "selector_human": sf["selector_human"],
            "label": sf["label"],
            "cause": info["cause"],
            "hypotheses": info["hypotheses"],
            "suggestion": info["suggestion"],
            "raw_message": sf["raw_message"][:300],
            "patch": patch,
            "confidence": _CONFIDENCE_BY_EXCEPTION.get(sf["exception_type"], _CONFIDENCE_DEFAULT),
        })

    # Cas : aucun STEP_FAIL dans les logs mais le scénario est quand même en
    # échec global (crash fatal avant le footer, cf. bloc "except" global du
    # script généré) -> on retombe sur report["error"] + recherche de
    # mots-clés pour deviner le type d'exception.
    if not steps_out and report.get("error"):
        error_msg = str(report["error"])
        exc_type = _guess_exception_from_message(error_msg) or "Erreur"
        info = _EXCEPTION_INFO.get(exc_type, _DEFAULT_INFO)
        patch = suggest_code_patch(exc_type, None) if exc_type != "Erreur" else None
        steps_out.append({
            "step_number": 1,
            "exception_type": exc_type,
            "selector": None,
            "selector_human": "l'exécution du scénario",
            "label": None,
            "cause": info["cause"],
            "hypotheses": info["hypotheses"],
            "suggestion": info["suggestion"],
            "raw_message": error_msg[:300],
            "patch": patch,
            "confidence": (_CONFIDENCE_BY_EXCEPTION.get(exc_type, _CONFIDENCE_DEFAULT)
                           if exc_type != "Erreur" else _CONFIDENCE_GUESSED),
        })

    if not steps_out:
        return {"has_failures": True, "summary": f"Le scénario « {scenario_title} » a échoué pour une raison indéterminée.", "steps": []}

    first = steps_out[0]
    n = len(steps_out)
    summary = (
        f"Le test « {scenario_title} » a échoué à l'étape {first['step_number']} "
        f"sur {first['selector_human']}. Cause probable : {first['cause'][0].lower()}{first['cause'][1:]}"
    )
    if n > 1:
        summary += f" ({n} étapes en échec au total.)"

    return {"has_failures": True, "summary": summary, "steps": steps_out}


_AGENT_SEP = "=" * 36


def build_agent_report(scenario_title: str, report: dict, explanation: dict) -> str:
    """
    Génère un bloc texte structuré, compact et à champs fixes, pensé pour
    être PARSÉ par un agent/outil externe (CI, autre LLM, script) plutôt
    que lu par un humain — contrairement à explanation_to_html() qui est
    la version "riche" pour un testeur QA dans le rapport HTML.

    Format volontairement plat (labels sur leur propre ligne, pas de JSON)
    pour rester lisible à l'écran ET trivialement extractible par un
    parseur simple (split sur les lignes "Label:"), sans dépendance à un
    format structuré particulier côté consommateur.
    """
    steps_total = report.get("steps_total", 0)
    steps_passed = report.get("steps_passed", 0)
    steps_failed = steps_total - steps_passed

    if report.get("success"):
        result_label = "PASS ✓"
    elif steps_passed > 0:
        result_label = "PARTIAL ⚠"
    else:
        result_label = "FAIL ✗"

    lines = [
        _AGENT_SEP,
        f"TEST RESULT : {result_label}",
        _AGENT_SEP,
        "",
        "Feature:",
        scenario_title,
        "",
        "Steps:",
        f"✓ Passed : {steps_passed}/{steps_total}",
        f"✗ Failed : {steps_failed}/{steps_total}",
        "",
    ]

    if explanation.get("has_failures") and explanation.get("steps"):
        step = explanation["steps"][0]
        target = step.get("label") or step.get("selector") or step.get("selector_human")

        lines += [
            "FAILED STEP:",
            f'Verify "{target}" visibility',
            "",
            "Error:",
            step["exception_type"],
            "",
            "Element:",
            step.get("selector") or step["selector_human"],
            "",
            "AI Analysis:",
            step["cause"],
        ]
        if step.get("hypotheses"):
            lines.append("Possible causes:")
            lines += [f"- {h}" for h in step["hypotheses"]]
        lines += [
            "",
            "AI Suggestion:",
            step["suggestion"],
            "",
        ]

    screenshot = report.get("screenshot_path")
    lines += [
        "Screenshot:",
        os.path.basename(screenshot) if screenshot else "N/A",
        "",
    ]

    if explanation.get("has_failures") and explanation.get("steps"):
        confidence = explanation["steps"][0].get("confidence", _CONFIDENCE_DEFAULT)
        lines += ["Confidence:", f"{confidence}%", ""]

    lines.append(_AGENT_SEP)
    return "\n".join(lines)


def explanation_to_html(explanation: dict) -> str:
    """Rend l'explication en bloc HTML, pour intégration dans report_service.py."""
    import html as _html

    if not explanation.get("has_failures"):
        return ""

    def esc(s):
        return _html.escape(s or "")

    parts = [f"<p class='fx-summary'>🧠 {esc(explanation['summary'])}</p>"]

    for step in explanation.get("steps", []):
        hyps = "".join(f"<li>{esc(h)}</li>" for h in step["hypotheses"])

        patch = step.get("patch")
        patch_html = ""
        if patch:
            after_blocks = "".join(
                f"<pre class='fx-code fx-code-after'>{esc(a)}</pre>" for a in patch.get("code_after", [])
            )
            html_suggestion_block = (
                f"<p class='fx-patch-label'>🌐 Correctif HTML suggéré :</p>"
                f"<pre class='fx-code fx-code-html'>{esc(patch['html_suggestion'])}</pre>"
                if patch.get("html_suggestion") else ""
            )
            patch_html = f"""
            <div class="fx-patch">
              <p class="fx-patch-label">🛠 Correctif de code suggéré</p>
              <p class="fx-patch-explain">{esc(patch.get('explanation', ''))}</p>
              <p class="fx-patch-label">Avant :</p>
              <pre class="fx-code fx-code-before">{esc(patch['code_before'])}</pre>
              <p class="fx-patch-label">Après :</p>
              {after_blocks}
              {html_suggestion_block}
            </div>
            """

        parts.append(f"""
        <div class="fx-step">
          <p><strong>Étape {step['step_number']}</strong> — {esc(step['exception_type'])} sur {esc(step['selector_human'])}</p>
          <p><em>Cause probable :</em> {esc(step['cause'])}</p>
          <p><em>Hypothèses :</em></p>
          <ul>{hyps}</ul>
          <p><em>Suggestion :</em> {esc(step['suggestion'])}</p>
          <p class="fx-raw">Message brut : {esc(step['raw_message'])}</p>
          {patch_html}
        </div>
        """)

    return "\n".join(parts)