import { useState, useRef, useCallback } from "react";

const API_BASE = "http://localhost:8000";

const STAGES = [
  { id: "parse",    label: "Analyse HTML",      icon: "🔍", desc: "Extraction des éléments interactifs" },
  { id: "scenario", label: "Scénarios de test", icon: "📋", desc: "Génération de tous les scénarios QA"  },
  { id: "script",   label: "Scripts Selenium",  icon: "⚙️", desc: "Génération du code de test"           },
  { id: "exec",     label: "Exécution",         icon: "▶️", desc: "Exécution des scripts générés"       },
];

const ELEMENT_COLORS = {
  button:         { bg: "#fff0f6", border: "#ffadd2", icon: "🔘" },
  input_text:     { bg: "#f0f5ff", border: "#adc6ff", icon: "✏️" },
  input_email:    { bg: "#f0f5ff", border: "#adc6ff", icon: "📧" },
  input_password: { bg: "#f9f0ff", border: "#d3adf7", icon: "🔒" },
  input_checkbox: { bg: "#f6ffed", border: "#b7eb8f", icon: "☑️" },
  input_radio:    { bg: "#f6ffed", border: "#b7eb8f", icon: "⭕" },
  link:           { bg: "#e6f7ff", border: "#91d5ff", icon: "🔗" },
  select:         { bg: "#fffbe6", border: "#ffe58f", icon: "▾"  },
  textarea:       { bg: "#f0f5ff", border: "#adc6ff", icon: "📝" },
};

function elementStyle(type) {
  return ELEMENT_COLORS[type] || { bg: "#f5f5f5", border: "#d9d9d9", icon: "◻️" };
}

// ── Composant principal ───────────────────────────────────────────────────────
export default function App() {
  const [code, setCode]           = useState("");
  const [running, setRunning]     = useState(false);
  const [stages, setStages]       = useState({});
  const [analysis, setAnalysis]   = useState(null);
  const [scenarios, setScenarios] = useState([]);   // tous les scénarios
  const [scripts, setScripts]     = useState({});   // {idx: code}
  const [reports, setReports]     = useState({});   // {idx: report}
  const [activeScenario, setActiveScenario] = useState(0);
  const [activeTab, setActiveTab] = useState("elements");
  const [error, setError]         = useState(null);
  const [summary, setSummary]     = useState(null);
  const abortRef = useRef(null);

  const reset = useCallback(() => {
    abortRef.current?.abort();
    setRunning(false);
    setStages({});
    setAnalysis(null);
    setScenarios([]);
    setScripts({});
    setReports({});
    setActiveScenario(0);
    setActiveTab("elements");
    setError(null);
    setSummary(null);
  }, []);

  // ── Pipeline SSE ─────────────────────────────────────────────────────────
  const runPipeline = async () => {
    if (!code.trim()) return;
    reset();
    setRunning(true);

    const form = new FormData();
    form.append("html_code", code);

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      const resp = await fetch(`${API_BASE}/generate-stream`, {
        method: "POST",
        body: form,
        signal: controller.signal,
      });
      if (!resp.ok) throw new Error(`Erreur HTTP ${resp.status}`);

      const reader  = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer    = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const frames = buffer.split("\n\n");
        buffer = frames.pop();

        for (const frame of frames) {
          if (!frame.trim()) continue;
          const lines = frame.split("\n");
          let eventName = "message", dataLine = "";
          for (const line of lines) {
            if (line.startsWith("event: ")) eventName = line.slice(7).trim();
            if (line.startsWith("data: "))  dataLine  = line.slice(6).trim();
          }
          if (!dataLine) continue;
          let payload;
          try { payload = JSON.parse(dataLine); } catch { continue; }
          handleEvent(eventName, payload);
        }
      }
    } catch (err) {
      if (err.name !== "AbortError") setError(err.message || "Erreur réseau");
    } finally {
      setRunning(false);
    }
  };

 
  // Remplace uniquement la fonction handleEvent dans ton App.jsx (lignes 109-147)
// Ajoute le case "server_ready" pour afficher l'URL du serveur temporaire dans la console

const handleEvent = (event, payload) => {
    switch (event) {
      case "stage":
        setStages(p => ({ ...p, [payload.stage]: payload.status }));
        break;
      case "parse_done":
        setStages(p => ({ ...p, parse: "done" }));
        setAnalysis(payload.analysis);
        setActiveTab("elements");
        break;
      case "scenarios_done":
        setStages(p => ({ ...p, scenario: "done" }));
        setScenarios(payload.scenarios);
        setActiveTab("scenarios");
        break;
      case "server_ready":
        // Le mini-serveur HTML est prêt — URL visible dans la console du navigateur
        console.info("[TestAuto] Serveur HTML temporaire :", payload.url);
        break;
      case "script_start":
        setStages(p => ({ ...p, script: "active" }));
        break;
      case "script_done":
        setStages(p => ({ ...p, script: "active" }));
        setScripts(p => ({ ...p, [payload.scenario_index]: payload.script_code }));
        break;
      case "exec_start":
        setStages(p => ({ ...p, exec: "active" }));
        break;
      case "exec_done":
        setStages(p => ({ ...p, exec: "active" }));
        setReports(p => ({ ...p, [payload.scenario_index]: payload.execution_report }));
        break;
      case "complete":
        setStages(p => ({ ...p, script: "done", exec: "done" }));
        setSummary(payload);
        setActiveTab("results");
        break;
      case "error":
        setError(payload.message || (payload.traceback ? payload.traceback.split("\n").filter(Boolean).pop() : null) || "Timeout Ollama — modele trop lent ou non disponible");
        break;
    }
  };

  const totalPassed = Object.values(reports).filter(r => r?.success).length;
  const totalDone   = Object.keys(reports).length;

  return (
    <div className="app">
      {/* ── Header ── */}
      <header className="app-header">
        <div className="logo">
          <span className="logo-icon">🤖</span>
          <span className="logo-text">Test<span className="logo-accent">Auto</span></span>
        </div>
        <p className="header-sub">
          HTML / JSX / Vue → Détection UI → Scénarios QA → Scripts Selenium
        </p>
      </header>

      <main className="app-main">
        {/* ── Colonne gauche : input + pipeline ── */}
        <section className="col-left">
          <div className="card">
            <div className="card-header">
              <span>📄 Code de l'interface</span>
              <span className="hint">HTML · JSX · Vue · Angular</span>
            </div>
            <textarea
              className="code-input"
              placeholder={`Collez votre code ici…\n\nExemple :\n<form>\n  <input type="email" placeholder="Email" />\n  <input type="password" placeholder="Mot de passe" />\n  <button type="submit">Se connecter</button>\n  <a href="/register">Créer un compte</a>\n  <a href="/forgot">Mot de passe oublié ?</a>\n</form>`}
              value={code}
              onChange={e => setCode(e.target.value)}
              disabled={running}
              spellCheck={false}
            />
            <div className="card-footer">
              {running ? (
                <button className="btn btn-danger" onClick={() => { abortRef.current?.abort(); setRunning(false); }}>
                  ⏹ Arrêter
                </button>
              ) : (
                <button className="btn btn-primary" onClick={runPipeline} disabled={!code.trim()}>
                  ▶ Analyser &amp; Générer
                </button>
              )}
              {(analysis || error) && (
                <button className="btn btn-ghost" onClick={reset}>↺ Réinitialiser</button>
              )}
              <span className="char-count">{code.length} caractères</span>
            </div>
          </div>

          {/* Pipeline steps */}
          <div className="pipeline-card">
            {STAGES.map((s, i) => {
              const status = stages[s.id] || "idle";
              return (
                <div key={s.id} className={`p-step p-step-${status}`}>
                  {i > 0 && <div className="p-connector" />}
                  <div className="p-dot">
                    {status === "active" && <span className="spinner" />}
                    {status === "done"   && "✓"}
                    {status === "error"  && "✗"}
                    {status === "idle"   && (i + 1)}
                  </div>
                  <div className="p-info">
                    <div className="p-label">{s.icon} {s.label}</div>
                    <div className="p-desc">
                      {status === "active" && `${s.desc}…`}
                      {status === "done" && s.id === "parse"    && analysis && `${analysis.elements?.length} éléments`}
                      {status === "done" && s.id === "scenario" && `${scenarios.length} scénarios`}
                      {status === "done" && s.id === "script"   && `${Object.keys(scripts).length} scripts`}
                      {status === "done" && s.id === "exec"     && `${totalPassed}/${totalDone} réussis`}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>

          {/* Résumé final */}
          {summary && (
            <div className={`summary-card ${summary.failed === 0 ? "summary-pass" : "summary-mixed"}`}>
              <div className="summary-row">
                <span className="summary-stat">
                  <strong>{summary.total}</strong> scénarios
                </span>
                <span className="summary-stat text-success">
                  <strong>{summary.passed}</strong> ✓ passés
                </span>
                {summary.failed > 0 && (
                  <span className="summary-stat text-danger">
                    <strong>{summary.failed}</strong> ✗ échoués
                  </span>
                )}
              </div>
            </div>
          )}
        </section>

        {/* ── Colonne droite : résultats ── */}
        <section className="col-right">
          {error && (
            <div className="alert alert-error">
              <strong>Erreur</strong>{error}
            </div>
          )}

          {!analysis && !error && (
            <div className="empty-state">
              <span className="empty-icon">⌨️</span>
              <p>Collez votre code HTML / JSX et lancez l'analyse</p>
              <p className="empty-sub">
                TestAuto extrait automatiquement tous les éléments interactifs,
                génère chaque scénario possible et produit les scripts Selenium associés.
              </p>
            </div>
          )}

          {analysis && (
            <>
              {/* Onglets */}
              <div className="tabs">
                <button className={`tab ${activeTab==="elements"  ? "active":""}`} onClick={()=>setActiveTab("elements")}>
                  🔍 Éléments <span className="tab-badge">{analysis.elements?.length}</span>
                </button>
                <button className={`tab ${activeTab==="scenarios" ? "active":""}`} onClick={()=>setActiveTab("scenarios")}>
                  📋 Scénarios <span className="tab-badge">{scenarios.length}</span>
                </button>
                {Object.keys(scripts).length > 0 && (
                  <button className={`tab ${activeTab==="results" ? "active":""}`} onClick={()=>setActiveTab("results")}>
                    ⚙️ Scripts &amp; Résultats <span className="tab-badge">{Object.keys(scripts).length}</span>
                  </button>
                )}
              </div>

              {/* ── Tab : Éléments ── */}
              {activeTab === "elements" && (
                <div className="panel">
                  <div className="panel-header">
                    <h2>Éléments détectés</h2>
                    <span className="badge">{analysis.page_type}</span>
                  </div>
                  <div className="elements-grid">
                    {analysis.elements?.map((el, i) => {
                      const style = elementStyle(el.type);
                      return (
                        <div key={i} className="el-card" style={{ background: style.bg, borderColor: style.border }}>
                          <div className="el-type-row">
                            <span className="el-icon">{style.icon}</span>
                            <span className="el-type-name">{el.type}</span>
                            {el.is_link && <span className="el-link-badge">link</span>}
                          </div>
                          <div className="el-label">{el.label || "—"}</div>
                          {el.selector_hint && (
                            <code className="el-selector">{el.selector_hint}</code>
                          )}
                          {el.possible_destination && (
                            <div className="el-dest">→ {el.possible_destination}</div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}

              {/* ── Tab : Scénarios ── */}
              {activeTab === "scenarios" && scenarios.length > 0 && (
                <div className="panel">
                  <div className="panel-header">
                    <h2>Tous les scénarios</h2>
                    <span className="badge">{scenarios.length} générés</span>
                  </div>
                  <div className="scenario-list">
                    {scenarios.map((sc, idx) => (
                      <div
                        key={idx}
                        className={`scenario-item ${activeScenario === idx ? "active" : ""}`}
                        onClick={() => setActiveScenario(idx)}
                      >
                        <div className="sc-header">
                          <span className="sc-num">{idx + 1}</span>
                          <span className="sc-title">{sc.title}</span>
                          {reports[idx] && (
                            <span className={`sc-badge ${reports[idx].success ? "badge-success" : "badge-danger"}`}>
                              {reports[idx].success ? "✓" : "✗"}
                            </span>
                          )}
                        </div>
                        {activeScenario === idx && (
                          <div className="sc-body">
                            <ol className="sc-steps">
                              {sc.steps.map((step, si) => (
                                <li key={si}>{step}</li>
                              ))}
                            </ol>
                            <div className="sc-expected">
                              <strong>Résultat attendu :</strong> {sc.expected_result}
                            </div>
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* ── Tab : Scripts & résultats ── */}
              {activeTab === "results" && (
                <div className="panel">
                  <div className="panel-header">
                    <h2>Scripts Selenium &amp; Résultats</h2>
                    <span className="badge">{Object.keys(scripts).length} scripts</span>
                  </div>

                  {/* Sélecteur de scénario */}
                  <div className="scenario-tabs">
                    {scenarios.map((sc, idx) => (
                      scripts[idx] !== undefined && (
                        <button
                          key={idx}
                          className={`sc-tab ${activeScenario === idx ? "sc-tab-active" : ""} ${reports[idx]?.success ? "sc-tab-pass" : reports[idx] ? "sc-tab-fail" : ""}`}
                          onClick={() => setActiveScenario(idx)}
                        >
                          #{idx + 1}
                        </button>
                      )
                    ))}
                  </div>

                  {scripts[activeScenario] !== undefined && (
                    <>
                      <div className="script-title">
                        <strong>{scenarios[activeScenario]?.title}</strong>
                        <button
                          className="btn-sm"
                          onClick={() => navigator.clipboard.writeText(scripts[activeScenario])}
                        >
                          📋 Copier
                        </button>
                      </div>
                      <pre className="code-block">{scripts[activeScenario]}</pre>

                      {reports[activeScenario] && (
                        <div className="exec-report">
                          <div className={`exec-status ${reports[activeScenario].success ? "exec-pass" : "exec-fail"}`}>
                            {reports[activeScenario].success ? "✓ Exécution réussie" : "✗ Exécution échouée"}
                          </div>
                          {reports[activeScenario].logs?.length > 0 && (
                            <div className="logs-block">
                              {reports[activeScenario].logs.map((l, i) => (
                                <div key={i} className="log-line">{l}</div>
                              ))}
                            </div>
                          )}
                          {reports[activeScenario].error && (
                            <div className="exec-error">{reports[activeScenario].error}</div>
                          )}
                        </div>
                      )}
                    </>
                  )}
                </div>
              )}
            </>
          )}
        </section>
      </main>

      <style>{`
        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: 'Inter', system-ui, sans-serif; background: #f0f2f7; color: #1a1d2e; }

        /* ── Layout ── */
        .app { min-height: 100vh; display: flex; flex-direction: column; }

        .app-header {
          background: linear-gradient(135deg, #1a1d2e 0%, #2d3360 100%);
          padding: 1.25rem 2rem;
          display: flex; align-items: center; justify-content: space-between;
          flex-wrap: wrap; gap: 0.5rem;
        }
        .logo { display: flex; align-items: center; gap: 10px; }
        .logo-icon { font-size: 1.6rem; }
        .logo-text { font-size: 1.4rem; font-weight: 800; color: #fff; letter-spacing: -0.5px; }
        .logo-accent { color: #e84393; }
        .header-sub { font-size: 0.8rem; color: #a8b0cc; max-width: 420px; line-height: 1.4; }

        .app-main {
          flex: 1;
          display: grid;
          grid-template-columns: 380px 1fr;
          gap: 1.5rem;
          padding: 1.5rem 2rem;
          max-width: 1400px;
          width: 100%;
          margin: 0 auto;
        }

        /* ── Left column ── */
        .col-left { display: flex; flex-direction: column; gap: 1rem; }

        .card {
          background: #fff;
          border-radius: 12px;
          border: 1px solid #e2e6f0;
          overflow: hidden;
          display: flex; flex-direction: column;
        }
        .card-header {
          padding: 0.75rem 1rem;
          background: #fafbfd;
          border-bottom: 1px solid #e8ebf3;
          display: flex; align-items: center; justify-content: space-between;
          font-size: 0.875rem; font-weight: 600; color: #2d3142;
        }
        .hint { font-size: 0.72rem; color: #a0a8bb; font-weight: 400; }

        .code-input {
          flex: 1;
          min-height: 280px;
          resize: vertical;
          border: none;
          padding: 1rem;
          font-family: 'Fira Code', 'Cascadia Code', monospace;
          font-size: 0.78rem;
          line-height: 1.6;
          color: #1e1e2e;
          background: #fafbff;
          outline: none;
          tab-size: 2;
        }
        .code-input:disabled { opacity: 0.6; cursor: not-allowed; }

        .card-footer {
          padding: 0.75rem 1rem;
          border-top: 1px solid #e8ebf3;
          display: flex; align-items: center; gap: 8px;
          flex-wrap: wrap;
        }
        .char-count { margin-left: auto; font-size: 0.72rem; color: #a0a8bb; }

        /* ── Buttons ── */
        .btn {
          padding: 8px 18px; border-radius: 8px; border: none;
          font-size: 0.875rem; font-weight: 600; cursor: pointer;
          transition: all 0.15s; white-space: nowrap;
        }
        .btn:disabled { opacity: 0.45; cursor: not-allowed; }
        .btn-primary { background: #e84393; color: #fff; }
        .btn-primary:hover:not(:disabled) { background: #d1357f; }
        .btn-danger  { background: #ff4d4f; color: #fff; }
        .btn-danger:hover { background: #d9363e; }
        .btn-ghost   { background: transparent; color: #5a6072; border: 1.5px solid #dde1eb; }
        .btn-ghost:hover { background: #f0f2f7; }
        .btn-sm {
          padding: 4px 10px; border-radius: 6px; border: 1.5px solid #dde1eb;
          background: #fff; font-size: 0.75rem; font-weight: 500; cursor: pointer; color: #5a6072;
        }
        .btn-sm:hover { background: #f0f2f7; }

        /* ── Pipeline ── */
        .pipeline-card {
          background: #fff;
          border-radius: 12px;
          border: 1px solid #e2e6f0;
          padding: 1rem;
          display: flex; flex-direction: column; gap: 4px;
        }
        .p-step { display: flex; align-items: flex-start; gap: 10px; position: relative; }
        .p-connector {
          position: absolute; left: 13px; top: -12px;
          width: 2px; height: 12px; background: #e0e4ee;
        }
        .p-dot {
          width: 26px; height: 26px; border-radius: 50%; flex-shrink: 0;
          display: flex; align-items: center; justify-content: center;
          font-size: 0.78rem; font-weight: 700; border: 2px solid #dde1eb;
          background: #edf0f7; color: #8991a4; transition: all 0.3s;
        }
        .p-step-active .p-dot { background: #fff3fd; border-color: #e84393; color: #e84393; }
        .p-step-done   .p-dot { background: #e6f9f0; border-color: #36b37e; color: #36b37e; }
        .p-step-error  .p-dot { background: #fff1f0; border-color: #ff4d4f; color: #ff4d4f; }
        .p-info { padding: 3px 0 10px; }
        .p-label { font-size: 0.83rem; font-weight: 600; color: #2d3142; }
        .p-desc  { font-size: 0.72rem; color: #8991a4; margin-top: 1px; }

        /* ── Summary ── */
        .summary-card {
          border-radius: 10px; padding: 0.75rem 1rem;
          border: 1.5px solid;
        }
        .summary-pass  { background: #f0fff4; border-color: #36b37e; }
        .summary-mixed { background: #fffbe6; border-color: #faad14; }
        .summary-row { display: flex; gap: 1rem; flex-wrap: wrap; }
        .summary-stat { font-size: 0.875rem; color: #3d4152; }
        .text-success { color: #1e7e50 !important; }
        .text-danger  { color: #c0392b !important; }

        /* ── Spinner ── */
        .spinner {
          display: inline-block; width: 12px; height: 12px;
          border: 2px solid #e84393; border-top-color: transparent;
          border-radius: 50%; animation: spin 0.6s linear infinite;
        }
        @keyframes spin { to { transform: rotate(360deg); } }

        /* ── Right column ── */
        .col-right { min-width: 0; display: flex; flex-direction: column; gap: 1rem; }

        .empty-state {
          display: flex; flex-direction: column; align-items: center; justify-content: center;
          gap: 0.5rem; padding: 5rem 2rem; text-align: center; color: #8991a4;
        }
        .empty-icon { font-size: 3rem; margin-bottom: 0.5rem; }
        .empty-state p { font-size: 1rem; font-weight: 500; color: #5a6072; }
        .empty-sub { font-size: 0.83rem; max-width: 360px; line-height: 1.6; color: #a0a8bb !important; font-weight: 400 !important; }

        /* ── Tabs ── */
        .tabs {
          display: flex; gap: 4px; border-bottom: 2px solid #e8ebf3;
          margin-bottom: 1rem; flex-wrap: wrap;
        }
        .tab {
          padding: 8px 14px; border: none; background: transparent;
          font-size: 0.85rem; font-weight: 500; color: #8991a4; cursor: pointer;
          border-bottom: 2px solid transparent; margin-bottom: -2px;
          transition: all 0.15s; border-radius: 6px 6px 0 0;
          display: flex; align-items: center; gap: 6px;
        }
        .tab:hover { color: #e84393; background: #fdf2f8; }
        .tab.active { color: #e84393; border-bottom-color: #e84393; }
        .tab-badge {
          font-size: 0.65rem; font-weight: 700; padding: 1px 6px;
          border-radius: 10px; background: #f0f2f7; color: #5a6072;
        }
        .tab.active .tab-badge { background: #fff0f6; color: #e84393; }

        /* ── Panel ── */
        .panel { background: #fff; border-radius: 12px; border: 1px solid #e8ebf3; overflow: hidden; }
        .panel-header {
          display: flex; align-items: center; justify-content: space-between;
          padding: 1rem 1.25rem; border-bottom: 1px solid #f0f2f7;
          background: #fafbfd;
        }
        .panel-header h2 { font-size: 1rem; font-weight: 600; color: #2d3142; }
        .badge {
          font-size: 0.72rem; font-weight: 600; padding: 3px 10px;
          border-radius: 20px; background: #e8f4fd; color: #1976d2;
        }
        .badge-success { background: #e6f9f0; color: #1e7e50; }
        .badge-danger  { background: #fff1f0; color: #c0392b; }

        /* ── Elements grid ── */
        .elements-grid {
          display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
          gap: 10px; padding: 1rem 1.25rem;
        }
        .el-card {
          padding: 10px 12px; border-radius: 8px; border: 1px solid;
        }
        .el-type-row { display: flex; align-items: center; gap: 6px; margin-bottom: 4px; }
        .el-icon { font-size: 0.9rem; }
        .el-type-name {
          font-size: 0.68rem; font-weight: 700; text-transform: uppercase;
          letter-spacing: 0.5px; color: #8991a4;
        }
        .el-link-badge {
          font-size: 0.65rem; padding: 1px 5px; border-radius: 4px;
          background: #e6f7ff; color: #1890ff; font-weight: 600;
        }
        .el-label   { font-size: 0.875rem; font-weight: 600; color: #2d3142; margin-bottom: 4px; }
        .el-selector {
          display: block; font-size: 0.68rem; color: #5a6072;
          background: rgba(0,0,0,0.04); padding: 2px 6px;
          border-radius: 4px; word-break: break-all; font-family: monospace;
        }
        .el-dest { font-size: 0.72rem; color: #1890ff; margin-top: 4px; }

        /* ── Scenario list ── */
        .scenario-list { display: flex; flex-direction: column; }
        .scenario-item {
          border-bottom: 1px solid #f0f2f7; cursor: pointer;
          transition: background 0.15s;
        }
        .scenario-item:hover { background: #fafbff; }
        .scenario-item.active { background: #fdf2f8; }
        .sc-header {
          display: flex; align-items: center; gap: 10px;
          padding: 0.75rem 1.25rem;
        }
        .sc-num {
          width: 24px; height: 24px; border-radius: 50%; background: #e84393; color: #fff;
          font-size: 0.72rem; font-weight: 700;
          display: flex; align-items: center; justify-content: center; flex-shrink: 0;
        }
        .sc-title { font-size: 0.875rem; font-weight: 500; color: #2d3142; flex: 1; }
        .sc-badge {
          font-size: 0.72rem; font-weight: 700; padding: 2px 8px;
          border-radius: 10px;
        }
        .sc-body { padding: 0 1.25rem 1rem 3.5rem; }
        .sc-steps { padding-left: 1.25rem; display: flex; flex-direction: column; gap: 4px; }
        .sc-steps li { font-size: 0.83rem; color: #3d4152; line-height: 1.5; }
        .sc-expected {
          margin-top: 0.75rem; padding: 10px 12px;
          background: #eafaf1; border-radius: 8px; border-left: 3px solid #36b37e;
          font-size: 0.83rem; color: #2d5a3d; line-height: 1.5;
        }

        /* ── Script results ── */
        .scenario-tabs {
          display: flex; gap: 6px; padding: 0.75rem 1.25rem; flex-wrap: wrap;
          border-bottom: 1px solid #f0f2f7;
        }
        .sc-tab {
          padding: 4px 12px; border-radius: 20px; border: 1.5px solid #dde1eb;
          font-size: 0.78rem; font-weight: 600; cursor: pointer; background: #fff;
          color: #8991a4; transition: all 0.15s;
        }
        .sc-tab:hover { border-color: #e84393; color: #e84393; }
        .sc-tab-active { border-color: #e84393; background: #fff0f6; color: #e84393; }
        .sc-tab-pass   { border-color: #36b37e; color: #1e7e50; }
        .sc-tab-fail   { border-color: #ff4d4f; color: #c0392b; }

        .script-title {
          display: flex; align-items: center; justify-content: space-between;
          padding: 0.75rem 1.25rem; font-size: 0.875rem; color: #2d3142;
        }
        .code-block {
          margin: 0; padding: 1rem 1.25rem;
          background: #1e1e2e; color: #cdd6f4;
          font-family: 'Fira Code', monospace; font-size: 0.76rem; line-height: 1.6;
          max-height: 400px; overflow: auto;
          white-space: pre;
        }
        .exec-report { padding: 0 1.25rem 1rem; }
        .exec-status {
          padding: 8px 12px; border-radius: 8px; font-size: 0.875rem;
          font-weight: 600; margin: 0.75rem 0 0.5rem;
        }
        .exec-pass { background: #e6f9f0; color: #1e7e50; }
        .exec-fail { background: #fff1f0; color: #c0392b; }
        .logs-block {
          background: #1e1e2e; border-radius: 8px; padding: 10px 12px;
          max-height: 200px; overflow-y: auto;
        }
        .log-line { font-family: monospace; font-size: 0.75rem; color: #cdd6f4; line-height: 1.6; }
        .exec-error {
          margin-top: 0.5rem; padding: 10px 12px; background: #fff1f0;
          border-radius: 8px; font-size: 0.78rem; color: #c0392b;
          font-family: monospace; white-space: pre-wrap; word-break: break-all;
        }

        /* ── Alert ── */
        .alert { border-radius: 8px; padding: 12px 16px; font-size: 0.875rem; line-height: 1.5; }
        .alert-error { background: #fff1f0; border: 1px solid #ffa39e; color: #c0392b; }
        .alert-error strong { display: block; margin-bottom: 4px; }

        /* ── Responsive ── */
        @media (max-width: 900px) {
          .app-main { grid-template-columns: 1fr; padding: 1rem; }
          .col-left  { order: 1; }
          .col-right { order: 2; }
        }
      `}</style>
    </div>
  );
}
