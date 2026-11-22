"""页面快照领域服务：页面DOM快照的采集与存储，消费login_mgr凭证和canvas_config配置。

快照数据集交付相关职责：
- capture_snapshot 同时保存完整正文（raw_dom）与预览（dom_preview），正文与内存条目分离存档；
- 启动回收时若该快照已随交付文件发布，只回收本地存档条目标记，交付文件内正文保持可读，
  保证「已完成文件不会因后续清理变成半包」；
- replay_from_dataset 把同一快照数据集交给页面快照读取，验正文并重建可回放条目，
  重复读取不把旧档算作本次新采集（幂等由 SnapshotDeliveryService 台账保证）。
"""
from __future__ import annotations

import hashlib
import os
from typing import Any

from app.store import store
from app.services.base import BaseModuleService, register_hook
from app.services.snapshot_delivery import (
    snapshot_delivery_service,
    _snapshot_body_key,
    DELIVERY_DIR,
)
from app import persistence as persist


def _is_delivered(snapshot_code: str) -> bool:
    """该快照是否已随某个已发布交付文件发出（回收冲突保护依据）。"""
    for record in persist.read_jsonl(DELIVERY_DIR, "index.jsonl"):
        dataset = snapshot_delivery_service.load_dataset_file(record.get("dataset_id", ""))
        if not dataset:
            continue
        if any(e.get("snapshot_code") == snapshot_code for e in dataset.get("entries", [])):
            return True
    return False


def _protect_delivered_on_reclaim(entry: dict[str, Any]) -> None:
    """回收与导出冲突时：已交付快照的正文文件保留，只标记本地条目已回收。"""
    code = str(entry.get("快照编号", ""))
    body_path = persist.data_path("snapshots", _snapshot_body_key(code))
    body_exists = os.path.exists(body_path)
    if _is_delivered(code) and body_exists:
        # 交付文件自带副本，本地正文也保留以便核对；记录冲突台账
        entry["reclaim_conflict"] = True
        entry["reclaim_note"] = "该快照已随交付文件发布，回收仅变更存档状态，交付正文保持可读"
        persist.append_jsonl(DELIVERY_DIR, "reclaim_conflicts.jsonl", {
            "snapshot_code": code,
            "action": "启动回收",
            "delivered": True,
            "body_preserved": True,
            "timestamp": persist.now_iso(),
        })
    else:
        entry["reclaim_conflict"] = False
        if body_exists:
            # 未交付的本地正文随回收清理
            os.remove(body_path)
        persist.append_jsonl(DELIVERY_DIR, "reclaim_conflicts.jsonl", {
            "snapshot_code": code,
            "action": "启动回收",
            "delivered": False,
            "body_preserved": False,
            "timestamp": persist.now_iso(),
        })


register_hook("page_snapshot", "启动回收", _protect_delivered_on_reclaim)


class PageSnapshotService(BaseModuleService):
    MODULE = "page_snapshot"
    ENTITY = "页面快照"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["快照编号", "采集来源", "页面版本", "DOM摘要", "快照时间", "快照类型", "数据量", "回收时间"]
    REQUIRED_FIELDS = ["快照编号", "采集来源", "页面版本"]
    STATUS_ORDER = ["草稿", "已存档", "已清洗", "已回收"]
    ACTION_RULES = {"标记存档": "已存档", "确认清洗": "已清洗", "启动回收": "已回收"}
    NEGATIVE_ACTIONS = []

    def capture_snapshot(self, page_url: str, dom_hash: str, raw_dom: str) -> dict[str, Any]:
        """采集页面快照：完整正文与预览分别存档到snapshots/目录。"""
        snapshot = {
            "page_url": page_url,
            "dom_hash": dom_hash,
            "raw_dom": raw_dom,
            "raw_dom_length": len(raw_dom),
            "timestamp": persist.now_iso(),
            "dom_preview": raw_dom[:500],
        }
        persist.write_json("snapshots", f"page_{dom_hash}.json", snapshot)
        return snapshot

    def attach_body(self, entry_id: int, raw_dom: str, page_url: str = "") -> tuple[dict[str, Any] | None, str]:
        """为已登记的页面快照附上采集到的完整正文（与内存条目分离存档）。

        未附正文（或正文已被回收）的条目在交付时即为「只有预览的旧档」。
        """
        row = self.get_entry(entry_id)
        if row is None:
            return None, f"页面快照 {entry_id} 不存在"
        if not str(raw_dom or "").strip():
            return None, "完整正文不能为空，空正文只能作为预览旧档处理"
        code = str(row.get("快照编号", ""))
        dom_hash = hashlib.md5(raw_dom.encode("utf-8")).hexdigest()[:16]
        doc = {
            "page_url": page_url or str(row.get("采集来源", "")),
            "dom_hash": dom_hash,
            "raw_dom": raw_dom,
            "raw_dom_length": len(raw_dom),
            "timestamp": persist.now_iso(),
            "dom_preview": raw_dom[:500],
        }
        persist.write_json("snapshots", _snapshot_body_key(code), doc)
        row["数据量"] = str(len(raw_dom))
        row["updated_at"] = persist.now_iso()
        return {"entry_id": entry_id, "snapshot_code": code, "body_length": len(raw_dom)}, ""

    def body_status(self, entry_id: int) -> dict[str, Any]:
        """说明一条快照当前实际可获取的正文、摘要和理由（供交付/核对）。"""
        row = self.get_entry(entry_id)
        if row is None:
            return {"exists": False}
        code = str(row.get("快照编号", ""))
        body_doc = persist.read_json("snapshots", _snapshot_body_key(code))
        has_body = bool(body_doc and body_doc.get("raw_dom"))
        return {
            "exists": True,
            "snapshot_code": code,
            "source": row.get("采集来源", ""),
            "summary": row.get("DOM摘要", ""),
            "status": row.get("status", ""),
            "has_full_body": has_body,
            "body_length": int(body_doc.get("raw_dom_length", 0)) if has_body else 0,
            "delivered": _is_delivered(code),
            "reason": (
                "完整正文可获取" if has_body
                else "仅预览旧档：完整正文未保留或已回收，需按原运行及来源补采"
            ),
        }

    def replay_from_dataset(self, dataset_id: str, entry_key: str) -> dict[str, Any] | None:
        """页面快照读取同一份快照数据集：验正文并重建可回放条目视图。

        重复读取同一数据集由交付服务导入台账去重，不会把旧档算作本次新采集。
        """
        return snapshot_delivery_service.replay_entry(dataset_id, entry_key)

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        snap_files = persist.list_files("snapshots", ".json")
        base["snapshot_files"] = len([f for f in snap_files if f.startswith("page_")])
        # 关联的登录凭证
        credentials = store.rows("login_mgr")
        snaps = store.rows(self.MODULE)
        sites = {r.get("采集来源") for r in snaps}
        base["sites_with_credentials"] = sum(1 for c in credentials if c.get("目标站点") in sites)
        # 交付与回收冲突保护统计
        conflicts = persist.read_jsonl(DELIVERY_DIR, "reclaim_conflicts.jsonl")
        base["reclaim_conflicts"] = len([c for c in conflicts if c.get("body_preserved")])
        base["delivered_snapshots"] = sum(
            1 for r in snaps if _is_delivered(str(r.get("快照编号", "")))
        )
        return base
