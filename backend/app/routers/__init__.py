"""路由汇总：压缩盘点专用接口先注册，随后是通用资源路由。"""
from __future__ import annotations

from app.routers import compaction_ext, resource

ROUTERS = [compaction_ext, resource]
