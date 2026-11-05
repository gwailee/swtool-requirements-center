# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 上海商江网络科技有限公司版权所有
# 软工云（swtool.com）开发团队；uqtool@163.com
from fastapi import APIRouter, Request

router = APIRouter()

@router.get('/hello')
def hello(request: Request):
    return {'message': '插件已激活', 'user': request.state.user['username']}
