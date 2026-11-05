# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 上海商江网络科技有限公司版权所有
# 软工云（swtool.com）开发团队；uqtool@163.com
"""Hot plugin adapter; keep host URLs as compatibility aliases and reuse existing data."""
from fastapi import APIRouter
from server import app as host

router = APIRouter()
# The same handlers, authentication and permission checks serve the plugin namespace.
# No app mutation or restart occurs when installing or activating this adapter.
router.routes.extend(route for route in host.app.routes
                     if getattr(route, 'path', '').startswith(('/api/tickets', '/issues', '/api/requirements/')))
