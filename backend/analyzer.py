import ast
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import networkx as nx


@dataclass
class FunctionDefInfo:
    qualified_name: str
    name: str
    file_path: str
    start_line: int
    end_line: int
    calls: List[str]


def ensure_sample_repo(base_dir: Path) -> Tuple[Path, str, str]:
    """Create a realistic two-commit Python repository with a pytest suite."""
    repo_dir = base_dir / "sample_repo"
    if (repo_dir / ".git").exists():
        commits = subprocess.check_output(
            ["git", "rev-list", "--reverse", "HEAD"],
            cwd=repo_dir,
            text=True,
        ).strip().splitlines()
        if len(commits) >= 2:
            return repo_dir, commits[0], commits[-1]

    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "dev@changescope.local"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "ChangeScope Demo"], cwd=repo_dir, check=True)

    (repo_dir / "tax.py").write_text(
        '''def legacy_round(amount: float) -> float:
    """Deprecated rounding helper removed in target commit."""
    return round(amount, 2)


def calculate_gst(subtotal: float, rate: float = 0.18) -> float:
    """Compute GST tax for an order subtotal."""
    if subtotal < 0:
        raise ValueError("Subtotal cannot be negative")
    return legacy_round(subtotal * rate)
''',
        encoding="utf-8",
    )
    (repo_dir / "billing.py").write_text(
        '''from tax import calculate_gst


def generate_invoice_total(subtotal: float, discount: float = 0.0) -> float:
    """Apply discount and add GST."""
    net = max(0.0, subtotal - discount)
    tax = calculate_gst(net)
    return round(net + tax, 2)
''',
        encoding="utf-8",
    )
    (repo_dir / "notifications.py").write_text(
        '''def format_receipt_banner(customer_name: str) -> str:
    """Unrelated module used to verify non-impacted test filtering."""
    return f"Thank you for your order, {customer_name}!"
''',
        encoding="utf-8",
    )
    (repo_dir / "test_billing.py").write_text(
        '''from billing import generate_invoice_total


def test_invoice_total_standard():
    assert generate_invoice_total(100.0, discount=10.0) == 106.2


def test_invoice_zero_subtotal():
    assert generate_invoice_total(0.0) == 0.0
''',
        encoding="utf-8",
    )
    (repo_dir / "test_tax.py").write_text(
        '''import pytest
from tax import calculate_gst


def test_calculate_gst_default():
    assert calculate_gst(200.0) == 36.0


def test_calculate_gst_negative_raises():
    with pytest.raises(ValueError):
        calculate_gst(-5.0)
''',
        encoding="utf-8",
    )
    (repo_dir / "test_notifications.py").write_text(
        '''from notifications import format_receipt_banner


def test_receipt_banner():
    assert format_receipt_banner("Asha") == "Thank you for your order, Asha!"
''',
        encoding="utf-8",
    )

    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(
        ["git", "commit", "-m", "feat: initial billing and tax modules"],
        cwd=repo_dir,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    base_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_dir, text=True).strip()

    (repo_dir / "tax.py").write_text(
        '''def calculate_gst(subtotal: float, rate: float = 0.18, exempt: bool = False) -> float:
    """Compute GST tax with exemption support; removes legacy_round."""
    if subtotal < 0:
        raise ValueError("Subtotal cannot be negative")
    if exempt:
        return 0.0
    return round(subtotal * rate, 2)
''',
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(
        ["git", "commit", "-m", "refactor(tax): add exempt flag and delete legacy_round"],
        cwd=repo_dir,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    target_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_dir, text=True).strip()
    return repo_dir, base_commit, target_commit


class _FunctionVisitor(ast.NodeVisitor):
    def __init__(self, module_name: str, file_path: str):
        self.module_name = module_name
        self.file_path = file_path
        self.functions: List[FunctionDefInfo] = []
        self._current_calls: Optional[List[str]] = None

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        prev_calls = self._current_calls
        self._current_calls = []
        self.generic_visit(node)
        qname = f"{self.module_name}.{node.name}"
        end_line = getattr(node, "end_lineno", node.lineno)
        self.functions.append(
            FunctionDefInfo(
                qualified_name=qname,
                name=node.name,
                file_path=self.file_path,
                start_line=node.lineno,
                end_line=end_line,
                calls=list(dict.fromkeys(self._current_calls)),
            )
        )
        self._current_calls = prev_calls

    def visit_Call(self, node: ast.Call) -> None:
        if self._current_calls is not None:
            if isinstance(node.func, ast.Name):
                self._current_calls.append(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                self._current_calls.append(f"unresolved.{node.func.attr}")
        self.generic_visit(node)


def _git_show_file(repo_path: Path, commit: str, file_path: str) -> Optional[str]:
    res = subprocess.run(
        ["git", "show", f"{commit}:{file_path}"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    return res.stdout if res.returncode == 0 else None


def _git_list_py_files(repo_path: Path, commit: str) -> List[str]:
    res = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", commit],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return [f for f in res.stdout.splitlines() if f.endswith(".py")]


def _parse_revision_functions(repo_path: Path, commit: str) -> Dict[str, FunctionDefInfo]:
    funcs: Dict[str, FunctionDefInfo] = {}
    for file_path in _git_list_py_files(repo_path, commit):
        source = _git_show_file(repo_path, commit, file_path)
        if source is None:
            continue
        module_name = Path(file_path).with_suffix("").as_posix().replace("/", ".")
        tree = ast.parse(source, filename=file_path)
        visitor = _FunctionVisitor(module_name=module_name, file_path=file_path)
        visitor.visit(tree)
        for fn in visitor.functions:
            funcs[fn.qualified_name] = fn
    return funcs


def _parse_changed_lines(repo_path: Path, base_commit: str, target_commit: str) -> Dict[str, Any]:
    diff_name_status = subprocess.check_output(
        ["git", "diff", "--name-status", base_commit, target_commit],
        cwd=repo_path,
        text=True,
    )
    files_changed: List[Dict[str, str]] = []
    for line in diff_name_status.strip().splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            status_map = {"A": "added", "M": "modified", "D": "deleted", "R": "renamed"}
            files_changed.append({"status": status_map.get(parts[0][0], parts[0]), "file_path": parts[-1]})

    unified = subprocess.check_output(
        ["git", "diff", "-U0", base_commit, target_commit],
        cwd=repo_path,
        text=True,
    )
    changed_lines_by_file: Dict[str, List[int]] = {}
    current_file: Optional[str] = None
    hunk_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
    for line in unified.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:].strip()
            changed_lines_by_file.setdefault(current_file, [])
        elif line.startswith("@@ ") and current_file:
            m = hunk_re.match(line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2)) if m.group(2) is not None else 1
                if count == 0:
                    changed_lines_by_file[current_file].append(start)
                else:
                    changed_lines_by_file[current_file].extend(range(start, start + count))
    return {"files": files_changed, "lines_by_file": changed_lines_by_file}


def run_impact_analysis(repo_path_str: str, base_commit: str, target_commit: str) -> Dict[str, Any]:
    repo_path = Path(repo_path_str).resolve()
    if not (repo_path / ".git").exists():
        raise ValueError(f"Repository path is not a valid Git repository: {repo_path}")

    base_funcs = _parse_revision_functions(repo_path, base_commit)
    target_funcs = _parse_revision_functions(repo_path, target_commit)
    diff_info = _parse_changed_lines(repo_path, base_commit, target_commit)

    changed_functions: List[Dict[str, Any]] = []
    changed_qnames: set[str] = set()

    for qname, base_fn in base_funcs.items():
        if qname not in target_funcs:
            changed_qnames.add(qname)
            changed_functions.append({
                "qualified_name": qname,
                "file_path": base_fn.file_path,
                "change_type": "deleted",
                "line_range": f"{base_fn.start_line}-{base_fn.end_line}",
            })

    for qname, target_fn in target_funcs.items():
        if qname not in base_funcs:
            changed_qnames.add(qname)
            changed_functions.append({
                "qualified_name": qname,
                "file_path": target_fn.file_path,
                "change_type": "added",
                "line_range": f"{target_fn.start_line}-{target_fn.end_line}",
            })
        else:
            file_lines = diff_info["lines_by_file"].get(target_fn.file_path, [])
            if any(target_fn.start_line <= line <= target_fn.end_line for line in file_lines):
                changed_qnames.add(qname)
                changed_functions.append({
                    "qualified_name": qname,
                    "file_path": target_fn.file_path,
                    "change_type": "modified",
                    "line_range": f"{target_fn.start_line}-{target_fn.end_line}",
                })

    graph = nx.DiGraph()
    name_lookup: Dict[str, List[str]] = {}
    for qname, fn in target_funcs.items():
        is_test = Path(fn.file_path).name.startswith("test_") and fn.name.startswith("test_")
        graph.add_node(
            qname,
            label=fn.name,
            file_path=fn.file_path,
            is_test=is_test,
            line_range=f"{fn.start_line}-{fn.end_line}",
        )
        name_lookup.setdefault(fn.name, []).append(qname)

    for qname, base_fn in base_funcs.items():
        if qname not in graph:
            graph.add_node(
                qname,
                label=f"{base_fn.name} (deleted)",
                file_path=base_fn.file_path,
                is_test=False,
                line_range=f"{base_fn.start_line}-{base_fn.end_line}",
            )

    unresolved_calls: List[Dict[str, str]] = []
    edges_payload: List[Dict[str, str]] = []
    for caller_qname, fn in target_funcs.items():
        for raw_call in fn.calls:
            if raw_call.startswith("unresolved."):
                unresolved_calls.append({"caller": caller_qname, "call": raw_call})
                continue
            targets = name_lookup.get(raw_call, [])
            if len(targets) == 1:
                callee_qname = targets[0]
                if callee_qname != caller_qname:
                    graph.add_edge(caller_qname, callee_qname, edge_type="call")
                    edges_payload.append({
                        "source": caller_qname,
                        "target": callee_qname,
                        "edge_type": "call",
                        "resolution": "resolved",
                    })
            elif len(targets) > 1:
                unresolved_calls.append({"caller": caller_qname, "call": f"ambiguous:{raw_call}"})

    affected_qnames: set[str] = set()
    shortest_distances: Dict[str, int] = {}
    evidence_paths: Dict[str, List[str]] = {}
    reverse_graph = graph.reverse(copy=True)
    for changed_qname in changed_qnames:
        if changed_qname not in reverse_graph:
            continue
        lengths = nx.single_source_shortest_path_length(reverse_graph, changed_qname)
        paths = nx.single_source_shortest_path(reverse_graph, changed_qname)
        for node, distance in lengths.items():
            if node not in changed_qnames:
                affected_qnames.add(node)
            if node not in shortest_distances or distance < shortest_distances[node]:
                shortest_distances[node] = distance
                evidence_paths[node] = list(reversed(paths[node]))

    nodes_payload: List[Dict[str, Any]] = []
    for node_id, attrs in graph.nodes(data=True):
        is_test = attrs.get("is_test", False)
        if node_id in changed_qnames:
            status = "changed"
        elif is_test:
            status = "test"
        elif node_id in affected_qnames:
            status = "affected"
        else:
            status = "unaffected"
        nodes_payload.append({
            "id": node_id,
            "label": attrs.get("label", node_id),
            "file_path": attrs.get("file_path", ""),
            "line_range": attrs.get("line_range", ""),
            "status": status,
            "is_test": is_test,
            "impacted": node_id in changed_qnames or node_id in affected_qnames,
            "evidence_path": evidence_paths.get(node_id, []),
        })

    ranked_tests: List[Dict[str, Any]] = []
    for node_id, attrs in graph.nodes(data=True):
        if not attrs.get("is_test", False):
            continue
        distance = shortest_distances.get(node_id)
        if distance is not None:
            direct_overlap = 1.0 if distance == 1 else 0.4
            proximity = round(1.0 / distance, 3)
            churn_prior = 0.65
            score = round(0.50 * direct_overlap + 0.35 * proximity + 0.15 * churn_prior, 3)
            path_str = " -> ".join(evidence_paths.get(node_id, []))
            explanation = f"Direct call/coverage link (hop={distance}) to changed code via [{path_str}]"
            coverage_status = "baseline_mapped"
        else:
            score = 0.05
            path_str = "No static path to changed functions"
            explanation = "No AST call or coverage overlap with modified revision"
            coverage_status = "no_overlap"
        ranked_tests.append({
            "test_id": f"{attrs['file_path']}::{attrs['label']}",
            "qualified_name": node_id,
            "file_path": attrs["file_path"],
            "score": score,
            "score_type": "deterministic_heuristic_v1",
            "ranking_method": "weighted_overlap_proximity_v1",
            "hop_distance": distance,
            "coverage_status": coverage_status,
            "evidence_path": path_str,
            "explanation": explanation,
            "recommended": score >= 0.30,
        })

    ranked_tests.sort(key=lambda test: (-test["score"], test["test_id"]))
    for rank, test in enumerate(ranked_tests, start=1):
        test["rank"] = rank
        test["estimated_seconds"] = round(0.025 + (0.055 * (1.0 - test["score"])), 3)
        test["predicted_risk"] = "HIGH" if test["score"] >= 0.70 else "MEDIUM" if test["score"] >= 0.30 else "LOW"

    for node in nodes_payload:
        node["role"] = {
            "changed": "CHANGED",
            "affected": "AFFECTED",
            "test": "TEST",
            "unaffected": "UNAFFECTED",
        }[node["status"]]
        node["hop_distance"] = shortest_distances.get(node["id"])

    for edge in edges_payload:
        edge["edgeType"] = "CALLS"
        edge["label"] = "calls"

    diff_result = subprocess.run(
        ["git", "diff", "-U3", base_commit, target_commit],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )

    blind_spots: List[Dict[str, str]] = []
    for unresolved in unresolved_calls:
        blind_spots.append({
            "type": "DYNAMIC_DISPATCH",
            "location": unresolved["caller"],
            "detail": f"Call '{unresolved['call']}' could not be resolved statically.",
        })
    isolated_count = sum(1 for test in ranked_tests if test["coverage_status"] == "no_overlap")
    if isolated_count:
        blind_spots.append({
            "type": "UNCOVERED_FUNCTIONS",
            "location": "Regression suite",
            "detail": f"{isolated_count} test(s) have no static overlap with changed code.",
        })

    return {
        "repository_path": str(repo_path),
        "base_commit": base_commit,
        "target_commit": target_commit,
        "base_hash": base_commit[:10],
        "target_hash": target_commit[:10],
        "method_version": "heuristic-ast-v1.0",
        "files_changed": diff_info["files"],
        "changed_files": diff_info["files"],
        "changed_functions": changed_functions,
        "unresolved_calls": unresolved_calls,
        "graph": {"nodes": nodes_payload, "edges": edges_payload},
        "ranked_tests": ranked_tests,
        "diff_preview": diff_result.stdout,
        "blind_spots": blind_spots,
        "limitations": [
            "Advisory only: affected nodes indicate potential reachability, not confirmed defects.",
            "Dynamic Python calls (getattr, reflection, runtime plugins) are marked unresolved.",
            "Test execution requires explicit human approval in a restricted worker.",
        ],
    }


def execute_approved_tests(repo_path_str: str, test_ids: List[str]) -> Dict[str, Any]:
    """Run explicitly approved pytest targets in a restricted subprocess."""
    repo_path = Path(repo_path_str).resolve()
    if not (repo_path / ".git").exists():
        raise ValueError("Invalid repository path.")
    if not test_ids:
        raise ValueError("No tests selected for execution.")

    safe_pattern = re.compile(r"^[a-zA-Z0-9_./-]+::test_[a-zA-Z0-9_]+$")
    for test_id in test_ids:
        if not safe_pattern.match(test_id) or ".." in test_id:
            raise ValueError(f"Unsafe or invalid test identifier blocked: {test_id}")

    proc = subprocess.run(
        ["python", "-m", "pytest", "-v", "--tb=short", *test_ids],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return {
        "status": "passed" if proc.returncode == 0 else "failed",
        "exit_code": proc.returncode,
        "executed_tests": test_ids,
        "stdout": proc.stdout[-4000:],
        "stderr": proc.stderr[-2000:],
    }
