"""
Generate pytest cases from Huawei Cloud docs by calling Cursor Background Agent API.
"""

from __future__ import annotations

import argparse
import inspect
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:
    from cursor_api_client import CursorAPIClient  # type: ignore
except Exception:  # pragma: no cover
    CursorAPIClient = Any  # type: ignore


LOGGER = logging.getLogger(__name__)
DEFAULT_OUTPUT_DIR = Path("tests/generated")
DEFAULT_MANIFEST_FILE = "generated_cases_manifest.json"
SUCCESS_STATUSES = {"succeeded", "success", "finished", "completed"}


@dataclass
class GeneratedFile:
    """Generated test file payload."""

    path: str
    content: Optional[str] = None
    source_path: Optional[str] = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_task_id(task_response: Any) -> str:
    if isinstance(task_response, str):
        return task_response
    if isinstance(task_response, dict):
        for key in ("task_id", "id"):
            value = task_response.get(key)
            if value:
                return str(value)
    for attr in ("task_id", "id"):
        value = getattr(task_response, attr, None)
        if value:
            return str(value)
    raise ValueError(f"Could not extract task id from response: {task_response!r}")


def _extract_nested_lists(payload: Any, keys: set[str]) -> List[Any]:
    collected: List[Any] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in keys and isinstance(value, list):
                    collected.extend(value)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return collected


def _normalize_path(raw_path: str) -> str:
    return raw_path.replace("\\", "/").lstrip("/")


def _sanitize_target_path(raw_path: str, output_dir: Path) -> Path:
    normalized = _normalize_path(raw_path)
    if normalized.startswith("tests/generated/"):
        normalized = normalized[len("tests/generated/") :]

    if not normalized:
        relative_path = Path("test_generated_api_cases.py")
    else:
        safe_parts = [part for part in Path(normalized).parts if part not in ("..", ".")]
        if not safe_parts:
            safe_parts = [Path(normalized).name or "test_generated_api_cases.py"]
        relative_path = Path(*safe_parts)

    return output_dir / relative_path


def _extract_generated_files_from_result(task_result: Dict[str, Any]) -> List[GeneratedFile]:
    file_items = _extract_nested_lists(
        task_result,
        keys={"generated_files", "artifacts", "files"},
    )
    generated: List[GeneratedFile] = []

    for item in file_items:
        if not isinstance(item, dict):
            continue
        path = str(
            item.get("path")
            or item.get("file_path")
            or item.get("name")
            or "test_generated_api_cases.py"
        )
        content = item.get("content") or item.get("code")
        source_path = item.get("source_path")
        if content is None and not source_path:
            continue
        generated.append(GeneratedFile(path=path, content=content, source_path=source_path))

    raw_result = task_result.get("result")
    if (
        not generated
        and isinstance(raw_result, str)
        and raw_result.strip()
    ):
        generated.append(
            GeneratedFile(path="test_generated_api_cases.py", content=raw_result)
        )
    return generated


def _extract_modified_files(task_result: Dict[str, Any]) -> List[str]:
    modified = task_result.get("modified_files")
    if isinstance(modified, list):
        values = [str(item).strip() for item in modified if str(item).strip()]
        if values:
            return values

    items = _extract_nested_lists(
        task_result,
        keys={"modified_files", "changed_files", "changedFiles", "files"},
    )
    paths: List[str] = []
    for item in items:
        if isinstance(item, str) and item.strip():
            paths.append(item.strip())
            continue
        if isinstance(item, dict):
            path = item.get("path") or item.get("file_path") or item.get("name")
            if isinstance(path, str) and path.strip():
                paths.append(path.strip())

    deduped: List[str] = []
    seen = set()
    for path in paths:
        if path not in seen:
            seen.add(path)
            deduped.append(path)
    return deduped


def _extract_case_list(task_result: Dict[str, Any]) -> List[str]:
    case_items = _extract_nested_lists(task_result, keys={"case_list", "generated_cases"})
    cases = [str(item).strip() for item in case_items if str(item).strip()]
    deduped: List[str] = []
    seen = set()
    for case_name in cases:
        if case_name not in seen:
            seen.add(case_name)
            deduped.append(case_name)
    return deduped


def _extract_task_branch(task_result: Dict[str, Any]) -> Optional[str]:
    branch = task_result.get("branch")
    if isinstance(branch, str) and branch.strip():
        return branch.strip()

    result_data = task_result.get("result")
    if isinstance(result_data, dict):
        agent = result_data.get("agent")
        if isinstance(agent, dict):
            target = agent.get("target")
            if isinstance(target, dict):
                branch_name = target.get("branchName")
                if isinstance(branch_name, str) and branch_name.strip():
                    return branch_name.strip()
    return None


def _invoke_cursor_task(
    client: CursorAPIClient,
    repository_url: str,
    instruction: str,
    context_json_path: str,
) -> str:
    create_agent_task = getattr(client, "create_agent_task", None)
    if callable(create_agent_task):
        task_id = create_agent_task(
            repository=repository_url,
            instruction=instruction,
            context_files=[context_json_path],
        )
        return str(task_id)

    create_task = getattr(client, "create_task", None)
    if callable(create_task):
        task_response = create_task(
            repository_url=repository_url,
            instruction=instruction,
            context_files=[context_json_path],
            task_type="generate_pytest_cases",
            metadata={
                "source": "huawei_cloud_docs",
                "generator": "case_generator.py",
                "created_at": _utc_now_iso(),
            },
        )
        return _extract_task_id(task_response)

    raise AttributeError(
        "CursorAPIClient must implement create_agent_task or create_task"
    )


def _wait_cursor_task_result(
    client: CursorAPIClient,
    task_id: str,
    poll_interval: int = 5,
    timeout: int = 1800,
) -> Dict[str, Any]:
    get_task_result = getattr(client, "get_task_result", None)
    if callable(get_task_result):
        signature = inspect.signature(get_task_result)
        kwargs: Dict[str, Any] = {"task_id": task_id, "wait": True}
        if "poll_interval" in signature.parameters:
            kwargs["poll_interval"] = poll_interval
        if "timeout" in signature.parameters:
            kwargs["timeout"] = timeout
        result = get_task_result(**kwargs)
        return result if isinstance(result, dict) else {"status": "unknown", "result": result}

    wait_method = getattr(client, "wait_for_task_completion", None)
    if callable(wait_method):
        result = wait_method(task_id=task_id, poll_interval=poll_interval, timeout=timeout)
        return result if isinstance(result, dict) else {"status": "unknown", "result": result}

    raise AttributeError(
        "CursorAPIClient must implement get_task_result(wait=True) or wait_for_task_completion"
    )


def _write_generated_files_from_payload(
    generated_files: Iterable[GeneratedFile],
    output_dir: Path,
) -> List[str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written_paths: List[str] = []

    for item in generated_files:
        target_path = _sanitize_target_path(item.path, output_dir)
        target_path.parent.mkdir(parents=True, exist_ok=True)

        if item.content is not None:
            target_path.write_text(item.content, encoding="utf-8")
            written_paths.append(str(target_path))
            continue

        if item.source_path:
            source = Path(item.source_path)
            if source.exists() and source.is_file():
                target_path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
                written_paths.append(str(target_path))

    return written_paths


def _merge_files_from_task_branch(
    repository_url: str,
    branch: Optional[str],
    modified_files: List[str],
    output_dir: Path,
) -> List[str]:
    if not branch:
        return []

    candidate_paths: List[str] = []
    for path in modified_files:
        normalized = _normalize_path(path)
        if normalized.startswith("tests/generated/") and normalized.endswith(".py"):
            candidate_paths.append(normalized)

    if not candidate_paths:
        return []

    output_dir.mkdir(parents=True, exist_ok=True)
    copied: List[str] = []

    with tempfile.TemporaryDirectory(prefix="cursor_agent_repo_") as temp_dir:
        temp_repo = Path(temp_dir) / "repo"
        command = [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            branch,
            repository_url,
            str(temp_repo),
        ]
        process = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
        if process.returncode != 0:
            raise RuntimeError(
                "Failed to clone task branch from repository. "
                f"branch={branch}, stderr={process.stderr.strip()}"
            )

        for source_rel in candidate_paths:
            source_path = temp_repo / source_rel
            if not source_path.exists() or not source_path.is_file():
                LOGGER.warning("Generated file not found in task branch: %s", source_rel)
                continue

            target_path = _sanitize_target_path(source_rel, output_dir)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_path, target_path)
            copied.append(str(target_path))

    return copied


def _derive_case_list_from_files(generated_paths: Iterable[str]) -> List[str]:
    pattern = re.compile(r"^\s*def\s+(test_[a-zA-Z0-9_]+)\s*\(", re.MULTILINE)
    names: List[str] = []

    for file_path in generated_paths:
        path_obj = Path(file_path)
        if not path_obj.exists() or not path_obj.is_file():
            continue
        content = path_obj.read_text(encoding="utf-8")
        names.extend(pattern.findall(content))

    deduped: List[str] = []
    seen = set()
    for name in names:
        if name not in seen:
            seen.add(name)
            deduped.append(name)
    return deduped


def _update_manifest(
    manifest_path: Path,
    generated_paths: List[str],
    case_list: List[str],
    repository_url: str,
    context_json_path: str,
    task_id: str,
    task_result: Dict[str, Any],
) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest: Dict[str, Any] = {"runs": []}
    if manifest_path.exists():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                manifest = loaded
        except json.JSONDecodeError:
            manifest = {"runs": []}

    runs = manifest.get("runs")
    if not isinstance(runs, list):
        runs = []
        manifest["runs"] = runs

    runs.append(
        {
            "generated_at": _utc_now_iso(),
            "task_id": task_id,
            "task_status": task_result.get("status"),
            "repository_url": repository_url,
            "branch": task_result.get("branch"),
            "agent_url": task_result.get("agent_url"),
            "pr_url": task_result.get("pr_url"),
            "context_json_path": context_json_path,
            "generated_files": generated_paths,
            "generated_cases": case_list,
            "error": task_result.get("error"),
        }
    )

    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def build_agent_instruction(repository_url: str, context_json_path: str) -> str:
    return f"""
You are an expert QA automation engineer.

Repository: {repository_url}
Context file: {context_json_path}

Please read the structured Huawei Cloud API documentation in the context JSON and generate pytest test files.

Requirements:
1) For each API operation in the document, generate an independent pytest test function.
2) Each test must include:
   - authentication handling (headers/token placeholder if secret not available),
   - request construction according to endpoint/method/parameters,
   - assertions based on documented examples (status code and key response fields).
3) Follow pytest conventions:
   - filename pattern: tests/generated/test_<service>_<operation>.py
   - function pattern: test_<service>_<operation>
   - fixtures for shared setup
   - clear Arrange/Act/Assert structure
4) Keep tests deterministic; if documentation is ambiguous, add explicit assumptions.
5) Ensure generated tests are placed in tests/generated/ in the repository.

If possible, include machine-readable output in your final response:
{{
  "case_list": ["test_<service>_<operation>"],
  "generated_files": [
    {{
      "path": "tests/generated/test_<service>_<operation>.py",
      "content": "<python code>"
    }}
  ]
}}
""".strip()


def generate_test_cases(
    doc_data: Dict[str, Any] | List[Any],
    repository_url: str,
    cursor_client: CursorAPIClient,
    output_dir: str = str(DEFAULT_OUTPUT_DIR),
    manifest_file: str = DEFAULT_MANIFEST_FILE,
) -> List[str]:
    """
    Generate pytest cases with Cursor API and merge results into tests/generated.
    """
    if not isinstance(doc_data, (dict, list)):
        raise TypeError("doc_data must be structured documentation content as dict or list")
    if not repository_url.strip():
        raise ValueError("repository_url must not be empty")

    output_path = Path(output_dir)
    manifest_path = output_path / manifest_file
    context_path: Optional[Path] = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            prefix="huawei_doc_context_",
            delete=False,
            encoding="utf-8",
        ) as temp_file:
            json.dump(doc_data, temp_file, ensure_ascii=False, indent=2)
            context_path = Path(temp_file.name)

        instruction = build_agent_instruction(
            repository_url=repository_url,
            context_json_path=str(context_path),
        )
        task_id = _invoke_cursor_task(
            client=cursor_client,
            repository_url=repository_url,
            instruction=instruction,
            context_json_path=str(context_path),
        )
        LOGGER.info("Cursor task created: %s", task_id)

        task_result = _wait_cursor_task_result(client=cursor_client, task_id=task_id)
        status = str(task_result.get("status", "")).lower()
        if status not in SUCCESS_STATUSES:
            raise RuntimeError(
                "Cursor task did not complete successfully. "
                f"task_id={task_id}, status={status}, error={task_result.get('error')}"
            )

        generated_from_payload = _extract_generated_files_from_result(task_result)
        written_paths = _write_generated_files_from_payload(
            generated_files=generated_from_payload,
            output_dir=output_path,
        )

        if not written_paths:
            written_paths = _merge_files_from_task_branch(
                repository_url=repository_url,
                branch=_extract_task_branch(task_result),
                modified_files=_extract_modified_files(task_result),
                output_dir=output_path,
            )

        if not written_paths:
            raise RuntimeError(
                "Cursor task succeeded but no generated test files were materialized. "
                f"task_id={task_id}"
            )

        case_list = _extract_case_list(task_result)
        if not case_list:
            case_list = _derive_case_list_from_files(written_paths)

        _update_manifest(
            manifest_path=manifest_path,
            generated_paths=written_paths,
            case_list=case_list,
            repository_url=repository_url,
            context_json_path=str(context_path),
            task_id=task_id,
            task_result=task_result,
        )
        return written_paths
    finally:
        if context_path and context_path.exists():
            try:
                context_path.unlink()
            except OSError:
                LOGGER.warning("Failed to remove temp context file: %s", context_path)


def _build_client_from_env() -> CursorAPIClient:
    if CursorAPIClient is Any:
        raise RuntimeError(
            "cursor_api_client module not found. Please provide CursorAPIClient implementation."
        )
    from_env = getattr(CursorAPIClient, "from_env", None)
    if callable(from_env):
        return from_env()

    signature = inspect.signature(CursorAPIClient)
    kwargs: Dict[str, Any] = {}
    if "api_key" in signature.parameters:
        kwargs["api_key"] = os.getenv("CURSOR_API_KEY", "")
    if "base_url" in signature.parameters:
        kwargs["base_url"] = os.getenv("CURSOR_API_BASE_URL", "")
    return CursorAPIClient(**kwargs)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate pytest cases from Huawei Cloud docs with Cursor API.",
    )
    parser.add_argument(
        "--input-json",
        required=True,
        help="Path to parsed structured Huawei Cloud documentation JSON file.",
    )
    parser.add_argument(
        "--repo-url",
        required=True,
        help="Target test code repository URL.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Output directory. Default: tests/generated",
    )
    parser.add_argument(
        "--manifest-file",
        default=DEFAULT_MANIFEST_FILE,
        help=f"Manifest file name. Default: {DEFAULT_MANIFEST_FILE}",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logger level.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level))

    input_path = Path(args.input_json)
    if not input_path.exists():
        raise FileNotFoundError(f"Input JSON does not exist: {input_path}")

    doc_data = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(doc_data, (dict, list)):
        raise ValueError("Input JSON root must be an object or array")

    client = _build_client_from_env()
    generated_files = generate_test_cases(
        doc_data=doc_data,
        repository_url=args.repo_url,
        cursor_client=client,
        output_dir=args.output_dir,
        manifest_file=args.manifest_file,
    )
    print(json.dumps({"generated_files": generated_files}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
