"""
Email notification module for doc-change and test-report delivery.

Capabilities:
- SMTP sending with configurable sender/auth/recipients
- Styled HTML body for change monitor + test summary
- Optional attachments (log/JUnit/analysis files)
- Compatibility entrypoints:
  - send_email(subject, body_html, attachments=None)
  - send_change_notification(payload)
  - send_notification(payload)
"""

from __future__ import annotations

import html
import json
import logging
import mimetypes
import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union


LOGGER = logging.getLogger(__name__)


def _to_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _split_recipients(raw: str) -> List[str]:
    parts = [item.strip() for item in raw.replace(";", ",").split(",")]
    return [item for item in parts if item]


def _escape(value: Any) -> str:
    return html.escape(str(value) if value is not None else "")


def _truncate(value: Any, max_len: int = 180) -> str:
    text = str(value) if value is not None else ""
    text = " ".join(text.split())
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


@dataclass
class SMTPConfig:
    host: str
    port: int
    sender: str
    password: str
    recipients: List[str]
    username: Optional[str] = None
    use_tls: bool = True
    use_ssl: bool = False
    timeout: int = 30
    sender_name: Optional[str] = None
    dry_run: bool = False

    @classmethod
    def from_env(cls) -> "SMTPConfig":
        host = os.getenv("SMTP_HOST", "").strip()
        sender = os.getenv("SMTP_SENDER", "").strip() or os.getenv("SMTP_USERNAME", "").strip()
        username = os.getenv("SMTP_USERNAME", "").strip() or sender
        password = os.getenv("SMTP_PASSWORD", "").strip()
        recipients = _split_recipients(os.getenv("SMTP_RECIPIENTS", ""))
        sender_name = os.getenv("SMTP_SENDER_NAME", "").strip() or None

        use_ssl = _to_bool(os.getenv("SMTP_USE_SSL"), default=False)
        default_port = 465 if use_ssl else 587
        port = int(os.getenv("SMTP_PORT", str(default_port)))
        use_tls = _to_bool(os.getenv("SMTP_USE_TLS"), default=not use_ssl)
        timeout = int(os.getenv("SMTP_TIMEOUT", "30"))
        dry_run = _to_bool(os.getenv("SMTP_DRY_RUN"), default=False)

        if not host:
            raise ValueError("SMTP_HOST is required")
        if not sender:
            raise ValueError("SMTP_SENDER or SMTP_USERNAME is required")
        if not recipients:
            raise ValueError("SMTP_RECIPIENTS is required")
        if not dry_run and not password:
            raise ValueError("SMTP_PASSWORD is required unless SMTP_DRY_RUN=true")

        return cls(
            host=host,
            port=port,
            sender=sender,
            password=password,
            recipients=recipients,
            username=username,
            use_tls=use_tls,
            use_ssl=use_ssl,
            timeout=timeout,
            sender_name=sender_name,
            dry_run=dry_run,
        )


def _normalize_attachments(
    attachments: Optional[Iterable[Union[str, Path, Dict[str, Any]]]],
) -> Dict[str, List[Path]]:
    valid: List[Path] = []
    missing: List[Path] = []
    if not attachments:
        return {"valid": valid, "missing": missing}

    for item in attachments:
        path_value: Optional[Union[str, Path]] = None
        if isinstance(item, (str, Path)):
            path_value = item
        elif isinstance(item, dict):
            maybe = item.get("path")
            if isinstance(maybe, (str, Path)):
                path_value = maybe

        if path_value is None:
            continue

        path = Path(path_value).expanduser()
        if path.exists() and path.is_file():
            valid.append(path.resolve())
        else:
            missing.append(path)

    return {"valid": valid, "missing": missing}


def _attach_files(msg: EmailMessage, files: List[Path]) -> List[str]:
    attached: List[str] = []
    for file_path in files:
        content = file_path.read_bytes()
        mime_type, _ = mimetypes.guess_type(str(file_path))
        if mime_type:
            maintype, subtype = mime_type.split("/", 1)
        else:
            maintype, subtype = "application", "octet-stream"

        msg.add_attachment(
            content,
            maintype=maintype,
            subtype=subtype,
            filename=file_path.name,
        )
        attached.append(str(file_path))
    return attached


def send_email(
    subject: str,
    body_html: str,
    attachments: Optional[Iterable[Union[str, Path, Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """
    Send HTML email via SMTP.

    Environment variables:
    - SMTP_HOST
    - SMTP_PORT (optional, default 587 or 465 for SSL)
    - SMTP_SENDER (fallback SMTP_USERNAME)
    - SMTP_USERNAME (optional, defaults to sender)
    - SMTP_PASSWORD
    - SMTP_RECIPIENTS (comma/semicolon separated)
    - SMTP_USE_TLS (default true if not SSL)
    - SMTP_USE_SSL (default false)
    - SMTP_TIMEOUT (default 30)
    - SMTP_SENDER_NAME (optional)
    - SMTP_DRY_RUN (optional; true to skip network send)
    """
    config = SMTPConfig.from_env()
    normalized = _normalize_attachments(attachments)
    valid_attachments = normalized["valid"]
    missing_attachments = normalized["missing"]

    msg = EmailMessage()
    from_header = (
        f"{config.sender_name} <{config.sender}>"
        if config.sender_name
        else config.sender
    )
    msg["Subject"] = subject
    msg["From"] = from_header
    msg["To"] = ", ".join(config.recipients)
    msg.set_content("This email contains HTML content. Please use an HTML-capable email client.")
    msg.add_alternative(body_html, subtype="html")

    attached_files = _attach_files(msg, valid_attachments)

    if config.dry_run:
        return {
            "status": "dry_run",
            "subject": subject,
            "recipients": config.recipients,
            "attached_files": attached_files,
            "missing_attachments": [str(path) for path in missing_attachments],
        }

    if config.use_ssl:
        with smtplib.SMTP_SSL(config.host, config.port, timeout=config.timeout) as smtp:
            if config.username:
                smtp.login(config.username, config.password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(config.host, config.port, timeout=config.timeout) as smtp:
            smtp.ehlo()
            if config.use_tls:
                smtp.starttls()
                smtp.ehlo()
            if config.username:
                smtp.login(config.username, config.password)
            smtp.send_message(msg)

    return {
        "status": "sent",
        "subject": subject,
        "recipients": config.recipients,
        "attached_files": attached_files,
        "missing_attachments": [str(path) for path in missing_attachments],
    }


def _build_change_monitor_section(change_data: Optional[Dict[str, Any]]) -> str:
    if not isinstance(change_data, dict):
        return """
        <div class="card">
          <h2>文档变更监控</h2>
          <p class="muted">本次未提供文档变更数据。</p>
        </div>
        """

    totals = change_data.get("totals", {})
    changed = int(totals.get("changed", 0) or 0)
    added = int(totals.get("added", 0) or 0)
    modified = int(totals.get("modified", 0) or 0)
    deleted = int(totals.get("deleted", 0) or 0)

    rows: List[str] = []
    products = change_data.get("products", [])
    if isinstance(products, list):
        for product in products:
            if not isinstance(product, dict):
                continue
            product_name = _escape(product.get("product") or "-")
            changes = product.get("changes", {})
            if not isinstance(changes, dict):
                changes = {}
            details = changes.get("details", [])
            if not isinstance(details, list):
                details = []

            rendered_items: List[str] = []
            for detail in details[:20]:
                if not isinstance(detail, dict):
                    continue
                change_type = _escape(detail.get("change_type") or "unknown")
                url = _escape(detail.get("url") or "")
                reasons = detail.get("reasons")
                reason_text = ""
                if isinstance(reasons, list) and reasons:
                    reason_text = f"（{_escape(', '.join(str(r) for r in reasons))}）"
                rendered_items.append(
                    f"<li><span class='tag {change_type}'>{change_type}</span> "
                    f"<span class='mono'>{url}</span> {reason_text}</li>"
                )

            if not rendered_items:
                rendered_items.append("<li class='muted'>无页面变更</li>")

            rows.append(
                f"""
                <tr>
                  <td>{product_name}</td>
                  <td>{int(changes.get('total', 0) or 0)}</td>
                  <td>{int(len(changes.get('added', []) or []))}</td>
                  <td>{int(len(changes.get('modified', []) or []))}</td>
                  <td>{int(len(changes.get('deleted', []) or []))}</td>
                  <td><ul class='compact'>{"".join(rendered_items)}</ul></td>
                </tr>
                """
            )

    if not rows:
        rows.append(
            "<tr><td colspan='6' class='muted'>没有产品级变更明细。</td></tr>"
        )

    return f"""
    <div class="card">
      <h2>文档变更监控</h2>
      <p>
        本次变更总数：<b>{changed}</b>（新增 {added}、修改 {modified}、删除 {deleted}）
      </p>
      <table>
        <thead>
          <tr>
            <th>产品</th>
            <th>总变更</th>
            <th>新增</th>
            <th>修改</th>
            <th>删除</th>
            <th>页面明细</th>
          </tr>
        </thead>
        <tbody>
          {"".join(rows)}
        </tbody>
      </table>
    </div>
    """


def _extract_failure_analysis_map(test_data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    mapping: Dict[str, Dict[str, Any]] = {}
    fa = test_data.get("failure_analysis")
    if not isinstance(fa, dict):
        return mapping

    payload = fa.get("result")
    if not isinstance(payload, dict):
        payload = fa
    analyses = payload.get("analyses")
    if not isinstance(analyses, list):
        return mapping

    for item in analyses:
        if not isinstance(item, dict):
            continue
        test_id = str(item.get("test_id") or "").strip()
        if test_id:
            mapping[test_id] = item
    return mapping


def _build_test_report_section(test_data: Optional[Dict[str, Any]]) -> str:
    if not isinstance(test_data, dict):
        return """
        <div class="card">
          <h2>测试报告</h2>
          <p class="muted">本次未提供测试结果数据。</p>
        </div>
        """

    stats = test_data.get("stats", {})
    total = int(stats.get("total", 0) or 0)
    passed = int(stats.get("passed", 0) or 0)
    failed = int(stats.get("failed", 0) or 0)
    errors = int(stats.get("errors", 0) or 0)
    skipped = int(stats.get("skipped", 0) or 0)
    pass_rate = (passed / total * 100.0) if total > 0 else 0.0

    failures = test_data.get("failures", [])
    analysis_map = _extract_failure_analysis_map(test_data)
    attached_analysis_file = None
    fa = test_data.get("failure_analysis")
    if isinstance(fa, dict):
        payload = fa.get("result")
        if isinstance(payload, dict):
            attached = payload.get("attached_report")
            if isinstance(attached, dict):
                attached_analysis_file = attached.get("analysis_file")

    failure_rows: List[str] = []
    if isinstance(failures, list):
        for item in failures[:30]:
            if not isinstance(item, dict):
                continue
            test_id = str(item.get("test_id") or item.get("name") or "-")
            message = _truncate(item.get("message") or "", 160)
            traceback_summary = _truncate(item.get("traceback") or "", 180)
            analysis = analysis_map.get(test_id, {})
            ai_summary = _truncate(
                analysis.get("root_cause")
                or "未提供结构化AI分析",
                160,
            )
            ai_locations = analysis.get("code_locations")
            if isinstance(ai_locations, list) and ai_locations:
                ai_loc_text = _escape(", ".join(str(x) for x in ai_locations[:3]))
            else:
                ai_loc_text = "-"

            failure_rows.append(
                f"""
                <tr>
                  <td class="mono">{_escape(test_id)}</td>
                  <td>{_escape(message)}</td>
                  <td>{_escape(traceback_summary)}</td>
                  <td>{_escape(ai_summary)}</td>
                  <td class="mono">{ai_loc_text}</td>
                </tr>
                """
            )

    if not failure_rows:
        failure_rows.append("<tr><td colspan='5' class='muted'>无失败用例。</td></tr>")

    analysis_link_html = ""
    if attached_analysis_file:
        analysis_link_html = (
            f"<p class='muted'>AI分析文件：<span class='mono'>{_escape(attached_analysis_file)}</span></p>"
        )

    return f"""
    <div class="card">
      <h2>测试报告</h2>
      <p>
        总用例：<b>{total}</b>，
        通过：<b class="ok">{passed}</b>，
        失败：<b class="bad">{failed}</b>，
        错误：<b class="bad">{errors}</b>，
        跳过：<b>{skipped}</b>，
        通过率：<b>{pass_rate:.2f}%</b>
      </p>
      {analysis_link_html}
      <table>
        <thead>
          <tr>
            <th>失败用例</th>
            <th>错误信息</th>
            <th>堆栈摘要</th>
            <th>AI分析摘要</th>
            <th>建议位置</th>
          </tr>
        </thead>
        <tbody>
          {"".join(failure_rows)}
        </tbody>
      </table>
    </div>
    """


def build_notification_body(
    change_monitor_data: Optional[Dict[str, Any]] = None,
    test_report_data: Optional[Dict[str, Any]] = None,
    title: str = "自动化测试与文档变更通知",
) -> str:
    """
    Build styled HTML email body.
    """
    generated_at = _escape(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"))
    return f"""
    <!doctype html>
    <html>
      <head>
        <meta charset="utf-8" />
        <style>
          body {{
            margin: 0;
            padding: 0;
            background: #f6f8fb;
            font-family: -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",Arial,sans-serif;
            color: #1f2d3d;
          }}
          .container {{
            max-width: 1120px;
            margin: 18px auto;
            padding: 0 14px;
          }}
          .header {{
            background: linear-gradient(120deg, #2f54eb, #1890ff);
            color: #fff;
            border-radius: 10px;
            padding: 18px 20px;
            box-shadow: 0 2px 8px rgba(24, 144, 255, .2);
          }}
          .card {{
            background: #fff;
            margin-top: 14px;
            border-radius: 10px;
            padding: 14px 16px;
            box-shadow: 0 1px 4px rgba(15, 35, 95, .08);
          }}
          h1 {{ margin: 0 0 6px 0; font-size: 22px; }}
          h2 {{ margin: 0 0 10px 0; font-size: 18px; }}
          p {{ margin: 6px 0 10px 0; }}
          .muted {{ color: #6b7a90; }}
          table {{
            width: 100%;
            border-collapse: collapse;
            margin-top: 8px;
            table-layout: fixed;
          }}
          th, td {{
            border: 1px solid #e6ecf2;
            padding: 8px;
            vertical-align: top;
            font-size: 13px;
            word-break: break-word;
          }}
          th {{
            background: #f2f6ff;
            text-align: left;
          }}
          .ok {{ color: #2f9e44; }}
          .bad {{ color: #d6336c; }}
          .mono {{ font-family: ui-monospace,Menlo,Consolas,monospace; }}
          ul.compact {{
            margin: 0;
            padding-left: 18px;
          }}
          .tag {{
            display: inline-block;
            border-radius: 999px;
            padding: 0 8px;
            margin-right: 6px;
            color: #fff;
            font-size: 12px;
            line-height: 20px;
            min-width: 46px;
            text-align: center;
          }}
          .tag.added {{ background: #2f9e44; }}
          .tag.modified {{ background: #f08c00; }}
          .tag.deleted {{ background: #d6336c; }}
          .tag.unknown {{ background: #6b7280; }}
        </style>
      </head>
      <body>
        <div class="container">
          <div class="header">
            <h1>{_escape(title)}</h1>
            <p>生成时间：{generated_at}</p>
          </div>
          {_build_change_monitor_section(change_monitor_data)}
          {_build_test_report_section(test_report_data)}
          <div class="card">
            <p class="muted">此邮件由自动化系统发送，请勿直接回复。</p>
          </div>
        </div>
      </body>
    </html>
    """


def _collect_default_attachments(payload: Dict[str, Any]) -> List[str]:
    attachments: List[str] = []

    for key in ("change_log_file", "run_log_file", "junit_xml", "log_file"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            attachments.append(value.strip())

    # Monitor payload with nested test report.
    test_report = payload.get("test_report")
    if isinstance(test_report, dict):
        for key in ("junit_xml", "log_file"):
            value = test_report.get(key)
            if isinstance(value, str) and value.strip():
                attachments.append(value.strip())
        fa = test_report.get("failure_analysis")
        if isinstance(fa, dict):
            result = fa.get("result")
            if isinstance(result, dict):
                attached = result.get("attached_report")
                if isinstance(attached, dict):
                    analysis_file = attached.get("analysis_file")
                    if isinstance(analysis_file, str) and analysis_file.strip():
                        attachments.append(analysis_file.strip())

    # Generic failure analysis.
    failure_analysis = payload.get("failure_analysis")
    if isinstance(failure_analysis, dict):
        attached = failure_analysis.get("attached_report")
        if isinstance(attached, dict):
            analysis_file = attached.get("analysis_file")
            if isinstance(analysis_file, str) and analysis_file.strip():
                attachments.append(analysis_file.strip())

    # Keep order and dedup.
    seen = set()
    deduped: List[str] = []
    for path in attachments:
        if path not in seen:
            seen.add(path)
            deduped.append(path)
    return deduped


def send_change_notification(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Send document-change notification email.
    Compatible with doc_change_monitor._trigger_email_notification(payload).
    """
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dictionary")

    totals = payload.get("totals", {}) if isinstance(payload.get("totals"), dict) else {}
    changed = int(totals.get("changed", 0) or 0)
    subject = f"[自动化通知] 华为云文档变更监控结果 - 变更 {changed} 项"

    body_html = build_notification_body(
        change_monitor_data=payload,
        test_report_data=payload.get("test_report") if isinstance(payload.get("test_report"), dict) else None,
        title="文档变更监控与测试同步通知",
    )
    attachments = _collect_default_attachments(payload)
    return send_email(subject=subject, body_html=body_html, attachments=attachments)


def send_notification(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Generic notification entry:
    - payload can contain change monitor data, test report data, or both.
    """
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dictionary")

    change_data = payload.get("change_monitor")
    if not isinstance(change_data, dict) and "products" in payload and "totals" in payload:
        change_data = payload

    test_data = payload.get("test_report")
    if not isinstance(test_data, dict) and "stats" in payload:
        test_data = payload

    totals = test_data.get("stats", {}) if isinstance(test_data, dict) else {}
    failed = int((totals.get("failed", 0) if isinstance(totals, dict) else 0) or 0)
    changed = 0
    if isinstance(change_data, dict):
        c_totals = change_data.get("totals")
        if isinstance(c_totals, dict):
            changed = int(c_totals.get("changed", 0) or 0)

    subject = payload.get("subject")
    if not isinstance(subject, str) or not subject.strip():
        subject = (
            f"[自动化通知] 文档变更 {changed} 项 | 测试失败 {failed} 项"
        )

    body_html = build_notification_body(
        change_monitor_data=change_data,
        test_report_data=test_data,
        title="自动化测试与文档变更汇总",
    )

    attachments = _collect_default_attachments(payload)
    extra_attachments = payload.get("attachments")
    if isinstance(extra_attachments, list):
        attachments.extend([str(item) for item in extra_attachments if str(item).strip()])
    # Dedup.
    seen = set()
    deduped: List[str] = []
    for path in attachments:
        if path not in seen:
            seen.add(path)
            deduped.append(path)

    return send_email(subject=subject, body_html=body_html, attachments=deduped)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    example_payload = {
        "totals": {"changed": 2, "added": 1, "modified": 1, "deleted": 0},
        "products": [
            {
                "product": "ecs",
                "changes": {
                    "total": 2,
                    "added": ["https://support.huaweicloud.com/ecs/new_api.html"],
                    "modified": ["https://support.huaweicloud.com/ecs/update_api.html"],
                    "deleted": [],
                    "details": [
                        {
                            "change_type": "added",
                            "url": "https://support.huaweicloud.com/ecs/new_api.html",
                            "reasons": ["new_page"],
                        },
                        {
                            "change_type": "modified",
                            "url": "https://support.huaweicloud.com/ecs/update_api.html",
                            "reasons": ["content_hash_changed"],
                        },
                    ],
                },
            }
        ],
        "test_report": {
            "stats": {"total": 12, "passed": 10, "failed": 2, "errors": 0, "skipped": 0},
            "failures": [
                {
                    "test_id": "tests.generated.test_ecs::test_create_server",
                    "message": "assert 400 == 200",
                    "traceback": "AssertionError: expected 200",
                }
            ],
            "failure_analysis": {
                "result": {
                    "analyses": [
                        {
                            "test_id": "tests.generated.test_ecs::test_create_server",
                            "root_cause": "文档示例已更新，断言未同步",
                            "code_locations": ["tests/generated/test_ecs.py:22"],
                        }
                    ]
                }
            },
        },
    }
    html_body = build_notification_body(
        change_monitor_data=example_payload,
        test_report_data=example_payload.get("test_report"),
    )
    print(json.dumps({"html_length": len(html_body)}, ensure_ascii=False))
