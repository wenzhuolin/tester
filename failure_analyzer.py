"""
Failure analyzer powered by Cursor Background Agent API.

Responsibilities:
- Consume failed test details and related document snippets
- Build temporary context file for Cursor
- Ask Cursor to analyze root causes and propose fixes
- Parse/normalize analysis result into structured output
- Optionally sync changed test files and auto-commit local changes
- Attach analysis artifact to test report for notification modules
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

from cursor_api_client import CursorAPIClient


LOGGER = logging.getLogger(__name__)

DEFAULT_REPORT_DIR = Path("reports/tests")
DEFAULT_WAIT_TIMEOUT = 1800
DEFAULT_POLL_INTERVAL = 5

JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL | re.IGNORECASE)
BRACE_BLOCK_RE = re.compile(r"(\{[\s\S]*\})")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_string(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _normalize_failure_item(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "test_id": _normalize_string(item.get("test_id") or item.get("name")),
        "name": _normalize_string(item.get("name")),
        "classname": _normalize_string(item.get("classname")),
        "file": _normalize_string(item.get("file")),
        "line": _normalize_string(item.get("line")),
        "error_type": _normalize_string(item.get("error_type") or "failure"),
        "message": _normalize_string(item.get("message")),
        "traceback": _normalize_string(item.get("traceback")),
        "related_doc_snippets": (
            item.get("related_doc_snippets")
            if isinstance(item.get("related_doc_snippets"), list)
            else []
        ),
    }


def _normalize_input(
    payload: Union[Dict[str, Any], List[Dict[str, Any]]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if isinstance(payload, dict):
        if isinstance(payload.get("failures"), list):
            failures = [
                _normalize_failure_item(item)
                for item in payload["failures"]
                if isinstance(item, dict)
            ]
            context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
            return failures, context

        # Single failure dict mode.
        possible_failure_keys = {"test_id", "name", "message", "traceback"}
        if any(key in payload for key in possible_failure_keys):
            return [_normalize_failure_item(payload)], {}
        return [], payload

    if isinstance(payload, list):
        failures = [_normalize_failure_item(item) for item in payload if isinstance(item, dict)]
        return failures, {}

    return [], {}


def _build_instruction(failure_count: int, context_json_path: str) -> str:
    return f"""
分析以下测试失败的原因，并给出修复建议。如果需要修改代码，请直接修改对应的测试文件。

失败上下文文件：{context_json_path}
失败用例数量：{failure_count}

要求：
1) 对每个失败用例给出错误类型（error_type）和根本原因（root_cause）。
2) 给出可执行的修复建议（fix_suggestions）。
3) 明确建议修改的代码位置或文档位置（code_locations/doc_locations）。
4) 如果你修改了测试文件，请在结果中列出 modified_test_files。

请严格返回 JSON，格式如下：
{{
  "analyses": [
    {{
      "test_id": "tests.test_xxx::test_yyy",
      "error_type": "assertion_error|api_contract_mismatch|env_issue|other",
      "root_cause": "根因分析",
      "fix_suggestions": [
        "建议1",
        "建议2"
      ],
      "code_locations": [
        "tests/generated/test_xxx.py:23"
      ],
      "doc_locations": [
        "https://support.huaweicloud.com/ecs/..."
      ],
      "proposed_patch": "可选：建议代码片段"
    }}
  ],
  "modified_test_files": [
    "tests/generated/test_xxx.py"
  ],
  "summary": "整体结论"
}}
""".strip()


def _extract_json_from_text(text: str) -> Optional[Dict[str, Any]]:
    content = (text or "").strip()
    if not content:
        return None

    # Direct JSON.
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # JSON fenced block.
    match = JSON_BLOCK_RE.search(content)
    if match:
        block = match.group(1).strip()
        try:
            parsed = json.loads(block)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    # Best effort first balanced-like object region.
    match = BRACE_BLOCK_RE.search(content)
    if match:
        block = match.group(1).strip()
        try:
            parsed = json.loads(block)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    return None


def _extract_cursor_text(task_result: Dict[str, Any]) -> str:
    texts: List[str] = []

    result = task_result.get("result")
    if isinstance(result, dict):
        conversation = result.get("conversation")
        if isinstance(conversation, dict):
            messages = conversation.get("messages")
            if isinstance(messages, list):
                for message in messages:
                    if not isinstance(message, dict):
                        continue
                    if message.get("type") != "assistant_message":
                        continue
                    text = message.get("text")
                    if isinstance(text, str) and text.strip():
                        texts.append(text.strip())

    summary = task_result.get("summary")
    if isinstance(summary, str) and summary.strip():
        texts.append(summary.strip())

    return "\n\n".join(texts).strip()


def _normalize_analysis_entries(
    parsed_json: Optional[Dict[str, Any]],
    failures: List[Dict[str, Any]],
    fallback_text: str,
) -> List[Dict[str, Any]]:
    analyses = parsed_json.get("analyses") if isinstance(parsed_json, dict) else None
    entries: List[Dict[str, Any]] = []

    if isinstance(analyses, list):
        for item in analyses:
            if not isinstance(item, dict):
                continue
            entries.append(
                {
                    "test_id": _normalize_string(item.get("test_id")),
                    "error_type": _normalize_string(item.get("error_type") or "other"),
                    "root_cause": _normalize_string(item.get("root_cause")),
                    "fix_suggestions": (
                        [str(x) for x in item.get("fix_suggestions", []) if str(x).strip()]
                        if isinstance(item.get("fix_suggestions"), list)
                        else []
                    ),
                    "code_locations": (
                        [str(x) for x in item.get("code_locations", []) if str(x).strip()]
                        if isinstance(item.get("code_locations"), list)
                        else []
                    ),
                    "doc_locations": (
                        [str(x) for x in item.get("doc_locations", []) if str(x).strip()]
                        if isinstance(item.get("doc_locations"), list)
                        else []
                    ),
                    "proposed_patch": _normalize_string(item.get("proposed_patch")),
                }
            )

    if entries:
        return entries

    # Fallback: map each failure to a coarse entry from raw assistant text.
    fallback_summary = fallback_text[:1200] if fallback_text else "No structured analysis returned."
    for failure in failures:
        entries.append(
            {
                "test_id": failure.get("test_id") or failure.get("name"),
                "error_type": failure.get("error_type") or "failure",
                "root_cause": fallback_summary,
                "fix_suggestions": [],
                "code_locations": [failure.get("file")] if failure.get("file") else [],
                "doc_locations": [
                    snippet.get("url")
                    for snippet in failure.get("related_doc_snippets", [])
                    if isinstance(snippet, dict) and isinstance(snippet.get("url"), str)
                ],
                "proposed_patch": "",
            }
        )
    return entries


def _extract_modified_files(
    task_result: Dict[str, Any],
    parsed_json: Optional[Dict[str, Any]],
) -> List[str]:
    files: List[str] = []

    task_level = task_result.get("modified_files")
    if isinstance(task_level, list):
        files.extend([str(path).strip() for path in task_level if str(path).strip()])

    if isinstance(parsed_json, dict):
        parsed_files = parsed_json.get("modified_test_files")
        if isinstance(parsed_files, list):
            files.extend([str(path).strip() for path in parsed_files if str(path).strip()])

    # Deduplicate.
    deduped: List[str] = []
    seen = set()
    for path in files:
        if path not in seen:
            seen.add(path)
            deduped.append(path)
    return deduped


def _filter_test_files(paths: Iterable[str]) -> List[str]:
    out: List[str] = []
    for raw in paths:
        path = raw.replace("\\", "/").lstrip("/")
        if path.startswith("tests/") and path.endswith(".py"):
            out.append(path)
    # Dedup.
    deduped: List[str] = []
    seen = set()
    for path in out:
        if path not in seen:
            seen.add(path)
            deduped.append(path)
    return deduped


def _run_command(command: List[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd),
        check=False,
        capture_output=True,
        text=True,
    )


def _sync_modified_files_from_branch(
    repo_dir: Path,
    repository_url: Optional[str],
    branch: Optional[str],
    modified_test_files: List[str],
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "status": "skipped",
        "copied_files": [],
    }
    if not repository_url or not branch or not modified_test_files:
        result["reason"] = "missing repository_url/branch/modified files"
        return result
    if not repo_dir.exists() or not (repo_dir / ".git").exists():
        result["reason"] = "repo_dir is not a git repo"
        return result

    with tempfile.TemporaryDirectory(prefix="cursor_failure_sync_") as temp_dir:
        clone_dir = Path(temp_dir) / "repo"
        clone_res = _run_command(
            ["git", "clone", "--depth", "1", "--branch", branch, repository_url, str(clone_dir)],
            cwd=Path(temp_dir),
        )
        if clone_res.returncode != 0:
            result["status"] = "failed"
            result["error"] = (
                f"git clone failed: {clone_res.stderr.strip() or clone_res.stdout.strip()}"
            )
            return result

        copied: List[str] = []
        for relative in modified_test_files:
            source = clone_dir / relative
            target = repo_dir / relative
            if not source.exists() or not source.is_file():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            copied.append(relative)

    result["status"] = "synced"
    result["copied_files"] = copied
    return result


def _commit_test_changes(
    repo_dir: Path,
    modified_test_files: List[str],
    commit_message: str = "chore: apply Cursor failure fix suggestions",
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "status": "skipped",
        "committed_files": [],
    }
    if not modified_test_files:
        result["reason"] = "no modified test files"
        return result
    if not repo_dir.exists() or not (repo_dir / ".git").exists():
        result["reason"] = "repo_dir is not a git repository"
        return result

    status_res = _run_command(["git", "status", "--porcelain", "--"] + modified_test_files, cwd=repo_dir)
    if status_res.returncode != 0:
        result["status"] = "failed"
        result["error"] = status_res.stderr.strip() or status_res.stdout.strip()
        return result
    if not status_res.stdout.strip():
        result["reason"] = "no local changes detected for modified test files"
        return result

    add_res = _run_command(["git", "add", "--"] + modified_test_files, cwd=repo_dir)
    if add_res.returncode != 0:
        result["status"] = "failed"
        result["error"] = add_res.stderr.strip() or add_res.stdout.strip()
        return result

    commit_res = _run_command(["git", "commit", "-m", commit_message], cwd=repo_dir)
    if commit_res.returncode != 0:
        result["status"] = "failed"
        result["error"] = commit_res.stderr.strip() or commit_res.stdout.strip()
        return result

    result["status"] = "committed"
    result["committed_files"] = modified_test_files
    result["commit_output"] = commit_res.stdout.strip()
    return result


def _attach_analysis_to_report(
    analysis: Dict[str, Any],
    context: Dict[str, Any],
    report_dir: Path = DEFAULT_REPORT_DIR,
) -> Dict[str, Any]:
    junit_xml = context.get("junit_xml")
    log_file = context.get("log_file")

    target_dir = report_dir
    if isinstance(junit_xml, str) and junit_xml.strip():
        target_dir = Path(junit_xml).resolve().parent
    elif isinstance(log_file, str) and log_file.strip():
        target_dir = Path(log_file).resolve().parent

    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    analysis_file = target_dir / f"failure_analysis_{stamp}.json"
    analysis_file.write_text(
        json.dumps(analysis, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Attach as a readable appendix in log file.
    if isinstance(log_file, str) and log_file.strip():
        log_path = Path(log_file)
        try:
            with log_path.open("a", encoding="utf-8") as log:
                log.write("\n\n==== FAILURE ANALYSIS ====\n")
                log.write(json.dumps(analysis, ensure_ascii=False, indent=2))
                log.write("\n")
        except OSError:
            pass

    return {"analysis_file": str(analysis_file)}


def analyze_failures(
    payload: Union[Dict[str, Any], List[Dict[str, Any]]],
    repository_url: Optional[str] = None,
    repo_dir: Union[str, Path] = ".",
    cursor_client: Optional[CursorAPIClient] = None,
    wait_timeout: int = DEFAULT_WAIT_TIMEOUT,
    poll_interval: int = DEFAULT_POLL_INTERVAL,
    auto_commit: bool = True,
) -> Dict[str, Any]:
    """
    Analyze failed tests with Cursor API and provide structured fix suggestions.
    """
    failures, context = _normalize_input(payload)
    if not failures:
        return {
            "status": "skipped",
            "reason": "no failures provided",
            "analyses": [],
            "attached_report": None,
        }

    repo_url = (
        repository_url
        or _normalize_string(context.get("repository_url"))
        or os.getenv("TEST_REPOSITORY_URL", "").strip()
        or os.getenv("TARGET_TEST_REPO_URL", "").strip()
    )
    if not repo_url:
        return {
            "status": "failed",
            "error": "repository_url is required (argument/context/env)",
            "analyses": [],
            "attached_report": None,
        }

    client = cursor_client or CursorAPIClient.from_env()

    temp_path_obj: Optional[Path] = None
    task_result: Dict[str, Any] = {}
    parsed_json: Optional[Dict[str, Any]] = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            prefix="failure_context_",
            delete=False,
            encoding="utf-8",
        ) as temp_file:
            payload_data = {
                "generated_at": _now_iso(),
                "repository_url": repo_url,
                "context": context,
                "failures": failures,
            }
            json.dump(payload_data, temp_file, ensure_ascii=False, indent=2)
            temp_path_obj = Path(temp_file.name)

        instruction = _build_instruction(
            failure_count=len(failures),
            context_json_path=str(temp_path_obj),
        )
        task_id = client.create_agent_task(
            repository=repo_url,
            instruction=instruction,
            context_files=[str(temp_path_obj)],
        )
        task_result = client.get_task_result(
            task_id=task_id,
            wait=True,
            poll_interval=poll_interval,
            timeout=wait_timeout,
        )
        status = _normalize_string(task_result.get("status")).lower()
        if status not in {"succeeded", "success", "finished", "completed"}:
            analysis = {
                "status": "failed",
                "task_id": task_id,
                "cursor_status": status,
                "error": task_result.get("error"),
                "analyses": [],
            }
            attach = _attach_analysis_to_report(analysis=analysis, context=context)
            analysis["attached_report"] = attach
            return analysis

        cursor_text = _extract_cursor_text(task_result)
        parsed_json = _extract_json_from_text(cursor_text)
        normalized_entries = _normalize_analysis_entries(
            parsed_json=parsed_json,
            failures=failures,
            fallback_text=cursor_text,
        )
        modified_files = _extract_modified_files(task_result, parsed_json)
        modified_test_files = _filter_test_files(modified_files)

        repo_path = Path(repo_dir).resolve()
        branch = _normalize_string(task_result.get("branch")) or None
        sync_result = _sync_modified_files_from_branch(
            repo_dir=repo_path,
            repository_url=repo_url,
            branch=branch,
            modified_test_files=modified_test_files,
        )
        commit_result = (
            _commit_test_changes(repo_dir=repo_path, modified_test_files=modified_test_files)
            if auto_commit
            else {"status": "skipped", "reason": "auto_commit disabled"}
        )

        analysis = {
            "status": "succeeded",
            "task_id": task_id,
            "cursor_status": status,
            "summary": (
                parsed_json.get("summary")
                if isinstance(parsed_json, dict) and isinstance(parsed_json.get("summary"), str)
                else _normalize_string(task_result.get("summary"))
            ),
            "error_type": "multi" if len(normalized_entries) > 1 else (
                normalized_entries[0]["error_type"] if normalized_entries else "unknown"
            ),
            "root_cause": (
                normalized_entries[0]["root_cause"] if normalized_entries else ""
            ),
            "analyses": normalized_entries,
            "modified_files": modified_files,
            "modified_test_files": modified_test_files,
            "suggested_code_locations": [
                loc
                for entry in normalized_entries
                for loc in entry.get("code_locations", [])
                if isinstance(loc, str) and loc.strip()
            ],
            "suggested_doc_locations": [
                loc
                for entry in normalized_entries
                for loc in entry.get("doc_locations", [])
                if isinstance(loc, str) and loc.strip()
            ],
            "sync_result": sync_result,
            "commit_result": commit_result,
            "cursor_task": task_result,
        }
        attach = _attach_analysis_to_report(analysis=analysis, context=context)
        analysis["attached_report"] = attach
        return analysis
    finally:
        if temp_path_obj and temp_path_obj.exists():
            try:
                temp_path_obj.unlink()
            except OSError:
                LOGGER.warning("Failed to remove temp failure context file: %s", temp_path_obj)


def analyze_failed_cases(
    payload: Union[Dict[str, Any], List[Dict[str, Any]]],
    **kwargs: Any,
) -> Dict[str, Any]:
    """
    Alias for compatibility with other modules.
    """
    return analyze_failures(payload, **kwargs)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze failed tests with Cursor API")
    parser.add_argument(
        "--input-json",
        required=True,
        help="Path to JSON payload containing failures and optional context",
    )
    parser.add_argument("--repo-url", default=None, help="Target repository URL")
    parser.add_argument("--repo-dir", default=".", help="Local git repository path")
    parser.add_argument(
        "--no-auto-commit",
        action="store_true",
        help="Disable auto commit for modified test files",
    )
    parser.add_argument(
        "--wait-timeout",
        type=int,
        default=DEFAULT_WAIT_TIMEOUT,
        help="Cursor task wait timeout in seconds",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=DEFAULT_POLL_INTERVAL,
        help="Cursor task poll interval in seconds",
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

    input_path = Path(args.input_json)
    if not input_path.exists():
        raise FileNotFoundError(f"Input JSON not found: {input_path}")

    payload = json.loads(input_path.read_text(encoding="utf-8"))
    result = analyze_failures(
        payload=payload,
        repository_url=args.repo_url,
        repo_dir=args.repo_dir,
        wait_timeout=args.wait_timeout,
        poll_interval=args.poll_interval,
        auto_commit=not args.no_auto_commit,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
