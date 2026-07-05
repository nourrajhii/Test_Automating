from pydantic import BaseModel
from typing import List, Optional


class UIElement(BaseModel):
    type: str                          # button, input, link, checkbox, select…
    label: Optional[str] = None
    selector_hint: Optional[str] = None   # CSS selector ou attribut probable
    is_link: bool = False
    possible_destination: Optional[str] = None


class UIAnalysisResult(BaseModel):
    elements: List[UIElement]
    raw_description: str
    page_type: Optional[str] = "general"


class TestScenario(BaseModel):
    title: str
    steps: List[str]
    expected_result: str


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


class ScenarioWithScript(BaseModel):
    """Un scénario et son script Selenium associé."""
    scenario: TestScenario
    script: GeneratedScript
    execution_report: Optional[ExecutionReport] = None
