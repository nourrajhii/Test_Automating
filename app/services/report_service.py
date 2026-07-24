"""
report_service.py
-------------------
Agent ⑨ Report Agent (voir architecture.md).

Rôle : construire un rapport HTML autonome (un seul fichier, pas de
dépendance externe) résumant l'exécution du pipeline : nombre de tests,
réussis/échoués, couverture (features détectées vs testées), détail par
scénario avec logs, code Selenium généré, temps d'exécution, étapes,
assertions, URL finale, titre de page et capture d'écran.

Pas de PDF ici volontairement : un seul fichier HTML autonome (CSS inline,
captures d'écran encodées en base64, pas de dépendance réseau) s'ouvre
partout sans lib supplémentaire. Si tu veux le PDF plus tard, le plus
simple est de convertir CE HTML avec un outil comme weasyprint plutôt que
de dupliquer la mise en page.
"""
import base64
import html
import os
from datetime import datetime

from app.services.failure_explainer_service import explain_failure, explanation_to_html, build_agent_report
from app.config import REPORTS_DIR


def _escape(s: str | None) -> str:
    return html.escape(s or "")


def _fmt_time(seconds) -> str:
    try:
        return f"{float(seconds):.2f} s"
    except (TypeError, ValueError):
        return "N/A"


def _screenshot_data_uri(path: str | None) -> str | None:
    """
    Lit le PNG écrit par le script Selenium généré (voir
    script_service.BOILERPLATE_FOOTER_TEMPLATE) et le renvoie encodé en
    base64, pour que le rapport reste un fichier HTML unique et autonome
    (pas de lien relatif cassé si le rapport est déplacé/partagé).
    Retourne None si le fichier n'existe pas (scénario en échec précoce,
    ancien script sans capture systématique, etc.).
    """
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        return f"data:image/png;base64,{b64}"
    except Exception:
        return None


def _console_block(idx: int, result: dict) -> str:
    """Vue 'console' compacte par scénario, dans le style demandé."""
    scenario = result["scenario"]
    report = result["execution_report"]
    status = "PASS" if report.get("success") else "FAIL"

    steps_total = report.get("steps_total", 0)
    steps_passed = report.get("steps_passed", 0)
    assertions_total = report.get("assertions_total", 0)
    assertions_passed = report.get("assertions_passed", 0)

    error = report.get("error")
    error_line = "Aucune erreur détectée" if report.get("success") else f"Erreur : {(error or 'inconnue')[:200]}"

    lines = [
        "=" * 40,
        f"Feature : {scenario.get('title')}",
        f"Status : {status}",
        f"Execution time : {_fmt_time(report.get('execution_time'))}",
        f"Steps : {steps_passed}/{steps_total}",
        f"Assertions : {assertions_passed}/{assertions_total}",
        "URL finale :",
        report.get("final_url") or "N/A",
        "",
        "Titre :",
        report.get("page_title") or "N/A",
        "",
        error_line,
        f"Screenshot : {report.get('screenshot_path') or 'N/A'}",
    ]
    return "\n".join(_escape(l) for l in lines)


def _scenario_block(idx: int, result: dict) -> str:
    scenario = result["scenario"]
    report = result["execution_report"]
    attempts = result.get("attempts", 1)
    status_class = "pass" if report["success"] else "fail"
    status_label = "✅ RÉUSSI" if report["success"] else "❌ ÉCHOUÉ"

    steps_html = "".join(f"<li>{_escape(s)}</li>" for s in scenario.get("steps", []))
    logs_html = "\n".join(_escape(l) for l in report.get("logs", []))
    error_html = f"<pre class='error'>{_escape(report.get('error'))}</pre>" if report.get("error") else ""
    explanation = explain_failure(scenario.get("title", ""), report)
    explanation_html = (
        f"<details open class='fx-block'><summary>🧠 Analyse intelligente de l'échec</summary>{explanation_to_html(explanation)}</details>"
        if explanation["has_failures"] else ""
    )
    retry_note = f"<span class='retry-note'>({attempts} tentative{'s' if attempts > 1 else ''})</span>" if attempts > 1 else ""

    steps_total = report.get("steps_total", 0)
    steps_passed = report.get("steps_passed", 0)
    assertions_total = report.get("assertions_total", 0)
    assertions_passed = report.get("assertions_passed", 0)

    objective = (scenario.get("objective") or "").strip()
    objective_html = (
        f"<p class='objective'><strong>Objectif :</strong> {_escape(objective)}</p>"
        if objective else ""
    )
    preconditions = [p for p in (scenario.get("preconditions") or []) if str(p).strip()]
    preconditions_html = (
        "<p class='preconditions'><strong>Préconditions :</strong></p><ul>"
        + "".join(f"<li>{_escape(str(p))}</li>" for p in preconditions)
        + "</ul>"
        if preconditions else ""
    )

    meta_html = f"""
      <div class="meta">
        <span class="meta-item">⏱ {_escape(_fmt_time(report.get('execution_time')))}</span>
        <span class="meta-item">📋 Étapes : {steps_passed}/{steps_total}</span>
        <span class="meta-item">✔ Assertions : {assertions_passed}/{assertions_total}</span>
      </div>
      <p class="url-title">
        <strong>URL finale :</strong> {_escape(report.get('final_url') or 'N/A')}<br>
        <strong>Titre :</strong> {_escape(report.get('page_title') or 'N/A')}
      </p>
    """

    data_uri = _screenshot_data_uri(report.get("screenshot_path"))
    screenshot_html = (
        f"<details><summary>Screenshot</summary><img class='shot' src='{data_uri}' alt='screenshot'/></details>"
        if data_uri else ""
    )

    console_html = f"<details><summary>Vue console</summary><pre class='console'>{_console_block(idx, result)}</pre></details>"

    agent_report_text = build_agent_report(scenario.get("title", ""), report, explanation)
    agent_report_html = f"""
    <details class="agent-block">
      <summary>🤖 Format Agent IA (texte structuré, pensé pour un parseur externe)</summary>
      <button class="copy-btn" onclick="navigator.clipboard.writeText(this.nextElementSibling.textContent)">📋 Copier</button>
      <pre class="agent-report">{_escape(agent_report_text)}</pre>
    </details>
    """

    return f"""
    <div class="scenario {status_class}">
      <div class="scenario-header">
        <span class="status">{status_label}</span> {retry_note}
        <h3>{idx + 1}. {_escape(scenario.get('title'))}</h3>
      </div>
      {meta_html}
      <p class="expected"><strong>Résultat attendu :</strong> {_escape(scenario.get('expected_result'))}</p>
      {objective_html}
      {preconditions_html}
      <details>
        <summary>Étapes du scénario</summary>
        <ol>{steps_html}</ol>
      </details>
      <details>
        <summary>Code Selenium généré</summary>
        <pre class="code">{_escape(result.get('script_code', ''))}</pre>
      </details>
      <details>
        <summary>Logs d'exécution</summary>
        <pre class="logs">{logs_html}</pre>
      </details>
      {screenshot_html}
      {console_html}
      {agent_report_html}
      {explanation_html}
      {error_html}
    </div>
    """


def _overall_summary_block(results: list[dict], report_filename: str, total_time: float) -> str:
    total = len(results)
    passed = sum(1 for r in results if r["execution_report"]["success"])
    failed = total - passed
    coverage = (passed / total * 100) if total else 0.0
    screenshots = sum(1 for r in results if r["execution_report"].get("screenshot_path")
                       and os.path.isfile(r["execution_report"]["screenshot_path"]))

    lines = [
        "=" * 25,
        "EXECUTION SUMMARY",
        "=" * 25,
        "",
        f"Total scenarios : {total}",
        f"Passed : {passed}",
        f"Failed : {failed}",
        f"Coverage : {coverage:.0f} %",
        f"Execution time : {total_time:.1f} s",
        f"Screenshots generated : {screenshots}",
        f"Report generated : {report_filename}",
    ]
    return "\n".join(_escape(l) for l in lines)


_CSS = """
body { font-family: -apple-system, Segoe UI, sans-serif; background:#0f1115; color:#e6e6e6; margin:0; padding:2rem; }
h1 { margin-bottom: .2rem; }
.subtitle { color:#9aa0a6; margin-top:0; }
.summary { display:flex; gap:1rem; margin: 1.5rem 0 1rem; flex-wrap:wrap; }
.card { background:#1a1d24; border-radius:10px; padding:1rem 1.5rem; min-width:140px; }
.card .num { font-size:2rem; font-weight:700; }
.card.total .num { color:#8ab4f8; }
.card.passed .num { color:#57c785; }
.card.failed .num { color:#e57373; }
.card.coverage .num { color:#f2c94c; }
.card.time .num { color:#c792ea; }
.card .label { color:#9aa0a6; font-size:.85rem; }
.ascii-summary { background:#1a1d24; border-radius:10px; padding:1rem 1.5rem; margin-bottom:2rem; font-size:.85rem; white-space:pre-wrap; }
.scenario { background:#1a1d24; border-left:4px solid #444; border-radius:8px; padding:1rem 1.5rem; margin-bottom:1rem; }
.scenario.pass { border-left-color:#57c785; }
.scenario.fail { border-left-color:#e57373; }
.scenario-header { display:flex; align-items:center; gap:.6rem; }
.scenario-header h3 { margin:0; font-size:1.05rem; }
.status { font-weight:600; }
.retry-note { color:#9aa0a6; font-size:.85rem; }
.meta { display:flex; gap:1rem; margin:.5rem 0; flex-wrap:wrap; }
.meta-item { background:#0d0f13; border-radius:6px; padding:.2rem .6rem; font-size:.82rem; color:#c8cad0; }
.url-title { color:#9aa0a6; font-size:.82rem; margin:.3rem 0 .6rem; }
.expected { color:#c8cad0; }
details { margin-top:.5rem; }
summary { cursor:pointer; color:#8ab4f8; }
pre { background:#0d0f13; padding:.8rem; border-radius:6px; overflow-x:auto; font-size:.82rem; white-space:pre-wrap; }
pre.error { border-left:3px solid #e57373; color:#ffb3b3; }
pre.console { border-left:3px solid #8ab4f8; }
img.shot { max-width:100%; border-radius:6px; margin-top:.5rem; border:1px solid #2a2d34; }
.fx-block { background:#161a22; border-left:3px solid #f2c94c; border-radius:6px; padding:.6rem 1rem; margin-top:.5rem; }
.fx-summary { color:#f2c94c; font-weight:600; }
.fx-step { border-top:1px solid #2a2d34; padding-top:.5rem; margin-top:.5rem; font-size:.88rem; }
.fx-step ul { margin:.3rem 0; padding-left:1.2rem; }
.fx-raw { color:#9aa0a6; font-size:.78rem; font-style:italic; }
.fx-patch { border-top:1px dashed #3a3f4a; margin-top:.7rem; padding-top:.6rem; }
.fx-patch-label { color:#e6e6e6; font-weight:600; font-size:.85rem; margin:.4rem 0 .2rem; }
.fx-patch-explain { color:#c8cad0; font-size:.85rem; margin:0 0 .4rem; }
.fx-code { font-family: "SFMono-Regular", Consolas, Menlo, monospace; font-size:.8rem; border-radius:6px; padding:.6rem .8rem; margin:.2rem 0; }
.fx-code-before { background:#2a1414; border-left:3px solid #e57373; color:#ffb3b3; }
.fx-code-after { background:#122a18; border-left:3px solid #57c785; color:#b7f0c6; }
.fx-code-html { background:#161f2a; border-left:3px solid #8ab4f8; color:#bcdcff; }
.agent-block { background:#0b0d12; border:1px solid #2a2d34; border-radius:8px; padding:.4rem .8rem .8rem; margin-top:.5rem; }
.agent-block summary { color:#c792ea; font-weight:600; }
.agent-report {
  background:#000; color:#7ee787; font-family: "SFMono-Regular", Consolas, Menlo, monospace;
  font-size:.78rem; line-height:1.5; border-radius:6px; padding:.8rem 1rem; white-space:pre-wrap;
}
.copy-btn {
  display:block; margin:.4rem 0 .3rem; background:#1e222b; color:#c8cad0; border:1px solid #3a3f4a;
  border-radius:6px; padding:.25rem .7rem; font-size:.75rem; cursor:pointer;
}
.copy-btn:hover { border-color:#8ab4f8; color:#8ab4f8; }
"""


def build_report(page_purpose: str, plan_summary: dict, results: list[dict]) -> str:
    """
    Construit le rapport HTML et l'écrit dans REPORTS_DIR.
    Retourne le chemin du fichier généré.
    """
    total = len(results)
    passed = sum(1 for r in results if r["execution_report"]["success"])
    failed = total - passed
    coverage = (passed / total * 100) if total else 0.0
    total_time = sum(
        (r["execution_report"].get("execution_time") or 0) for r in results
    )

    filename = f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"

    scenarios_html = "".join(
        _scenario_block(i, r) for i, r in enumerate(results)
    )

    by_type_html = "".join(
        f"<li>{_escape(t)} : {n}</li>" for t, n in (plan_summary or {}).get("by_type", {}).items()
    )

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ascii_summary = _overall_summary_block(results, filename, total_time)

    doc = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<title>Rapport de tests — Test_Auto</title>
<style>{_CSS}</style>
</head>
<body>
  <h1>Rapport de tests automatisés</h1>
  <p class="subtitle">{_escape(page_purpose)} — généré le {timestamp}</p>

  <div class="summary">
    <div class="card total"><div class="num">{total}</div><div class="label">Scénarios exécutés</div></div>
    <div class="card passed"><div class="num">{passed}</div><div class="label">Réussis</div></div>
    <div class="card failed"><div class="num">{failed}</div><div class="label">Échoués</div></div>
    <div class="card coverage"><div class="num">{coverage:.0f}%</div><div class="label">Couverture</div></div>
    <div class="card time"><div class="num">{total_time:.1f}s</div><div class="label">Temps total</div></div>
  </div>

  <pre class="ascii-summary">{ascii_summary}</pre>

  <details style="margin-bottom:2rem;">
    <summary>Plan de test (types de cas générés)</summary>
    <ul>{by_type_html}</ul>
  </details>

  <h2>Détail par scénario</h2>
  {scenarios_html}
</body>
</html>"""

    os.makedirs(REPORTS_DIR, exist_ok=True)
    path = os.path.join(REPORTS_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    return path