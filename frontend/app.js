let currentAnalysis = null;

const STATUS_COLORS = {
  changed: "#ef4444",
  affected: "#f59e0b",
  test: "#a855f7",
  unaffected: "#64748b",
};

async function init() {
  const response = await fetch("/api/projects");
  const projects = await response.json();
  const select = document.getElementById("repo-select");
  projects.forEach((project) => {
    const option = document.createElement("option");
    option.value = project.id;
    option.textContent = `${project.name} (${project.id})`;
    select.appendChild(option);
  });

  if (projects.length) {
    const project = projects[0];
    document.getElementById("base-commit").value = project.default_base.slice(0, 10);
    document.getElementById("base-commit").dataset.full = project.default_base;
    document.getElementById("target-commit").value = project.default_target.slice(0, 10);
    document.getElementById("target-commit").dataset.full = project.default_target;
  }

  document.getElementById("btn-run-analysis").addEventListener("click", runAnalysis);
  document.getElementById("btn-approve-tests").addEventListener("click", approveAndRunTests);
  document.getElementById("btn-export-report").addEventListener("click", exportReport);
  document.getElementById("btn-invoke-mcp").addEventListener("click", invokeMcpTool);
}

async function runAnalysis() {
  const button = document.getElementById("btn-run-analysis");
  button.disabled = true;
  button.textContent = "Analyzing AST and Reverse Graph...";
  try {
    const response = await fetch("/api/analyses", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        repo_id: document.getElementById("repo-select").value,
        base_commit: document.getElementById("base-commit").dataset.full,
        target_commit: document.getElementById("target-commit").dataset.full,
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Analysis failed");
    currentAnalysis = data;
    renderChangeSummary(data);
    renderGraph(data.graph);
    renderTestTable(data.ranked_tests);
    document.getElementById("btn-export-report").disabled = false;
    document.getElementById("btn-approve-tests").disabled = false;
    document.getElementById("test-run-result").classList.add("hidden");
  } catch (error) {
    document.getElementById("change-summary").textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = "Run Deterministic Impact Analysis";
  }
}

function renderChangeSummary(data) {
  const container = document.getElementById("change-summary");
  if (!data.changed_functions.length) {
    container.innerHTML = "<p class='muted'>No function-level changes detected.</p>";
    return;
  }
  container.innerHTML = data.changed_functions.map((fn) => `
    <div class="change-item">
      <strong>${escapeHtml(fn.qualified_name)}</strong>
      <span class="tag tag-${fn.change_type}">${fn.change_type}</span>
      <div class="muted">${escapeHtml(fn.file_path)} (lines ${fn.line_range})</div>
    </div>
  `).join("");
}

function renderGraph(graph) {
  const svg = document.getElementById("impact-svg");
  svg.innerHTML = `
    <defs>
      <marker id="arrow" viewBox="0 0 10 10" refX="22" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
        <path d="M 0 1 L 10 5 L 0 9 z" fill="#94a3b8"></path>
      </marker>
    </defs>
  `;

  const layers = { test: [], affected: [], unaffected: [], changed: [] };
  graph.nodes.forEach((node) => (layers[node.status] || layers.unaffected).push(node));
  const positions = {};
  const placeRow = (nodes, y) => {
    nodes.forEach((node, index) => {
      positions[node.id] = { x: ((index + 1) * 760) / (nodes.length + 1), y };
    });
  };
  placeRow(layers.test, 50);
  placeRow([...layers.affected, ...layers.unaffected], 140);
  placeRow(layers.changed, 230);

  graph.edges.forEach((edge) => {
    const source = positions[edge.source];
    const target = positions[edge.target];
    if (!source || !target) return;
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line.setAttribute("x1", source.x);
    line.setAttribute("y1", source.y);
    line.setAttribute("x2", target.x);
    line.setAttribute("y2", target.y);
    line.setAttribute("stroke", "#64748b");
    line.setAttribute("stroke-width", "1.75");
    line.setAttribute("marker-end", "url(#arrow)");
    svg.appendChild(line);
  });

  graph.nodes.forEach((node) => {
    const position = positions[node.id];
    if (!position) return;
    const group = document.createElementNS("http://www.w3.org/2000/svg", "g");
    group.style.cursor = "pointer";
    const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    circle.setAttribute("cx", position.x);
    circle.setAttribute("cy", position.y);
    circle.setAttribute("r", "15");
    circle.setAttribute("fill", STATUS_COLORS[node.status] || "#64748b");
    circle.setAttribute("stroke", node.impacted ? "#ffffff" : "#1e293b");
    circle.setAttribute("stroke-width", node.impacted ? "2.5" : "1.5");
    const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
    text.setAttribute("x", position.x);
    text.setAttribute("y", position.y + 28);
    text.setAttribute("text-anchor", "middle");
    text.setAttribute("fill", "#e2e8f0");
    text.setAttribute("font-size", "11");
    text.textContent = node.label;
    group.appendChild(circle);
    group.appendChild(text);
    group.addEventListener("click", () => showNodeDetails(node));
    svg.appendChild(group);
  });
}

function showNodeDetails(node) {
  const inspector = document.getElementById("node-inspector");
  const chain = node.evidence_path.length ? node.evidence_path.join(" -> ") : "None (isolated)";
  inspector.innerHTML = `
    <h3>${escapeHtml(node.label)}</h3>
    <p><strong>Qualified ID:</strong><br><code>${escapeHtml(node.id)}</code></p>
    <p><strong>File:</strong> ${escapeHtml(node.file_path)} (${node.line_range})</p>
    <p><strong>Role:</strong> ${node.status.toUpperCase()}</p>
    <p><strong>Impact Evidence Chain:</strong><br><code>${escapeHtml(chain)}</code></p>
  `;
}

function renderTestTable(tests) {
  const tbody = document.getElementById("tests-tbody");
  tbody.innerHTML = tests.map((test) => `
    <tr>
      <td><strong>#${test.rank}</strong></td>
      <td><code>${escapeHtml(test.test_id)}</code></td>
      <td><strong>${test.score.toFixed(2)}</strong></td>
      <td>${test.recommended ? "Recommended" : "Low Priority"}</td>
      <td>${escapeHtml(test.explanation)}</td>
    </tr>
  `).join("");
}

async function approveAndRunTests() {
  if (!currentAnalysis) return;
  const recommendedIds = currentAnalysis.ranked_tests
    .filter((test) => test.recommended)
    .map((test) => test.test_id);
  if (!window.confirm(`Human-in-the-Loop Execution Gate:\n\nApprove running ${recommendedIds.length} prioritized regression tests in the isolated pytest worker?`)) return;

  const response = await fetch(`/api/analyses/${currentAnalysis.id}/test-runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ approved_by_user: true, test_ids: recommendedIds }),
  });
  const outcome = await response.json();
  const box = document.getElementById("test-run-result");
  box.classList.remove("hidden");
  box.innerHTML = `
    <strong>Isolated Worker Outcome: ${(outcome.status || "error").toUpperCase()}</strong>
    (Executed ${outcome.executed_tests?.length || 0} of ${currentAnalysis.ranked_tests.length} total suite tests)
    <pre>${escapeHtml(outcome.stdout || outcome.detail || "")}</pre>
  `;
}

async function invokeMcpTool() {
  const toolName = document.getElementById("mcp-tool-select").value;
  const output = document.getElementById("mcp-output");
  output.textContent = "Calling MCP Server...";
  const response = await fetch("/api/mcp/invoke", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tool_name: toolName, arguments: {} }),
  });
  const data = await response.json();
  output.textContent = JSON.stringify(data, null, 2);
}

function exportReport() {
  if (currentAnalysis) window.open(`/api/analyses/${currentAnalysis.id}/report`, "_blank");
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

init();
