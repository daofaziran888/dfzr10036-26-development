"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import compaction_dispatch, resource

# 固定路径的合并派发路由必须先于 /api/{module} 动态资源路由注册。
ROUTERS = [compaction_dispatch, resource]
