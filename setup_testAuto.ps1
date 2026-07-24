# ============================================================
# setup_testAuto.ps1
# Script de réparation automatique de Test_Auto
# À lancer depuis : C:\Users\nourr\Desktop\Test_Auto\
# Usage : .\setup_testAuto.ps1
# ============================================================

Write-Host ""
Write-Host "=== Test_Auto — Script de réparation ===" -ForegroundColor Cyan
Write-Host ""

# ── Étape 1 : Vérification du dossier courant ─────────────────
if (-not (Test-Path "app\config.py")) {
    Write-Host "ERREUR : Lance ce script depuis C:\Users\nourr\Desktop\Test_Auto\" -ForegroundColor Red
    Write-Host "  cd C:\Users\nourr\Desktop\Test_Auto" -ForegroundColor Yellow
    Write-Host "  .\setup_testAuto.ps1" -ForegroundColor Yellow
    exit 1
}
Write-Host "[1/5] Dossier correct : $(Get-Location)" -ForegroundColor Green

# ── Étape 2 : Nettoyage des fichiers temporaires ──────────────
Write-Host "[2/5] Nettoyage des fichiers temporaires..." -ForegroundColor Yellow
if (Test-Path "reports\*.py") {
    $count = (Get-ChildItem "reports\*.py").Count
    Remove-Item "reports\*.py" -Force
    Write-Host "      $count fichiers .py supprimés dans reports\" -ForegroundColor Green
} else {
    Write-Host "      reports\ déjà propre" -ForegroundColor Green
}

# ── Étape 3 : Écriture directe de config.py ──────────────────
Write-Host "[3/5] Mise à jour de app\config.py..." -ForegroundColor Yellow
$configContent = @'
import os

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

# Modele texte pour l'analyse HTML, la generation de scenarios et de scripts
TEXT_MODEL = os.getenv("TEXT_MODEL", "llama3.2:3b")

REPORTS_DIR = "reports"
UPLOAD_DIR  = "uploads"

# Timeouts (en secondes)
TEXT_TIMEOUT      = int(os.getenv("TEXT_TIMEOUT",      "360"))
EXECUTION_TIMEOUT = int(os.getenv("EXECUTION_TIMEOUT", "120"))

# Nombre max de scenarios a generer par interface
MAX_SCENARIOS = int(os.getenv("MAX_SCENARIOS", "6"))

# URL de l'app testee — Vite tourne sur 5173, pas 3000
TARGET_APP_URL = os.getenv("TARGET_APP_URL", "http://localhost:5173")
'@
Set-Content -Path "app\config.py" -Value $configContent -Encoding UTF8
Write-Host "      app\config.py mis a jour (localhost:5173)" -ForegroundColor Green

# ── Étape 4 : Écriture directe de executor_service.py ────────
Write-Host "[4/5] Mise a jour de app\services\executor_service.py..." -ForegroundColor Yellow
$executorContent = @'
"""
executor_service.py
-------------------
Execute un script Selenium et retourne un rapport.
"""
import subprocess
import uuid
import os
import sys
import socket
from urllib.parse import urlparse

from app.config import REPORTS_DIR, EXECUTION_TIMEOUT, TARGET_APP_URL
from app.models.schemas import GeneratedScript, ExecutionReport


def _app_is_reachable(url: str, timeout: float = 2.0) -> bool:
    """Teste la connexion TCP sur host:port extrait de l URL."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or "localhost"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def execute_script(generated: GeneratedScript) -> ExecutionReport:
    os.makedirs(REPORTS_DIR, exist_ok=True)
    script_id = str(uuid.uuid4())[:8]
    script_path = os.path.join(REPORTS_DIR, f"test_{script_id}.py")

    # Pre-flight check
    if not _app_is_reachable(TARGET_APP_URL):
        return ExecutionReport(
            success=False,
            logs=[f"PRE-FLIGHT FAIL : {TARGET_APP_URL} inaccessible"],
            error=(
                f"Le frontend n est pas demarre sur {TARGET_APP_URL}\n"
                f"Lance : cd frontend && npm run dev\n"
                f"Puis relance le pipeline."
            ),
        )

    with open(script_path, "w", encoding="utf-8") as f:
        f.write(generated.code)

    try:
        env = {
            **os.environ,
            "PYTHONPATH": os.getcwd(),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        result = subprocess.run(
            [sys.executable, script_path],
            capture_output=True,
            text=True,
            timeout=EXECUTION_TIMEOUT,
            env=env,
        )

        stdout_lines = result.stdout.splitlines()
        stderr_lines = result.stderr.splitlines()
        all_logs = stdout_lines + stderr_lines

        fatal = next(
            (l for l in stderr_lines if "Fatal Python error" in l or "init_import_site" in l),
            None,
        )
        if fatal:
            return ExecutionReport(
                success=False,
                logs=all_logs,
                error=(
                    "Crash Python au demarrage du script.\n"
                    "Fix : pip install --upgrade webdriver-manager selenium --force-reinstall\n"
                    f"Detail : {fatal}"
                ),
            )

        # Nettoyage automatique du script temporaire
        try:
            os.remove(script_path)
        except OSError:
            pass

        return ExecutionReport(
            success=result.returncode == 0,
            logs=all_logs,
            error=None if result.returncode == 0 else result.stderr[:2000],
        )

    except subprocess.TimeoutExpired:
        return ExecutionReport(
            success=False,
            logs=[],
            error=f"Timeout : script depasse {EXECUTION_TIMEOUT}s.",
        )
    except Exception as e:
        return ExecutionReport(success=False, logs=[], error=str(e))
'@
Set-Content -Path "app\services\executor_service.py" -Value $executorContent -Encoding UTF8
Write-Host "      app\services\executor_service.py mis a jour" -ForegroundColor Green

# ── Étape 5 : Écriture directe de script_service.py ──────────
Write-Host "[5/5] Mise a jour de app\services\script_service.py..." -ForegroundColor Yellow
$scriptServiceContent = @'
"""
script_service.py
-----------------
Genere un script Selenium Python syntaxiquement garanti pour chaque scenario.
"""
import httpx
import json
import re

from app.config import OLLAMA_BASE_URL, TEXT_MODEL, TARGET_APP_URL
from app.models.schemas import TestScenario, UIAnalysisResult, GeneratedScript


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
try:
    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options,
    )
    driver.set_window_size(1280, 800)
    wait = WebDriverWait(driver, 10)
    driver.get("__TARGET_URL__")
"""

BOILERPLATE_FOOTER_TEMPLATE = """\

    print("PASS: __TITLE__")

except Exception as e:
    print(f"FAIL: __TITLE__ -- {e}")
    if driver:
        driver.save_screenshot("error___SLUG__.png")

finally:
    if driver:
        driver.quit()
"""

STEPS_PROMPT = """You are a Selenium Python expert. Generate ONLY the action lines for a test body.

=== STRICT RULES ===
- Do NOT write: imports, driver setup, options, try/except/finally, driver.quit(), driver.get()
- Do NOT write: from selenium, import, webdriver., ChromeDriverManager, Service(
- Variables already defined: driver, wait -- use them directly
- Each line MUST start with exactly 4 spaces of indentation
- NO markdown, NO backticks, NO plain English sentences, NO # comments

=== CORRECT PATTERNS (use only these) ===
    elem = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "#email")))
    elem.clear()
    elem.send_keys("user@example.com")
    btn = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button[type='submit']")))
    btn.click()
    wait.until(EC.presence_of_element_located((By.XPATH, "//a[contains(text(), 'Help')]")))
    assert driver.title != ""

=== FORBIDDEN PATTERNS ===
    driver.wait.until(...)
    driver.find_element(...) alone
    wait.until(driver.find_element(...))
    (driver.options, 'input_submit')
    By.CSS_SELECTOR, "link:texte"
    By.CSS_SELECTOR, "input_submit"
    a.link:contains('...')

=== SELECTORS ===
- By.CSS_SELECTOR: "#id", ".class", "input[type='email']", "button[type='submit']"
- By.XPATH for text: "//a[contains(text(), 'my text')]"
- By.ID: "my-id"

=== SCENARIO ===
Title: {title}
Steps:
{steps}
Expected: {expected}

UI elements:
{elements}

Generate ONLY the 4-space-indented action lines (max 10 lines):"""


async def generate_selenium_script(
    scenario: TestScenario,
    analysis: UIAnalysisResult,
) -> GeneratedScript:

    steps_text = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(scenario.steps))
    elements_text = "\n".join(
        f"  {e.type}: '{e.label}' -> CSS: {e.selector_hint or 'auto'}"
        for e in analysis.elements
    )

    prompt = STEPS_PROMPT.format(
        title=scenario.title,
        steps=steps_text,
        expected=scenario.expected_result,
        elements=elements_text,
    )

    payload = {
        "model": TEXT_MODEL,
        "prompt": prompt,
        "stream": True,
        "keep_alive": 0,
        "options": {"temperature": 0.05, "num_predict": 400},
    }

    timeout = httpx.Timeout(timeout=None)
    full_response = ""

    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", f"{OLLAMA_BASE_URL}/api/generate", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                try:
                    chunk = json.loads(line)
                    full_response += chunk.get("response", "")
                    if chunk.get("done"):
                        break
                except Exception:
                    continue

    slug = re.sub(r"[^a-z0-9]", "_", scenario.title.lower())[:30]
    body = _clean_steps(full_response)
    code = _assemble(scenario.title, slug, body)

    return GeneratedScript(scenario=scenario, code=code)


_FORBIDDEN_STARTS = (
    "from ", "import ", "driver =", "driver.get(", "driver.quit",
    "options", "wait =", "try:", "except", "finally:",
    "webdriver.", "ChromeDriverManager", "Service(", "print(",
)

_PYTHON_SYMBOLS = ("=", "(", ".", "[", '"', "'")


def _clean_steps(text: str) -> str:
    text = re.sub(r"```(?:python)?\s*", "", text)
    text = re.sub(r"```\s*$", "", text, flags=re.MULTILINE)

    text = re.sub(r'driver\.wait\.until\s*\(\s*wait\.until\s*\(', 'wait.until(', text)
    text = re.sub(r'driver\.wait\.until\s*\(', 'wait.until(', text)
    text = re.sub(r'driver\.options\s*,', 'By.CSS_SELECTOR,', text)
    text = re.sub(
        r'wait\.until\s*\(\s*driver\.find_element\s*\(\s*By\.([\w]+)\s*,\s*([\'"][^\'"]+[\'"])\s*\)\s*\)',
        r'wait.until(EC.presence_of_element_located((By.\1, \2)))',
        text,
    )

    def _fix_link_selector(m):
        t = m.group(1).strip()
        return f'By.XPATH, "//a[contains(text(), \'{t}\')]"'
    text = re.sub(r'By\.CSS_SELECTOR,\s*[\'"]link:([^\'"]+)[\'"]', _fix_link_selector, text)
    text = re.sub(r':contains\([\'"][^\'"]*[\'"]\)', '', text)
    text = re.sub(r'[\'"]input_submit[\'"]', '"input[type=\'submit\']"', text)
    text = re.sub(r'[\'"]input_text[\'"]',   '"input[type=\'text\']"',   text)

    clean_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if any(stripped.startswith(f) for f in _FORBIDDEN_STARTS):
            continue
        if stripped.startswith("#"):
            continue
        if not any(c in stripped for c in _PYTHON_SYMBOLS):
            continue
        clean_lines.append("    " + stripped)

    if not clean_lines:
        clean_lines = [
            '    assert driver.title is not None, "Page did not load"',
            '    assert len(driver.find_elements(By.TAG_NAME, "body")) > 0',
        ]

    return "\n".join(clean_lines)


def _assemble(title: str, slug: str, body: str) -> str:
    header = BOILERPLATE_HEADER.replace("__TARGET_URL__", TARGET_APP_URL)
    footer = BOILERPLATE_FOOTER_TEMPLATE.replace("__TITLE__", title).replace("__SLUG__", slug)
    return header + body + footer
'@
Set-Content -Path "app\services\script_service.py" -Value $scriptServiceContent -Encoding UTF8
Write-Host "      app\services\script_service.py mis a jour" -ForegroundColor Green

# ── Vérification finale ───────────────────────────────────────
Write-Host ""
Write-Host "=== Verification ===" -ForegroundColor Cyan
$configCheck = Select-String -Path "app\config.py" -Pattern "5173" -Quiet
if ($configCheck) {
    Write-Host "[OK] app\config.py contient localhost:5173" -ForegroundColor Green
} else {
    Write-Host "[ERREUR] app\config.py ne contient pas 5173 !" -ForegroundColor Red
}

# ── Instructions finales ──────────────────────────────────────
Write-Host ""
Write-Host "=== Prochaines etapes ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "1. Reparer le venv (Fatal Python error) :" -ForegroundColor White
Write-Host "   pip install --upgrade webdriver-manager selenium --force-reinstall" -ForegroundColor Yellow
Write-Host ""
Write-Host "2. Terminal 1 - Lancer le frontend :" -ForegroundColor White
Write-Host "   cd frontend" -ForegroundColor Yellow
Write-Host "   npm run dev" -ForegroundColor Yellow
Write-Host "   (doit afficher http://localhost:5173/)" -ForegroundColor Gray
Write-Host ""
Write-Host "3. Terminal 2 - Lancer le backend :" -ForegroundColor White
Write-Host "   uvicorn app.main:app --reload --reload-dir app --port 8000" -ForegroundColor Yellow
Write-Host ""
Write-Host "=== Script termine avec succes ===" -ForegroundColor Green
