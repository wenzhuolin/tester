"""
Automated test executor for generated pytest suites.

Features:
- Pull latest test code from Git repository
- Run full pytest suite
- Generate JUnit XML report and execution log
- Collect pass/fail/error/skip statistics
- Forward failure details to failure analysis module
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import re
import shlex
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union


LOGGER = logging.getLogger(__name__)

DEFAULT_REPORT_DIR = Path("reports/tests")
DEFAULT_DOCS_ROOT = Path("data/huawei_docs")
DEFAULT_TESTS_PATH = "tests"

URL_PATTERN = re.compile(r"https://support\.huaweicloud\.com/[^\s'\"`<>]+")
TOKEN_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")
COMMON_STOPWORDS = {
    "test",
    "assert",
    "true",
    "false",
    "none",
    "status",
    "code",
    "request",
    "response",
    "failed",
    "error",
    "traceback",
    "line",
    "module",
    "class",
    "function",
    "http",
    "json",
    "python",
    "pytest",
    "generated",
}


@dataclass
class DocSnippet:
    url: str
    title: str
    snippet: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_subprocess(
    command: List[str],
    cwd: Path,
    timeout: Optional[int] = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _git_sync_repo(
    repo_dir: Path,
    repository_url: Optional[str],
    branch: Optional[str],
    pull_latest: bool,
) -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "path": str(repo_dir),
        "repository_url": repository_url,
        "branch": branch,
        "synced": False,
        "action": "none",
    }
    if not pull_latest:
        info["synced"] = True
        info["action"] = "skip_pull"
        return info

    if repo_dir.exists() and not (repo_dir / ".git").exists():
        info["error"] = f"repo_dir exists but is not a git repository: {repo_dir}"
        return info

    if not repo_dir.exists():
        if not repository_url:
            info["error"] = "repository_url is required when repo_dir does not exist"
            return info
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        clone_cmd = ["git", "clone", repository_url, str(repo_dir)]
        if branch:
            clone_cmd = ["git", "clone", "--branch", branch, repository_url, str(repo_dir)]
        clone_res = _run_subprocess(clone_cmd, cwd=repo_dir.parent)
        info["action"] = "clone"
        info["stdout"] = clone_res.stdout
        info["stderr"] = clone_res.stderr
        if clone_res.returncode != 0:
            info["error"] = f"git clone failed with code {clone_res.returncode}"
            return info
        info["synced"] = True
        return info

    # Existing git repo: fetch + pull.
    info["action"] = "pull"
    if repository_url:
        remote_res = _run_subprocess(["git", "remote", "get-url", "origin"], cwd=repo_dir)
        if remote_res.returncode == 0:
            current_remote = remote_res.stdout.strip()
            info["current_remote"] = current_remote
            if current_remote and current_remote != repository_url:
                set_url_res = _run_subprocess(
                    ["git", "remote", "set-url", "origin", repository_url],
                    cwd=repo_dir,
                )
                if set_url_res.returncode != 0:
                    info["error"] = (
                        "failed to set origin URL: "
                        f"{set_url_res.stderr.strip() or set_url_res.stdout.strip()}"
                    )
                    return info

    fetch_cmd = ["git", "fetch", "origin"]
    if branch:
        fetch_cmd = ["git", "fetch", "origin", branch]
    fetch_res = _run_subprocess(fetch_cmd, cwd=repo_dir)
    if fetch_res.returncode != 0:
        info["stdout"] = fetch_res.stdout
        info["stderr"] = fetch_res.stderr
        info["error"] = f"git fetch failed with code {fetch_res.returncode}"
        return info

    if branch:
        checkout_res = _run_subprocess(["git", "checkout", branch], cwd=repo_dir)
        if checkout_res.returncode != 0:
            info["stdout"] = checkout_res.stdout
            info["stderr"] = checkout_res.stderr
            info["error"] = f"git checkout {branch} failed with code {checkout_res.returncode}"
            return info
        pull_cmd = ["git", "pull", "origin", branch]
    else:
        pull_cmd = ["git", "pull"]

    pull_res = _run_subprocess(pull_cmd, cwd=repo_dir)
    info["stdout"] = pull_res.stdout
    info["stderr"] = pull_res.stderr
    if pull_res.returncode != 0:
        info["error"] = f"git pull failed with code {pull_res.returncode}"
        return info

    info["synced"] = True
    return info


def _parse_junit_stats(junit_xml_path: Path) -> Dict[str, int]:
    stats = {"total": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    if not junit_xml_path.exists():
        return stats

    root = ET.fromstring(junit_xml_path.read_text(encoding="utf-8"))
    suites: List[ET.Element] = []
    if root.tag == "testsuite":
        suites = [root]
    elif root.tag == "testsuites":
        suites = list(root.findall("testsuite"))

    for suite in suites:
        tests = int(suite.attrib.get("tests", 0))
        failures = int(suite.attrib.get("failures", 0))
        errors = int(suite.attrib.get("errors", 0))
        skipped = int(suite.attrib.get("skipped", 0))
        stats["total"] += tests
        stats["failed"] += failures
        stats["errors"] += errors
        stats["skipped"] += skipped

    stats["passed"] = max(
        0,
        stats["total"] - stats["failed"] - stats["errors"] - stats["skipped"],
    )
    return stats


def _load_docs_corpus(docs_root: Path) -> Dict[str, Dict[str, str]]:
    corpus: Dict[str, Dict[str, str]] = {}
    if not docs_root.exists():
        return corpus

    for index_path in docs_root.glob("*/index.json"):
        try:
            index_data = json.loads(index_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not isinstance(index_data, dict):
            continue
        pages = index_data.get("pages")
        if not isinstance(pages, dict):
            continue

        for url, page_record in pages.items():
            if not isinstance(url, str) or not isinstance(page_record, dict):
                continue
            file_path = page_record.get("file_path")
            if not isinstance(file_path, str) or not file_path.strip():
                continue
            page_file = Path(file_path)
            if not page_file.exists() or not page_file.is_file():
                continue
            try:
                page_data = json.loads(page_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if not isinstance(page_data, dict):
                continue
            title = str(page_data.get("title") or page_record.get("title") or url)
            full_text = str(page_data.get("full_text") or "")
            corpus[url] = {"title": title, "full_text": full_text}
    return corpus


def _extract_keywords(texts: Iterable[str], limit: int = 20) -> List[str]:
    scores: Dict[str, int] = {}
    for text in texts:
        for token in TOKEN_PATTERN.findall(text or ""):
            token_lower = token.lower()
            if token_lower in COMMON_STOPWORDS or len(token_lower) < 3:
                continue
            scores[token_lower] = scores.get(token_lower, 0) + 1
    sorted_tokens = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    return [token for token, _ in sorted_tokens[:limit]]


def _make_snippet(text: str, keyword: Optional[str], size: int = 260) -> str:
    source = re.sub(r"\s+", " ", text or "").strip()
    if not source:
        return ""
    if keyword:
        index = source.lower().find(keyword.lower())
        if index >= 0:
            start = max(0, index - size // 3)
            end = min(len(source), start + size)
            return source[start:end]
    return source[:size]


def _find_related_doc_snippets(
    docs_corpus: Dict[str, Dict[str, str]],
    test_name: str,
    message: str,
    traceback: str,
    max_snippets: int = 3,
) -> List[Dict[str, str]]:
    if not docs_corpus:
        return []

    urls = set(URL_PATTERN.findall(" ".join([test_name, message, traceback])))
    snippets: List[DocSnippet] = []

    # First priority: explicit URLs in failure output.
    for url in urls:
        if url in docs_corpus:
            doc = docs_corpus[url]
            snippet = _make_snippet(doc.get("full_text", ""), None)
            snippets.append(DocSnippet(url=url, title=doc.get("title", url), snippet=snippet))
        if len(snippets) >= max_snippets:
            break

    if len(snippets) >= max_snippets:
        return [snippet.__dict__ for snippet in snippets]

    keywords = _extract_keywords([test_name, message, traceback], limit=30)
    if not keywords:
        return [snippet.__dict__ for snippet in snippets]

    ranked: List[Tuple[int, str, Dict[str, str], str]] = []
    for url, doc in docs_corpus.items():
        haystack = f"{doc.get('title', '')} {doc.get('full_text', '')}".lower()
        score = 0
        top_keyword = ""
        for keyword in keywords:
            if keyword in haystack:
                score += 1
                if not top_keyword:
                    top_keyword = keyword
        if score > 0:
            ranked.append((score, url, doc, top_keyword))

    ranked.sort(key=lambda item: item[0], reverse=True)
    for _, url, doc, keyword in ranked:
        if any(existing.url == url for existing in snippets):
            continue
        snippets.append(
            DocSnippet(
                url=url,
                title=doc.get("title", url),
                snippet=_make_snippet(doc.get("full_text", ""), keyword),
            )
        )
        if len(snippets) >= max_snippets:
            break

    return [snippet.__dict__ for snippet in snippets]


def _parse_failures(
    junit_xml_path: Path,
    docs_root: Path,
) -> List[Dict[str, Any]]:
    if not junit_xml_path.exists():
        return []

    docs_corpus = _load_docs_corpus(docs_root)
    root = ET.fromstring(junit_xml_path.read_text(encoding="utf-8"))
    testcases = list(root.iter("testcase"))
    failures: List[Dict[str, Any]] = []

    for testcase in testcases:
        failure_node = testcase.find("failure")
        error_node = testcase.find("error")
        node = failure_node if failure_node is not None else error_node
        if node is None:
            continue

        classname = testcase.attrib.get("classname", "")
        name = testcase.attrib.get("name", "")
        message = node.attrib.get("message", "") if node is not None else ""
        traceback_text = (node.text or "").strip() if node is not None else ""
        test_id = "::".join([part for part in [classname, name] if part])

        related_snippets = _find_related_doc_snippets(
            docs_corpus=docs_corpus,
            test_name=name,
            message=message,
            traceback=traceback_text,
        )

        failures.append(
            {
                "test_id": test_id or name,
                "classname": classname,
                "name": name,
                "file": testcase.attrib.get("file"),
                "line": testcase.attrib.get("line"),
                "error_type": "failure" if failure_node is not None else "error",
                "message": message,
                "traceback": traceback_text,
                "related_doc_snippets": related_snippets,
            }
        )
    return failures


def _resolve_failure_analyzer(
    custom_failure_analyzer: Optional[Callable[[Dict[str, Any]], Any]],
) -> Optional[Callable[[Dict[str, Any]], Any]]:
    if callable(custom_failure_analyzer):
        return custom_failure_analyzer

    candidates = [
        ("failure_analyzer", "analyze_failures"),
        ("failure_analyzer", "analyze_failed_cases"),
        ("failure_analysis", "analyze_failures"),
        ("failed_case_analyzer", "analyze_failures"),
    ]
    for module_name, func_name in candidates:
        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue
        func = getattr(module, func_name, None)
        if callable(func):
            return func
    return None


def _invoke_failure_analyzer(
    failures: List[Dict[str, Any]],
    context: Dict[str, Any],
    custom_failure_analyzer: Optional[Callable[[Dict[str, Any]], Any]] = None,
) -> Dict[str, Any]:
    if not failures:
        return {"status": "skipped", "reason": "no failed tests"}

    analyzer = _resolve_failure_analyzer(custom_failure_analyzer)
    if not callable(analyzer):
        return {"status": "skipped", "reason": "failure analysis module not found"}

    payload = {"failures": failures, "context": context}
    try:
        result = analyzer(payload)
        return {"status": "triggered", "result": result}
    except TypeError:
        # Backward compatibility for analyzers expecting failures list only.
        try:
            result = analyzer(failures)
            return {"status": "triggered", "result": result}
        except Exception as exc:
            return {"status": "failed", "error": str(exc)}
    except Exception as exc:
        return {"status": "failed", "error": str(exc)}


def _prepare_report_paths(report_dir: Path) -> Tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    junit_xml = report_dir / f"junit_{stamp}.xml"
    log_file = report_dir / f"pytest_{stamp}.log"
    return junit_xml, log_file


def run_tests(
    repository_url: Optional[str] = None,
    repo_dir: Union[str, Path] = ".",
    branch: Optional[str] = None,
    tests_path: str = DEFAULT_TESTS_PATH,
    report_dir: Union[str, Path] = DEFAULT_REPORT_DIR,
    docs_root: Union[str, Path] = DEFAULT_DOCS_ROOT,
    pytest_args: Optional[List[str]] = None,
    pull_latest: bool = True,
    failure_analyzer: Optional[Callable[[Dict[str, Any]], Any]] = None,
    timeout: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Pull latest tests, run full pytest suite, and return result statistics.
    """
    started_at = _now_iso()
    repo_path = Path(repo_dir).resolve()
    report_path = Path(report_dir).resolve()
    docs_path = Path(docs_root).resolve()

    sync_info = _git_sync_repo(
        repo_dir=repo_path,
        repository_url=repository_url,
        branch=branch,
        pull_latest=pull_latest,
    )
    if not sync_info.get("synced"):
        return {
            "started_at": started_at,
            "finished_at": _now_iso(),
            "success": False,
            "sync": sync_info,
            "stats": {"total": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0},
            "junit_xml": None,
            "log_file": None,
            "failures": [],
            "failure_analysis": {"status": "skipped", "reason": "sync failed"},
            "error": sync_info.get("error"),
        }

    junit_xml_path, log_file_path = _prepare_report_paths(report_path)
    command = ["python3", "-m", "pytest", tests_path, f"--junitxml={junit_xml_path}"]
    if pytest_args:
        command.extend(pytest_args)

    start_ts = time.monotonic()
    try:
        execution = _run_subprocess(command=command, cwd=repo_path, timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        combined_output = (
            f"Command timed out after {timeout} seconds.\n\n"
            f"STDOUT:\n{stdout}\n\nSTDERR:\n{stderr}\n"
        )
        log_file_path.write_text(combined_output, encoding="utf-8")
        return {
            "started_at": started_at,
            "finished_at": _now_iso(),
            "success": False,
            "sync": sync_info,
            "stats": {"total": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0},
            "junit_xml": str(junit_xml_path),
            "log_file": str(log_file_path),
            "failures": [],
            "failure_analysis": {"status": "skipped", "reason": "pytest timed out"},
            "timeout": True,
            "duration_seconds": round(time.monotonic() - start_ts, 3),
            "error": str(exc),
            "command": shlex.join(command),
        }

    combined_output = (
        f"COMMAND: {shlex.join(command)}\n"
        f"EXIT_CODE: {execution.returncode}\n\n"
        f"STDOUT:\n{execution.stdout}\n\n"
        f"STDERR:\n{execution.stderr}\n"
    )
    log_file_path.write_text(combined_output, encoding="utf-8")

    stats = _parse_junit_stats(junit_xml_path)
    failures = _parse_failures(junit_xml_path=junit_xml_path, docs_root=docs_path)
    no_tests_collected = execution.returncode == 5
    failure_analysis = _invoke_failure_analyzer(
        failures=failures,
        context={
            "repository_url": repository_url,
            "repo_dir": str(repo_path),
            "junit_xml": str(junit_xml_path),
            "log_file": str(log_file_path),
            "stats": stats,
            "return_code": execution.returncode,
            "no_tests_collected": no_tests_collected,
        },
        custom_failure_analyzer=failure_analyzer,
    )

    success = (
        execution.returncode == 0
        and stats["failed"] == 0
        and stats["errors"] == 0
    )

    return {
        "started_at": started_at,
        "finished_at": _now_iso(),
        "duration_seconds": round(time.monotonic() - start_ts, 3),
        "success": success,
        "sync": sync_info,
        "command": shlex.join(command),
        "return_code": execution.returncode,
        "no_tests_collected": no_tests_collected,
        "stats": stats,
        "junit_xml": str(junit_xml_path),
        "log_file": str(log_file_path),
        "failures": failures,
        "failure_analysis": failure_analysis,
        "timeout": timed_out,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run full pytest tests with reporting")
    parser.add_argument("--repo-url", default=None, help="Git repository URL")
    parser.add_argument("--repo-dir", default=".", help="Local repository directory")
    parser.add_argument("--branch", default=None, help="Branch name to pull")
    parser.add_argument("--tests-path", default=DEFAULT_TESTS_PATH, help="Pytest target path")
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR), help="Report output dir")
    parser.add_argument("--docs-root", default=str(DEFAULT_DOCS_ROOT), help="Docs root directory")
    parser.add_argument(
        "--no-pull",
        action="store_true",
        help="Skip git pull/clone and run tests directly",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="Timeout in seconds for pytest execution",
    )
    parser.add_argument(
        "--pytest-args",
        default="",
        help="Additional pytest args as shell-like string",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logger level",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level))

    extra_args = shlex.split(args.pytest_args) if args.pytest_args else None
    summary = run_tests(
        repository_url=args.repo_url,
        repo_dir=args.repo_dir,
        branch=args.branch,
        tests_path=args.tests_path,
        report_dir=args.report_dir,
        docs_root=args.docs_root,
        pytest_args=extra_args,
        pull_latest=not args.no_pull,
        timeout=args.timeout,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
