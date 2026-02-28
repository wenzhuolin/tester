#!/usr/bin/env bash
set -euo pipefail

# Generate or install cron jobs for HuaweiDocTester.
#
# Usage:
#   ./scripts/schedule_cron.sh
#   ./scripts/schedule_cron.sh --install
#   ./scripts/schedule_cron.sh --config /workspace/config.yaml --python python3

WORKSPACE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_PATH="${WORKSPACE_DIR}/config.yaml"
PYTHON_BIN="python3"
INSTALL="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      CONFIG_PATH="$2"
      shift 2
      ;;
    --python)
      PYTHON_BIN="$2"
      shift 2
      ;;
    --install)
      INSTALL="true"
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if [[ ! -f "${CONFIG_PATH}" ]]; then
  echo "Config file not found: ${CONFIG_PATH}" >&2
  exit 1
fi

mapfile -t CRON_LINES < <("${PYTHON_BIN}" - <<PY
import pathlib
import yaml

workspace = pathlib.Path("${WORKSPACE_DIR}").resolve()
config_path = pathlib.Path("${CONFIG_PATH}").resolve()
config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
schedule = config.get("schedule", {}) if isinstance(config, dict) else {}

monitor_cron = str(schedule.get("monitor_cron", "0 2 * * *")).strip()
full_test_cron = str(schedule.get("full_test_cron", "0 3 * * 1")).strip()

base = f"cd {workspace} && ${PYTHON_BIN} huawei_doc_tester.py --config {config_path}"
monitor = f"{monitor_cron} {base} --action monitor-and-update >> {workspace}/logs/monitor_cron.log 2>&1"
full_test = f"{full_test_cron} {base} --action run-full-test >> {workspace}/logs/full_test_cron.log 2>&1"

print(monitor)
print(full_test)
PY
)

echo "===== Cron jobs ====="
for line in "${CRON_LINES[@]}"; do
  echo "${line}"
done
echo
echo "注意：Cursor Background Agent API 仅支持 Pro 及以上计划，调用会产生配额与计费。"

if [[ "${INSTALL}" == "true" ]]; then
  mkdir -p "${WORKSPACE_DIR}/logs"

  EXISTING="$(crontab -l 2>/dev/null || true)"
  FILTERED="$(printf "%s\n" "${EXISTING}" | sed '/huawei_doc_tester.py --config/d')"

  {
    printf "%s\n" "${FILTERED}"
    for line in "${CRON_LINES[@]}"; do
      printf "%s\n" "${line}"
    done
  } | crontab -

  echo "Cron jobs installed successfully."
else
  echo "Use --install to write them into crontab."
fi
