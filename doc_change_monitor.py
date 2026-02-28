"""
Huawei Cloud docs change monitor.

Responsibilities:
- Run periodic doc crawling
- Detect added/modified/deleted pages from previous snapshot
- Trigger incremental pytest case generation via Cursor API
- Persist change logs
- Trigger email notification hook
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from urllib.parse import urlsplit

from case_generator import generate_test_cases
from cursor_api_client import CursorAPIClient
from huawei_docs_fetcher import DEFAULT_OUTPUT_ROOT, HuaweiDocsCrawler


LOGGER = logging.getLogger(__name__)

DEFAULT_PRODUCTS: List[Dict[str, Any]] = [
    {
        "name": "ecs",
        "start_url": "https://support.huaweicloud.com/ecs/index.html",
        "scope_prefixes": ["/ecs/"],
        "max_pages": 200,
    }
]
DEFAULT_LOG_DIR = Path("data/monitor_logs")
DEFAULT_CHANGE_LOG_FILE = DEFAULT_LOG_DIR / "change_log.jsonl"
DEFAULT_GENERATED_TESTS_DIR = Path("tests/generated")
DELETED_HTTP_CODES = {404, 410}


@dataclass
class ChangeSet:
    added: List[str]
    modified: List[str]
    deleted: List[str]
    details: List[Dict[str, Any]]

    @property
    def total(self) -> int:
        return len(self.added) + len(self.modified) + len(self.deleted)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _infer_product_name_from_url(url: str) -> str:
    parsed = urlsplit(url)
    segments = [seg for seg in parsed.path.split("/") if seg]
    if segments:
        return re.sub(r"[^a-zA-Z0-9_-]+", "_", segments[0].lower())
    return "product"


def _get_product_name(product_config: Union[str, Dict[str, Any]]) -> str:
    if isinstance(product_config, str):
        return _infer_product_name_from_url(product_config)

    if isinstance(product_config, dict):
        explicit = product_config.get("name")
        if isinstance(explicit, str) and explicit.strip():
            return explicit.strip()

        start_urls = product_config.get("start_urls")
        if isinstance(start_urls, list):
            for url in start_urls:
                if isinstance(url, str) and url.strip():
                    return _infer_product_name_from_url(url)

        for key in ("start_url", "url"):
            raw_url = product_config.get(key)
            if isinstance(raw_url, str) and raw_url.strip():
                return _infer_product_name_from_url(raw_url)

    return "product"


def _load_products_from_env() -> List[Union[str, Dict[str, Any]]]:
    raw_json = os.getenv("HUAWEI_DOC_PRODUCT_LIST")
    if not raw_json:
        return list(DEFAULT_PRODUCTS)

    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "HUAWEI_DOC_PRODUCT_LIST must be valid JSON list"
        ) from exc

    if not isinstance(parsed, list) or not parsed:
        raise ValueError("HUAWEI_DOC_PRODUCT_LIST must be a non-empty JSON list")
    return parsed


def _load_index_pages(index_path: Path) -> Dict[str, Dict[str, Any]]:
    if not index_path.exists():
        return {}
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    pages = data.get("pages")
    if not isinstance(pages, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for url, record in pages.items():
        if isinstance(url, str) and isinstance(record, dict):
            out[url] = record
    return out


def _build_pre_snapshot(
    product_list: List[Union[str, Dict[str, Any]]],
    output_root: Path,
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    snapshot: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for product in product_list:
        product_name = _get_product_name(product)
        index_path = output_root / product_name / "index.json"
        snapshot[product_name] = _load_index_pages(index_path)
    return snapshot


def _is_modified(old: Dict[str, Any], new: Dict[str, Any]) -> Tuple[bool, List[str]]:
    reasons: List[str] = []

    old_hash = old.get("content_hash")
    new_hash = new.get("content_hash")
    if old_hash and new_hash and old_hash != new_hash:
        reasons.append("content_hash_changed")

    old_last_modified = old.get("last_modified")
    new_last_modified = new.get("last_modified")
    if old_last_modified and new_last_modified and old_last_modified != new_last_modified:
        reasons.append("last_modified_changed")

    old_version = old.get("version")
    new_version = new.get("version")
    if old_version and new_version and old_version != new_version:
        reasons.append("version_changed")

    old_etag = old.get("etag")
    new_etag = new.get("etag")
    if old_etag and new_etag and old_etag != new_etag:
        reasons.append("etag_changed")

    return (len(reasons) > 0, reasons)


def _detect_changes(
    old_pages: Dict[str, Dict[str, Any]],
    new_pages: Dict[str, Dict[str, Any]],
    run_files: List[Dict[str, Any]],
) -> ChangeSet:
    old_urls = set(old_pages.keys())
    new_urls = set(new_pages.keys())

    added = sorted(new_urls - old_urls)
    modified: List[str] = []
    details: List[Dict[str, Any]] = []

    for url in sorted(old_urls & new_urls):
        is_changed, reasons = _is_modified(old_pages[url], new_pages[url])
        if is_changed:
            modified.append(url)
            details.append(
                {
                    "url": url,
                    "change_type": "modified",
                    "reasons": reasons,
                }
            )

    for url in added:
        details.append({"url": url, "change_type": "added", "reasons": ["new_page"]})

    deleted_candidates = {
        str(item.get("url"))
        for item in run_files
        if isinstance(item, dict)
        and item.get("status") == "error"
        and item.get("http_status") in DELETED_HTTP_CODES
        and isinstance(item.get("url"), str)
    }
    deleted = sorted((old_urls & deleted_candidates) - set(modified))
    for url in deleted:
        details.append(
            {
                "url": url,
                "change_type": "deleted",
                "reasons": ["http_deleted"],
            }
        )

    return ChangeSet(
        added=added,
        modified=modified,
        deleted=deleted,
        details=sorted(details, key=lambda x: (x["change_type"], x["url"])),
    )


def _read_page_payload(page_record: Dict[str, Any]) -> Dict[str, Any]:
    file_path = page_record.get("file_path")
    if not isinstance(file_path, str) or not file_path.strip():
        return {}

    path = Path(file_path)
    if not path.exists() or not path.is_file():
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _extract_parameters(page_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    parameter_tables = page_data.get("parameter_tables")
    if not isinstance(parameter_tables, list):
        return []

    parameters: List[Dict[str, Any]] = []
    for table in parameter_tables:
        if not isinstance(table, dict):
            continue
        headers = table.get("headers")
        rows = table.get("rows")
        if not isinstance(headers, list) or not isinstance(rows, list):
            continue

        normalized_headers = [str(header).strip() for header in headers]
        for row in rows:
            if not isinstance(row, list):
                continue
            row_values = [str(cell).strip() for cell in row]
            row_map: Dict[str, Any] = {}
            for idx, header in enumerate(normalized_headers):
                if header:
                    row_map[header] = row_values[idx] if idx < len(row_values) else ""
            if row_map:
                parameters.append(row_map)
    return parameters


def _build_operation_id(product: str, url: str) -> str:
    parsed = urlsplit(url)
    path_part = parsed.path.strip("/").replace("/", "_")
    path_part = re.sub(r"[^a-zA-Z0-9_]+", "_", path_part).strip("_")
    if not path_part:
        path_part = "index"
    return f"{product}_{path_part}"[:100]


def _build_incremental_doc_data(
    product: str,
    change_set: ChangeSet,
    old_pages: Dict[str, Dict[str, Any]],
    new_pages: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    operations: List[Dict[str, Any]] = []

    for detail in change_set.details:
        url = detail["url"]
        change_type = detail["change_type"]
        page_record = new_pages.get(url) or old_pages.get(url) or {}
        page_data = _read_page_payload(page_record)

        title = page_data.get("title") or page_record.get("title") or url
        full_text = page_data.get("full_text") or ""
        headings = page_data.get("headings") if isinstance(page_data.get("headings"), list) else []
        code_examples = (
            page_data.get("code_examples") if isinstance(page_data.get("code_examples"), list) else []
        )
        request_examples = [{"code": code} for code in code_examples[:6] if isinstance(code, str)]

        operations.append(
            {
                "operation_id": _build_operation_id(product, url),
                "title": title,
                "api_description": full_text,
                "parameters": _extract_parameters(page_data),
                "request_examples": request_examples,
                "response_examples": [],
                "headings": headings,
                "source_url": url,
                "change_type": change_type,
                "change_reasons": detail.get("reasons", []),
                "version": page_record.get("version"),
                "last_modified": page_record.get("last_modified"),
            }
        )

    return {
        "title": f"Huawei Cloud {product} incremental documentation changes",
        "product": product,
        "generated_at": _now_iso(),
        "change_summary": {
            "added": len(change_set.added),
            "modified": len(change_set.modified),
            "deleted": len(change_set.deleted),
            "total": change_set.total,
        },
        "apis": operations,
    }


def _append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _save_run_snapshot(log_dir: Path, payload: Dict[str, Any]) -> str:
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    file_path = log_dir / f"run_{stamp}.json"
    file_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return str(file_path)


def _resolve_email_notifier(
    custom_notifier: Optional[Callable[[Dict[str, Any]], Any]],
) -> Optional[Callable[[Dict[str, Any]], Any]]:
    if callable(custom_notifier):
        return custom_notifier

    candidates = [
        ("email_notifier", "send_change_notification"),
        ("email_notifier", "send_notification"),
        ("mail_notifier", "send_change_notification"),
        ("mail_module", "send_change_notification"),
    ]
    for module_name, function_name in candidates:
        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue
        notifier = getattr(module, function_name, None)
        if callable(notifier):
            return notifier
    return None


def _trigger_email_notification(
    payload: Dict[str, Any],
    notifier: Optional[Callable[[Dict[str, Any]], Any]] = None,
) -> Dict[str, Any]:
    resolved = _resolve_email_notifier(notifier)
    if not callable(resolved):
        return {"status": "skipped", "reason": "email module not available"}

    try:
        result = resolved(payload)
        return {"status": "sent", "result": result}
    except Exception as exc:
        return {"status": "failed", "error": str(exc)}


def _resolve_repository_url(repository_url: Optional[str]) -> Optional[str]:
    if repository_url and repository_url.strip():
        return repository_url.strip()
    for key in ("TEST_REPOSITORY_URL", "CURSOR_TEST_REPOSITORY", "TARGET_TEST_REPO_URL"):
        value = os.getenv(key, "").strip()
        if value:
            return value
    return None


def monitor_changes(
    product_list: Optional[List[Union[str, Dict[str, Any]]]] = None,
    repository_url: Optional[str] = None,
    output_root: Union[str, Path] = DEFAULT_OUTPUT_ROOT,
    generated_tests_dir: Union[str, Path] = DEFAULT_GENERATED_TESTS_DIR,
    change_log_file: Union[str, Path] = DEFAULT_CHANGE_LOG_FILE,
    cursor_client: Optional[CursorAPIClient] = None,
    email_notifier: Optional[Callable[[Dict[str, Any]], Any]] = None,
    crawler: Optional[HuaweiDocsCrawler] = None,
    case_generator_fn: Optional[
        Callable[[Dict[str, Any], str, CursorAPIClient, str], List[str]]
    ] = None,
) -> Dict[str, Any]:
    """
    Monitor Huawei docs changes and trigger incremental case generation.

    Returns a run summary dictionary.
    """
    started_at = _now_iso()
    products = product_list or _load_products_from_env()

    output_root_path = Path(output_root)
    generated_tests_path = Path(generated_tests_dir)
    change_log_path = Path(change_log_file)

    case_generator = case_generator_fn or (
        lambda doc_data, repo_url, client, out_dir: generate_test_cases(
            doc_data=doc_data,
            repository_url=repo_url,
            cursor_client=client,
            output_dir=out_dir,
        )
    )

    pre_snapshot = _build_pre_snapshot(products, output_root_path)
    crawler_instance = crawler or HuaweiDocsCrawler(output_root=output_root_path)
    crawl_result = crawler_instance.fetch_docs(products)

    repo_url = _resolve_repository_url(repository_url)

    run_products: List[Dict[str, Any]] = []
    total_added = 0
    total_modified = 0
    total_deleted = 0
    total_generated_files = 0

    shared_client = cursor_client

    for product_summary in crawl_result:
        product_name = str(product_summary.get("product") or "product")
        index_file = Path(str(product_summary.get("index_file", "")))
        run_files = product_summary.get("files", [])
        if not isinstance(run_files, list):
            run_files = []

        old_pages = pre_snapshot.get(product_name, {})
        new_pages = _load_index_pages(index_file)
        change_set = _detect_changes(old_pages=old_pages, new_pages=new_pages, run_files=run_files)

        total_added += len(change_set.added)
        total_modified += len(change_set.modified)
        total_deleted += len(change_set.deleted)

        generation: Dict[str, Any] = {"status": "skipped", "generated_files": []}

        if change_set.total > 0:
            if not repo_url:
                generation = {
                    "status": "failed",
                    "error": "missing repository_url (or TEST_REPOSITORY_URL env)",
                    "generated_files": [],
                }
            else:
                try:
                    if shared_client is None:
                        shared_client = CursorAPIClient.from_env()

                    doc_data = _build_incremental_doc_data(
                        product=product_name,
                        change_set=change_set,
                        old_pages=old_pages,
                        new_pages=new_pages,
                    )
                    generated_files = case_generator(
                        doc_data,
                        repo_url,
                        shared_client,
                        str(generated_tests_path),
                    )
                    generation = {
                        "status": "triggered",
                        "generated_files": generated_files,
                        "generated_count": len(generated_files),
                    }
                    total_generated_files += len(generated_files)
                except Exception as exc:
                    generation = {
                        "status": "failed",
                        "error": str(exc),
                        "generated_files": [],
                    }

        run_products.append(
            {
                "product": product_name,
                "index_file": str(index_file),
                "changes": {
                    "added": change_set.added,
                    "modified": change_set.modified,
                    "deleted": change_set.deleted,
                    "details": change_set.details,
                    "total": change_set.total,
                },
                "generation": generation,
                "crawl_stats": {
                    "pages_crawled": product_summary.get("pages_crawled"),
                    "pages_saved": product_summary.get("pages_saved"),
                    "pages_not_modified": product_summary.get("pages_not_modified"),
                    "pages_failed": product_summary.get("pages_failed"),
                },
            }
        )

    run_summary: Dict[str, Any] = {
        "started_at": started_at,
        "finished_at": _now_iso(),
        "repository_url": repo_url,
        "products": run_products,
        "totals": {
            "products": len(run_products),
            "added": total_added,
            "modified": total_modified,
            "deleted": total_deleted,
            "changed": total_added + total_modified + total_deleted,
            "generated_files": total_generated_files,
        },
    }

    _append_jsonl(change_log_path, run_summary)
    run_summary["run_log_file"] = _save_run_snapshot(change_log_path.parent, run_summary)
    run_summary["change_log_file"] = str(change_log_path)
    run_summary["email_notification"] = _trigger_email_notification(
        payload=run_summary,
        notifier=email_notifier,
    )
    return run_summary


def build_cron_command(
    python_bin: str = "python3",
    script_path: str = "doc_change_monitor.py",
    run_time: str = "0 2 * * *",
) -> str:
    """
    Build example cron expression to run monitor daily.
    """
    return f"{run_time} cd /workspace && {python_bin} {script_path} --once"


def schedule_daily_monitor(
    product_list: Optional[List[Union[str, Dict[str, Any]]]] = None,
    repository_url: Optional[str] = None,
    hour: int = 2,
    minute: int = 0,
    timezone: str = "Asia/Shanghai",
) -> None:
    """
    Schedule daily monitor with APScheduler.
    """
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "APScheduler is not installed. Install with `pip install apscheduler` "
            "or use cron: "
            + build_cron_command()
        ) from exc

    scheduler = BlockingScheduler(timezone=timezone)
    scheduler.add_job(
        monitor_changes,
        trigger="cron",
        hour=hour,
        minute=minute,
        id="huawei_doc_change_monitor",
        replace_existing=True,
        kwargs={
            "product_list": product_list,
            "repository_url": repository_url,
        },
    )
    LOGGER.info(
        "Scheduler started: daily at %02d:%02d (%s)",
        hour,
        minute,
        timezone,
    )
    scheduler.start()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Huawei docs change monitor")
    parser.add_argument("--once", action="store_true", help="Run monitor once and exit")
    parser.add_argument(
        "--schedule-daily",
        action="store_true",
        help="Run monitor in APScheduler daily mode",
    )
    parser.add_argument("--hour", type=int, default=2, help="Daily hour for scheduler")
    parser.add_argument("--minute", type=int, default=0, help="Daily minute for scheduler")
    parser.add_argument(
        "--repo-url",
        default=None,
        help="Target test repository URL (or use TEST_REPOSITORY_URL env)",
    )
    parser.add_argument(
        "--products-json",
        default=None,
        help="Inline JSON list for product configs",
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

    product_list = None
    if args.products_json:
        product_list = json.loads(args.products_json)
        if not isinstance(product_list, list):
            raise ValueError("--products-json must be a JSON list")

    if args.schedule_daily:
        schedule_daily_monitor(
            product_list=product_list,
            repository_url=args.repo_url,
            hour=args.hour,
            minute=args.minute,
        )
        return

    # Default behavior: run once.
    summary = monitor_changes(
        product_list=product_list,
        repository_url=args.repo_url,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
