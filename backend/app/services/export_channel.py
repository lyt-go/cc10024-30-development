"""导出通道领域服务：数据导出、格式转换、分片与归档，消费去重结果和内容缓存。"""
from __future__ import annotations

import json
from typing import Any

from app.store import store
from app.services.base import BaseModuleService
from app.services.snapshot_delivery import snapshot_delivery_service
from app import persistence as persist


class ExportChannelService(BaseModuleService):
    MODULE = "export_channel"
    ENTITY = "导出配置"
    OPERATION_TERM = "导出"
    LIST_FIELDS = ["通道编号", "输出格式", "目标路径", "压缩方式", "分片策略", "保留策略", "加密标识", "通道版本"]
    REQUIRED_FIELDS = ["通道编号", "输出格式", "目标路径"]
    STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "启用通道": "已启用", "停用通道": "已停用"}
    NEGATIVE_ACTIONS = ["停用通道"]

    def export_data(self, channel_id: int, data: list[dict[str, Any]]) -> str:
        """通过指定通道导出数据到exports/目录。"""
        channel = store.find(self.MODULE, channel_id)
        if not channel or channel.get("status") != "已启用":
            raise ValueError(f"导出通道{channel_id}未启用或不存在")
        fmt = channel.get("输出格式", "json")
        filename = f"export_{channel_id}_{persist.now_iso().replace(':', '-')}.{fmt}"
        path = persist.data_path("exports", filename)
        if fmt == "json":
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        elif fmt == "csv":
            import csv
            with open(path, "w", newline="", encoding="utf-8") as f:
                if data:
                    writer = csv.DictWriter(f, fieldnames=list(data[0].keys()))
                    writer.writeheader()
                    writer.writerows(data)
        else:
            raise ValueError(f"不支持的输出格式: {fmt}")
        return path

    def deliver_snapshot_dataset(
        self,
        *,
        channel_id: int,
        run_id: int,
        snapshot_ids: list[int] | None = None,
        remark: str = "",
    ) -> tuple[dict[str, Any] | None, str]:
        """导出通道写快照数据集：按采集运行发布一份可独立回放的交付文件。

        通道必须已启用。交付文件自包含，发布后即使本地快照被回收，文件仍完整可读。
        """
        channel = store.find(self.MODULE, channel_id)
        if not channel or channel.get("status") != "已启用":
            return None, f"导出通道{channel_id}未启用或不存在，无法交付快照数据集"
        return snapshot_delivery_service.publish_for_run(
            run_id=run_id,
            snapshot_ids=snapshot_ids,
            channel_id=channel_id,
            remark=remark,
        )

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        channels = store.rows(self.MODULE)
        base["active_channels"] = sum(1 for c in channels if c.get("status") == "已启用")
        # 导出文件列表
        export_files = persist.list_files("exports")
        base["export_files"] = len(export_files)
        # 内容缓存关联
        caches = store.rows("content_cache")
        base["linked_caches"] = len([c for c in caches if c.get("status") == "已生效"])
        return base