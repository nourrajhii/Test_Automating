"""
executor_service.py
-------------------
Execute un script Selenium et retourne un rapport.

FIX 1 : sys.executable garantit le python du venv (évite Python 3.14 système)
FIX 2 : filtre le bruit zipimport/KeyboardInterrupt dans stderr
FIX 3 : ne supprime plus le script avant que le rapport soit transmis
FIX 4 : env nettoyé — supprime les valeurs None qui causent KeyError sur Windows
<<<<<<< HEAD
FIX 5 : parse la ligne RESULT_JSON imprimée par le script généré (voir
        script_service.BOILERPLATE_FOOTER_TEMPLATE) pour remplir temps
        d'exécution, étapes, assertions, URL finale, titre et screenshot.
        Rétro-compatible : si absente (ancien script), les champs restent
        à leurs valeurs par défaut (0 / None) sans planter.
=======
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd
"""
import subprocess
import uuid
import os
import sys
import re
<<<<<<< HEAD
import json
=======
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd

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
<<<<<<< HEAD
    # FIX UnicodeEncodeError (Windows, cp1252) : force l'UTF-8 pour TOUTE
    # sortie du script enfant (print, stdout, stderr), indépendamment de la
    # code page active de la console Windows. Complète le
    # sys.stdout.reconfigure() déjà ajouté dans le script généré
    # (script_service.py) — défense en profondeur si jamais reconfigure()
    # échouait pour une raison quelconque (Python embarqué, etc.).
    base["PYTHONUTF8"] = "1"
    base["PYTHONIOENCODING"] = "utf-8"
    return base


def _parse_result_json(stdout_text: str) -> dict:
    """
    Cherche la ligne `RESULT_JSON:{...}` imprimée en fin de script Selenium
    généré et la parse. Retourne {} si absente ou invalide (ancien script
    généré avant cet ajout, ou script qui a crashé avant de l'imprimer).
    """
    for line in stdout_text.splitlines():
        if line.startswith("RESULT_JSON:"):
            try:
                return json.loads(line[len("RESULT_JSON:"):])
            except Exception:
                return {}
    return {}


=======
    return base


>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd
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
<<<<<<< HEAD
            encoding="utf-8",                # FIX Windows : ne pas dépendre
            errors="replace",                # de la code page cp1252 pour décoder
=======
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd
            timeout=EXECUTION_TIMEOUT,
            env=_safe_env(),                 # env nettoyé, sans None
        )

<<<<<<< HEAD
        stdout_text  = result.stdout
        result_data  = _parse_result_json(stdout_text)

        # La ligne RESULT_JSON est une métadonnée machine, déjà exploitée
        # ci-dessus -> on la retire des logs affichés à l'utilisateur.
        stdout_lines = [l for l in stdout_text.splitlines() if not l.startswith("RESULT_JSON:")]
=======
        stdout_lines = result.stdout.splitlines()
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd
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
<<<<<<< HEAD
        # - result_data["success"] : filet de sécurité si les préfixes
        #   texte ci-dessus venaient à changer
        has_pass     = "PASS:"    in stdout_text
        has_partial  = "PARTIAL:" in stdout_text
        is_success   = (result.returncode == 0) or has_pass or has_partial or bool(result_data.get("success"))
=======
        stdout_text  = result.stdout
        has_pass     = "PASS:"    in stdout_text
        has_partial  = "PARTIAL:" in stdout_text
        is_success   = (result.returncode == 0) or has_pass or has_partial
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd

        error_msg = None
        if not is_success and stderr_clean:
            error_msg = stderr_clean[:2000] or None
<<<<<<< HEAD
        if not error_msg and result_data.get("error"):
            error_msg = str(result_data["error"])[:2000]
=======
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd

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
<<<<<<< HEAD
            screenshot_path=result_data.get("screenshot_path"),
            error=error_msg,
            execution_time=result_data.get("execution_time"),
            steps_total=result_data.get("steps_total", 0),
            steps_passed=result_data.get("steps_passed", 0),
            assertions_total=result_data.get("assertions_total", 0),
            assertions_passed=result_data.get("assertions_passed", 0),
            final_url=result_data.get("final_url"),
            page_title=result_data.get("page_title"),
        )

    except subprocess.TimeoutExpired as e:
        # FIX PRINCIPAL : avant, on jetait tout stdout/stderr déjà capturés
        # avant le kill -> logs=[] -> failure_explainer_service n'avait
        # RIEN de spécifique à parser et retombait systématiquement sur le
        # même diagnostic générique "TimeoutException / selector=None",
        # ce qui produisait ensuite le MÊME correctif suggéré pour TOUS
        # les scénarios qui timeoutaient, peu importe leur contenu réel.
        #
        # subprocess.TimeoutExpired transporte déjà, dans .stdout/.stderr,
        # tout ce qui a été lu sur les pipes AVANT le kill (CPython lit les
        # pipes progressivement, pas seulement à la fin). On l'exploite :
        # script_service._wrap_steps imprime désormais un
        # "STEP_START: [sélecteur]" (flush=True) avant CHAQUE étape ->
        # le dernier "STEP_START:" présent dans ce stdout partiel indique
        # exactement sur quel élément le script était bloqué au moment du
        # timeout global.
        partial_stdout = e.stdout or ""
        partial_stderr = _filter_noise(e.stderr or "")

        stdout_lines = [l for l in partial_stdout.splitlines() if not l.startswith("RESULT_JSON:")]
        stderr_lines = partial_stderr.splitlines() if partial_stderr else []
        all_logs = stdout_lines + stderr_lines

        last_step_start = None
        for line in stdout_lines:
            if line.startswith("STEP_START:"):
                last_step_start = line[len("STEP_START:"):].strip()

        if last_step_start:
            synth_selector = last_step_start.strip("[]")
            error_msg = (
                f"Timeout global du script ({EXECUTION_TIMEOUT}s) atteint pendant "
                f"l'étape sur {synth_selector} : le script était bloqué sur cet "
                f"élément et n'a pas eu le temps de terminer."
            )
            # Ligne synthétique au format STEP_FAIL, reconnue par
            # failure_explainer_service._parse_step_failures, pour obtenir
            # un diagnostic + un correctif spécifiques à CET élément plutôt
            # qu'un message générique identique à chaque fois.
            all_logs.append(f"STEP_FAIL: TimeoutException: [{synth_selector}] -- {error_msg}")
        else:
            error_msg = (
                f"Timeout global du script ({EXECUTION_TIMEOUT}s) atteint avant "
                "même le premier marqueur d'étape (démarrage de Chrome ou "
                "chargement initial de la page trop lent)."
            )

        return ExecutionReport(
            success=False,
            logs=all_logs,
            error=error_msg,
=======
            screenshot_path=None,
            error=error_msg,
        )

    except subprocess.TimeoutExpired:
        return ExecutionReport(
            success=False,
            logs=[],
            error=f"Timeout : script dépasse {EXECUTION_TIMEOUT}s.",
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd
        )
    except Exception as e:
        return ExecutionReport(
            success=False,
            logs=[],
            error=f"Erreur subprocess : {type(e).__name__}: {e}",
        )