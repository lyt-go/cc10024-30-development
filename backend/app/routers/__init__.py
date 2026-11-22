"""业务路由汇总：固定路径的交付路由先注册，动态模块资源路由后注册。"""
from __future__ import annotations

from app.routers import resource, snapshot_delivery

# snapshot_delivery 的 /api/snapshot-delivery/* 为固定路径，必须先于
# resource 的 /api/{module} 动态路径注册，避免被抢占匹配。
ROUTERS = [snapshot_delivery, resource]
