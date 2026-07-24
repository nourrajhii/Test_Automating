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
  const [inputMode, setInputMode] = useState("code"); // "code" | "screenshot"
  const [code, setCode]           = useState("");
  const [imageFile, setImageFile] = useState(null);
  const [imagePreview, setImagePreview] = useState(null);
  const [targetUrl, setTargetUrl] = useState("");
  const [running, setRunning]     = useState(false);
  const [stages, setStages]       = useState({});
  const [analysis, setAnalysis]   = useState(null);
  const [scenarios, setScenarios] = useState([]);   // tous les scénarios
  const [scripts, setScripts]     = useState({});   // {idx: code}
  const [reports, setReports]     = useState({});   // {idx: report}
  const [diagnoses, setDiagnoses] = useState({});   // {idx: diagnosis IA (cause + patch)}
  const [agentReports, setAgentReports] = useState({}); // {idx: bloc texte format "Agent IA"}
  const [activeScenario, setActiveScenario] = useState(0);
  const [activeTab, setActiveTab] = useState("scenarios");
  const [error, setError]         = useState(null);
  const [warning, setWarning]     = useState(null);
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
    setDiagnoses({});
    setAgentReports({});
    setActiveScenario(0);
    setActiveTab("scenarios");
    setError(null);
    setWarning(null);
    setSummary(null);
  }, []);

  const handleImageChange = (file) => {
    setImageFile(file);
    if (imagePreview) URL.revokeObjectURL(imagePreview);
    setImagePreview(file ? URL.createObjectURL(file) : null);
  };

  const canRun =
    inputMode === "code"
      ? code.trim().length > 0
      : Boolean(imageFile) && targetUrl.trim().length > 0;

  // ── Pipeline SSE ─────────────────────────────────────────────────────────
  const runPipeline = async () => {
    if (!canRun) return;
    reset();
    setRunning(true);

    const form = new FormData();
    let endpoint;
    if (inputMode === "code") {
      form.append("html_code", code);
      endpoint = "/generate-stream";
    } else {
      form.append("image", imageFile);
      if (targetUrl.trim()) form.append("target_url", targetUrl.trim());
      endpoint = "/generate-stream-vision";
    }

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      const resp = await fetch(`${API_BASE}${endpoint}`, {
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
        // Le rapport commence directement par les scénarios — on ne
        // bascule plus automatiquement sur l'onglet technique "Éléments".
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
        if (payload.diagnosis) {
          setDiagnoses(p => ({ ...p, [payload.scenario_index]: payload.diagnosis }));
        }
        if (payload.agent_report) {
          setAgentReports(p => ({ ...p, [payload.scenario_index]: payload.agent_report }));
        }
        break;
      case "complete":
        setStages(p => ({ ...p, script: "done", exec: "done" }));
        setSummary(payload);
        setActiveTab("results");
        break;
      case "warning":
        setWarning(payload.message || "Analyse en mode dégradé.");
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
              <span>{inputMode === "code" ? "📄 Code de l'interface" : "🖼️ Capture d'écran"}</span>
              <span className="hint">HTML · JSX · Vue · Angular · Image</span>
            </div>

            {/* ── Sélecteur de mode d'entrée ── */}
            <div className="mode-toggle">
              <button
                className={`mode-btn ${inputMode === "code" ? "mode-btn-active" : ""}`}
                onClick={() => !running && setInputMode("code")}
                disabled={running}
              >
                💻 Code source
              </button>
              <button
                className={`mode-btn ${inputMode === "screenshot" ? "mode-btn-active" : ""}`}
                onClick={() => !running && setInputMode("screenshot")}
                disabled={running}
              >
                🖼️ Capture d'écran
              </button>
            </div>

            {inputMode === "code" ? (
              <textarea
                className="code-input"
                placeholder={`Collez votre code ici…\n\nExemple :\n<form>\n  <input type="email" placeholder="Email" />\n  <input type="password" placeholder="Mot de passe" />\n  <button type="submit">Se connecter</button>\n  <a href="/register">Créer un compte</a>\n  <a href="/forgot">Mot de passe oublié ?</a>\n</form>`}
                value={code}
                onChange={e => setCode(e.target.value)}
                disabled={running}
                spellCheck={false}
              />
            ) : (
              <div className="vision-input">
                <label className="vision-dropzone">
                  {imagePreview ? (
                    <img src={imagePreview} alt="preview" className="vision-preview" />
                  ) : (
                    <div className="vision-placeholder">
                      <span className="empty-icon">🖼️</span>
                      <p>Cliquez pour choisir une capture d'écran</p>
                      <span className="p-desc">PNG, JPG… 8 Mo max</span>
                    </div>
                  )}
                  <input
                    type="file"
                    accept="image/*"
                    hidden
                    disabled={running}
                    onChange={e => handleImageChange(e.target.files?.[0] || null)}
                  />
                </label>

                <div className="vision-url-field">
                  <label className="vision-url-label">URL réelle de la page (obligatoire — nécessaire pour générer et exécuter les scripts Selenium)</label>
                  <input
                    type="text"
                    className="vision-url-input"
                    placeholder="http://localhost:5173"
                    value={targetUrl}
                    onChange={e => setTargetUrl(e.target.value)}
                    disabled={running}
                    required
                  />
                  <span className="p-desc">
                    Selenium a besoin d'une vraie page à ouvrir dans le navigateur pour
                    pouvoir cliquer/saisir sur les éléments détectés dans la capture.
                    Sans URL, seuls les scénarios de test (texte) peuvent être générés —
                    aucun script Selenium ne sera produit.
                  </span>
                </div>
              </div>
            )}

            <div className="card-footer">
              {running ? (
                <button className="btn btn-danger" onClick={() => { abortRef.current?.abort(); setRunning(false); }}>
                  ⏹ Arrêter
                </button>
              ) : (
                <button className="btn btn-primary" onClick={runPipeline} disabled={!canRun}>
                  ▶ Analyser &amp; Générer
                </button>
              )}
              {(analysis || error) && (
                <button className="btn btn-ghost" onClick={reset}>↺ Réinitialiser</button>
              )}
              {inputMode === "code" && <span className="char-count">{code.length} caractères</span>}
              {inputMode === "screenshot" && Boolean(imageFile) && !targetUrl.trim() && (
                <span className="char-count">⚠️ Renseigne l'URL de la page pour générer les scripts Selenium</span>
              )}
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
              <div className="summary-title">📊 Résumé d'exécution</div>
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
                {summary.coverage != null && (
                  <span className="summary-stat">
                    <strong>{summary.coverage}%</strong> couverture
                  </span>
                )}
                {summary.total_time != null && (
                  <span className="summary-stat">
                    ⏱ <strong>{summary.total_time}s</strong> au total
                  </span>
                )}
                {summary.screenshots != null && (
                  <span className="summary-stat">
                    📸 <strong>{summary.screenshots}</strong> captures
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

          {warning && !error && (
            <div className="alert alert-warning">
              <strong>Mode dégradé</strong>{warning}
            </div>
          )}

          {!analysis && !error && (
            <div className="empty-state">
              <span className="empty-icon">{inputMode === "code" ? "⌨️" : "🖼️"}</span>
              <p>
                {inputMode === "code"
                  ? "Collez votre code HTML / JSX et lancez l'analyse"
                  : "Ajoutez une capture d'écran et l'URL de la page, puis lancez l'analyse"}
              </p>
              <p className="empty-sub">
                TestAuto extrait automatiquement tous les éléments interactifs
                (par lecture du code ou par vision IA), génère chaque scénario
                possible et produit les scripts Selenium associés.
              </p>
            </div>
          )}

          {analysis && (
            <>
              {/* Le rapport démarre directement sur les scénarios : plus de
                  bandeau technique (page_type / raw_description brut,
                  noms de zones type "navbar"/"block-...") avant celui-ci. */}

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
                            {reports[activeScenario].execution_time != null && (
                              <span className="exec-time">⏱ {reports[activeScenario].execution_time.toFixed(2)} s</span>
                            )}
                          </div>

                          {/* ── Métriques (steps / assertions) ── */}
                          <div className="metrics-row">
                            <div className="metric-chip">
                              📋 Étapes : <strong>{reports[activeScenario].steps_passed ?? 0}/{reports[activeScenario].steps_total ?? 0}</strong>
                            </div>
                            <div className="metric-chip">
                              ✔ Assertions : <strong>{reports[activeScenario].assertions_passed ?? 0}/{reports[activeScenario].assertions_total ?? 0}</strong>
                            </div>
                          </div>

                          {(reports[activeScenario].final_url || reports[activeScenario].page_title) && (
                            <div className="url-title-block">
                              {reports[activeScenario].final_url && (
                                <div><strong>URL finale :</strong> {reports[activeScenario].final_url}</div>
                              )}
                              {reports[activeScenario].page_title && (
                                <div><strong>Titre :</strong> {reports[activeScenario].page_title}</div>
                              )}
                            </div>
                          )}

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

                          {/* ── Diagnostic IA : cause probable + patch de code suggéré ── */}
                          {diagnoses[activeScenario]?.has_failures && (
                            <div className="ai-diagnosis">
                              <div className="ai-diag-title">🧠 Analyse intelligente de l'échec</div>
                              <p className="ai-diag-summary">{diagnoses[activeScenario].summary}</p>

                              {diagnoses[activeScenario].steps?.map((step, si) => (
                                <div key={si} className="ai-step">
                                  <div className="ai-step-head">
                                    Étape {step.step_number} — <code>{step.exception_type}</code> sur {step.selector_human}
                                  </div>

                                  <div className="ai-step-cause"><strong>Cause probable :</strong> {step.cause}</div>

                                  {step.hypotheses?.length > 0 && (
                                    <ul className="ai-hyps">
                                      {step.hypotheses.map((h, hi) => <li key={hi}>{h}</li>)}
                                    </ul>
                                  )}

                                  <div className="ai-step-suggestion"><strong>Suggestion :</strong> {step.suggestion}</div>

                                  {step.patch && (
                                    <div className="ai-patch">
                                      <div className="ai-patch-label">🛠 Correctif de code suggéré</div>
                                      <p className="ai-patch-explain">{step.patch.explanation}</p>

                                      <div className="ai-patch-sublabel">Avant :</div>
                                      <pre className="ai-code ai-code-before">{step.patch.code_before}</pre>

                                      <div className="ai-patch-sublabel">Après :</div>
                                      {step.patch.code_after?.map((snippet, ai) => (
                                        <pre key={ai} className="ai-code ai-code-after">{snippet}</pre>
                                      ))}

                                      {step.patch.html_suggestion && (
                                        <>
                                          <div className="ai-patch-sublabel">🌐 Correctif HTML suggéré :</div>
                                          <pre className="ai-code ai-code-html">{step.patch.html_suggestion}</pre>
                                        </>
                                      )}
                                    </div>
                                  )}
                                </div>
                              ))}
                            </div>
                          )}

                          {/* ── Screenshot prise par le script Selenium ── */}
                          {reports[activeScenario].screenshot_path && (
                            <details className="screenshot-block">
                              <summary>📸 Screenshot</summary>
                              <img
                                className="exec-screenshot"
                                alt={`Capture — ${scenarios[activeScenario]?.title || ""}`}
                                src={`${API_BASE}/${reports[activeScenario].screenshot_path}`}
                              />
                            </details>
                          )}

                          {/* ── Format Agent IA : bloc texte compact, pensé pour être
                               consommé/parsé par un agent externe (CI, autre LLM...),
                               distinct de la vue "Analyse IA" ci-dessus destinée à un
                               testeur QA humain. ── */}
                          {agentReports[activeScenario] && (
                            <details className="agent-block">
                              <summary>🤖 Format Agent IA</summary>
                              <button
                                className="copy-btn"
                                onClick={() => navigator.clipboard.writeText(agentReports[activeScenario])}
                              >
                                📋 Copier
                              </button>
                              <pre className="agent-report">{agentReports[activeScenario]}</pre>
                            </details>
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

        /* ── Mode toggle (code vs capture d'écran) ── */
        .mode-toggle {
          display: flex; gap: 6px; padding: 0.6rem 1rem 0;
        }
        .mode-btn {
          flex: 1; padding: 6px 10px; border-radius: 8px;
          border: 1.5px solid #e8ebf3; background: #fafbfd;
          font-size: 0.8rem; font-weight: 600; color: #8991a4; cursor: pointer;
          transition: all 0.15s;
        }
        .mode-btn:hover { border-color: #e84393; color: #e84393; }
        .mode-btn-active { border-color: #e84393; background: #fff0f6; color: #e84393; }
        .mode-btn:disabled { cursor: not-allowed; opacity: 0.6; }

        /* ── Vision (screenshot) input ── */
        .vision-input {
          flex: 1; min-height: 280px; padding: 1rem;
          display: flex; flex-direction: column; gap: 0.9rem;
          background: #fafbff;
        }
        .vision-dropzone {
          flex: 1; min-height: 160px; border-radius: 10px;
          border: 2px dashed #dde1eb; display: flex;
          align-items: center; justify-content: center; cursor: pointer;
          overflow: hidden; background: #fff; transition: border-color 0.15s;
        }
        .vision-dropzone:hover { border-color: #e84393; }
        .vision-placeholder {
          display: flex; flex-direction: column; align-items: center; gap: 4px;
          padding: 1.5rem; text-align: center;
        }
        .vision-placeholder p { font-size: 0.85rem; font-weight: 500; color: #5a6072; margin: 0; }
        .vision-preview { max-width: 100%; max-height: 220px; object-fit: contain; }
        .vision-url-field { display: flex; flex-direction: column; gap: 4px; }
        .vision-url-label { font-size: 0.78rem; font-weight: 600; color: #2d3142; }
        .vision-url-input {
          padding: 8px 10px; border-radius: 8px; border: 1.5px solid #e8ebf3;
          font-size: 0.83rem; outline: none; font-family: monospace;
        }
        .vision-url-input:focus { border-color: #e84393; }
        .vision-url-input:disabled { opacity: 0.6; cursor: not-allowed; }

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
        .summary-title { font-weight: 700; font-size: 0.9rem; margin-bottom: 0.5rem; }
        .summary-row { display: flex; gap: 1rem; flex-wrap: wrap; }
        .summary-stat { font-size: 0.875rem; color: #3d4152; }
        .text-success { color: #1e7e50 !important; }
        .interface-banner { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; padding: 10px 14px; margin-bottom: 14px; background: #f0f3fb; border: 1px solid #dbe1f0; border-radius: 8px; font-size: 0.9rem; }
        .interface-banner-label { font-weight: 600; color: #4a5578; }
        .interface-banner-type { background: #4a5578; color: #fff; padding: 2px 10px; border-radius: 999px; font-size: 0.8rem; font-weight: 600; text-transform: capitalize; }
        .interface-banner-desc { color: #6b7591; }
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
          display: flex; align-items: center; justify-content: space-between; gap: 0.5rem;
        }
        .exec-pass { background: #e6f9f0; color: #1e7e50; }
        .exec-fail { background: #fff1f0; color: #c0392b; }
        .exec-time { font-weight: 500; font-size: 0.8rem; opacity: 0.85; }
        .metrics-row { display: flex; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 0.5rem; }
        .metric-chip {
          background: #f0f2f7; border-radius: 6px; padding: 4px 10px;
          font-size: 0.78rem; color: #3d4152;
        }
        .url-title-block {
          background: #f7f8fc; border-radius: 8px; padding: 8px 12px;
          font-size: 0.78rem; color: #5a6079; margin-bottom: 0.5rem;
          word-break: break-all; line-height: 1.6;
        }
        .screenshot-block { margin-top: 0.75rem; }
        .screenshot-block summary { cursor: pointer; font-size: 0.85rem; color: #4c5fd5; font-weight: 600; }
        .exec-screenshot {
          max-width: 100%; border-radius: 8px; margin-top: 0.5rem;
          border: 1px solid #e2e5f0;
        }
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

        /* ── Diagnostic IA (bug + correctif) ── */
        .ai-diagnosis {
          margin-top: 0.85rem; padding: 12px 14px;
          background: #fffaf0; border: 1px solid #ffe58f; border-radius: 10px;
        }
        .ai-diag-title { font-size: 0.85rem; font-weight: 700; color: #ad8b00; margin-bottom: 4px; }
        .ai-diag-summary { font-size: 0.82rem; color: #6b5900; line-height: 1.5; margin-bottom: 0.5rem; }
        .ai-step {
          border-top: 1px dashed #eadfa8; padding-top: 0.6rem; margin-top: 0.6rem;
          font-size: 0.8rem; color: #3d4152; line-height: 1.55;
        }
        .ai-step:first-of-type { border-top: none; padding-top: 0; margin-top: 0; }
        .ai-step-head { font-weight: 600; color: #2d3142; margin-bottom: 2px; }
        .ai-step-head code {
          background: rgba(0,0,0,0.06); padding: 1px 5px; border-radius: 4px; font-size: 0.76rem;
        }
        .ai-hyps { padding-left: 1.1rem; margin: 4px 0; }
        .ai-hyps li { margin-bottom: 2px; }
        .ai-patch { border-top: 1px dashed #eadfa8; margin-top: 0.6rem; padding-top: 0.55rem; }
        .ai-patch-label { font-weight: 700; font-size: 0.8rem; color: #2d3142; margin-bottom: 3px; }
        .ai-patch-sublabel { font-weight: 600; font-size: 0.76rem; color: #5a6072; margin: 0.4rem 0 2px; }
        .ai-patch-explain { font-size: 0.78rem; color: #5a6072; margin: 0 0 4px; }
        .ai-code {
          font-family: 'Fira Code', monospace; font-size: 0.74rem; line-height: 1.55;
          border-radius: 6px; padding: 8px 10px; margin: 2px 0 6px;
          white-space: pre-wrap; word-break: break-word;
        }
        .ai-code-before { background: #2a1414; border-left: 3px solid #ff4d4f; color: #ffb3b3; }
        .ai-code-after  { background: #122a18; border-left: 3px solid #36b37e; color: #b7f0c6; }
        .ai-code-html   { background: #10192b; border-left: 3px solid #4c8ff5; color: #bcdcff; }

        /* ── Format Agent IA (bloc texte structuré pour parseur externe) ── */
        .agent-block {
          margin-top: 0.85rem; background: #0b0d12; border: 1px solid #2a2d34;
          border-radius: 10px; padding: 0.5rem 0.85rem 0.85rem;
        }
        .agent-block summary { cursor: pointer; font-size: 0.85rem; font-weight: 600; color: #c792ea; }
        .agent-block .copy-btn {
          display: block; margin: 0.5rem 0 0.4rem; background: #1e222b; color: #c8cad0;
          border: 1px solid #3a3f4a; border-radius: 6px; padding: 4px 10px;
          font-size: 0.75rem; font-weight: 600; cursor: pointer;
        }
        .agent-block .copy-btn:hover { border-color: #8ab4f8; color: #8ab4f8; }
        .agent-report {
          background: #000; color: #7ee787; font-family: 'Fira Code', monospace;
          font-size: 0.76rem; line-height: 1.55; border-radius: 6px;
          padding: 0.75rem 0.9rem; white-space: pre-wrap; word-break: break-word;
          max-height: 360px; overflow-y: auto;
        }

        /* ── Alert ── */
        .alert { border-radius: 8px; padding: 12px 16px; font-size: 0.875rem; line-height: 1.5; }
        .alert-error { background: #fff1f0; border: 1px solid #ffa39e; color: #c0392b; }
        .alert-error strong { display: block; margin-bottom: 4px; }
        .alert-warning { background: #fffbe6; border: 1px solid #ffe58f; color: #ad8b00; }
        .alert-warning strong { display: block; margin-bottom: 4px; }

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