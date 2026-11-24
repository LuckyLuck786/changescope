import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

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
    project_id: Optional[str] = None
    repo_path: Optional[str] = None
    base_commit: str = ""
    target_commit: str = ""
    budget_seconds: float = 0.25


class TestRunRequest(BaseModel):
    approved_by_user: bool = False
    user_authorized: bool = False
    test_ids: List[str] = []
    test_identifiers: List[str] = []
    repo_path: Optional[str] = None


@app.get("/api/defaults")
def get_defaults() -> Dict[str, Any]:
    repo = APPROVED_REPOS["sample-billing-repo"]
    project = {
        "id": "sample-billing-repo",
        "name": "Sample Billing & Tax Service (sample-billing-repo)",
        "path": repo["repository_path"],
        "base_commit": repo["default_base"][:10],
        "target_commit": repo["default_target"][:10],
    }
    return {
        "projects": [project],
        "default_project": project,
        "demo_repo_path": repo["repository_path"],
        "base_commit": repo["default_base"],
        "target_commit": repo["default_target"],
        "budget_seconds": 0.25,
    }


class McpInvokeRequest(BaseModel):
    tool_name: str
    arguments: Dict[str, Any] = {}
    repo_path: Optional[str] = None
    base_commit: str = ""
    target_commit: str = ""
    budget_seconds: float = 0.25


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
    project_id = req.project_id or req.repo_id
    repo = APPROVED_REPOS.get(project_id)
    repository_path = req.repo_path
    if repository_path:
        approved_paths = {item["repository_path"] for item in APPROVED_REPOS.values()}
        if str(Path(repository_path).resolve()) not in approved_paths:
            raise HTTPException(status_code=400, detail="Repository is not in the approved allowlist.")
    elif repo:
        repository_path = repo["repository_path"]
    else:
        raise HTTPException(status_code=400, detail="Repository is not in the approved allowlist.")
    try:
        result = run_impact_analysis(repository_path, req.base_commit, req.target_commit)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    used = 0.0
    for test in result["ranked_tests"]:
        estimated = test["estimated_seconds"]
        fits = test["score"] >= 0.40 and round(used + estimated, 4) <= req.budget_seconds
        test["selected_in_budget"] = fits
        if fits:
            used = round(used + estimated, 4)

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


@app.post("/api/test-runs")
def run_authorized_tests(req: TestRunRequest) -> Dict[str, Any]:
    if not req.approved_by_user and not req.user_authorized:
        raise HTTPException(
            status_code=403,
            detail="Human-in-the-loop gate blocked execution: explicit user authorization required.",
        )

    selected = req.test_identifiers or req.test_ids
    repository_path = req.repo_path
    if not repository_path:
        repository_path = APPROVED_REPOS["sample-billing-repo"]["repository_path"]

    approved_paths = {item["repository_path"] for item in APPROVED_REPOS.values()}
    if str(Path(repository_path).resolve()) not in approved_paths:
        raise HTTPException(status_code=400, detail="Repository is not in the approved allowlist.")

    outcomes = []
    raw_sections = []
    for test_id in selected:
        try:
            started = datetime.now(timezone.utc)
            result = execute_approved_tests(repository_path, [test_id])
            duration = round(max(0.001, (datetime.now(timezone.utc) - started).total_seconds()), 3)
            observed = "PASSED" if result["status"] == "passed" else "FAILED"
            summary = (
                result["stdout"].strip().splitlines()[-1]
                if result["stdout"].strip()
                else result["stderr"].strip().splitlines()[-1]
                if result["stderr"].strip()
                else observed
            )
            outcomes.append({
                "test_id": test_id,
                "observed_outcome": observed,
                "duration_seconds": duration,
                "summary": summary,
            })
            raw_sections.append(f"$ pytest -v --tb=short {test_id}\n{result['stdout']}{result['stderr']}")
        except Exception as exc:
            outcomes.append({
                "test_id": test_id,
                "observed_outcome": "FAILED",
                "duration_seconds": 0.0,
                "summary": str(exc),
            })
            raw_sections.append(f"$ pytest -v --tb=short {test_id}\n{exc}")

    return {
        "outcomes": outcomes,
        "raw_output": "\n\n".join(raw_sections),
    }


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
    tool_name = req.tool_name
    if tool_name == "changescope://limitations" or tool_name == "limitations":
        return {
            "resource": "changescope://limitations",
            "supported_language": "Python 3.10+ (AST static subset)",
            "advisory_boundary": "Affected != Broken; requires human test approval.",
        }

    if tool_name == "list_approved_repositories":
        return {
            "protocol": "Model Context Protocol (MCP)",
            "tool": tool_name,
            "structured_response": json.loads(list_approved_repositories()),
        }

    try:
        repo = APPROVED_REPOS["sample-billing-repo"]
        repo_path = req.repo_path or req.arguments.get("repo_path") or repo["repository_path"]
        base_commit = req.base_commit or req.arguments.get("base_commit") or repo["default_base"]
        target_commit = req.target_commit or req.arguments.get("target_commit") or repo["default_target"]
        budget = float(req.budget_seconds or req.arguments.get("budget_seconds", 0.25))
        result = run_impact_analysis(repo_path, base_commit, target_commit)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if tool_name in {"get_impact_graph", "get_prioritized_tests"}:
        if tool_name == "get_impact_graph":
            return {
                "mcp_tool": "get_impact_graph",
                "nodes": len(result["graph"]["nodes"]),
                "edges": len(result["graph"]["edges"]),
                "impacted": [
                    node["id"] for node in result["graph"]["nodes"]
                    if node["role"] in {"CHANGED", "AFFECTED"}
                ],
            }
        return {
            "mcp_tool": "get_prioritized_tests",
            "budget_seconds": budget,
            "ranked_tests": result["ranked_tests"],
        }
    if tool_name == "rank_regression_tests":
        return {
            "mcp_tool": "rank_regression_tests",
            "budget_seconds": budget,
            "selected_tests": [
                test["test_id"] for test in result["ranked_tests"] if test["score"] >= 0.40
            ],
        }
    if tool_name not in {"analyze_revision_impact", "analyze_code_changes"}:
        raise HTTPException(status_code=404, detail=f"Unknown MCP tool: {tool_name}")
    return {
        "mcp_tool": "analyze_revision_impact",
        "revisions": f"{result['base_hash']}..{result['target_hash']}",
        "changed_functions": result["changed_functions"],
        "recommended_tests": [
            test["test_id"] for test in result["ranked_tests"] if test["score"] >= 0.40
        ],
    }


@app.post("/api/report/markdown", response_class=PlainTextResponse)
def create_markdown_report(req: AnalysisCreateRequest) -> str:
    repo = APPROVED_REPOS.get(req.project_id or req.repo_id)
    repository_path = req.repo_path
    if repository_path:
        approved_paths = {item["repository_path"] for item in APPROVED_REPOS.values()}
        if str(Path(repository_path).resolve()) not in approved_paths:
            raise HTTPException(status_code=400, detail="Repository is not in the approved allowlist.")
    elif repo:
        repository_path = repo["repository_path"]
    else:
        raise HTTPException(status_code=400, detail="Repository is not in the approved allowlist.")
    try:
        data = run_impact_analysis(
            repository_path,
            req.base_commit or APPROVED_REPOS["sample-billing-repo"]["default_base"],
            req.target_commit or APPROVED_REPOS["sample-billing-repo"]["default_target"],
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    lines = [
        "# ChangeScope MCP Analysis Report",
        f"- **Revisions:** `{data['base_hash']}` -> `{data['target_hash']}`",
        f"- **Method:** `{data['method_version']}`",
        "",
        "## Changed Functions",
        *[
            f"- `{item['qualified_name']}` ({item['change_type']}, lines {item['line_range']})"
            for item in data["changed_functions"]
        ],
        "",
        "## Ranked Regression Tests",
        *[
            f"{test['rank']}. `{test['test_id']}` — Score `{test['score']}` | {test['explanation']}"
            for test in data["ranked_tests"]
        ],
    ]
    return "\n".join(lines)


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/")
def serve_index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")
