"""
Generate pytest cases from structured Huawei Cloud documentation by using Cursor API.
"""

from __future__ import annotations

import argparse
import inspect
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:
    from cursor_api_client import CursorAPIClient  # type: ignore
except Exception:  # pragma: no cover - fallback for environments without the SDK file.
    CursorAPIClient = Any  # type: ignore


LOGGER = logging.getLogger(__name__)
DEFAULT_OUTPUT_DIR = Path("tests/generated")
DEFAULT_MANIFEST_FILE = "generated_cases_manifest.json"
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}


@dataclass
class GeneratedFile:
    """Normalized generated test file information."""

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
            if task_response.get(key):
                return str(task_response[key])

    for attr in ("task_id", "id"):
        value = getattr(task_response, attr, None)
        if value:
            return str(value)

    raise ValueError(f"Could not extract task id from response: {task_response!r}")


def _invoke_create_task(
    client: CursorAPIClient,
    repository_url: str,
    instruction: str,
    context_json_path: str,
) -> str:
    if not hasattr(client, "create_task"):
        raise AttributeError("CursorAPIClient must implement create_task")

    create_task = client.create_task  # type: ignore[attr-defined]
    payload = {
        "task_type": "generate_pytest_cases",
        "repository_url": repository_url,
        "instruction": instruction,
        "context_files": [context_json_path],
        "metadata": {
            "source": "huawei_cloud_docs",
            "generator": "case_generator.py",
            "created_at": _utc_now_iso(),
        },
    }

    signature = inspect.signature(create_task)
    kwargs: Dict[str, Any] = {}
    has_var_kw = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )

    if has_var_kw:
        kwargs = payload
    else:
        for key, value in payload.items():
            if key in signature.parameters:
                kwargs[key] = value

    if not kwargs and signature.parameters:
        # Best effort fallback for unknown method signatures.
        first_param = next(iter(signature.parameters))
        kwargs[first_param] = payload

    task_response = create_task(**kwargs)
    task_id = _extract_task_id(task_response)
    LOGGER.info("Cursor task created: %s", task_id)
    return task_id


def _normalize_task_response(data: Any) -> Dict[str, Any]:
    if isinstance(data, dict):
        return data

    return {"status": "unknown", "result": data}


def _poll_task_until_complete(
    client: CursorAPIClient,
    task_id: str,
    poll_interval: int = 5,
    timeout: int = 1800,
) -> Dict[str, Any]:
    # Prefer dedicated waiting API if CursorAPIClient provides one.
    for method_name in ("wait_for_task_completion", "wait_for_task"):
        wait_method = getattr(client, method_name, None)
        if callable(wait_method):
            signature = inspect.signature(wait_method)
            kwargs: Dict[str, Any] = {"task_id": task_id}
            if "poll_interval" in signature.parameters:
                kwargs["poll_interval"] = poll_interval
            if "timeout" in signature.parameters:
                kwargs["timeout"] = timeout
            result = wait_method(**kwargs)
            data = _normalize_task_response(result)
            if data.get("status") not in TERMINAL_STATUSES and data.get("result"):
                # Some clients return just the result payload for a wait method.
                data["status"] = "succeeded"
            return data

    get_task = getattr(client, "get_task", None)
    if not callable(get_task):
        raise AttributeError(
            "CursorAPIClient must implement wait_for_task_completion/wait_for_task/get_task"
        )

    deadline = time.time() + timeout
    while time.time() < deadline:
        task_data = _normalize_task_response(get_task(task_id=task_id))
        status = str(task_data.get("status", "")).lower()

        if status in TERMINAL_STATUSES:
            return task_data

        time.sleep(poll_interval)

    raise TimeoutError(f"Polling Cursor task timed out: {task_id}")


def _sanitize_target_path(raw_path: str, output_dir: Path) -> Path:
    normalized = raw_path.replace("\\", "/").lstrip("/")
    if normalized.startswith("tests/generated/"):
        normalized = normalized[len("tests/generated/") :]
    if not normalized:
        target_relative = Path("test_generated_api_cases.py")
    else:
        safe_parts = [part for part in Path(normalized).parts if part not in ("..", ".")]
        if not safe_parts:
            safe_parts = [Path(normalized).name or "test_generated_api_cases.py"]
        target_relative = Path(*safe_parts)
    return output_dir / target_relative


def _extract_generated_files(task_result: Dict[str, Any]) -> List[GeneratedFile]:
    raw_result = task_result.get("result")
    if isinstance(raw_result, dict):
        payload: Dict[str, Any] = raw_result
    else:
        payload = task_result

    files_raw = (
        payload.get("generated_files")
        or payload.get("files")
        or payload.get("artifacts")
        or []
    )

    generated_files: List[GeneratedFile] = []

    if isinstance(files_raw, list):
        for item in files_raw:
            if isinstance(item, str):
                generated_files.append(GeneratedFile(path=item, source_path=item))
                continue

            if isinstance(item, dict):
                path = str(
                    item.get("path")
                    or item.get("file_path")
                    or item.get("name")
                    or "test_generated_api_cases.py"
                )
                generated_files.append(
                    GeneratedFile(
                        path=path,
                        content=item.get("content") or item.get("code"),
                        source_path=item.get("source_path"),
                    )
                )

    if not generated_files and isinstance(raw_result, str) and raw_result.strip():
        generated_files.append(
            GeneratedFile(
                path="test_generated_api_cases.py",
                content=raw_result,
            )
        )

    if not generated_files:
        # Fallback if API returns a single blob.
        test_code = payload.get("test_code")
        if isinstance(test_code, str) and test_code.strip():
            generated_files.append(
                GeneratedFile(
                    path="test_generated_api_cases.py",
                    content=test_code,
                )
            )

    return generated_files


def _write_generated_files(
    generated_files: Iterable[GeneratedFile],
    output_dir: Path,
) -> List[str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written_paths: List[str] = []

    for generated_file in generated_files:
        target_path = _sanitize_target_path(generated_file.path, output_dir)
        target_path.parent.mkdir(parents=True, exist_ok=True)

        if generated_file.content is not None:
            target_path.write_text(generated_file.content, encoding="utf-8")
            written_paths.append(str(target_path))
            continue

        if generated_file.source_path:
            source = Path(generated_file.source_path)
            if not source.exists():
                raise FileNotFoundError(
                    f"Cursor result source path does not exist: {generated_file.source_path}"
                )
            target_path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            written_paths.append(str(target_path))
            continue

        raise ValueError(
            f"Generated file missing both content and source path: {generated_file}"
        )

    return written_paths


def _extract_case_list(task_result: Dict[str, Any]) -> List[str]:
    payload = task_result.get("result") if isinstance(task_result.get("result"), dict) else task_result
    case_list = payload.get("case_list")
    if isinstance(case_list, list):
        normalized = [str(item) for item in case_list if str(item).strip()]
        if normalized:
            return normalized
    return []


def _update_manifest(
    manifest_path: Path,
    generated_paths: List[str],
    case_list: List[str],
    repository_url: str,
    context_json_path: str,
) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest: Dict[str, Any] = {"runs": []}

    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                manifest = {"runs": []}
        except json.JSONDecodeError:
            manifest = {"runs": []}

    runs = manifest.get("runs")
    if not isinstance(runs, list):
        runs = []
        manifest["runs"] = runs

    runs.append(
        {
            "generated_at": _utc_now_iso(),
            "repository_url": repository_url,
            "context_json_path": context_json_path,
            "generated_files": generated_paths,
            "generated_cases": case_list,
        }
    )

    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def build_agent_instruction(repository_url: str, context_json_path: str) -> str:
    return f"""
You are an expert QA automation engineer.

Repository: {repository_url}
Context file (JSON): {context_json_path}

Task:
Generate pytest test files from Huawei Cloud API documentation data in the context JSON.
The JSON includes fields such as title, api_description, parameters, request_examples,
response_examples, error_codes and operation_id.

Mandatory requirements:
1) Create independent pytest test functions for each API operation.
2) Each test must include:
   - Authentication setup (token/header signing placeholder if real secret is unavailable).
   - Request construction using documented endpoint/method/parameters.
   - Assertions based on documented response examples (status code, key fields).
3) Follow pytest conventions:
   - function naming: test_<service>_<operation>
   - fixtures for shared auth/session setup
   - clear Arrange/Act/Assert structure
4) Prefer deterministic assertions, and annotate assumptions when docs are ambiguous.
5) Output only Python test files.

Return format (strict JSON):
{{
  "generated_files": [
    {{
      "path": "tests/generated/test_<service>_<operation>.py",
      "content": "<python code>"
    }}
  ],
  "case_list": [
    "test_<service>_<operation>"
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
    Generate pytest cases with Cursor API and merge them into tests/generated.

    Args:
        doc_data: Structured Huawei Cloud documentation data.
        repository_url: Target test code repository URL.
        cursor_client: Cursor API client instance.
        output_dir: Output directory for generated test files.
        manifest_file: File name for generated case manifest.

    Returns:
        List of generated test file paths.
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
        task_id = _invoke_create_task(
            client=cursor_client,
            repository_url=repository_url,
            instruction=instruction,
            context_json_path=str(context_path),
        )

        task_result = _poll_task_until_complete(client=cursor_client, task_id=task_id)
        status = str(task_result.get("status", "")).lower()
        if status != "succeeded":
            raise RuntimeError(
                f"Cursor task failed. task_id={task_id}, status={status}, result={task_result}"
            )

        generated_files = _extract_generated_files(task_result)
        if not generated_files:
            raise RuntimeError(
                f"Cursor task succeeded but no generated files found. task_id={task_id}"
            )

        written_paths = _write_generated_files(generated_files=generated_files, output_dir=output_path)
        case_list = _extract_case_list(task_result)
        _update_manifest(
            manifest_path=manifest_path,
            generated_paths=written_paths,
            case_list=case_list,
            repository_url=repository_url,
            context_json_path=str(context_path),
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

    # Prefer explicit helper if available.
    from_env = getattr(CursorAPIClient, "from_env", None)
    if callable(from_env):
        return from_env()

    init_signature = inspect.signature(CursorAPIClient)
    kwargs: Dict[str, Any] = {}
    if "api_key" in init_signature.parameters:
        kwargs["api_key"] = os.getenv("CURSOR_API_KEY", "")
    if "base_url" in init_signature.parameters:
        kwargs["base_url"] = os.getenv("CURSOR_API_BASE_URL", "")

    return CursorAPIClient(**kwargs)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate pytest cases from Huawei Cloud docs with Cursor API."
    )
    parser.add_argument(
        "--input-json",
        required=True,
        help="Path to parsed structured Huawei Cloud documentation JSON file.",
    )
    parser.add_argument(
        "--repo-url",
        required=True,
        help="Target Git repository URL for generated tests.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Output directory for generated pytest files. Default: tests/generated",
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
    generated_paths = generate_test_cases(
        doc_data=doc_data,
        repository_url=args.repo_url,
        cursor_client=client,
        output_dir=args.output_dir,
        manifest_file=args.manifest_file,
    )
    print(json.dumps({"generated_files": generated_paths}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
