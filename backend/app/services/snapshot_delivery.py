"""快照数据集交付领域服务：按采集运行把页面快照打成可独立回放的数据集。

设计约束（对应业务诉求）：
- 一次交付是一份自包含快照数据集（JSON），接收方无需访问存档方即可导入回放；
- 每个条目说清实际可获取的正文（body）、摘要（summary）、来源（source）和理由（reason）；
- 只有预览的旧档（body 缺失）标记为 preview_only，不能计入完整回放，进入待补清单，
  待补项可按「原运行 + 采集来源」定位补采；
- 已交付文件的清单与实有正文始终一致（manifest 与 entries 双向核对，校验和随包发布）；
- 导出与回收发生冲突时仍可读：交付瞬间把完整正文复制进交付文件（原子落盘），
  之后存档方再回收只影响本地存档，交付文件不会变成半包；
- 同一数据集重复导入幂等：以 dataset_id + 快照编号去重，回放不把旧档算作本次新采集。

落盘布局：
data/deliveries/<dataset_id>.json    不可变交付文件（原子写入）
data/deliveries/index.jsonl          交付登记索引（每次发布追加一条）
data/deliveries/imports.jsonl        接收方导入台账（幂等判重）
data/snapshots/page_<key>.json       页面快照正文存档（与内存条目分离）
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from typing import Any

from app.store import store
from app import persistence as persist

DATASET_KIND = "snapshot_dataset"
DATASET_VERSION = "1.0"
DELIVERY_DIR = "deliveries"
INDEX_FILE = "index.jsonl"
IMPORT_LEDGER = "imports.jsonl"

# 条目可回放性分级
FULL = "full"            # 完整快照：正文随包，可独立回放
PREVIEW_ONLY = "preview_only"  # 只有预览的旧档：无完整正文，不可完整回放，待补


def _checksum(payload: str) -> str:
    """交付文件内容指纹，随 manifest 发布，接收方可核对文件未被改写。"""
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _content_checksum(dataset: dict[str, Any]) -> str:
    """数据集内容指纹：计算时剔除 manifest.body_checksum 自身，保证可复算。"""
    stripped = json.loads(json.dumps(dataset, ensure_ascii=False, default=str))
    stripped.get("manifest", {}).pop("body_checksum", None)
    return _checksum(json.dumps(stripped, ensure_ascii=False, sort_keys=True))


def _snapshot_body_key(snapshot_code: str) -> str:
    """页面快照正文在 snapshots/ 下的存档文件名（与 capture_snapshot 的 page_ 前缀对齐）。"""
    digest = hashlib.md5(str(snapshot_code).encode("utf-8")).hexdigest()[:16]
    return f"page_{digest}.json"


def _entry_replay_key(entry: dict[str, Any]) -> str:
    """条目在数据集内的稳定身份：快照编号（同运行内唯一、跨导入可去重）。

    兼容内存原始行（快照编号/id）与已构建交付条目（entry_key/snapshot_code）。
    """
    return str(
        entry.get("entry_key")
        or entry.get("快照编号")
        or entry.get("snapshot_code")
        or entry.get("id")
    )


class SnapshotDeliveryService:
    """快照数据集的打包、发布、下载、导入回放与完整性校验。"""

    MODULE = "snapshot_delivery"
    ENTITY = "快照数据集"

    # ------------------------------------------------------------------ 打包
    def build_dataset(
        self,
        *,
        run_id: int | None,
        snapshot_ids: list[int] | None = None,
        channel_id: int | None = None,
        remark: str = "",
    ) -> tuple[dict[str, Any] | None, str]:
        """按采集运行挑选页面快照，构建一份自包含快照数据集（尚未落盘发布）。"""
        run = store.find("run_history", run_id) if run_id else None
        if run_id is not None and run is None:
            return None, f"采集运行 {run_id} 不存在，无法按运行交付"

        rows = store.rows("page_snapshot")
        if snapshot_ids:
            wanted = set(snapshot_ids)
            rows = [row for row in rows if int(row.get("id", 0)) in wanted]
            missing = wanted - {int(row.get("id", 0)) for row in rows}
            if missing:
                return None, f"页面快照 {sorted(missing)} 不存在，无法纳入交付"
        if not rows:
            return None, "本次运行没有可交付的页面快照条目"

        entries: list[dict[str, Any]] = []
        for row in rows:
            entries.append(self._build_entry(row, run_id))

        full_entries = [e for e in entries if e["replay_level"] == FULL]
        preview_entries = [e for e in entries if e["replay_level"] == PREVIEW_ONLY]
        backfill = [self._backfill_item(e, run_id, (run or {}).get("记录编号", ""))
                    for e in preview_entries]

        dataset_id = self._next_dataset_id(run_id)
        dataset: dict[str, Any] = {
            "dataset_id": dataset_id,
            "kind": DATASET_KIND,
            "dataset_version": DATASET_VERSION,
            "run_id": run_id,
            "run_code": (run or {}).get("记录编号", ""),
            "plan": (run or {}).get("关联计划", ""),
            "channel_id": channel_id,
            "remark": remark,
            "published_at": persist.now_iso(),
            "entries": entries,
            "manifest": {
                "entry_count": len(entries),
                "full_count": len(full_entries),
                "preview_only_count": len(preview_entries),
                "backfill_count": len(backfill),
                # 清单：每个条目身份 -> 是否带完整正文，与 entries 实有正文双向一致
                "entry_index": [
                    {
                        "entry_key": _entry_replay_key(e),
                        "snapshot_id": e["snapshot_id"],
                        "replay_level": e["replay_level"],
                        "has_body": e["has_body"],
                    }
                    for e in entries
                ],
            },
            "backfill": backfill,
        }
        # 先算内容指纹写进 manifest（计算时不含指纹自身），落盘后再复算核对
        body_checksum = _content_checksum(dataset)
        dataset["manifest"]["body_checksum"] = body_checksum
        return dataset, ""

    def _build_entry(self, row: dict[str, Any], run_id: int | None) -> dict[str, Any]:
        """把一条页面快照转成交付条目，明确正文/摘要/来源/理由与可回放性。"""
        code = str(row.get("快照编号", ""))
        source = str(row.get("采集来源", ""))
        summary = str(row.get("DOM摘要", ""))
        status = str(row.get("status", ""))
        body_doc = persist.read_json("snapshots", _snapshot_body_key(code))

        has_body = bool(body_doc and body_doc.get("raw_dom"))
        if has_body:
            replay_level = FULL
            body: Any = body_doc.get("raw_dom")
            body_length = int(body_doc.get("raw_dom_length") or len(str(body)))
            reason = "完整快照：采集正文已随包复制，接收方可独立回放"
        else:
            # 只有预览的旧档：正文未存档或已被回收，只剩 DOM 摘要/预览
            replay_level = PREVIEW_ONLY
            body = None
            body_length = 0
            reason = "仅预览旧档：完整正文未随本次存档保留（历史仅存预览/已回收），不可完整回放，需补采"

        return {
            "entry_key": _entry_replay_key(row),
            "snapshot_id": int(row.get("id", 0)),
            "snapshot_code": code,
            "source": source,
            "run_id": run_id,
            "page_version": row.get("页面版本", ""),
            "summary": summary,
            "snapshot_time": row.get("快照时间", ""),
            "snapshot_type": row.get("快照类型", ""),
            "archived_status": status,
            "reclaimed_at": row.get("回收时间", ""),
            "replay_level": replay_level,
            "has_body": has_body,
            "body_length": body_length,
            "body": body,
            "reason": reason,
        }

    def _backfill_item(
        self, entry: dict[str, Any], run_id: int | None, run_code: str = ""
    ) -> dict[str, Any]:
        """不可回放项的待补清单：按原运行及来源定位补采。"""
        return {
            "entry_key": entry["entry_key"],
            "snapshot_id": entry["snapshot_id"],
            "snapshot_code": entry["snapshot_code"],
            "origin_run_id": run_id,
            "origin_run_code": run_code,
            "source": entry["source"],
            "page_version": entry["page_version"],
            "reason": entry["reason"],
            "backfill_hint": f"按原运行 {run_id} 与采集来源「{entry['source']}」重新采集该页面正文",
        }

    # ------------------------------------------------------------------ 发布/下载
    def publish_dataset(self, dataset: dict[str, Any]) -> str:
        """把数据集原子落盘为不可变交付文件，并登记交付索引。

        原子写（临时文件 + rename）保证导出与回收冲突时文件要么完整可读、要么不存在，
        不会留下半包。
        """
        dataset_id = dataset["dataset_id"]
        filename = f"{dataset_id}.json"
        final_path = persist.data_path(DELIVERY_DIR, filename)
        payload = json.dumps(dataset, ensure_ascii=False, indent=2, default=str)

        # 重新序列化后回算指纹：计算时剔除 body_checksum 自身（构建时也是先算后填），
        # 二者必须一致，防止落盘内容与清单漂移
        reparsed = json.loads(payload)
        actual_checksum = _content_checksum(reparsed)
        if actual_checksum != reparsed["manifest"].get("body_checksum"):
            raise RuntimeError("交付数据集校验和不一致，已中止发布")
        self._assert_manifest_consistency(reparsed)

        tmp_dir = os.path.dirname(final_path)
        fd, tmp_path = tempfile.mkstemp(prefix=f".{dataset_id}.", suffix=".tmp", dir=tmp_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, final_path)  # 同目录原子替换
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

        persist.append_jsonl(DELIVERY_DIR, INDEX_FILE, {
            "dataset_id": dataset_id,
            "run_id": dataset.get("run_id"),
            "run_code": dataset.get("run_code", ""),
            "channel_id": dataset.get("channel_id"),
            "published_at": dataset.get("published_at"),
            "filename": filename,
            "entry_count": dataset["manifest"]["entry_count"],
            "full_count": dataset["manifest"]["full_count"],
            "preview_only_count": dataset["manifest"]["preview_only_count"],
            "backfill_count": dataset["manifest"]["backfill_count"],
            "body_checksum": actual_checksum,
        })
        return final_path

    def publish_for_run(
        self,
        *,
        run_id: int,
        snapshot_ids: list[int] | None = None,
        channel_id: int | None = None,
        remark: str = "",
    ) -> tuple[dict[str, Any] | None, str]:
        """导出通道写快照数据集：构建并原子发布一份交付文件，返回交付摘要。"""
        dataset, err = self.build_dataset(
            run_id=run_id, snapshot_ids=snapshot_ids,
            channel_id=channel_id, remark=remark,
        )
        if err:
            return None, err
        path = self.publish_dataset(dataset)
        return {
            "dataset_id": dataset["dataset_id"],
            "path": path,
            "manifest": dataset["manifest"],
            "backfill": dataset["backfill"],
        }, ""

    def delivery_file_path(self, dataset_id: str) -> str | None:
        """已发布交付文件的路径；不存在返回 None。"""
        path = persist.data_path(DELIVERY_DIR, f"{dataset_id}.json")
        return path if os.path.exists(path) else None

    def load_dataset_file(self, dataset_id: str) -> dict[str, Any] | None:
        """读取已发布交付文件（下载/回放共用，文件自包含）。"""
        return persist.read_json(DELIVERY_DIR, f"{dataset_id}.json")

    def list_deliveries(self, run_id: int | None = None) -> list[dict[str, Any]]:
        """交付登记索引：运行历史据此取得已发布的交付范围和待补清单。"""
        records = persist.read_jsonl(DELIVERY_DIR, INDEX_FILE)
        if run_id is not None:
            records = [r for r in records if r.get("run_id") == run_id]
        return records

    # ------------------------------------------------------------------ 校验
    def _assert_manifest_consistency(self, dataset: dict[str, Any]) -> None:
        """清单与实有正文必须一致：manifest.entry_index 与 entries 逐条核对。"""
        entries = dataset.get("entries", [])
        index = {i["entry_key"]: i for i in dataset["manifest"].get("entry_index", [])}
        if len(index) != len(entries):
            raise RuntimeError("交付清单条目数与实际条目数不一致")
        for e in entries:
            slot = index.get(e["entry_key"])
            if slot is None:
                raise RuntimeError(f"条目 {e['entry_key']} 未登记到交付清单")
            if bool(slot["has_body"]) != bool(e.get("has_body")):
                raise RuntimeError(f"条目 {e['entry_key']} 清单正文标记与实有正文不一致")
            if slot["replay_level"] != e.get("replay_level"):
                raise RuntimeError(f"条目 {e['entry_key']} 清单回放分级与实际不一致")
            # full 必须真带正文；preview_only 必须没有正文，旧档不能混进完整回放
            if e["replay_level"] == FULL and not e.get("body"):
                raise RuntimeError(f"条目 {e['entry_key']} 标记完整却缺少正文")
            if e["replay_level"] == PREVIEW_ONLY and e.get("body"):
                raise RuntimeError(f"条目 {e['entry_key']} 仅预览档却携带正文")

    def verify_dataset(self, dataset: dict[str, Any]) -> tuple[bool, list[str]]:
        """接收方核对数据集：结构、校验和、清单与正文一致性。"""
        problems: list[str] = []
        if dataset.get("kind") != DATASET_KIND:
            problems.append("数据集类型标识不正确")
        manifest = dataset.get("manifest") or {}
        expected = manifest.get("body_checksum")
        if not expected:
            problems.append("清单缺少校验和")
        elif _content_checksum(dataset) != expected:
            problems.append("内容校验和与清单记录不一致，文件可能被改写")
        try:
            self._assert_manifest_consistency(dataset)
        except RuntimeError as exc:
            problems.append(str(exc))
        if manifest.get("entry_count") != len(dataset.get("entries", [])):
            problems.append("清单 entry_count 与实际条目数不符")
        if problems:
            return False, problems
        return True, [
            f"校验通过：完整 {manifest.get('full_count', 0)} 条、"
            f"仅预览待补 {manifest.get('preview_only_count', 0)} 条，"
            f"校验和 {str(expected)[:12]}"
        ]

    # ------------------------------------------------------------------ 导入回放
    def import_dataset(self, dataset: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """接收方导入数据集并重建可回放条目（幂等）。

        - 以 dataset_id + entry_key 去重：重复导入同一数据集不产生新采集、不重复建条目；
        - 只有预览的旧档不计入完整回放，只进待补清单；
        - 完整快照把正文写回接收方本地 page 存档，可随时验正文并回放。
        """
        ok, messages = self.verify_dataset(dataset)
        if not ok:
            return None, messages

        dataset_id = str(dataset.get("dataset_id", ""))
        ledger = persist.read_jsonl(DELIVERY_DIR, IMPORT_LEDGER)
        already = {
            (r.get("dataset_id"), r.get("entry_key"))
            for r in ledger if r.get("action") == "imported"
        }

        replayed: list[dict[str, Any]] = []
        backfill: list[dict[str, Any]] = list(dataset.get("backfill", []))
        skipped_duplicates: list[str] = []
        snap_rows = store.rows("page_snapshot")

        for entry in dataset.get("entries", []):
            key = (dataset_id, entry["entry_key"])
            if key in already:
                # 重复读取：旧档/已导入项不算本次新采集
                skipped_duplicates.append(entry["entry_key"])
                continue

            if entry["replay_level"] == FULL:
                # 完整快照：正文落接收方本地存档，并重建一条可回放页面快照条目
                body_doc = {
                    "page_url": entry.get("source", ""),
                    "dom_hash": hashlib.md5(str(entry.get("body", "")).encode()).hexdigest()[:16],
                    "raw_dom": entry.get("body"),
                    "raw_dom_length": entry.get("body_length", 0),
                    "timestamp": entry.get("snapshot_time", persist.now_iso()),
                    "dom_preview": str(entry.get("body", ""))[:500],
                    "origin_dataset": dataset_id,
                    "imported": True,
                }
                persist.write_json(
                    "snapshots", _snapshot_body_key(entry["snapshot_code"]), body_doc
                )
                replayed_row = self._upsert_imported_snapshot(snap_rows, entry, dataset_id)
                replayed.append({
                    "entry_key": entry["entry_key"],
                    "snapshot_code": entry["snapshot_code"],
                    "snapshot_id": replayed_row["id"],
                    "summary": entry.get("summary", ""),
                    "body_length": entry.get("body_length", 0),
                })
            # preview_only：不建可回放正文条目，保留在待补清单
            persist.append_jsonl(DELIVERY_DIR, IMPORT_LEDGER, {
                "dataset_id": dataset_id,
                "entry_key": entry["entry_key"],
                "action": "imported",
                "replay_level": entry["replay_level"],
                "timestamp": persist.now_iso(),
            })

        report = {
            "dataset_id": dataset_id,
            "run_id": dataset.get("run_id"),
            "imported_full": len(replayed),
            "backfill_required": len(backfill),
            "skipped_duplicates": skipped_duplicates,
            "replayed": replayed,
            "backfill": backfill,
            "verify_messages": messages,
            "imported_at": persist.now_iso(),
        }
        persist.append_jsonl(DELIVERY_DIR, IMPORT_LEDGER, {
            "dataset_id": dataset_id,
            "action": "import_summary",
            "imported_full": len(replayed),
            "backfill_required": len(backfill),
            "skipped": len(skipped_duplicates),
            "timestamp": persist.now_iso(),
        })
        return report, []

    def _upsert_imported_snapshot(
        self, snap_rows: list[dict[str, Any]], entry: dict[str, Any], dataset_id: str
    ) -> dict[str, Any]:
        """导入回放：按快照编号复用或新建页面快照条目，标记为回放而非新采集。"""
        existing = next(
            (r for r in snap_rows if str(r.get("快照编号")) == entry["snapshot_code"]), None
        )
        if existing is not None:
            existing["imported_from"] = dataset_id
            existing["replay"] = True
            existing["updated_at"] = persist.now_iso()
            return existing
        new_id = max((int(r.get("id", 0)) for r in snap_rows), default=0) + 1
        row = {
            "id": new_id,
            "快照编号": entry["snapshot_code"],
            "采集来源": entry.get("source", ""),
            "页面版本": entry.get("page_version", ""),
            "DOM摘要": entry.get("summary", ""),
            "快照时间": entry.get("snapshot_time", ""),
            "快照类型": entry.get("snapshot_type", ""),
            "数据量": str(entry.get("body_length", 0)),
            "回收时间": entry.get("reclaimed_at", ""),
            "status": "已存档",
            "pending": True,
            "abnormal": False,
            "version": "",
            "imported_from": dataset_id,
            "replay": True,  # 回放重建，不算本次新采集
            "updated_at": persist.now_iso(),
        }
        snap_rows.append(row)
        return row

    def replay_entry(self, dataset_id: str, entry_key: str) -> dict[str, Any] | None:
        """页面快照读取同一份数据集：定位条目并验正文，重建可回放条目视图。"""
        dataset = self.load_dataset_file(dataset_id)
        if dataset is None:
            return None
        entry = next(
            (e for e in dataset["entries"] if e["entry_key"] == entry_key), None
        )
        if entry is None:
            return None
        body_ok = False
        if entry.get("has_body") and entry.get("body"):
            stored = persist.read_json(
                "snapshots", _snapshot_body_key(entry["snapshot_code"])
            )
            body_ok = bool(stored and stored.get("raw_dom") == entry["body"])
        return {
            "dataset_id": dataset_id,
            "entry_key": entry_key,
            "replay_level": entry["replay_level"],
            "has_body": entry["has_body"],
            "body_verified": body_ok,
            "summary": entry.get("summary", ""),
            "source": entry.get("source", ""),
            "reason": entry.get("reason", ""),
            "replayable": entry["replay_level"] == FULL and body_ok,
        }

    # ------------------------------------------------------------------ 内部
    def _next_dataset_id(self, run_id: int | None) -> str:
        existing = len(persist.read_jsonl(DELIVERY_DIR, INDEX_FILE))
        stamp = persist.now_iso().replace(":", "").replace("-", "")[2:13]
        return f"DS-{stamp}-R{run_id or 0}-{existing + 1:03d}"


# 单例，供路由和其它服务复用
snapshot_delivery_service = SnapshotDeliveryService()
