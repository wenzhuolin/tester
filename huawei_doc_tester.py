"""
Integrated entrypoint for Huawei documentation-driven test automation.

Main class: HuaweiDocTester
Methods:
- fetch_and_generate()
- monitor_and_update()
- run_full_test()

This module wires together:
- huawei_docs_fetcher.py
- case_generator.py
- doc_change_monitor.py
- test_executor.py
- failure_analyzer.py
- email_notifier.py
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import yaml

from case_generator import generate_test_cases
from cursor_api_client import CursorAPIClient
from doc_change_monitor import monitor_changes
from email_notifier import send_change_notification, send_notification
from failure_analyzer import analyze_failures
from huawei_docs_fetcher import HuaweiDocsCrawler
from test_executor import run_tests


LOGGER = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("config.yaml")
SUPPORTED_CURSOR_PLANS = {"pro", "business", "enterprise", "team"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(value, dict)
        ):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("/")


def _safe_operation_id(product: str, url: str) -> str:
    parsed = urlsplit(url)
    path = parsed.path.strip("/").replace("/", "_")
    path = re.sub(r"[^a-zA-Z0-9_]+", "_", path).strip("_")
    if not path:
        path = "index"
    return f"{product}_{path}"[:100]


class HuaweiDocTester:
    """Coordinator for document fetch, update monitor and full testing."""

    def __init__(self, config_path: str = str(DEFAULT_CONFIG_PATH)) -> None:
        self.config_path = Path(config_path).resolve()
        self.config = self._load_config(self.config_path)
        self._cursor_client: Optional[CursorAPIClient] = None
        self._apply_runtime_env()

    def _load_config(self, path: Path) -> Dict[str, Any]:
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("config.yaml root must be a map/object")

        defaults: Dict[str, Any] = {
            "products": [],
            "repository": {
                "url": "",
                "local_path": ".",
                "branch": None,
                "pull_latest": True,
            },
            "paths": {
                "docs_output_root": "data/huawei_docs",
                "generated_tests_dir": "tests/generated",
                "report_dir": "reports/tests",
                "change_log_file": "data/monitor_logs/change_log.jsonl",
            },
            "cursor": {
                "enabled": True,
                "api_key": "",
                "api_base_url": "https://api.cursor.com",
                "wait_timeout": 1800,
                "poll_interval": 5,
                "plan": "pro",
                "max_generation_tasks_per_run": 20,
            },
            "pytest": {
                "tests_path": "tests",
                "extra_args": [],
                "timeout": 3600,
            },
            "failure_analysis": {
                "enabled": True,
                "auto_commit": True,
            },
            "email": {
                "enabled": False,
                "smtp_host": "",
                "smtp_port": 587,
                "smtp_sender": "",
                "smtp_username": "",
                "smtp_password": "",
                "smtp_recipients": [],
                "smtp_use_tls": True,
                "smtp_use_ssl": False,
                "smtp_timeout": 30,
                "smtp_sender_name": "",
                "smtp_dry_run": False,
            },
            "schedule": {
                "timezone": "Asia/Shanghai",
                "monitor_cron": "0 2 * * *",
                "full_test_cron": "0 3 * * 1",
            },
        }

        merged = _deep_merge(defaults, raw)
        products = merged.get("products")
        if not isinstance(products, list) or not products:
            raise ValueError("config.yaml requires non-empty 'products' list")
        return merged

    def _apply_runtime_env(self) -> None:
        cursor_cfg = self.config.get("cursor", {})
        repo_cfg = self.config.get("repository", {})
        email_cfg = self.config.get("email", {})
        products = self.config.get("products", [])

        api_key = str(cursor_cfg.get("api_key") or "").strip()
        if api_key:
            os.environ["CURSOR_API_KEY"] = api_key
        base_url = str(cursor_cfg.get("api_base_url") or "").strip()
        if base_url:
            os.environ["CURSOR_API_BASE_URL"] = base_url
        os.environ["CURSOR_API_WAIT_TIMEOUT"] = str(cursor_cfg.get("wait_timeout", 1800))
        os.environ["CURSOR_API_POLL_INTERVAL"] = str(cursor_cfg.get("poll_interval", 5))

        repo_url = str(repo_cfg.get("url") or "").strip()
        if repo_url:
            os.environ["TEST_REPOSITORY_URL"] = repo_url
            os.environ["TARGET_TEST_REPO_URL"] = repo_url

        os.environ["HUAWEI_DOC_PRODUCT_LIST"] = json.dumps(products, ensure_ascii=False)

        if bool(email_cfg.get("enabled")):
            _set = lambda key, val: os.environ.__setitem__(key, str(val))
            if email_cfg.get("smtp_host"):
                _set("SMTP_HOST", email_cfg.get("smtp_host"))
            _set("SMTP_PORT", email_cfg.get("smtp_port", 587))
            if email_cfg.get("smtp_sender"):
                _set("SMTP_SENDER", email_cfg.get("smtp_sender"))
            if email_cfg.get("smtp_username"):
                _set("SMTP_USERNAME", email_cfg.get("smtp_username"))
            if email_cfg.get("smtp_password"):
                _set("SMTP_PASSWORD", email_cfg.get("smtp_password"))
            recipients = email_cfg.get("smtp_recipients", [])
            if isinstance(recipients, list):
                _set("SMTP_RECIPIENTS", ",".join(str(item).strip() for item in recipients if str(item).strip()))
            _set("SMTP_USE_TLS", str(bool(email_cfg.get("smtp_use_tls", True))).lower())
            _set("SMTP_USE_SSL", str(bool(email_cfg.get("smtp_use_ssl", False))).lower())
            _set("SMTP_TIMEOUT", email_cfg.get("smtp_timeout", 30))
            if email_cfg.get("smtp_sender_name"):
                _set("SMTP_SENDER_NAME", email_cfg.get("smtp_sender_name"))
            _set("SMTP_DRY_RUN", str(bool(email_cfg.get("smtp_dry_run", False))).lower())

    def _repo_url(self) -> str:
        return str(self.config.get("repository", {}).get("url") or "").strip()

    def _repo_dir(self) -> Path:
        return Path(str(self.config.get("repository", {}).get("local_path") or ".")).resolve()

    def _repo_branch(self) -> Optional[str]:
        branch = self.config.get("repository", {}).get("branch")
        if isinstance(branch, str) and branch.strip():
            return branch.strip()
        return None

    def _paths(self) -> Dict[str, Path]:
        paths_cfg = self.config.get("paths", {})
        return {
            "docs_output_root": Path(str(paths_cfg.get("docs_output_root", "data/huawei_docs"))).resolve(),
            "generated_tests_dir": Path(str(paths_cfg.get("generated_tests_dir", "tests/generated"))).resolve(),
            "report_dir": Path(str(paths_cfg.get("report_dir", "reports/tests"))).resolve(),
            "change_log_file": Path(str(paths_cfg.get("change_log_file", "data/monitor_logs/change_log.jsonl"))).resolve(),
        }

    def _cursor_enabled(self) -> bool:
        return bool(self.config.get("cursor", {}).get("enabled", True))

    def _warn_cursor_cost_and_plan(self) -> None:
        cursor_cfg = self.config.get("cursor", {})
        plan = str(cursor_cfg.get("plan") or "").strip().lower()
        if plan and plan not in SUPPORTED_CURSOR_PLANS:
            LOGGER.warning(
                "当前配置的 Cursor 计划为 '%s'，Background Agent API 仅支持 Pro 及以上计划。",
                plan,
            )
        LOGGER.info(
            "注意：Cursor Background Agent API 会消耗配额并产生计费，请控制任务频率和批量规模。"
        )

    def _get_cursor_client(self, required: bool = True) -> Optional[CursorAPIClient]:
        if self._cursor_client is not None:
            return self._cursor_client

        if not self._cursor_enabled():
            if required:
                raise RuntimeError("Cursor API is disabled in config.cursor.enabled")
            return None

        cursor_cfg = self.config.get("cursor", {})
        api_key = str(cursor_cfg.get("api_key") or os.getenv("CURSOR_API_KEY", "")).strip()
        if not api_key:
            if required:
                raise RuntimeError("Cursor API key is missing (config.cursor.api_key)")
            return None

        self._warn_cursor_cost_and_plan()
        self._cursor_client = CursorAPIClient(
            api_key=api_key,
            base_url=str(cursor_cfg.get("api_base_url") or "https://api.cursor.com"),
            wait_timeout=int(cursor_cfg.get("wait_timeout", 1800)),
            poll_interval=int(cursor_cfg.get("poll_interval", 5)),
        )
        return self._cursor_client

    def _extract_parameters(self, page_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        parameter_tables = page_data.get("parameter_tables")
        if not isinstance(parameter_tables, list):
            return []

        rows: List[Dict[str, Any]] = []
        for table in parameter_tables:
            if not isinstance(table, dict):
                continue
            headers = table.get("headers")
            data_rows = table.get("rows")
            if not isinstance(headers, list) or not isinstance(data_rows, list):
                continue
            header_names = [str(h).strip() for h in headers]
            for data_row in data_rows:
                if not isinstance(data_row, list):
                    continue
                item: Dict[str, Any] = {}
                for idx, header in enumerate(header_names):
                    if not header:
                        continue
                    item[header] = str(data_row[idx]).strip() if idx < len(data_row) else ""
                if item:
                    rows.append(item)
        return rows

    def _load_page_json(self, page_file: Path) -> Dict[str, Any]:
        if not page_file.exists() or not page_file.is_file():
            return {}
        try:
            data = json.loads(page_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _build_full_doc_payload(self, product: str, index_file: Path) -> Dict[str, Any]:
        if not index_file.exists():
            return {
                "title": f"Huawei Cloud {product} documentation",
                "product": product,
                "generated_at": _now_iso(),
                "apis": [],
            }

        try:
            index_data = json.loads(index_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            index_data = {}
        pages = index_data.get("pages") if isinstance(index_data, dict) else {}
        if not isinstance(pages, dict):
            pages = {}

        operations: List[Dict[str, Any]] = []
        for url in sorted(pages.keys()):
            page_record = pages.get(url)
            if not isinstance(page_record, dict):
                continue
            page_file = Path(str(page_record.get("file_path") or ""))
            page_data = self._load_page_json(page_file)

            title = str(page_data.get("title") or page_record.get("title") or url)
            full_text = str(page_data.get("full_text") or "")
            code_examples = page_data.get("code_examples") if isinstance(page_data.get("code_examples"), list) else []
            request_examples = [
                {"code": code}
                for code in code_examples[:8]
                if isinstance(code, str) and code.strip()
            ]
            headings = page_data.get("headings") if isinstance(page_data.get("headings"), list) else []

            operations.append(
                {
                    "operation_id": _safe_operation_id(product, url),
                    "title": title,
                    "api_description": full_text,
                    "parameters": self._extract_parameters(page_data),
                    "request_examples": request_examples,
                    "response_examples": [],
                    "headings": headings,
                    "source_url": url,
                    "version": page_record.get("version"),
                    "last_modified": page_record.get("last_modified"),
                }
            )

        return {
            "title": f"Huawei Cloud {product} full documentation snapshot",
            "product": product,
            "generated_at": _now_iso(),
            "apis": operations,
        }

    def _email_enabled(self) -> bool:
        return bool(self.config.get("email", {}).get("enabled", False))

    def fetch_and_generate(self) -> Dict[str, Any]:
        """
        First-run flow:
        crawl docs -> build full doc payload -> generate full test cases via Cursor.
        """
        started_at = _now_iso()
        paths = self._paths()
        products = self.config.get("products", [])
        repo_url = self._repo_url()
        if not repo_url:
            raise RuntimeError("repository.url is required for fetch_and_generate")

        cursor_client = self._get_cursor_client(required=True)
        crawler = HuaweiDocsCrawler(output_root=paths["docs_output_root"])
        crawl_summary = crawler.fetch_docs(products)

        max_tasks = int(self.config.get("cursor", {}).get("max_generation_tasks_per_run", 20))
        generated_total = 0
        results: List[Dict[str, Any]] = []
        tasks_used = 0

        for item in crawl_summary:
            product = str(item.get("product") or "product")
            index_file = Path(str(item.get("index_file") or ""))
            payload = self._build_full_doc_payload(product, index_file)
            product_result: Dict[str, Any] = {
                "product": product,
                "index_file": str(index_file),
                "api_count": len(payload.get("apis", [])),
                "generation": {"status": "skipped", "generated_files": []},
                "crawl_stats": {
                    "pages_crawled": item.get("pages_crawled"),
                    "pages_saved": item.get("pages_saved"),
                    "pages_not_modified": item.get("pages_not_modified"),
                    "pages_failed": item.get("pages_failed"),
                },
            }

            if not payload.get("apis"):
                product_result["generation"] = {"status": "skipped", "reason": "no api docs extracted"}
                results.append(product_result)
                continue

            if tasks_used >= max_tasks:
                product_result["generation"] = {
                    "status": "skipped",
                    "reason": f"cursor task limit reached ({max_tasks})",
                }
                results.append(product_result)
                continue

            try:
                generated_files = generate_test_cases(
                    doc_data=payload,
                    repository_url=repo_url,
                    cursor_client=cursor_client,
                    output_dir=str(paths["generated_tests_dir"]),
                )
                tasks_used += 1
                generated_total += len(generated_files)
                product_result["generation"] = {
                    "status": "triggered",
                    "generated_files": generated_files,
                    "generated_count": len(generated_files),
                }
            except Exception as exc:
                product_result["generation"] = {
                    "status": "failed",
                    "error": str(exc),
                    "generated_files": [],
                }

            results.append(product_result)

        summary = {
            "stage": "fetch_and_generate",
            "started_at": started_at,
            "finished_at": _now_iso(),
            "repository_url": repo_url,
            "task_limit": max_tasks,
            "tasks_used": tasks_used,
            "products": results,
            "totals": {
                "products": len(results),
                "generated_files": generated_total,
            },
        }
        return summary

    def monitor_and_update(self) -> Dict[str, Any]:
        """
        Incremental flow:
        monitor docs changes -> generate/update tests via Cursor if needed.
        """
        paths = self._paths()
        products = self.config.get("products", [])
        repo_url = self._repo_url()
        cursor_client = self._get_cursor_client(required=False)
        max_tasks = int(self.config.get("cursor", {}).get("max_generation_tasks_per_run", 20))
        counter = {"used": 0}

        def limited_case_generator(
            doc_data: Dict[str, Any],
            repository: str,
            client: CursorAPIClient,
            output_dir: str,
        ) -> List[str]:
            if counter["used"] >= max_tasks:
                raise RuntimeError(
                    f"Cursor generation task limit reached in this run ({max_tasks})"
                )
            generated = generate_test_cases(
                doc_data=doc_data,
                repository_url=repository,
                cursor_client=client,
                output_dir=output_dir,
            )
            counter["used"] += 1
            return generated

        email_callback = send_change_notification if self._email_enabled() else None
        summary = monitor_changes(
            product_list=products,
            repository_url=repo_url,
            output_root=paths["docs_output_root"],
            generated_tests_dir=paths["generated_tests_dir"],
            change_log_file=paths["change_log_file"],
            cursor_client=cursor_client,
            email_notifier=email_callback,
            case_generator_fn=limited_case_generator,
        )
        summary["cursor_tasks_used"] = counter["used"]
        summary["cursor_task_limit"] = max_tasks
        return summary

    def run_full_test(self) -> Dict[str, Any]:
        """
        Full test flow:
        pull latest tests -> run pytest -> trigger Cursor failure analyzer -> send report.
        """
        paths = self._paths()
        repo_cfg = self.config.get("repository", {})
        pytest_cfg = self.config.get("pytest", {})
        fa_cfg = self.config.get("failure_analysis", {})
        cursor_cfg = self.config.get("cursor", {})

        repo_url = self._repo_url()
        repo_dir = self._repo_dir()
        branch = self._repo_branch()
        pull_latest = bool(repo_cfg.get("pull_latest", True))
        tests_path = str(pytest_cfg.get("tests_path", "tests"))
        extra_args = pytest_cfg.get("extra_args", [])
        timeout = int(pytest_cfg.get("timeout", 3600))
        enable_failure_analysis = bool(fa_cfg.get("enabled", True))
        auto_commit = bool(fa_cfg.get("auto_commit", True))
        wait_timeout = int(cursor_cfg.get("wait_timeout", 1800))
        poll_interval = int(cursor_cfg.get("poll_interval", 5))

        cursor_client = self._get_cursor_client(required=False)

        def failure_analyzer(payload: Dict[str, Any]) -> Dict[str, Any]:
            if not enable_failure_analysis:
                return {"status": "skipped", "reason": "failure_analysis.enabled=false"}
            if cursor_client is None:
                return {"status": "skipped", "reason": "cursor api not configured"}
            return analyze_failures(
                payload=payload,
                repository_url=repo_url,
                repo_dir=repo_dir,
                cursor_client=cursor_client,
                wait_timeout=wait_timeout,
                poll_interval=poll_interval,
                auto_commit=auto_commit,
            )

        if not isinstance(extra_args, list):
            extra_args = []
        pytest_args = [str(item) for item in extra_args if str(item).strip()]

        summary = run_tests(
            repository_url=repo_url,
            repo_dir=repo_dir,
            branch=branch,
            tests_path=tests_path,
            report_dir=paths["report_dir"],
            docs_root=paths["docs_output_root"],
            pytest_args=pytest_args,
            pull_latest=pull_latest,
            failure_analyzer=failure_analyzer,
            timeout=timeout,
        )

        if self._email_enabled():
            try:
                subject = (
                    f"[自动化测试] 全量测试结果 - 失败 {summary['stats']['failed']} / "
                    f"错误 {summary['stats']['errors']} / 总计 {summary['stats']['total']}"
                )
                email_result = send_notification(
                    {
                        "subject": subject,
                        "test_report": summary,
                        "attachments": [
                            summary.get("junit_xml"),
                            summary.get("log_file"),
                        ],
                    }
                )
            except Exception as exc:
                email_result = {"status": "failed", "error": str(exc)}
            summary["email_notification"] = email_result
        else:
            summary["email_notification"] = {"status": "skipped", "reason": "email disabled"}

        return summary

    def cron_examples(self, python_bin: str = "python3") -> Dict[str, str]:
        schedule_cfg = self.config.get("schedule", {})
        monitor_cron = str(schedule_cfg.get("monitor_cron") or "0 2 * * *").strip()
        full_test_cron = str(schedule_cfg.get("full_test_cron") or "0 3 * * 1").strip()
        workspace = self._repo_dir()
        config_path = self.config_path

        base_cmd = f"cd {workspace} && {python_bin} huawei_doc_tester.py --config {config_path}"
        monitor_cmd = (
            f"{monitor_cron} {base_cmd} --action monitor-and-update "
            f">> {workspace}/logs/monitor_cron.log 2>&1"
        )
        full_test_cmd = (
            f"{full_test_cron} {base_cmd} --action run-full-test "
            f">> {workspace}/logs/full_test_cron.log 2>&1"
        )
        note = (
            "注意：Cursor Background Agent API 仅支持 Pro 及以上计划，"
            "且调用会消耗配额并产生计费。"
        )
        return {
            "monitor_cron": monitor_cmd,
            "full_test_cron": full_test_cmd,
            "note": note,
        }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Huawei doc-driven automated tester")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="Path to config.yaml",
    )
    parser.add_argument(
        "--action",
        required=True,
        choices=[
            "fetch-and-generate",
            "monitor-and-update",
            "run-full-test",
            "all",
            "print-cron",
        ],
        help="Action to run",
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

    tester = HuaweiDocTester(config_path=args.config)

    if args.action == "print-cron":
        print(json.dumps(tester.cron_examples(), ensure_ascii=False, indent=2))
        return

    if args.action == "fetch-and-generate":
        result = tester.fetch_and_generate()
    elif args.action == "monitor-and-update":
        result = tester.monitor_and_update()
    elif args.action == "run-full-test":
        result = tester.run_full_test()
    else:
        result = {
            "fetch_and_generate": tester.fetch_and_generate(),
            "monitor_and_update": tester.monitor_and_update(),
            "run_full_test": tester.run_full_test(),
        }

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
