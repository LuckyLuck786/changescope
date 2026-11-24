import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.analyzer import ensure_sample_repo, execute_approved_tests, run_impact_analysis
from backend.mcp_server import (
    APPROVED_REPOS,
    analyze_revision_impact,
    explain_impact_path,
    get_prioritized_tests,
    list_approved_repositories,
)


BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
DB_PATH = BASE_DIR / "changescope.db"

ensure_sample_repo(BASE_DIR)

app = FastAPI(title="ChangeScope MCP Web API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS analyses (
                id TEXT PRIMARY KEY,
                repo_id TEXT NOT NULL,
                base_commit TEXT NOT NULL,
                target_commit TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )


_init_db()


class AnalysisCreateRequest(BaseModel):
    repo_id: str = "sample-billing-repo"
    base_commit: str
    target_commit: str


class TestRunRequest(BaseModel):
    approved_by_user: bool
    test_ids: List[str]


class McpInvokeRequest(BaseModel):
    tool_name: str
    arguments: Dict[str, Any] = {}


@app.get("/api/projects")
def get_projects() -> List[Dict[str, Any]]:
    return list(APPROVED_REPOS.values())


@app.get("/api/analyses")
def list_analyses() -> List[Dict[str, Any]]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, repo_id, base_commit, target_commit, created_at FROM analyses "
            "ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
    return [
        {
            "id": row[0],
            "repo_id": row[1],
            "base_commit": row[2],
            "target_commit": row[3],
            "created_at": row[4],
        }
        for row in rows
    ]


@app.post("/api/analyses")
def create_analysis(req: AnalysisCreateRequest) -> Dict[str, Any]:
    repo = APPROVED_REPOS.get(req.repo_id)
    if not repo:
        raise HTTPException(status_code=400, detail="Repository is not in the approved allowlist.")
    try:
        result = run_impact_analysis(repo["repository_path"], req.base_commit, req.target_commit)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    analysis_id = f"ana_{uuid.uuid4().hex[:8]}"
    created_at = datetime.now(timezone.utc).isoformat()
    record = {"id": analysis_id, "repo_id": req.repo_id, "created_at": created_at, **result}
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO analyses "
            "(id, repo_id, base_commit, target_commit, created_at, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                analysis_id,
                req.repo_id,
                req.base_commit,
                req.target_commit,
                created_at,
                json.dumps(record),
            ),
        )
    return record


@app.get("/api/analyses/{analysis_id}")
def get_analysis(analysis_id: str) -> Dict[str, Any]:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT payload_json FROM analyses WHERE id = ?", (analysis_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Analysis not found.")
    return json.loads(row[0])


@app.post("/api/analyses/{analysis_id}/test-runs")
def run_tests_for_analysis(analysis_id: str, req: TestRunRequest) -> Dict[str, Any]:
    if not req.approved_by_user:
        raise HTTPException(
            status_code=403,
            detail="Human-in-the-loop gate blocked execution: explicit user approval required.",
        )
    analysis = get_analysis(analysis_id)
    try:
        outcome = execute_approved_tests(analysis["repository_path"], req.test_ids)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"analysis_id": analysis_id, **outcome}


@app.get("/api/analyses/{analysis_id}/report", response_class=PlainTextResponse)
def export_markdown_report(analysis_id: str) -> str:
    data = get_analysis(analysis_id)
    lines = [
        f"# ChangeScope Impact Report (`{data['id']}`)",
        f"- **Repository**: `{data['repository_path']}`",
        f"- **Base Commit**: `{data['base_commit']}`",
        f"- **Target Commit**: `{data['target_commit']}`",
        f"- **Method Version**: `{data['method_version']}`",
        "",
        "## Changed Functions",
    ]
    for function in data["changed_functions"]:
        lines.append(
            f"- `{function['qualified_name']}` "
            f"({function['change_type']}, lines {function['line_range']})"
        )
    lines.extend(["", "## Prioritized Regression Tests"])
    for test in data["ranked_tests"]:
        lines.append(
            f"{test['rank']}. `{test['test_id']}` — "
            f"**Score: {test['score']}** | {test['explanation']}"
        )
    lines.extend(["", "## Advisory Limitations"])
    for limitation in data["limitations"]:
        lines.append(f"- {limitation}")
    return "\n".join(lines)


@app.post("/api/mcp/invoke")
def invoke_mcp_tool(req: McpInvokeRequest) -> Dict[str, Any]:
    tools = {
        "list_approved_repositories": lambda args: list_approved_repositories(),
        "analyze_revision_impact": lambda args: analyze_revision_impact(**args),
        "get_prioritized_tests": lambda args: get_prioritized_tests(**args),
        "explain_impact_path": lambda args: explain_impact_path(**args),
    }
    function = tools.get(req.tool_name)
    if not function:
        raise HTTPException(status_code=404, detail=f"Unknown MCP tool: {req.tool_name}")
    return {
        "protocol": "Model Context Protocol (MCP)",
        "tool": req.tool_name,
        "structured_response": json.loads(function(req.arguments)),
    }


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/")
def serve_index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")
