from pydantic import BaseModel
from typing import List, Optional


class UIElement(BaseModel):
    type: str                          # button, input, link, checkbox, select…
    label: Optional[str] = None
    selector_hint: Optional[str] = None   # CSS selector ou attribut probable
    is_link: bool = False
    possible_destination: Optional[str] = None

<<<<<<< HEAD
    # ── Support des menus déroulants / flyout (survol pour révéler) ───────
    # Renseigné par html_parser_service quand cet élément est détecté à
    # l'intérieur d'un sous-menu (ex: <a>Menu</a><ul class="sub">...</ul>).
    # Sans ces champs, script_service ne sait pas qu'un hover est requis
    # avant de pouvoir cliquer/interagir avec l'élément, et
    # scenario_service ne peut pas garantir un scénario dédié par sous-lien.
    requires_hover: bool = False
    hover_target_hint: Optional[str] = None    # sélecteur du déclencheur (élément à survoler)
    hover_target_label: Optional[str] = None   # libellé du déclencheur (pour les scénarios)

    # ── Regroupement déterministe (ex: sélecteur de langue) ────────────────
    # Renseigné par html_parser_service quand plusieurs éléments détectés
    # appartiennent à la MÊME fonctionnalité groupée (ex: tous les liens
    # d'un sélecteur de langue). scenario_service retire ces éléments du
    # lot envoyé au LLM et leur génère UN SEUL scénario/feature couvrant
    # tous les éléments partageant la même valeur de nav_group — évite que
    # chaque langue devienne un scénario séparé (voir Problème 2).
    nav_group: Optional[str] = None

    # ── UI Semantic Graph (ui_graph_service) ────────────────────────────────
    # Avant ce champ, la zone structurelle (navbar/footer/header/form...)
    # calculée par html_parser_service._get_context n'existait QUE comme
    # préfixe texte "[zone] label" dans `label` — illisible pour du code de
    # regroupement. `zone` la rend exploitable programmatiquement par
    # ui_graph_service.build_ui_graph sans rien retirer : le préfixe reste
    # aussi dans `label` pour ne casser aucun code existant qui le parse
    # (ex: scenario_grounding_service._label_core).
    zone: Optional[str] = None

    # Heuristique déterministe (html_parser_service) : True si le lien
    # pointe vers une URL absolue http(s) — traité comme "externe" par
    # ui_graph_service / navigation_discovery_service, qui lui associe un
    # scénario dédié plutôt que de le mélanger à la navigation interne.
    is_external: bool = False

=======
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd

class UIAnalysisResult(BaseModel):
    elements: List[UIElement]
    raw_description: str
    page_type: Optional[str] = "general"


class TestScenario(BaseModel):
    title: str
    steps: List[str]
    expected_result: str

<<<<<<< HEAD
    # ── Format QA enrichi (objectif + préconditions) ───────────────────────
    # Un scénario "Ouvrir la page -> Cliquer sur X" n'est pas un scénario
    # QA exploitable : il manque l'INTENTION (pourquoi ce test existe) et
    # les PRÉCONDITIONS (état requis avant de commencer). Champs optionnels
    # pour rester rétro-compatible avec les anciens scripts/rapports qui ne
    # les fournissent pas (fallback_from_elements, vieux scripts générés...).
    objective: Optional[str] = None
    preconditions: List[str] = []

    # ── Scénarios de catégorie (navigation_discovery_service) ──────────────
    # Un scénario "classique" (login, formulaire...) décrit une séquence
    # d'actions sur une cible unique via `steps`. Un scénario de NAVIGATION
    # ("Vérifier la navigation principale") décrit au contraire un même
    # comportement à vérifier sur un ENSEMBLE de liens. `category` +
    # `target_labels` portent cet ensemble ; `steps` reste rempli avec un
    # résumé lisible pour l'affichage, mais script_service route la
    # génération Selenium via target_labels quand ces champs sont présents
    # (voir script_service._generate_actions_for_category). Vides par
    # défaut : AUCUN scénario existant (steps-only) n'est affecté.
    category: Optional[str] = None
    target_labels: List[str] = []

=======
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd

class AllScenarios(BaseModel):
    """Contient tous les scénarios générés pour une interface."""
    scenarios: List[TestScenario]


class GeneratedScript(BaseModel):
    scenario: TestScenario
    code: str
    framework: str = "selenium"


class ExecutionReport(BaseModel):
    success: bool
    logs: List[str]
    screenshot_path: Optional[str] = None
    error: Optional[str] = None

<<<<<<< HEAD
    # ── Métriques ajoutées pour un rapport "pro" ──────────────────────────
    # Toutes remplies par la ligne RESULT_JSON imprimée en fin de script
    # Selenium généré (voir script_service.BOILERPLATE_FOOTER_TEMPLATE) et
    # parsée par executor_service._parse_result_json. Valeurs par défaut
    # neutres pour rester rétro-compatible (ex: timeout, vieux scripts).
    execution_time: Optional[float] = None       # en secondes
    steps_total: int = 0
    steps_passed: int = 0
    assertions_total: int = 0
    assertions_passed: int = 0
    final_url: Optional[str] = None
    page_title: Optional[str] = None

=======
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd

class ScenarioWithScript(BaseModel):
    """Un scénario et son script Selenium associé."""
    scenario: TestScenario
    script: GeneratedScript
<<<<<<< HEAD
    execution_report: Optional[ExecutionReport] = None
=======
    execution_report: Optional[ExecutionReport] = None
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd
