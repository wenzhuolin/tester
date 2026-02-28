"""
Huawei Cloud documentation crawler.

Features:
- Crawl product docs under https://support.huaweicloud.com/
- Recursive traversal for API reference / operation guide pages
- Structured extraction: title, headings, code examples, parameter tables
- Incremental metadata: last-modified / etag / version / content hash
- Anti-crawl basics: User-Agent, retry, randomized delay
- Public function: fetch_docs(product_list)
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple, Union
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests


DEFAULT_OUTPUT_ROOT = Path("data/huawei_docs")
DEFAULT_TIMEOUT = 20
DEFAULT_MAX_RETRIES = 2
DEFAULT_DELAY_RANGE = (0.8, 1.8)
DEFAULT_MAX_PAGES = 200
SUPPORTED_HOST = "support.huaweicloud.com"

TRACKING_QUERY_PREFIXES = ("utm_",)
SKIP_SCHEMES = ("mailto:", "javascript:", "tel:")
SKIP_EXTENSIONS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    ".ico",
    ".pdf",
    ".zip",
    ".rar",
    ".7z",
    ".exe",
    ".dmg",
    ".mp4",
    ".avi",
    ".mov",
    ".css",
    ".js",
    ".xml",
    ".txt",
)
PARAMETER_HEADER_KEYWORDS = (
    "参数",
    "字段",
    "名称",
    "类型",
    "必选",
    "描述",
    "说明",
    "parameter",
    "name",
    "type",
    "required",
    "description",
)
VERSION_META_KEYS = (
    "doc-version",
    "version",
    "last-modified",
    "last_modified",
    "updated_at",
    "update_time",
    "publishdate",
)

COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 "
        "HuaweiDocsCrawler/1.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _normalize_code(value: str) -> str:
    raw = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in raw.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _unique_keep_order(values: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for item in values:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _normalize_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    scheme = parsed.scheme.lower() or "https"
    netloc = parsed.netloc.lower()
    path = parsed.path or "/"
    if not path.startswith("/"):
        path = "/" + path

    query_items = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith(TRACKING_QUERY_PREFIXES)
    ]
    query = urlencode(query_items, doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))


def _infer_product_name(start_url: str) -> str:
    parsed = urlsplit(start_url)
    segments = [seg for seg in parsed.path.split("/") if seg]
    if segments:
        return re.sub(r"[^a-zA-Z0-9_-]+", "_", segments[0].lower())
    return "product"


def _infer_scope_prefix(start_url: str) -> str:
    parsed = urlsplit(start_url)
    segments = [seg for seg in parsed.path.split("/") if seg]
    if segments:
        return f"/{segments[0]}/"
    return "/"


def _is_doc_link(path: str) -> bool:
    lower_path = path.lower()
    if any(lower_path.endswith(ext) for ext in SKIP_EXTENSIONS):
        return False
    if lower_path.endswith((".html", ".htm", "/")):
        return True
    basename = lower_path.rsplit("/", 1)[-1]
    return "." not in basename


def _resolve_and_filter_links(
    base_url: str,
    href_values: Iterable[str],
    allowed_host: str,
    scope_prefixes: List[str],
) -> List[str]:
    links: List[str] = []
    for raw_href in href_values:
        href = (raw_href or "").strip()
        if not href:
            continue
        if href.startswith("#") or href.lower().startswith(SKIP_SCHEMES):
            continue

        absolute = _normalize_url(urljoin(base_url, href))
        parsed = urlsplit(absolute)
        if parsed.netloc != allowed_host:
            continue
        if not _is_doc_link(parsed.path):
            continue
        if scope_prefixes and not any(parsed.path.startswith(prefix) for prefix in scope_prefixes):
            continue
        links.append(absolute)
    return _unique_keep_order(links)


def _safe_filename_from_url(url: str) -> str:
    parsed = urlsplit(url)
    basename = parsed.path.rstrip("/").split("/")[-1] or "index"
    basename = basename.replace(".html", "").replace(".htm", "") or "index"
    basename = re.sub(r"[^a-zA-Z0-9_-]+", "_", basename).strip("_") or "index"
    short_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    return f"{basename}_{short_hash}.json"


def _is_parameter_table(headers: List[str]) -> bool:
    if not headers:
        return False
    header_text = " ".join(headers).lower()
    return any(keyword in header_text for keyword in PARAMETER_HEADER_KEYWORDS)


def _normalize_delay_range(raw: Any) -> Tuple[float, float]:
    if (
        isinstance(raw, (list, tuple))
        and len(raw) == 2
    ):
        try:
            start = float(raw[0])
            end = float(raw[1])
            if start <= end and start >= 0:
                return (start, end)
            if end >= 0:
                return (end, start)
        except (TypeError, ValueError):
            pass
    return DEFAULT_DELAY_RANGE


class HuaweiDocHTMLExtractor(HTMLParser):
    """Extract structured content from Huawei doc HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.headings: List[Dict[str, Any]] = []
        self.code_examples: List[str] = []
        self.tables: List[Dict[str, Any]] = []
        self.parameter_tables: List[Dict[str, Any]] = []
        self.links: List[str] = []
        self.meta: Dict[str, str] = {}
        self.text_blocks: List[str] = []

        self._ignore_depth = 0
        self._in_title = False
        self._title_buf: List[str] = []

        self._active_heading_tag: Optional[str] = None
        self._heading_buf: List[str] = []

        self._code_depth = 0
        self._code_buf: List[str] = []

        self._text_block_depth = 0
        self._text_block_buf: List[str] = []

        self._active_link_href: Optional[str] = None
        self._link_text_buf: List[str] = []

        self._table_depth = 0
        self._table_rows: List[Dict[str, Any]] = []
        self._active_row: Optional[List[str]] = None
        self._active_row_is_header = False
        self._in_table_cell = False
        self._table_cell_buf: List[str] = []

        self._inline_tokens: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        lower = tag.lower()
        attrs_dict = {k.lower(): (v or "") for k, v in attrs}

        if lower in {"script", "style", "noscript"}:
            self._ignore_depth += 1
            return
        if self._ignore_depth > 0:
            return

        if lower == "title":
            self._in_title = True
            self._title_buf = []
            return

        if lower in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._active_heading_tag = lower
            self._heading_buf = []
            return

        if lower in {"pre", "code"}:
            if self._code_depth == 0:
                self._code_buf = []
            self._code_depth += 1
            return

        if lower in {"p", "li"}:
            if self._text_block_depth == 0:
                self._text_block_buf = []
            self._text_block_depth += 1
            return

        if lower == "a":
            href = attrs_dict.get("href", "").strip()
            if href:
                self._active_link_href = href
                self._link_text_buf = []
            return

        if lower == "meta":
            key = (
                attrs_dict.get("name")
                or attrs_dict.get("property")
                or attrs_dict.get("http-equiv")
            )
            value = attrs_dict.get("content", "").strip()
            if key and value:
                self.meta[key.lower().strip()] = value
            return

        if lower == "table":
            self._table_depth += 1
            if self._table_depth == 1:
                self._table_rows = []
            return

        if lower == "tr" and self._table_depth > 0:
            self._active_row = []
            self._active_row_is_header = False
            return

        if lower in {"th", "td"} and self._table_depth > 0 and self._active_row is not None:
            self._in_table_cell = True
            self._table_cell_buf = []
            if lower == "th":
                self._active_row_is_header = True
            return

        if lower == "br":
            if self._code_depth > 0:
                self._code_buf.append("\n")
            if self._text_block_depth > 0:
                self._text_block_buf.append(" ")
            if self._active_heading_tag:
                self._heading_buf.append(" ")
            if self._in_table_cell:
                self._table_cell_buf.append(" ")
            if self._active_link_href:
                self._link_text_buf.append(" ")

    def handle_endtag(self, tag: str) -> None:
        lower = tag.lower()
        if lower in {"script", "style", "noscript"}:
            if self._ignore_depth > 0:
                self._ignore_depth -= 1
            return
        if self._ignore_depth > 0:
            return

        if lower == "title":
            self._in_title = False
            title = _normalize_text("".join(self._title_buf))
            if title:
                self.title = title
            return

        if self._active_heading_tag == lower:
            title = _normalize_text("".join(self._heading_buf))
            if title:
                level = int(lower[1])
                self.headings.append({"level": level, "title": title})
            self._active_heading_tag = None
            self._heading_buf = []
            return

        if lower in {"pre", "code"} and self._code_depth > 0:
            self._code_depth -= 1
            if self._code_depth == 0:
                code = _normalize_code("".join(self._code_buf))
                if code:
                    self.code_examples.append(code)
            return

        if lower in {"p", "li"} and self._text_block_depth > 0:
            self._text_block_depth -= 1
            if self._text_block_depth == 0:
                text = _normalize_text("".join(self._text_block_buf))
                if text:
                    self.text_blocks.append(text)
            return

        if lower == "a" and self._active_link_href:
            link_text = _normalize_text("".join(self._link_text_buf))
            # Keep href only for traversal; text is not essential for this module.
            self.links.append(self._active_link_href)
            if link_text:
                self._inline_tokens.append(link_text)
            self._active_link_href = None
            self._link_text_buf = []
            return

        if lower in {"th", "td"} and self._in_table_cell and self._active_row is not None:
            cell_text = _normalize_text("".join(self._table_cell_buf))
            self._active_row.append(cell_text)
            self._in_table_cell = False
            self._table_cell_buf = []
            return

        if lower == "tr" and self._active_row is not None:
            if any(cell for cell in self._active_row):
                self._table_rows.append(
                    {"is_header": self._active_row_is_header, "cells": self._active_row}
                )
            self._active_row = None
            self._active_row_is_header = False
            return

        if lower == "table" and self._table_depth > 0:
            self._table_depth -= 1
            if self._table_depth == 0:
                table = self._build_table(self._table_rows)
                if table:
                    self.tables.append(table)
                    if _is_parameter_table(table["headers"]):
                        self.parameter_tables.append(table)
            return

    def handle_data(self, data: str) -> None:
        if self._ignore_depth > 0:
            return
        if not data:
            return

        if self._in_title:
            self._title_buf.append(data)
        if self._active_heading_tag:
            self._heading_buf.append(data)
        if self._code_depth > 0:
            self._code_buf.append(data)
        if self._text_block_depth > 0:
            self._text_block_buf.append(data)
        if self._active_link_href:
            self._link_text_buf.append(data)
        if self._in_table_cell:
            self._table_cell_buf.append(data)

        if self._code_depth == 0 and not self._in_table_cell:
            normalized = _normalize_text(data)
            if normalized:
                self._inline_tokens.append(normalized)

    def _build_table(self, rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not rows:
            return None

        header: List[str] = []
        body: List[List[str]] = []
        assigned_header = False

        for row in rows:
            cells = [cell for cell in row["cells"] if cell is not None]
            if not any(cells):
                continue

            if row.get("is_header") and not assigned_header:
                header = cells
                assigned_header = True
                continue

            if not assigned_header:
                header = cells
                assigned_header = True
                continue

            body.append(cells)

        if not header and not body:
            return None

        col_count = max(len(header), max((len(row) for row in body), default=0))
        if col_count <= 0:
            return None
        if not header:
            header = [f"col_{i + 1}" for i in range(col_count)]
        if len(header) < col_count:
            header.extend([f"col_{i + 1}" for i in range(len(header), col_count)])

        normalized_rows: List[List[str]] = []
        for row in body:
            normalized = list(row) + [""] * (col_count - len(row))
            normalized_rows.append(normalized[:col_count])

        return {"headers": header[:col_count], "rows": normalized_rows}

    def to_dict(self) -> Dict[str, Any]:
        title = self.title
        if not title and self.headings:
            title = self.headings[0]["title"]

        unique_codes = _unique_keep_order([code for code in self.code_examples if code.strip()])
        unique_links = _unique_keep_order(self.links)
        text_blocks = _unique_keep_order(self.text_blocks)
        full_text = "\n".join(text_blocks).strip()
        if not full_text:
            full_text = " ".join(_unique_keep_order(self._inline_tokens)).strip()

        return {
            "title": title,
            "headings": self.headings,
            "code_examples": unique_codes,
            "tables": self.tables,
            "parameter_tables": self.parameter_tables,
            "links": unique_links,
            "meta": self.meta,
            "text_blocks": text_blocks,
            "full_text": full_text,
        }


@dataclass
class ProductConfig:
    name: str
    start_urls: List[str]
    scope_prefixes: List[str]
    max_pages: int = DEFAULT_MAX_PAGES
    delay_range: Tuple[float, float] = DEFAULT_DELAY_RANGE
    timeout: int = DEFAULT_TIMEOUT


class HuaweiDocsCrawler:
    """Crawler implementation for Huawei Cloud support docs."""

    def __init__(
        self,
        output_root: Union[str, Path] = DEFAULT_OUTPUT_ROOT,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self.output_root = Path(output_root)
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update(COMMON_HEADERS)
        self._last_request_time = 0.0

    def fetch_docs(self, product_list: List[Union[str, Dict[str, Any]]]) -> List[Dict[str, Any]]:
        products = self._normalize_products(product_list)
        summaries: List[Dict[str, Any]] = []
        for product in products:
            summaries.append(self._crawl_product(product))
        return summaries

    def _normalize_products(self, product_list: List[Union[str, Dict[str, Any]]]) -> List[ProductConfig]:
        if not isinstance(product_list, list) or not product_list:
            raise ValueError("product_list must be a non-empty list")

        configs: List[ProductConfig] = []
        for item in product_list:
            if isinstance(item, str):
                start_url = _normalize_url(item)
                parsed = urlsplit(start_url)
                if parsed.netloc != SUPPORTED_HOST:
                    raise ValueError(
                        f"Unsupported host for product URL: {start_url}, expected {SUPPORTED_HOST}"
                    )
                configs.append(
                    ProductConfig(
                        name=_infer_product_name(start_url),
                        start_urls=[start_url],
                        scope_prefixes=[_infer_scope_prefix(start_url)],
                    )
                )
                continue

            if isinstance(item, dict):
                raw_urls = (
                    item.get("start_urls")
                    or [item.get("start_url") or item.get("url")]
                )
                start_urls = [
                    _normalize_url(str(url))
                    for url in raw_urls
                    if isinstance(url, str) and url.strip()
                ]
                if not start_urls:
                    raise ValueError(f"Invalid product config, missing start URLs: {item}")

                for url in start_urls:
                    if urlsplit(url).netloc != SUPPORTED_HOST:
                        raise ValueError(
                            f"Unsupported host for product URL: {url}, expected {SUPPORTED_HOST}"
                        )

                scope_prefixes: List[str] = []
                configured_prefixes = item.get("scope_prefixes") or item.get("scope_prefix")
                if configured_prefixes:
                    if isinstance(configured_prefixes, str):
                        scope_prefixes = [configured_prefixes]
                    elif isinstance(configured_prefixes, list):
                        scope_prefixes = [str(prefix) for prefix in configured_prefixes]
                if not scope_prefixes:
                    scope_prefixes = [_infer_scope_prefix(url) for url in start_urls]

                scope_prefixes = [
                    prefix if prefix.startswith("/") else f"/{prefix}"
                    for prefix in scope_prefixes
                ]
                scope_prefixes = [
                    prefix if prefix.endswith("/") else f"{prefix}/"
                    for prefix in scope_prefixes
                ]

                configs.append(
                    ProductConfig(
                        name=str(item.get("name") or _infer_product_name(start_urls[0])),
                        start_urls=start_urls,
                        scope_prefixes=_unique_keep_order(scope_prefixes),
                        max_pages=max(1, int(item.get("max_pages", DEFAULT_MAX_PAGES))),
                        delay_range=_normalize_delay_range(
                            item.get("delay_range", DEFAULT_DELAY_RANGE)
                        ),
                        timeout=int(item.get("timeout", DEFAULT_TIMEOUT)),
                    )
                )
                continue

            raise TypeError(f"Unsupported product config type: {type(item)}")

        return configs

    def _crawl_product(self, product: ProductConfig) -> Dict[str, Any]:
        fetched_at = _now_iso()
        product_dir = self.output_root / product.name
        pages_dir = product_dir / "pages"
        pages_dir.mkdir(parents=True, exist_ok=True)
        index_path = product_dir / "index.json"
        index_data = self._load_index(index_path)
        page_index = index_data.get("pages", {})

        queue: Deque[str] = deque(product.start_urls)
        queued = set(product.start_urls)
        visited = set()

        page_summaries: List[Dict[str, Any]] = []
        pages_saved = 0
        pages_not_modified = 0
        pages_failed = 0

        while queue and len(visited) < product.max_pages:
            current_url = queue.popleft()
            queued.discard(current_url)
            if current_url in visited:
                continue
            visited.add(current_url)

            prev_record = page_index.get(current_url, {})
            page_summary, discovered_links, updated_record = self._fetch_and_parse_page(
                url=current_url,
                prev_record=prev_record,
                product_scope_prefixes=product.scope_prefixes,
                timeout=product.timeout,
                delay_range=product.delay_range,
                pages_dir=pages_dir,
            )
            page_summaries.append(page_summary)

            status = page_summary.get("status")
            if status == "updated":
                pages_saved += 1
            elif status == "not_modified":
                pages_not_modified += 1
            elif status in {"failed", "error"}:
                pages_failed += 1

            if updated_record:
                page_index[current_url] = updated_record

            for link in discovered_links:
                if link not in visited and link not in queued:
                    queue.append(link)
                    queued.add(link)

        index_data["product"] = product.name
        index_data["scope_prefixes"] = product.scope_prefixes
        index_data["updated_at"] = _now_iso()
        index_data["pages"] = page_index
        index_path.write_text(
            json.dumps(index_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        return {
            "product": product.name,
            "fetched_at": fetched_at,
            "output_dir": str(product_dir),
            "index_file": str(index_path),
            "pages_crawled": len(visited),
            "pages_saved": pages_saved,
            "pages_not_modified": pages_not_modified,
            "pages_failed": pages_failed,
            "files": page_summaries,
        }

    def _load_index(self, index_path: Path) -> Dict[str, Any]:
        if not index_path.exists():
            return {"pages": {}}
        try:
            data = json.loads(index_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                pages = data.get("pages")
                if isinstance(pages, dict):
                    return data
        except json.JSONDecodeError:
            pass
        return {"pages": {}}

    def _respect_delay(self, delay_range: Tuple[float, float]) -> None:
        min_delay, max_delay = delay_range
        min_delay = max(0.0, float(min_delay))
        max_delay = max(min_delay, float(max_delay))
        now = time.monotonic()
        elapsed = now - self._last_request_time
        target_delay = random.uniform(min_delay, max_delay)
        if elapsed < target_delay:
            time.sleep(target_delay - elapsed)
        self._last_request_time = time.monotonic()

    def _request_page(
        self,
        url: str,
        timeout: int,
        conditional_headers: Optional[Dict[str, str]] = None,
    ) -> requests.Response:
        headers = dict(COMMON_HEADERS)
        if conditional_headers:
            headers.update(conditional_headers)

        transient_codes = {408, 429, 500, 502, 503, 504}
        last_exc: Optional[Exception] = None

        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.get(
                    url,
                    headers=headers,
                    timeout=timeout,
                    allow_redirects=True,
                )
                if response.status_code in transient_codes and attempt < self.max_retries:
                    time.sleep(2**attempt)
                    continue
                return response
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    time.sleep(2**attempt)
                    continue
                break

        raise RuntimeError(f"Failed to request URL: {url}, error={last_exc}")

    def _fetch_and_parse_page(
        self,
        url: str,
        prev_record: Dict[str, Any],
        product_scope_prefixes: List[str],
        timeout: int,
        delay_range: Tuple[float, float],
        pages_dir: Path,
    ) -> Tuple[Dict[str, Any], List[str], Optional[Dict[str, Any]]]:
        conditional_headers: Dict[str, str] = {}
        if prev_record.get("etag"):
            conditional_headers["If-None-Match"] = str(prev_record["etag"])
        if prev_record.get("last_modified"):
            conditional_headers["If-Modified-Since"] = str(prev_record["last_modified"])

        self._respect_delay(delay_range)
        fetched_at = _now_iso()

        try:
            response = self._request_page(url, timeout=timeout, conditional_headers=conditional_headers)
        except Exception as exc:
            return (
                {
                    "url": url,
                    "status": "failed",
                    "error": str(exc),
                    "fetched_at": fetched_at,
                },
                [],
                None,
            )

        final_url = _normalize_url(response.url)
        content_type = response.headers.get("Content-Type", "")
        etag = response.headers.get("ETag")
        last_modified = response.headers.get("Last-Modified")

        # Handle HTTP 304 for incremental updates.
        if response.status_code == 304:
            cached_links = prev_record.get("links", [])
            links = [link for link in cached_links if isinstance(link, str)]
            summary = {
                "url": final_url,
                "status": "not_modified",
                "file_path": prev_record.get("file_path"),
                "last_modified": prev_record.get("last_modified"),
                "etag": prev_record.get("etag"),
                "version": prev_record.get("version"),
                "fetched_at": fetched_at,
            }
            updated = dict(prev_record)
            updated["last_checked_at"] = fetched_at
            return summary, links, updated

        if response.status_code >= 400:
            return (
                {
                    "url": final_url,
                    "status": "error",
                    "http_status": response.status_code,
                    "fetched_at": fetched_at,
                },
                [],
                None,
            )

        if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
            summary = {
                "url": final_url,
                "status": "skipped_non_html",
                "http_status": response.status_code,
                "content_type": content_type,
                "fetched_at": fetched_at,
            }
            return summary, [], None

        html = response.text
        content_hash = hashlib.sha256(html.encode(response.encoding or "utf-8", errors="ignore")).hexdigest()

        extractor = HuaweiDocHTMLExtractor()
        extractor.feed(html)
        extracted = extractor.to_dict()

        scope_prefixes = prev_record.get("scope_prefixes", [])
        if not isinstance(scope_prefixes, list):
            scope_prefixes = []

        # Fallback for old index format where scope wasn't persisted per page.
        if not scope_prefixes:
            scope_prefixes = product_scope_prefixes or [_infer_scope_prefix(final_url)]

        links = _resolve_and_filter_links(
            base_url=final_url,
            href_values=extracted["links"],
            allowed_host=SUPPORTED_HOST,
            scope_prefixes=scope_prefixes,
        )

        version = self._resolve_version(
            extracted=extracted,
            etag=etag,
            last_modified=last_modified,
        )

        file_name = _safe_filename_from_url(final_url)
        file_path = pages_dir / file_name
        page_data = {
            "url": final_url,
            "fetched_at": fetched_at,
            "http_status": response.status_code,
            "last_modified": last_modified,
            "etag": etag,
            "version": version,
            "content_hash": content_hash,
            "title": extracted["title"],
            "headings": extracted["headings"],
            "code_examples": extracted["code_examples"],
            "parameter_tables": extracted["parameter_tables"],
            "tables": extracted["tables"],
            "text_blocks": extracted["text_blocks"],
            "full_text": extracted["full_text"],
            "links": links,
            "meta": extracted["meta"],
        }
        file_path.write_text(json.dumps(page_data, ensure_ascii=False, indent=2), encoding="utf-8")

        updated_record = {
            "url": final_url,
            "file_path": str(file_path),
            "title": extracted["title"],
            "last_modified": last_modified,
            "etag": etag,
            "version": version,
            "content_hash": content_hash,
            "links": links,
            "scope_prefixes": scope_prefixes,
            "updated_at": fetched_at,
            "last_checked_at": fetched_at,
        }

        summary = {
            "url": final_url,
            "status": "updated",
            "file_path": str(file_path),
            "title": extracted["title"],
            "last_modified": last_modified,
            "etag": etag,
            "version": version,
            "fetched_at": fetched_at,
        }
        return summary, links, updated_record

    def _resolve_version(
        self,
        extracted: Dict[str, Any],
        etag: Optional[str],
        last_modified: Optional[str],
    ) -> Optional[str]:
        if etag:
            return str(etag)
        if last_modified:
            return str(last_modified)

        meta = extracted.get("meta", {})
        if isinstance(meta, dict):
            lower_meta = {str(k).lower(): str(v) for k, v in meta.items()}
            for key in VERSION_META_KEYS:
                if key in lower_meta:
                    return lower_meta[key]

        text = extracted.get("full_text", "") or ""
        if isinstance(text, str):
            date_match = re.search(
                r"(20\d{2}[年\-/\.]\d{1,2}[月\-/\.]\d{1,2}日?)",
                text,
            )
            if date_match:
                return date_match.group(1)

        return None


def fetch_docs(product_list: List[Union[str, Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """
    Crawl Huawei Cloud docs for products and return summary information.

    Args:
        product_list: list of product configs.
            Supported formats:
            1) URL string:
               "https://support.huaweicloud.com/ecs/index.html"
            2) Dict:
               {
                 "name": "ecs",
                 "start_url": "https://support.huaweicloud.com/ecs/index.html",
                 "max_pages": 100,
                 "scope_prefixes": ["/ecs/"],
                 "delay_range": [1.0, 2.0],
                 "timeout": 20
               }

    Returns:
        A list of per-product summary dicts, including generated file paths and timestamps.
    """
    crawler = HuaweiDocsCrawler()
    return crawler.fetch_docs(product_list)


if __name__ == "__main__":
    # Example: small crawl with strict page cap.
    sample_products = [
        {
            "name": "ecs",
            "start_url": "https://support.huaweicloud.com/ecs/index.html",
            "max_pages": 3,
            "scope_prefixes": ["/ecs/"],
        }
    ]
    result = fetch_docs(sample_products)
    print(json.dumps(result, ensure_ascii=False, indent=2))
