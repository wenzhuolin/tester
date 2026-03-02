"""
Web dashboard for HuaweiDocTester automation platform.

Features:
- View document crawl status and recent changes
- Manually trigger fetch/generate/monitor/test tasks
- Visualize test report charts (pass rate/failure distribution)
- View AI failure analysis results
- Read and update config.yaml
- Tail task logs in near real-time
"""

from __future__ import annotations

import argparse
import json
import logging
import threading
import traceback
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from flask import Flask, jsonify, render_template_string, request

from huawei_doc_tester import HuaweiDocTester


LOGGER = logging.getLogger(__name__)

DEFAULT_CONFIG = "config.yaml"
WEB_TASK_LOG_DIR = Path("logs/web_tasks")

ACTION_MAP = {
    "fetch-and-generate": "fetch_and_generate",
    "monitor-and-update": "monitor_and_update",
    "run-full-test": "run_full_test",
}


HTML_TEMPLATE = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>HuaweiDocTester 控制台</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
  <style>
    body { margin: 0; font-family: -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,"PingFang SC","Microsoft YaHei",Arial,sans-serif; background: #f5f7fb; color: #1f2937; }
    .wrap { max-width: 1440px; margin: 0 auto; padding: 16px; }
    .header { background: linear-gradient(120deg, #2f54eb, #1677ff); color: #fff; border-radius: 12px; padding: 16px 20px; box-shadow: 0 4px 14px rgba(22,119,255,.28);}
    .header h1 { margin: 0; font-size: 24px; }
    .header p { margin: 8px 0 0 0; opacity: .95; }
    .grid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 12px; margin-top: 14px; }
    .card { background: #fff; border-radius: 12px; box-shadow: 0 1px 6px rgba(3, 27, 78, .08); padding: 14px; }
    .span-12 { grid-column: span 12; } .span-8 { grid-column: span 8; } .span-6 { grid-column: span 6; } .span-4 { grid-column: span 4; }
    .stat { display: flex; flex-direction: column; gap: 4px; }
    .stat .label { color: #6b7280; font-size: 12px; }
    .stat .value { font-size: 22px; font-weight: 700; }
    .actions button { margin-right: 8px; margin-bottom: 8px; background: #1677ff; border: 0; color: #fff; border-radius: 8px; padding: 8px 12px; cursor: pointer; }
    .actions button.secondary { background: #475569; }
    .actions button:disabled { opacity: .5; cursor: not-allowed; }
    .muted { color: #6b7280; font-size: 13px; }
    table { width: 100%; border-collapse: collapse; table-layout: fixed; }
    th, td { border: 1px solid #e5e7eb; padding: 8px; font-size: 13px; text-align: left; vertical-align: top; word-break: break-word; }
    th { background: #f8fafc; }
    .tag { display: inline-block; border-radius: 999px; padding: 2px 8px; font-size: 12px; color: #fff; }
    .queued { background: #f59e0b; } .running { background: #1677ff; } .success { background: #16a34a; } .failed { background: #dc2626; }
    pre { background: #0b1020; color: #e5e7eb; border-radius: 8px; padding: 12px; overflow: auto; min-height: 260px; max-height: 420px; font-size: 12px; line-height: 1.45; }
    textarea { width: 100%; min-height: 280px; font-family: ui-monospace,Menlo,Consolas,monospace; font-size: 12px; border: 1px solid #d1d5db; border-radius: 8px; padding: 8px; }
    .row { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
    .btn-row button { margin-left: 8px; }
    .danger { background: #dc2626 !important; }
    .ok { color: #16a34a; } .bad { color: #dc2626; }
    .mono { font-family: ui-monospace,Menlo,Consolas,monospace; }
    @media (max-width: 1100px) {
      .span-8,.span-6,.span-4 { grid-column: span 12; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="header">
      <h1>HuaweiDocTester 控制台</h1>
      <p>文档抓取 / 用例生成 / 变更监控 / 全量测试 / AI失败分析 一体化看板</p>
    </div>

    <div class="grid">
      <div class="card span-4">
        <div class="stat"><div class="label">产品数</div><div class="value" id="stat-products">-</div></div>
      </div>
      <div class="card span-4">
        <div class="stat"><div class="label">文档页面总数</div><div class="value" id="stat-pages">-</div></div>
      </div>
      <div class="card span-4">
        <div class="stat"><div class="label">最近变更总数</div><div class="value" id="stat-changes">-</div></div>
      </div>
    </div>

    <div class="grid">
      <div class="card span-12">
        <div class="row">
          <h3 style="margin:0;">手动任务触发</h3>
          <div class="actions btn-row">
            <button id="btn-fetch" onclick="triggerTask('fetch-and-generate')">抓取并全量生成</button>
            <button id="btn-monitor" onclick="triggerTask('monitor-and-update')">变更监控与更新</button>
            <button id="btn-test" onclick="triggerTask('run-full-test')">运行全量测试</button>
            <button class="secondary" onclick="refreshAll()">刷新</button>
          </div>
        </div>
        <p class="muted" id="cost-note"></p>
      </div>
    </div>

    <div class="grid">
      <div class="card span-8">
        <div class="row"><h3 style="margin:0;">任务列表</h3><span class="muted">点击“查看日志”可实时轮询</span></div>
        <table>
          <thead><tr><th>任务ID</th><th>动作</th><th>状态</th><th>开始时间</th><th>结束时间</th><th>操作</th></tr></thead>
          <tbody id="task-table"></tbody>
        </table>
      </div>
      <div class="card span-4">
        <div class="row"><h3 style="margin:0;">任务日志</h3><span class="muted mono" id="log-task-id">未选择任务</span></div>
        <pre id="task-log"></pre>
      </div>
    </div>

    <div class="grid">
      <div class="card span-6">
        <h3 style="margin-top:0;">测试通过率趋势</h3>
        <canvas id="passRateChart" height="180"></canvas>
      </div>
      <div class="card span-6">
        <h3 style="margin-top:0;">最近一次失败分布</h3>
        <canvas id="distChart" height="180"></canvas>
      </div>
    </div>

    <div class="grid">
      <div class="card span-6">
        <h3 style="margin-top:0;">文档抓取状态</h3>
        <table>
          <thead><tr><th>产品</th><th>页面数</th><th>最后更新</th><th>索引文件</th></tr></thead>
          <tbody id="docs-status-table"></tbody>
        </table>
      </div>
      <div class="card span-6">
        <h3 style="margin-top:0;">最近变更记录</h3>
        <table>
          <thead><tr><th>时间</th><th>新增</th><th>修改</th><th>删除</th><th>总变更</th></tr></thead>
          <tbody id="changes-table"></tbody>
        </table>
      </div>
    </div>

    <div class="grid">
      <div class="card span-12">
        <h3 style="margin-top:0;">失败用例 AI 分析结果</h3>
        <table>
          <thead><tr><th>测试用例</th><th>错误类型</th><th>根因摘要</th><th>建议位置</th></tr></thead>
          <tbody id="ai-analysis-table"></tbody>
        </table>
      </div>
    </div>

    <div class="grid">
      <div class="card span-12">
        <div class="row">
          <h3 style="margin:0;">系统配置 (config.yaml)</h3>
          <div class="btn-row">
            <button class="secondary" onclick="loadConfig()">重新加载</button>
            <button onclick="saveConfig()">保存配置</button>
          </div>
        </div>
        <textarea id="config-text"></textarea>
      </div>
    </div>
  </div>

  <script>
    let currentTaskId = null;
    let currentOffset = 0;
    let passRateChart = null;
    let distChart = null;
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));

    async function api(path, method='GET', body=null) {
      const options = { method, headers: {} };
      if (body !== null) {
        options.headers['Content-Type'] = 'application/json';
        options.body = JSON.stringify(body);
      }
      const res = await fetch(path, options);
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || data.message || `HTTP ${res.status}`);
      }
      return data;
    }

    function statusTag(status) {
      const s = (status || '').toLowerCase();
      if (s === 'success' || s === 'succeeded') return `<span class="tag success">${s}</span>`;
      if (s === 'running') return `<span class="tag running">${s}</span>`;
      if (s === 'queued') return `<span class="tag queued">${s}</span>`;
      return `<span class="tag failed">${s || 'unknown'}</span>`;
    }

    function updateStats(overview) {
      const docs = overview.docs_status || {};
      const changes = overview.recent_changes || [];
      let lastChanged = 0;
      if (changes.length > 0) lastChanged = Number((changes[0].totals || {}).changed || 0);
      document.getElementById('stat-products').textContent = docs.total_products ?? '-';
      document.getElementById('stat-pages').textContent = docs.total_pages ?? '-';
      document.getElementById('stat-changes').textContent = lastChanged;
      document.getElementById('cost-note').textContent = overview.cursor_note || '';
    }

    function renderTasks(tasks) {
      const tb = document.getElementById('task-table');
      tb.innerHTML = '';
      if (!tasks || tasks.length === 0) {
        tb.innerHTML = `<tr><td colspan="6" class="muted">暂无任务</td></tr>`;
        return;
      }
      for (const t of tasks) {
        const tr = document.createElement('tr');
        tr.innerHTML = `
          <td class="mono">${t.id}</td>
          <td>${t.action}</td>
          <td>${statusTag(t.status)}</td>
          <td>${t.started_at || '-'}</td>
          <td>${t.ended_at || '-'}</td>
          <td><button class="secondary" onclick="selectTask('${t.id}')">查看日志</button></td>
        `;
        tb.appendChild(tr);
      }
    }

    function renderDocsStatus(docs) {
      const tb = document.getElementById('docs-status-table');
      tb.innerHTML = '';
      const products = (docs && docs.products) || [];
      if (products.length === 0) {
        tb.innerHTML = `<tr><td colspan="4" class="muted">暂无抓取数据</td></tr>`;
        return;
      }
      for (const p of products) {
        const tr = document.createElement('tr');
        tr.innerHTML = `
          <td>${p.product}</td>
          <td>${p.pages}</td>
          <td>${p.updated_at || '-'}</td>
          <td class="mono">${p.index_file || '-'}</td>
        `;
        tb.appendChild(tr);
      }
    }

    function renderRecentChanges(changes) {
      const tb = document.getElementById('changes-table');
      tb.innerHTML = '';
      if (!changes || changes.length === 0) {
        tb.innerHTML = `<tr><td colspan="5" class="muted">暂无变更记录</td></tr>`;
        return;
      }
      for (const item of changes) {
        const totals = item.totals || {};
        const tr = document.createElement('tr');
        tr.innerHTML = `
          <td>${item.finished_at || item.started_at || '-'}</td>
          <td>${totals.added || 0}</td>
          <td>${totals.modified || 0}</td>
          <td>${totals.deleted || 0}</td>
          <td>${totals.changed || 0}</td>
        `;
        tb.appendChild(tr);
      }
    }

    function renderAIAnalysis(ai) {
      const tb = document.getElementById('ai-analysis-table');
      tb.innerHTML = '';
      const entries = (ai && ai.analyses) || [];
      if (entries.length === 0) {
        tb.innerHTML = `<tr><td colspan="4" class="muted">暂无AI失败分析结果</td></tr>`;
        return;
      }
      for (const a of entries) {
        const codeLoc = (a.code_locations || []).slice(0, 3).join(', ');
        const tr = document.createElement('tr');
        tr.innerHTML = `
          <td class="mono">${a.test_id || '-'}</td>
          <td>${a.error_type || '-'}</td>
          <td>${a.root_cause || '-'}</td>
          <td class="mono">${codeLoc || '-'}</td>
        `;
        tb.appendChild(tr);
      }
    }

    function renderCharts(report) {
      const history = (report && report.history) || [];
      const latest = (report && report.latest) || {};
      const labels = history.map(x => x.timestamp || '-');
      const passRates = history.map(x => Number(x.pass_rate || 0));
      const dist = latest.distribution || {
        passed: 0, failed: 0, errors: 0, skipped: 0
      };

      const passCtx = document.getElementById('passRateChart').getContext('2d');
      if (passRateChart) passRateChart.destroy();
      passRateChart = new Chart(passCtx, {
        type: 'line',
        data: {
          labels,
          datasets: [{
            label: '通过率(%)',
            data: passRates,
            borderColor: '#1677ff',
            backgroundColor: 'rgba(22,119,255,.15)',
            fill: true,
            tension: .25,
          }]
        },
        options: {
          responsive: true,
          scales: { y: { min: 0, max: 100 } }
        }
      });

      const distCtx = document.getElementById('distChart').getContext('2d');
      if (distChart) distChart.destroy();
      distChart = new Chart(distCtx, {
        type: 'doughnut',
        data: {
          labels: ['通过', '失败', '错误', '跳过'],
          datasets: [{
            data: [dist.passed || 0, dist.failed || 0, dist.errors || 0, dist.skipped || 0],
            backgroundColor: ['#16a34a', '#dc2626', '#f59e0b', '#64748b']
          }]
        },
      });
    }

    async function loadOverview() {
      const data = await api('/api/overview');
      updateStats(data);
      renderDocsStatus(data.docs_status || {});
      renderRecentChanges(data.recent_changes || []);
      renderAIAnalysis(data.failure_analysis || {});
      renderCharts(data.test_reports || {});
    }

    async function loadTasks() {
      const data = await api('/api/tasks');
      renderTasks(data.tasks || []);
    }

    async function triggerTask(action) {
      try {
        const data = await api('/api/tasks', 'POST', { action });
        alert(`任务已创建: ${data.task.id}`);
        await refreshAll();
      } catch (e) {
        alert(`触发失败: ${e.message}`);
      }
    }

    function selectTask(taskId) {
      currentTaskId = taskId;
      currentOffset = 0;
      document.getElementById('log-task-id').textContent = taskId;
      document.getElementById('task-log').textContent = '';
      fetchTaskLogs();
    }

    async function fetchTaskLogs() {
      if (!currentTaskId) return;
      try {
        const data = await api(`/api/tasks/${currentTaskId}/logs?offset=${currentOffset}`);
        const box = document.getElementById('task-log');
        if (data.chunk) {
          box.textContent += data.chunk;
          box.scrollTop = box.scrollHeight;
        }
        currentOffset = data.next_offset || currentOffset;
      } catch (e) {
        // ignore polling errors
      }
    }

    async function loadConfig() {
      try {
        const data = await api('/api/config');
        document.getElementById('config-text').value = data.yaml || '';
      } catch (e) {
        alert(`加载配置失败: ${e.message}`);
      }
    }

    async function saveConfig() {
      const text = document.getElementById('config-text').value;
      try {
        const data = await api('/api/config', 'POST', { yaml: text });
        alert(`配置已保存: ${data.config_path}`);
      } catch (e) {
        alert(`保存失败: ${e.message}`);
      }
    }

    async function refreshAll() {
      await Promise.all([loadOverview(), loadTasks()]);
    }

    async function loopPoll() {
      while (true) {
        await sleep(3000);
        await Promise.all([loadOverview().catch(()=>{}), loadTasks().catch(()=>{}), fetchTaskLogs().catch(()=>{})]);
      }
    }

    (async function init() {
      await refreshAll();
      await loadConfig();
      loopPoll();
    })();
  </script>
</body>
</html>
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jsonable(data: Any) -> Any:
    try:
        json.dumps(data, ensure_ascii=False)
        return data
    except TypeError:
        return json.loads(json.dumps(data, ensure_ascii=False, default=str))


def _parse_junit_stats(junit_xml_path: Path) -> Dict[str, int]:
    stats = {"total": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    if not junit_xml_path.exists():
        return stats
    try:
        root = ET.fromstring(junit_xml_path.read_text(encoding="utf-8"))
    except Exception:
        return stats

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


def _read_jsonl_tail(path: Path, limit: int = 20) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    out: List[Dict[str, Any]] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                out.append(parsed)
        except json.JSONDecodeError:
            continue
    out.reverse()
    return out


class WebTaskManager:
    """Background task manager for web-triggered jobs."""

    def __init__(self, config_path: Path, log_dir: Path = WEB_TASK_LOG_DIR) -> None:
        self.config_path = config_path
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._tasks: Dict[str, Dict[str, Any]] = {}

    def _task_log_path(self, task_id: str) -> Path:
        return self.log_dir / f"{task_id}.log"

    def _append_log(self, task_id: str, message: str) -> None:
        line = f"[{_now_iso()}] {message.rstrip()}\n"
        path = self._task_log_path(task_id)
        with path.open("a", encoding="utf-8") as f:
            f.write(line)

    def _run_action(self, action: str) -> Dict[str, Any]:
        tester = HuaweiDocTester(config_path=str(self.config_path))
        method_name = ACTION_MAP[action]
        method = getattr(tester, method_name)
        return method()

    def create_task(self, action: str) -> Dict[str, Any]:
        if action not in ACTION_MAP:
            raise ValueError(f"Unsupported action: {action}")

        task_id = f"task_{uuid.uuid4().hex[:12]}"
        task_info = {
            "id": task_id,
            "action": action,
            "status": "queued",
            "started_at": None,
            "ended_at": None,
            "error": None,
            "result": None,
            "log_file": str(self._task_log_path(task_id).resolve()),
        }
        with self._lock:
            self._tasks[task_id] = task_info

        thread = threading.Thread(
            target=self._execute_task,
            args=(task_id, action),
            daemon=True,
        )
        thread.start()
        return dict(task_info)

    def _execute_task(self, task_id: str, action: str) -> None:
        with self._lock:
            self._tasks[task_id]["status"] = "running"
            self._tasks[task_id]["started_at"] = _now_iso()

        self._append_log(task_id, f"Task started. action={action}")
        self._append_log(task_id, f"Using config: {self.config_path}")
        self._append_log(task_id, "Note: Cursor Background Agent API requires Pro+ plan and consumes quota.")

        try:
            result = self._run_action(action)
            self._append_log(task_id, f"Task completed successfully.")
            self._append_log(task_id, "Result summary:")
            self._append_log(task_id, json.dumps(_jsonable(result), ensure_ascii=False, indent=2))
            with self._lock:
                self._tasks[task_id]["status"] = "success"
                self._tasks[task_id]["ended_at"] = _now_iso()
                self._tasks[task_id]["result"] = _jsonable(result)
        except Exception as exc:
            self._append_log(task_id, f"Task failed: {exc}")
            self._append_log(task_id, traceback.format_exc())
            with self._lock:
                self._tasks[task_id]["status"] = "failed"
                self._tasks[task_id]["ended_at"] = _now_iso()
                self._tasks[task_id]["error"] = str(exc)

    def list_tasks(self) -> List[Dict[str, Any]]:
        with self._lock:
            tasks = [dict(item) for item in self._tasks.values()]
        tasks.sort(key=lambda x: x.get("started_at") or "", reverse=True)
        return tasks

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            task = self._tasks.get(task_id)
            return dict(task) if task else None

    def read_task_log(self, task_id: str, offset: int = 0, limit: int = 65536) -> Dict[str, Any]:
        path = self._task_log_path(task_id)
        if not path.exists():
            return {"chunk": "", "next_offset": offset, "exists": False}

        with path.open("rb") as f:
            f.seek(max(0, offset))
            data = f.read(max(1, limit))
            next_offset = f.tell()
        return {
            "chunk": data.decode("utf-8", errors="replace"),
            "next_offset": next_offset,
            "exists": True,
        }


class DashboardService:
    """Service helpers for API endpoints."""

    def __init__(self, config_path: Path, task_manager: WebTaskManager) -> None:
        self.config_path = config_path
        self.task_manager = task_manager

    def _load_config(self) -> Dict[str, Any]:
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}

    def _docs_status(self, docs_root: Path) -> Dict[str, Any]:
        products: List[Dict[str, Any]] = []
        total_pages = 0
        latest_updated = ""

        if docs_root.exists():
            for index_path in sorted(docs_root.glob("*/index.json")):
                product = index_path.parent.name
                try:
                    data = json.loads(index_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    data = {}
                pages = data.get("pages") if isinstance(data, dict) else {}
                page_count = len(pages) if isinstance(pages, dict) else 0
                total_pages += page_count
                updated_at = data.get("updated_at") if isinstance(data, dict) else None
                updated_at = str(updated_at) if updated_at else ""
                if updated_at and updated_at > latest_updated:
                    latest_updated = updated_at
                products.append(
                    {
                        "product": data.get("product") or product,
                        "pages": page_count,
                        "updated_at": updated_at,
                        "index_file": str(index_path.resolve()),
                    }
                )

        return {
            "products": products,
            "total_products": len(products),
            "total_pages": total_pages,
            "last_updated": latest_updated or None,
        }

    def _recent_changes(self, change_log_file: Path) -> List[Dict[str, Any]]:
        return _read_jsonl_tail(change_log_file, limit=20)

    def _latest_failure_analysis(self, report_dir: Path) -> Dict[str, Any]:
        analyses = sorted(
            report_dir.glob("failure_analysis_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not analyses:
            return {"analyses": [], "summary": None, "source_file": None}
        latest = analyses[0]
        try:
            data = json.loads(latest.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"analyses": [], "summary": None, "source_file": str(latest)}
        if not isinstance(data, dict):
            return {"analyses": [], "summary": None, "source_file": str(latest)}
        return {
            "analyses": data.get("analyses", []) if isinstance(data.get("analyses"), list) else [],
            "summary": data.get("summary"),
            "source_file": str(latest.resolve()),
            "timestamp": datetime.fromtimestamp(latest.stat().st_mtime, timezone.utc).isoformat(),
        }

    def _test_reports(self, report_dir: Path) -> Dict[str, Any]:
        xml_files = sorted(
            report_dir.glob("junit_*.xml"),
            key=lambda p: p.stat().st_mtime,
        )
        history: List[Dict[str, Any]] = []
        for xml in xml_files[-20:]:
            stats = _parse_junit_stats(xml)
            total = stats["total"]
            pass_rate = (stats["passed"] / total * 100.0) if total > 0 else 0.0
            history.append(
                {
                    "file": str(xml.resolve()),
                    "timestamp": datetime.fromtimestamp(xml.stat().st_mtime, timezone.utc).isoformat(),
                    "stats": stats,
                    "pass_rate": round(pass_rate, 2),
                }
            )

        latest = history[-1] if history else None
        distribution = {
            "passed": 0,
            "failed": 0,
            "errors": 0,
            "skipped": 0,
        }
        if latest:
            distribution = {
                "passed": latest["stats"]["passed"],
                "failed": latest["stats"]["failed"],
                "errors": latest["stats"]["errors"],
                "skipped": latest["stats"]["skipped"],
            }

        return {
            "latest": {
                "file": latest["file"] if latest else None,
                "timestamp": latest["timestamp"] if latest else None,
                "stats": latest["stats"] if latest else distribution,
                "pass_rate": latest["pass_rate"] if latest else 0.0,
                "distribution": distribution,
            },
            "history": history,
        }

    def overview(self) -> Dict[str, Any]:
        tester = HuaweiDocTester(config_path=str(self.config_path))
        paths = tester._paths()  # pylint: disable=protected-access
        docs_status = self._docs_status(paths["docs_output_root"])
        recent_changes = self._recent_changes(paths["change_log_file"])
        test_reports = self._test_reports(paths["report_dir"])
        failure_analysis = self._latest_failure_analysis(paths["report_dir"])
        cron = tester.cron_examples()
        return {
            "timestamp": _now_iso(),
            "docs_status": docs_status,
            "recent_changes": recent_changes,
            "test_reports": test_reports,
            "failure_analysis": failure_analysis,
            "cron_examples": cron,
            "cursor_note": cron.get("note"),
        }

    def get_config_yaml(self) -> str:
        return self.config_path.read_text(encoding="utf-8")

    def update_config_yaml(self, yaml_text: str) -> Dict[str, Any]:
        parsed = yaml.safe_load(yaml_text)
        if not isinstance(parsed, dict):
            raise ValueError("配置根节点必须是对象(map)")
        if not isinstance(parsed.get("products"), list) or not parsed.get("products"):
            raise ValueError("配置必须包含非空 products 列表")

        backup_path = self.config_path.with_suffix(
            self.config_path.suffix + f".bak.{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        )
        backup_path.write_text(self.config_path.read_text(encoding="utf-8"), encoding="utf-8")
        self.config_path.write_text(yaml_text, encoding="utf-8")

        # Validate after write by constructing tester.
        _ = HuaweiDocTester(config_path=str(self.config_path))
        return {
            "config_path": str(self.config_path.resolve()),
            "backup_path": str(backup_path.resolve()),
        }


def create_app(config_path: str = DEFAULT_CONFIG) -> Flask:
    config_file = Path(config_path).resolve()
    if not config_file.exists():
        raise FileNotFoundError(f"Config file not found: {config_file}")

    task_manager = WebTaskManager(config_path=config_file)
    service = DashboardService(config_path=config_file, task_manager=task_manager)
    app = Flask(__name__)

    @app.route("/")
    def index() -> str:
        return render_template_string(HTML_TEMPLATE)

    @app.route("/api/overview")
    def api_overview() -> Any:
        return jsonify(service.overview())

    @app.route("/api/tasks", methods=["GET", "POST"])
    def api_tasks() -> Any:
        if request.method == "GET":
            return jsonify({"tasks": task_manager.list_tasks()})

        body = request.get_json(silent=True) or {}
        action = str(body.get("action") or "").strip()
        if action not in ACTION_MAP:
            return jsonify({"error": f"Invalid action: {action}"}), 400
        task = task_manager.create_task(action)
        return jsonify({"task": task}), 202

    @app.route("/api/tasks/<task_id>", methods=["GET"])
    def api_task_detail(task_id: str) -> Any:
        task = task_manager.get_task(task_id)
        if not task:
            return jsonify({"error": "Task not found"}), 404
        return jsonify(task)

    @app.route("/api/tasks/<task_id>/logs", methods=["GET"])
    def api_task_logs(task_id: str) -> Any:
        try:
            offset = int(request.args.get("offset", "0"))
        except ValueError:
            offset = 0
        data = task_manager.read_task_log(task_id=task_id, offset=max(0, offset))
        return jsonify(data)

    @app.route("/api/config", methods=["GET", "POST"])
    def api_config() -> Any:
        if request.method == "GET":
            yaml_text = service.get_config_yaml()
            return jsonify({"config_path": str(config_file), "yaml": yaml_text})

        body = request.get_json(silent=True) or {}
        yaml_text = body.get("yaml")
        if not isinstance(yaml_text, str) or not yaml_text.strip():
            return jsonify({"error": "yaml field is required"}), 400

        try:
            result = service.update_config_yaml(yaml_text)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(result)

    @app.route("/api/cron", methods=["GET"])
    def api_cron() -> Any:
        tester = HuaweiDocTester(config_path=str(config_file))
        return jsonify(tester.cron_examples())

    return app


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HuaweiDocTester Web Dashboard")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="Path to config.yaml")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument("--port", type=int, default=8080, help="Bind port")
    parser.add_argument("--debug", action="store_true", help="Enable Flask debug mode")
    parser.add_argument(
        "--dump-overview",
        action="store_true",
        help="Print overview JSON and exit (for quick validation)",
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
    app = create_app(config_path=args.config)

    if args.dump_overview:
        with app.test_client() as client:
            resp = client.get("/api/overview")
            print(json.dumps(resp.get_json(), ensure_ascii=False, indent=2))
        return

    LOGGER.info("Starting web dashboard on %s:%s", args.host, args.port)
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
