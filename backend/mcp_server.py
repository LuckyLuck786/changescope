import json
from pathlib import Path

try:
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("ChangeScope MCP")
except ImportError:
    mcp = None

from backend.analyzer import ensure_sample_repo, run_impact_analysis


BASE_DIR = Path(__file__).resolve().parent.parent
SAMPLE_REPO, DEFAULT_BASE, DEFAULT_TARGET = ensure_sample_repo(BASE_DIR)

APPROVED_REPOS = {
    "sample-billing-repo": {
        "id": "sample-billing-repo",
        "name": "Sample Billing & Tax Service",
        "repository_path": str(SAMPLE_REPO),
        "default_base": DEFAULT_BASE,
        "default_target": DEFAULT_TARGET,
    }
}


def list_approved_repositories() -> str:
    """Return local Git repositories allowlisted for ChangeScope impact analysis."""
    return json.dumps(list(APPROVED_REPOS.values()), indent=2)


def analyze_revision_impact(
    repo_id: str = "sample-billing-repo",
    base_commit: str = "",
    target_commit: str = "",
) -> str:
    """Analyze Git diff, Python AST changes, and reverse call-graph impact."""
    repo = APPROVED_REPOS.get(repo_id)
    if not repo:
        return json.dumps({"error": f"Repository '{repo_id}' is not in the approved allowlist."})
    base = base_commit or repo["default_base"]
    target = target_commit or repo["default_target"]
    result = run_impact_analysis(repo["repository_path"], base, target)
    return json.dumps({
        "repo_id": repo_id,
        "base_commit": result["base_commit"],
        "target_commit": result["target_commit"],
        "changed_functions": result["changed_functions"],
        "affected_nodes": [
            node["id"] for node in result["graph"]["nodes"] if node["status"] == "affected"
        ],
        "unresolved_calls": result["unresolved_calls"],
        "limitations": result["limitations"],
    }, indent=2)


def get_prioritized_tests(
    repo_id: str = "sample-billing-repo",
    base_commit: str = "",
    target_commit: str = "",
) -> str:
    """Return deterministically ranked regression tests with evidence paths."""
    repo = APPROVED_REPOS.get(repo_id)
    if not repo:
        return json.dumps({"error": f"Repository '{repo_id}' is not approved."})
    base = base_commit or repo["default_base"]
    target = target_commit or repo["default_target"]
    result = run_impact_analysis(repo["repository_path"], base, target)
    return json.dumps({
        "method_version": result["method_version"],
        "ranked_tests": result["ranked_tests"],
        "human_gate_note": "To execute tests, open the ChangeScope Web UI and confirm the execution gate.",
    }, indent=2)


def explain_impact_path(
    test_id: str,
    repo_id: str = "sample-billing-repo",
    base_commit: str = "",
    target_commit: str = "",
) -> str:
    """Explain the evidence chain linking a change to a test."""
    repo = APPROVED_REPOS.get(repo_id)
    if not repo:
        return json.dumps({"error": f"Repository '{repo_id}' is not approved."})
    base = base_commit or repo["default_base"]
    target = target_commit or repo["default_target"]
    result = run_impact_analysis(repo["repository_path"], base, target)
    for test in result["ranked_tests"]:
        if test["test_id"] == test_id:
            return json.dumps(test, indent=2)
    return json.dumps({"error": f"Test '{test_id}' not found in analysis."})


if mcp is not None:
    mcp.tool()(list_approved_repositories)
    mcp.tool()(analyze_revision_impact)
    mcp.tool()(get_prioritized_tests)
    mcp.tool()(explain_impact_path)

    @mcp.resource("changescope://limitations")
    def get_limitations() -> str:
        return json.dumps({
            "advisory_notice": "Affected code does not mean broken code.",
            "supported_language": "Python 3.x (static AST + Git diff)",
            "execution_policy": "Test execution is disabled in MCP tools; requires human approval via Web UI.",
            "known_limits": [
                "Dynamic calls (getattr, reflection) are marked unresolved rather than guessed.",
                "Heuristic scores are deterministic rankings, not calibrated failure probabilities.",
            ],
        }, indent=2)


if __name__ == "__main__":
    if mcp is None:
        raise RuntimeError("The MCP package is not installed.")
    mcp.run()
