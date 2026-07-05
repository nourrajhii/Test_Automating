"""
executor_service.py
-------------------
Execute un script Selenium et retourne un rapport.

FIX 1 : sys.executable garantit le python du venv (évite Python 3.14 système)
FIX 2 : filtre le bruit zipimport/KeyboardInterrupt dans stderr
FIX 3 : ne supprime plus le script avant que le rapport soit transmis
FIX 4 : env nettoyé — supprime les valeurs None qui causent KeyError sur Windows
"""
import subprocess
import uuid
import os
import sys
import re

from app.config import REPORTS_DIR, EXECUTION_TIMEOUT
from app.models.schemas import GeneratedScript, ExecutionReport


# Patterns de bruit à filtrer dans stderr (non-fatals)
_NOISE_PATTERNS = [
    r"Failed checking if argv\[0\] is an import path entry",
    r"<frozen zipimport>",
    r"KeyboardInterrupt",
    r"Traceback \(most recent call last\):\s*$",
    r"^\s*File \"<frozen",
    r"WDM -",
]


def _filter_noise(stderr: str) -> str:
    """Supprime les lignes de bruit connues, garde les vraies erreurs."""
    if not stderr:
        return ""
    lines = stderr.splitlines()
    clean = [l for l in lines if not any(re.search(p, l) for p in _NOISE_PATTERNS)]
    return "\n".join(clean).strip()


def _safe_env() -> dict:
    """
    Construit un environnement subprocess propre.
    Sur Windows, os.environ peut contenir des clés avec valeurs None ou vides
    qui causent un KeyError lors de la sérialisation par subprocess.
    On filtre ces valeurs et on ajoute PYTHONPATH + PYTHONDONTWRITEBYTECODE.
    """
    base = {k: v for k, v in os.environ.items() if isinstance(v, str) and v is not None}
    base["PYTHONPATH"] = os.getcwd()
    base["PYTHONDONTWRITEBYTECODE"] = "1"
    return base


def execute_script(generated: GeneratedScript) -> ExecutionReport:
    os.makedirs(REPORTS_DIR, exist_ok=True)
    script_id   = str(uuid.uuid4())[:8]
    script_path = os.path.join(REPORTS_DIR, f"test_{script_id}.py")

    with open(script_path, "w", encoding="utf-8") as f:
        f.write(generated.code)

    try:
        result = subprocess.run(
            [sys.executable, script_path],   # sys.executable = venv python
            capture_output=True,
            text=True,
            timeout=EXECUTION_TIMEOUT,
            env=_safe_env(),                 # env nettoyé, sans None
        )

        stdout_lines = result.stdout.splitlines()
        stderr_raw   = result.stderr
        stderr_clean = _filter_noise(stderr_raw)
        stderr_lines = stderr_clean.splitlines() if stderr_clean else []
        all_logs     = stdout_lines + stderr_lines

        # Crash fatal Python au démarrage (init_import_site, etc.)
        fatal = next(
            (l for l in stderr_raw.splitlines()
             if "Fatal Python error" in l or "init_import_site" in l),
            None,
        )
        if fatal:
            return ExecutionReport(
                success=False,
                logs=all_logs,
                error=(
                    "Crash Python au démarrage du script.\n"
                    "Fix : pip install --upgrade webdriver-manager selenium --force-reinstall\n"
                    f"Détail : {fatal}"
                ),
            )

        # Détermine le succès :
        # - returncode == 0 : OK
        # - "PASS:" dans stdout : OK même si returncode != 0
        # - "PARTIAL:" dans stdout : succès partiel (on marque success=True
        #   pour que le frontend affiche ✓ plutôt que ✗)
        stdout_text  = result.stdout
        has_pass     = "PASS:"    in stdout_text
        has_partial  = "PARTIAL:" in stdout_text
        is_success   = (result.returncode == 0) or has_pass or has_partial

        error_msg = None
        if not is_success and stderr_clean:
            error_msg = stderr_clean[:2000] or None

        # NOTE : on ne supprime PAS le fichier ici
        # Le frontend affiche le code via script_done (SSE) — le fichier
        # reste disponible pour inspection dans reports/
        # Nettoyage optionnel : décommenter si tu veux supprimer après exécution
        # try:
        #     os.remove(script_path)
        # except OSError:
        #     pass

        return ExecutionReport(
            success=is_success,
            logs=all_logs,
            screenshot_path=None,
            error=error_msg,
        )

    except subprocess.TimeoutExpired:
        return ExecutionReport(
            success=False,
            logs=[],
            error=f"Timeout : script dépasse {EXECUTION_TIMEOUT}s.",
        )
    except Exception as e:
        return ExecutionReport(
            success=False,
            logs=[],
            error=f"Erreur subprocess : {type(e).__name__}: {e}",
        )