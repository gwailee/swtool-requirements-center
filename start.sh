#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 上海商江网络科技有限公司版权所有
# 软工云（swtool.com）开发团队；uqtool@163.com
set -euo pipefail
cd "$(dirname "$0")"
export SWTOOL_REQUIREMENTS_STANDALONE=1
export SWTOOL_DATA_DIR="${SWTOOL_DATA_DIR:-$PWD/data}"
if [[ -f service.env ]]; then
  set -a
  source service.env
  set +a
fi
: "${SWTOOL_DATABASE_URL:?请先复制 service.env.example 为 service.env，配置 PostgreSQL 连接}"
[[ -x .venv/bin/python ]] || "${PYTHON:-python3}" -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
exec .venv/bin/python -m uvicorn standalone:app --host "${SWTOOL_HOST:-127.0.0.1}" --port "${SWTOOL_PORT:-8080}"
