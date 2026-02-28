"""
Cursor Background Agent API client.

This client wraps Cursor Cloud Agents API so callers can:
- create a background agent task against a repository
- poll task status and retrieve structured results

Reference: Cursor Cloud Agents API (Background Agent) in cloud Ubuntu VM.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib import error, request


DEFAULT_BASE_URL = "https://api.cursor.com"
DEFAULT_REQUEST_TIMEOUT = 30
DEFAULT_POLL_INTERVAL = 5
DEFAULT_WAIT_TIMEOUT = 1800
DEFAULT_MAX_RETRIES = 3
DEFAULT_MAX_CONTEXT_CHARS = 120000

RAW_STATUS_CREATING = "CREATING"
RAW_STATUS_RUNNING = "RUNNING"
RAW_STATUS_FINISHED = "FINISHED"
RAW_STATUS_ERROR = "ERROR"
RAW_STATUS_EXPIRED = "EXPIRED"
RAW_STATUS_STOPPED = "STOPPED"
RAW_STATUS_CANCELLED = "CANCELLED"

TERMINAL_RAW_STATUSES = {
    RAW_STATUS_FINISHED,
    RAW_STATUS_ERROR,
    RAW_STATUS_EXPIRED,
    RAW_STATUS_STOPPED,
    RAW_STATUS_CANCELLED,
}

STATUS_MAP = {
    RAW_STATUS_CREATING: "queued",
    RAW_STATUS_RUNNING: "running",
    RAW_STATUS_FINISHED: "succeeded",
    RAW_STATUS_ERROR: "failed",
    RAW_STATUS_EXPIRED: "failed",
    RAW_STATUS_STOPPED: "cancelled",
    RAW_STATUS_CANCELLED: "cancelled",
}


class CursorAPIError(Exception):
    """Base Cursor API exception."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.details = details or {}


@dataclass
class CursorClientConfig:
    """Configuration for CursorAPIClient."""

    api_key: str
    base_url: str = DEFAULT_BASE_URL
    request_timeout: int = DEFAULT_REQUEST_TIMEOUT
    poll_interval: int = DEFAULT_POLL_INTERVAL
    wait_timeout: int = DEFAULT_WAIT_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES
    max_context_chars: int = DEFAULT_MAX_CONTEXT_CHARS


class CursorAPIClient:
    """
    Client for Cursor Background Agent API.

    API key is read from CURSOR_API_KEY by default.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        request_timeout: int = DEFAULT_REQUEST_TIMEOUT,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
        wait_timeout: int = DEFAULT_WAIT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_context_chars: int = DEFAULT_MAX_CONTEXT_CHARS,
    ) -> None:
        resolved_api_key = (api_key or os.getenv("CURSOR_API_KEY", "")).strip()
        if not resolved_api_key:
            raise ValueError(
                "Missing Cursor API key. Set CURSOR_API_KEY or pass api_key explicitly."
            )

        self.config = CursorClientConfig(
            api_key=resolved_api_key,
            base_url=base_url.rstrip("/"),
            request_timeout=int(request_timeout),
            poll_interval=int(poll_interval),
            wait_timeout=int(wait_timeout),
            max_retries=int(max_retries),
            max_context_chars=int(max_context_chars),
        )

    @classmethod
    def from_env(cls) -> "CursorAPIClient":
        """Build client from environment variables."""
        return cls(
            api_key=os.getenv("CURSOR_API_KEY", "").strip(),
            base_url=os.getenv("CURSOR_API_BASE_URL", DEFAULT_BASE_URL),
            request_timeout=int(
                os.getenv("CURSOR_API_REQUEST_TIMEOUT", str(DEFAULT_REQUEST_TIMEOUT))
            ),
            poll_interval=int(
                os.getenv("CURSOR_API_POLL_INTERVAL", str(DEFAULT_POLL_INTERVAL))
            ),
            wait_timeout=int(
                os.getenv("CURSOR_API_WAIT_TIMEOUT", str(DEFAULT_WAIT_TIMEOUT))
            ),
            max_retries=int(os.getenv("CURSOR_API_MAX_RETRIES", str(DEFAULT_MAX_RETRIES))),
            max_context_chars=int(
                os.getenv("CURSOR_MAX_CONTEXT_CHARS", str(DEFAULT_MAX_CONTEXT_CHARS))
            ),
        )

    def create_agent_task(
        self,
        repository: str,
        instruction: str,
        context_files: Optional[Iterable[str]] = None,
    ) -> str:
        """
        Create a Cursor Background Agent task and return task_id.
        """
        repository = (repository or "").strip()
        instruction = (instruction or "").strip()
        if not repository:
            raise ValueError("repository must not be empty")
        if not instruction:
            raise ValueError("instruction must not be empty")

        prompt_text = self._compose_prompt_text(
            instruction=instruction,
            context_files=context_files or [],
        )

        payload = {
            "prompt": {"text": prompt_text},
            "source": {"repository": repository},
        }
        response_data = self._request_json("POST", "/v0/agents", payload)
        task_id = str(response_data.get("id", "")).strip()
        if not task_id:
            raise CursorAPIError(
                "Cursor API did not return task id",
                details={"response": response_data},
            )
        return task_id

    def get_task_result(
        self,
        task_id: str,
        wait: bool = True,
        poll_interval: Optional[int] = None,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Get structured task result.

        If wait=True, poll until task reaches terminal state or timeout.
        """
        task_id = (task_id or "").strip()
        if not task_id:
            return self._build_error_result(
                task_id=task_id,
                message="task_id must not be empty",
                code="INVALID_TASK_ID",
            )

        interval = int(poll_interval or self.config.poll_interval)
        max_wait = int(timeout or self.config.wait_timeout)

        try:
            if not wait:
                agent_data = self._get_agent(task_id)
                conversation_data = self._get_agent_conversation(task_id)
                return self._build_structured_result(task_id, agent_data, conversation_data)

            deadline = time.monotonic() + max_wait
            last_snapshot: Optional[Dict[str, Any]] = None

            while time.monotonic() < deadline:
                agent_data = self._get_agent(task_id)
                conversation_data = self._get_agent_conversation(task_id)
                result = self._build_structured_result(task_id, agent_data, conversation_data)
                last_snapshot = result

                if result.get("completed"):
                    return result

                time.sleep(max(interval, 1))

            timeout_result = self._build_error_result(
                task_id=task_id,
                message=f"Timed out waiting for task {task_id}",
                code="TIMEOUT",
            )
            if last_snapshot:
                timeout_result["raw_status"] = last_snapshot.get("raw_status")
                timeout_result["summary"] = last_snapshot.get("summary")
                timeout_result["agent_url"] = last_snapshot.get("agent_url")
                timeout_result["pr_url"] = last_snapshot.get("pr_url")
            return timeout_result
        except CursorAPIError as exc:
            return self._build_error_result(
                task_id=task_id,
                message=str(exc),
                code=exc.code or "CURSOR_API_ERROR",
                details={"status_code": exc.status_code, **exc.details},
            )
        except Exception as exc:  # pragma: no cover - safety net for caller.
            return self._build_error_result(
                task_id=task_id,
                message=str(exc),
                code="UNEXPECTED_ERROR",
            )

    # Compatibility helpers for existing generators/integrations.
    def create_task(self, **kwargs: Any) -> Dict[str, str]:
        repository = (
            kwargs.get("repository")
            or kwargs.get("repository_url")
            or (kwargs.get("source") or {}).get("repository")
            or ""
        )
        instruction = (
            kwargs.get("instruction")
            or kwargs.get("prompt")
            or kwargs.get("task_description")
            or ""
        )
        context_files = kwargs.get("context_files")
        task_id = self.create_agent_task(
            repository=repository,
            instruction=instruction,
            context_files=context_files,
        )
        return {"task_id": task_id}

    def get_task(self, task_id: str) -> Dict[str, Any]:
        return self._get_agent(task_id)

    def wait_for_task_completion(
        self,
        task_id: str,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
        timeout: int = DEFAULT_WAIT_TIMEOUT,
    ) -> Dict[str, Any]:
        return self.get_task_result(
            task_id=task_id,
            wait=True,
            poll_interval=poll_interval,
            timeout=timeout,
        )

    def wait_for_task(
        self,
        task_id: str,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
        timeout: int = DEFAULT_WAIT_TIMEOUT,
    ) -> Dict[str, Any]:
        return self.wait_for_task_completion(
            task_id=task_id,
            poll_interval=poll_interval,
            timeout=timeout,
        )

    def _compose_prompt_text(
        self,
        instruction: str,
        context_files: Iterable[str],
    ) -> str:
        prompt_parts = [instruction.strip()]
        for file_path in context_files:
            resolved = Path(file_path)
            if not resolved.exists():
                raise FileNotFoundError(f"Context file not found: {file_path}")
            if not resolved.is_file():
                raise ValueError(f"Context path is not a file: {file_path}")

            content = resolved.read_text(encoding="utf-8")
            if len(content) > self.config.max_context_chars:
                content = (
                    content[: self.config.max_context_chars]
                    + "\n\n...[truncated by cursor_api_client due to size limit]..."
                )
            prompt_parts.append(
                "\n".join(
                    [
                        "",
                        f"### Context File: {resolved.name}",
                        content,
                    ]
                )
            )
        return "\n".join(prompt_parts).strip()

    def _request_json(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Accept": "application/json",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"

        url = f"{self.config.base_url}{path}"
        transient_http_codes = {408, 429, 500, 502, 503, 504}

        for attempt in range(self.config.max_retries + 1):
            req = request.Request(url=url, data=body, headers=headers, method=method)
            try:
                with request.urlopen(req, timeout=self.config.request_timeout) as resp:
                    response_bytes = resp.read()
                    if not response_bytes:
                        return {}
                    try:
                        return json.loads(response_bytes.decode("utf-8"))
                    except json.JSONDecodeError:
                        return {"raw": response_bytes.decode("utf-8", errors="replace")}
            except error.HTTPError as exc:
                parsed_error = self._parse_http_error(exc)
                if (
                    exc.code in transient_http_codes
                    and attempt < self.config.max_retries
                ):
                    time.sleep(2**attempt)
                    continue
                raise CursorAPIError(
                    message=parsed_error.get("message", f"HTTP {exc.code}"),
                    status_code=exc.code,
                    code=parsed_error.get("code"),
                    details=parsed_error,
                ) from exc
            except (error.URLError, TimeoutError) as exc:
                if attempt < self.config.max_retries:
                    time.sleep(2**attempt)
                    continue
                raise CursorAPIError(
                    message=f"Network error calling Cursor API: {exc}",
                    code="NETWORK_ERROR",
                ) from exc

        raise CursorAPIError(message="Cursor API request failed", code="REQUEST_FAILED")

    def _parse_http_error(self, exc: error.HTTPError) -> Dict[str, Any]:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            raw = ""

        if not raw:
            return {"message": f"HTTP {exc.code}", "code": str(exc.code)}

        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                if "error" in parsed and isinstance(parsed["error"], dict):
                    message = parsed["error"].get("message", f"HTTP {exc.code}")
                    code = parsed["error"].get("code", str(exc.code))
                    return {
                        "message": str(message),
                        "code": str(code),
                        "raw_error": parsed,
                    }
                message = parsed.get("message", f"HTTP {exc.code}")
                code = parsed.get("code", str(exc.code))
                return {"message": str(message), "code": str(code), "raw_error": parsed}
        except json.JSONDecodeError:
            pass

        return {"message": raw, "code": str(exc.code)}

    def _get_agent(self, task_id: str) -> Dict[str, Any]:
        return self._request_json("GET", f"/v0/agents/{task_id}")

    def _get_agent_conversation(self, task_id: str) -> Dict[str, Any]:
        # Conversation endpoint may fail for transient states; degrade gracefully.
        try:
            return self._request_json("GET", f"/v0/agents/{task_id}/conversation")
        except CursorAPIError:
            return {}

    def _build_structured_result(
        self,
        task_id: str,
        agent_data: Dict[str, Any],
        conversation_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        raw_status = str(agent_data.get("status", "")).upper()
        status = STATUS_MAP.get(raw_status, "unknown")
        completed = raw_status in TERMINAL_RAW_STATUSES
        success = raw_status == RAW_STATUS_FINISHED

        target = agent_data.get("target") if isinstance(agent_data.get("target"), dict) else {}
        source = agent_data.get("source") if isinstance(agent_data.get("source"), dict) else {}

        modified_files = self._extract_modified_files(agent_data, conversation_data or {})
        error_payload: Optional[Dict[str, Any]] = None
        if status == "failed":
            error_payload = {
                "code": agent_data.get("errorCode") or "AGENT_FAILED",
                "message": agent_data.get("error") or agent_data.get("summary") or "Task failed",
            }

        return {
            "task_id": task_id,
            "status": status,
            "raw_status": raw_status,
            "completed": completed,
            "success": success,
            "repository": source.get("repository"),
            "branch": target.get("branchName"),
            "agent_url": target.get("url"),
            "pr_url": target.get("prUrl"),
            "summary": agent_data.get("summary"),
            "modified_files": modified_files,
            "error": error_payload,
            "result": {
                "agent": agent_data,
                "conversation": conversation_data or {},
            },
        }

    def _extract_modified_files(
        self,
        agent_data: Dict[str, Any],
        conversation_data: Dict[str, Any],
    ) -> List[str]:
        files: List[str] = []
        candidate_keys = {
            "modifiedFiles",
            "modified_files",
            "files",
            "changedFiles",
            "changed_files",
            "artifacts",
            "generated_files",
        }

        def _collect(value: Any) -> None:
            if isinstance(value, dict):
                for key, nested in value.items():
                    if key in candidate_keys and isinstance(nested, list):
                        for item in nested:
                            if isinstance(item, str) and item.strip():
                                files.append(item.strip())
                            elif isinstance(item, dict):
                                path = (
                                    item.get("path")
                                    or item.get("file")
                                    or item.get("file_path")
                                    or item.get("name")
                                )
                                if isinstance(path, str) and path.strip():
                                    files.append(path.strip())
                    else:
                        _collect(nested)
            elif isinstance(value, list):
                for nested in value:
                    _collect(nested)

        _collect(agent_data)

        # Some API versions expose file edits in conversation messages.
        messages = conversation_data.get("messages")
        if isinstance(messages, list):
            for message in messages:
                if not isinstance(message, dict):
                    continue
                text = message.get("text")
                if not isinstance(text, str):
                    continue
                for token in text.replace("\r", "\n").split():
                    if "/" in token and "." in token:
                        cleaned = token.strip("`*,:;()[]{}<>\"'")
                        if cleaned and cleaned.count("/") >= 1 and "." in cleaned.split("/")[-1]:
                            files.append(cleaned)

        # Deduplicate while preserving order.
        deduped: List[str] = []
        seen = set()
        for item in files:
            if item not in seen:
                seen.add(item)
                deduped.append(item)
        return deduped

    def _build_error_result(
        self,
        task_id: str,
        message: str,
        code: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return {
            "task_id": task_id,
            "status": "failed" if code != "TIMEOUT" else "timeout",
            "raw_status": None,
            "completed": True,
            "success": False,
            "repository": None,
            "branch": None,
            "agent_url": None,
            "pr_url": None,
            "summary": None,
            "modified_files": [],
            "error": {
                "code": code,
                "message": message,
                "details": details or {},
            },
            "result": {},
        }
