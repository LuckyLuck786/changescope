let analysis = null;
let cy = null;
let layoutDir = "BT";
let impactedOnly = false;
let tableMode = false;
let budgetChart = null;
let outcomes = {};

async function bootstrap() {
  try {
    const response = await fetch("/api/defaults");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const cfg = await response.json();
    const project = cfg.default_project;
    repoPath.innerHTML = cfg.projects.map(item =>
      `<option value="${escapeHtml(item.path)}">${escapeHtml(item.name)}</option>`
    ).join("");
    repoPath.value = project.path;
    baseCommit.value = project.base_commit;
    targetCommit.value = project.target_commit;
    budgetSlider.value = cfg.budget_seconds;
    budgetLabel.textContent = `${cfg.budget_seconds}s`;
    statusBanner.className = "advisory hidden";
    await runAnalysis();
  } catch (error) {
    statusBanner.className = "advisory";
    statusBanner.textContent = `Backend Connection Notice: ${error.message}`;
  }
}

async function runAnalysis() {
  try {
    runBtn.disabled = true;
    runBtn.textContent = "Analyzing...";
    outcomes = {};
    workerConsoleWrap.className = "hidden";
    const response = await fetch("/api/analyses", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({
        repo_path: repoPath.value,
        base_commit: baseCommit.value,
        target_commit: targetCommit.value,
        budget_seconds: Number(budgetSlider.value),
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Analysis failed");
    analysis = data;
    exportBtn.disabled = false;
    statusBanner.className = "advisory hidden";
    renderDashboard();
  } catch (error) {
    statusBanner.className = "advisory";
    statusBanner.textContent = `Analysis Notice: ${error.message}`;
  } finally {
    runBtn.disabled = false;
    runBtn.textContent = "Run Deterministic Impact Analysis";
  }
}

function escapeHtml(value) {
  return String(value ?? "").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;");
}

function renderDashboard() {
  kpiRevisions.textContent = `${analysis.base_hash} → ${analysis.target_hash}`;
  kpiFiles.textContent = `${analysis.changed_files.length} file(s) changed`;
  kpiBlind.textContent = `${analysis.blind_spots.length} Flagged`;

  changedList.innerHTML = analysis.changed_functions.map(fn => `
    <div class="change-item">
      <strong>${escapeHtml(fn.qualified_name)}</strong>
      <span class="badge ${fn.change_type === "DELETED" ? "badge-fail" : "badge-medium"}">${fn.change_type}</span>
      <div><small>${escapeHtml(fn.file_path)} · lines ${fn.line_range}</small></div>
    </div>`).join("") || "<small class='muted'>No function-level changes.</small>";

  diffViewer.innerHTML = (analysis.diff_preview || "(No diff)").split("\n").map(line =>
    line.startsWith("+") && !line.startsWith("+++") ? `<span class="diff-line-add">${escapeHtml(line)}</span>` :
    line.startsWith("-") && !line.startsWith("---") ? `<span class="diff-line-del">${escapeHtml(line)}</span>` :
    escapeHtml(line)
  ).join("");

  blindSpots.innerHTML = analysis.blind_spots.map(spot => `
    <div class="change-item">
      <strong class="badge badge-medium">${escapeHtml(spot.type)}</strong>
      <code>${escapeHtml(spot.location)}</code>
      <div><small>${escapeHtml(spot.detail)}</small></div>
    </div>`).join("") || `<div class="change-item"><strong>No AST blind spots detected.</strong></div>`;

  recomputeBudget(Number(budgetSlider.value));
  renderGraph();
  renderGraphTable();
}

function recomputeBudget(budget) {
  budgetLabel.textContent = `${budget.toFixed(2)}s`;
  let used = 0;
  let selectedCount = 0;
  analysis.ranked_tests.forEach(test => {
    const fits = test.score >= 0.40 && used + test.estimated_seconds <= budget;
    test.selected_in_budget = Boolean(fits);
    if (fits) { used += test.estimated_seconds; selectedCount++; }
  });
  used = Number(used.toFixed(3));
  const total = analysis.ranked_tests.reduce((sum,test) => sum + test.estimated_seconds, 0);
  const saved = total ? Math.max(0, Math.round((1 - used / total) * 100)) : 0;
  kpiSavings.textContent = `${used.toFixed(2)}s / ${total.toFixed(2)}s · ${saved}% saved`;
  kpiBlast.textContent = `${countRole("CHANGED")} Changed / ${countRole("AFFECTED")} Affected`;
  renderBlastStack();
  renderBudgetChart(used, Math.max(0, total - used));
  renderTests();
}

function countRole(role) {
  return analysis.graph.nodes.filter(node => (node.role || node.status?.toUpperCase()) === role).length;
}

function renderBlastStack() {
  const counts = [
    ["CHANGED","#ef4444"],["AFFECTED","#d29922"],["TEST","#a371f7"],["UNAFFECTED","#6e7681"]
  ].map(([role,color]) => [countRole(role),color]);
  const total = counts.reduce((sum,[count])=>sum+count,0) || 1;
  blastStack.innerHTML = counts.map(([count,color]) => `<i style="width:${count/total*100}%;background:${color}"></i>`).join("");
}

function renderBudgetChart(selected, fullRemaining) {
  if (!budgetChart || typeof Chart === "undefined") return;
  if (window.budgetChartInstance) window.budgetChartInstance.destroy();
  window.budgetChartInstance = new Chart(budgetChart, {
    type:"bar",
    data:{labels:["Suite"],datasets:[
      {label:"Selected",data:[selected],backgroundColor:"#58a6ff",stack:"suite"},
      {label:"Deferred",data:[fullRemaining],backgroundColor:"#283548",stack:"suite"}
    ]},
    options:{indexAxis:"y",responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{stacked:true,display:false},y:{stacked:true,display:false}}}
  });
}

function renderGraph() {
  if (!analysis || typeof cytoscape === "undefined") return;
  if (cy) cy.destroy();
  const nodes = analysis.graph.nodes.filter(node => !impactedOnly || node.role === "CHANGED" || node.role === "AFFECTED" || node.role === "TEST");
  const ids = new Set(nodes.map(node => node.id));
  const elements = [
    ...nodes.map(node => ({data:{id:node.id,label:node.label,role:node.role || node.status.toUpperCase(),raw:node}})),
    ...analysis.graph.edges.filter(edge => ids.has(edge.source) && ids.has(edge.target)).map((edge,index) => ({
      data:{id:`edge-${index}`,source:edge.source,target:edge.target,label:edge.label || "calls",edgeType:edge.edgeType || "CALLS"}
    }))
  ];
  cy = cytoscape({
    container: document.getElementById("cy"),
    elements,
    style:[
      {selector:"node",style:{
        "shape":"round-rectangle","width":"label","height":"label","padding":"12px",
        "background-color":"#111827","border-width":2,"border-color":"#6e7681",
        "label":"data(label)","color":"#f8fafc","font-size":"11px","font-family":"ui-monospace, monospace",
        "text-valign":"center","text-halign":"center","text-wrap":"wrap","text-max-width":"180px"
      }},
      {selector:'node[role="CHANGED"]',style:{"border-color":"#f85149","background-color":"#2b1216"}},
      {selector:'node[role="AFFECTED"]',style:{"border-color":"#d29922","background-color":"#291f10"}},
      {selector:'node[role="TEST"]',style:{"border-color":"#a371f7","background-color":"#211433"}},
      {selector:'node[role="UNAFFECTED"]',style:{"border-color":"#6e7681","background-color":"#111827"}},
      {selector:"edge",style:{
        "width":1.8,"line-color":"#58a6ff","target-arrow-color":"#58a6ff","target-arrow-shape":"triangle",
        "curve-style":"bezier","label":"data(label)","font-size":"9px","color":"#94a3b8",
        "text-background-color":"#070a10","text-background-opacity":.85,"text-background-padding":"2px"
      }},
      {selector:'edge[edgeType="COVERS"]',style:{"line-style":"dashed","line-color":"#a371f7","target-arrow-color":"#a371f7"}},
      {selector:"edge[edgeType=\"CALLS\"]",style:{"line-style":"solid","line-color":"#58a6ff","target-arrow-color":"#58a6ff"}},
      {selector:"node.dimmed, edge.dimmed",style:{opacity:.22}},
      {selector:"node.highlighted",style:{border-width:3.5,opacity:1}}
    ],
    layout:{name:"dagre",rankDir:layoutDir,nodeSep:45,rankSep:85,padding:24,fit:true}
  });
  if (typeof cy.layout === "function") cy.layout({name:"dagre",rankDir:layoutDir,nodeSep:45,rankSep:85,padding:24}).run();
  cy.on("mouseover","node",evt => highlight(evt.target));
  cy.on("tap","node",evt => {highlight(evt.target); inspectNode(evt.target.data("raw"));});
  cy.on("tap",evt => {if(evt.target===cy){cy.elements().removeClass("dimmed highlighted");}});
  cy.ready(() => cy.fit(undefined,28));
}

function highlight(target) {
  cy.elements().removeClass("dimmed highlighted").addClass("dimmed");
  const neighborhood = target.closedNeighborhood();
  neighborhood.removeClass("dimmed").addClass("highlighted");
  neighborhood.forEach(el => el.style("opacity",1));
}

function inspectNode(node) {
  const role = node.role || node.status.toUpperCase();
  const hops = node.evidence_path?.length
    ? node.evidence_path.map(hop => `<button class="hop" data-hop="${escapeHtml(hop)}">${escapeHtml(hop)}</button>`).join("")
    : `<div class="mono">Isolated from modified code</div>`;
  nodeInspector.innerHTML = `
    <h3>${escapeHtml(node.label)}</h3>
    <span class="badge ${role==="CHANGED"?"badge-fail":role==="AFFECTED"?"badge-medium":role==="TEST"?"badge-medium":"badge-none"}">${role}</span>
    <label>Qualified ID</label><div class="mono">${escapeHtml(node.id)}</div>
    <label>File & Line Range</label><div class="mono">${escapeHtml(node.file_path)} (${escapeHtml(node.line_range)})</div>
    <label>Impact Evidence Chain</label>${hops}`;
  nodeInspector.querySelectorAll("[data-hop]").forEach(button => button.addEventListener("click", () => focusNode(button.dataset.hop)));
}

function focusNode(id) {
  const node = cy?.getElementById(id);
  if (node?.length) {
    highlight(node);
    cy.animate({center:{eles:node},duration:250});
    inspectNode(node.data("raw"));
  }
}

function renderGraphTable() {
  graphTable.innerHTML = analysis.graph.nodes.map(node => `
    <tr data-node="${escapeHtml(node.id)}" style="cursor:pointer">
      <td><code>${escapeHtml(node.id)}</code></td><td>${escapeHtml(node.role || node.status)}</td>
      <td>${escapeHtml(node.file_path)} (${escapeHtml(node.line_range)})</td><td>${node.hop_distance ?? "-"}</td>
    </tr>`).join("");
  graphTable.querySelectorAll("tr").forEach(row => row.addEventListener("click",()=>inspectNode(analysis.graph.nodes.find(node=>node.id===row.dataset.node))));
}

function renderTests() {
  testTable.innerHTML = analysis.ranked_tests.map(test => {
    const outcome = outcomes[test.test_id];
    const risk = test.predicted_risk || (test.score >= .70 ? "HIGH" : test.score >= .30 ? "MEDIUM" : "LOW");
    return `<tr>
      <td><input type="checkbox" class="test-check" value="${escapeHtml(test.test_id)}" ${test.selected_in_budget?"checked":""}></td>
      <td><strong>#${test.rank}</strong><br><code>${escapeHtml(test.test_id)}</code></td>
      <td>${test.estimated_seconds.toFixed(2)}s</td>
      <td><strong>${test.score.toFixed(2)}</strong></td>
      <td><span class="badge badge-${risk.toLowerCase()}">${risk}</span></td>
      <td>${outcome ? `<span class="badge ${outcome.observed_outcome==="PASSED"?"badge-pass":"badge-fail"}">${outcome.observed_outcome}</span><small>${escapeHtml(outcome.summary)}</small>` : `<span class="badge badge-none">NOT RUN</span>`}</td>
      <td>${escapeHtml(test.explanation || test.reason || test.evidence_path)}</td>
    </tr>`;
  }).join("");
  runTestsBtn.disabled = false;
}

async function approveTests() {
  const selected = [...document.querySelectorAll(".test-check:checked")].map(input => input.value);
  if (!selected.length || !confirm(`Execute ${selected.length} selected pytest target(s)?`)) return;
  const response = await fetch("/api/test-runs", {
    method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({approved_by_user:true,user_authorized:true,repo_path:repoPath.value,test_identifiers:selected})
  });
  const data = await response.json();
  if (!response.ok) {alert(data.detail);return;}
  data.outcomes.forEach(outcome => outcomes[outcome.test_id]=outcome);
  renderTests();
  workerConsoleWrap.className = "";
  workerConsole.textContent = data.raw_output;
}

async function invokeMcp() {
  try {
    mcpOutput.textContent = "Calling MCP...";
    const response = await fetch("/api/mcp/invoke",{
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({
        tool_name:mcpTool.value,
        repo_path:repoPath.value,
        base_commit:baseCommit.value,
        target_commit:targetCommit.value,
        budget_seconds:Number(budgetSlider.value)
      })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "MCP invocation failed");
    mcpOutput.textContent = JSON.stringify(data,null,2);
  } catch (error) {
    mcpOutput.textContent = JSON.stringify({error:error.message},null,2);
  }
}

function setLayout(direction) {
  layoutDir=direction;
  dirBT.classList.toggle("active",direction==="BT");
  dirLR.classList.toggle("active",direction==="LR");
  renderGraph();
}

budgetSlider.addEventListener("input",()=>analysis&&recomputeBudget(Number(budgetSlider.value)));
runBtn.addEventListener("click",runAnalysis);
runTestsBtn.addEventListener("click",approveTests);
mcpBtn.addEventListener("click",invokeMcp);
exportBtn.addEventListener("click", async () => {
  if (!analysis) return;
  const response = await fetch("/api/report/markdown", {
    method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({
      repo_path:repoPath.value,
      base_commit:baseCommit.value,
      target_commit:targetCommit.value,
      budget_seconds:Number(budgetSlider.value)
    })
  });
  if (!response.ok) return;
  const blob = new Blob([await response.text()], {type:"text/markdown"});
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "changescope-report.md";
  link.click();
  URL.revokeObjectURL(url);
});
dirBT.addEventListener("click",()=>setLayout("BT"));
dirLR.addEventListener("click",()=>setLayout("LR"));
filterAll.addEventListener("click",()=>{impactedOnly=false;filterAll.classList.add("active");filterImpacted.classList.remove("active");renderGraph();});
filterImpacted.addEventListener("click",()=>{impactedOnly=true;filterImpacted.classList.add("active");filterAll.classList.remove("active");renderGraph();});
viewToggle.addEventListener("click",()=>{tableMode=!tableMode;document.getElementById("cy").classList.toggle("hidden",tableMode);graphTableWrap.classList.toggle("hidden",!tableMode);viewToggle.textContent=tableMode?"Graph View":"Table View";});
fitGraph.addEventListener("click",()=>cy&&cy.fit(undefined,28));
zoomIn.addEventListener("click",()=>cy&&cy.zoom({level:cy.zoom()*1.2,position:cy.extent().center}));
zoomOut.addEventListener("click",()=>cy&&cy.zoom({level:cy.zoom()/1.2,position:cy.extent().center}));

bootstrap();
