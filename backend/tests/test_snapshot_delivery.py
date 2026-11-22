"""快照数据集交付端到端测试。

场景（对应业务诉求）：
1. 选入三个页面快照档案：两份完整快照、一份只有预览的旧档；
2. 导出通道按采集运行构建并发布快照数据集（完整快照 + 预览旧档 + 待补清单）；
3. 在确认交付和下载之间触发一次回收（含已交付快照，验证冲突保护）；
4. 接收方导入两次：验证幂等、运行交付记录、可回放正文摘要、待补来源；
5. 再核对已完成文件不会因后续清理变成半包（清单与实有正文始终一致）；
6. 页面快照读取同一数据集验正文并重建可回放条目，重复读取不把旧档算新采集。
"""
from __future__ import annotations

import os
import sys
import json

backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app import persistence as persist

# 清理测试数据目录，保证可重复执行
for sub in ["runs", "snapshots", "reports", "exports", "deliveries"]:
    p = persist.data_path(sub, "")
    if os.path.isdir(p):
        for f in os.listdir(p):
            os.remove(os.path.join(p, f))


def _seed_three_archives():
    """准备三个档案：完整 A、完整 B、只有预览的旧档 C。"""
    from app.store import store
    from app.services.page_snapshot import PageSnapshotService

    svc = PageSnapshotService()
    codes = ["PAGE-T-A", "PAGE-T-B", "PAGE-T-C"]
    created = []
    for idx, code in enumerate(codes, start=1):
        entry, missing = svc.create_entry({
            "快照编号": code,
            "采集来源": f"https://src{idx}.test.test/p{idx}",
            "页面版本": f"v{idx}",
            "DOM摘要": f"摘要-{code}",
            "快照时间": f"2026-11-2{idx}",
            "快照类型": "HTML",
            "数据量": "0",
            "回收时间": "",
        })
        assert entry is not None, f"登记失败: {missing}"
        created.append(entry)
        svc.run_action(entry["id"], "标记存档")

    # A、B 采集完整正文；C 模拟历史旧档：只有预览、没有完整正文文件
    svc.capture_snapshot("https://src1.test.test/p1", "hashA", "<html><body>完整正文A</body></html>")
    svc.capture_snapshot("https://src2.test.test/p2", "hashB", "<html><body>完整正文B</body></html>")
    # 通过正式服务能力把完整正文挂到对应条目（交付服务按快照编号定位正文）
    svc.attach_body(created[0]["id"], "<html><body>完整正文A</body></html>",
                    "https://src1.test.test/p1")
    svc.attach_body(created[1]["id"], "<html><body>完整正文B</body></html>",
                    "https://src2.test.test/p2")
    # 旧档 C 只保留一条预览记录文件（无 raw_dom）
    from app.services.snapshot_delivery import _snapshot_body_key
    persist.write_json("snapshots", _snapshot_body_key("PAGE-T-C"), {
        "page_url": "https://src3.test.test/p3",
        "dom_hash": "hashC",
        "raw_dom": "",
        "raw_dom_length": 0,
        "timestamp": persist.now_iso(),
        "dom_preview": "仅预览片段C",
    })
    return svc, created


def test_full_delivery_replay_and_backfill():
    from app.store import store
    from app.services.export_channel import ExportChannelService
    from app.services.run_history import RunHistoryService
    from app.services.snapshot_delivery import (
        snapshot_delivery_service as delivery,
        _snapshot_body_key,
    )

    snap_svc, archives = _seed_three_archives()
    id_a, id_b, id_c = [a["id"] for a in archives]

    # 一个已启用的导出通道 + 一个采集运行
    export_svc = ExportChannelService()
    channel, missing = export_svc.create_entry({
        "通道编号": "EXPO-T-1", "输出格式": "json", "目标路径": "deliveries/",
    })
    assert channel is not None
    export_svc.run_action(channel["id"], "启用通道")

    run_svc = RunHistoryService()
    run, missing = run_svc.create_entry({
        "记录编号": "RUN-T-1", "关联计划": "PLAN-T-1", "开始时间": "2026-11-22",
    })
    assert run is not None
    run_id = run["id"]

    # 1) 构建数据集：预览每个条目的正文/摘要/来源/理由
    dataset, err = delivery.build_dataset(
        run_id=run_id, snapshot_ids=[id_a, id_b, id_c], channel_id=channel["id"],
    )
    assert err == "", err
    levels = {e["snapshot_code"]: e["replay_level"] for e in dataset["entries"]}
    assert levels["PAGE-T-A"] == "full"
    assert levels["PAGE-T-B"] == "full"
    assert levels["PAGE-T-C"] == "preview_only"
    # 每个条目都带正文/摘要/来源/理由四要素
    for e in dataset["entries"]:
        assert e["source"] and e["summary"] and e["reason"]
        assert "body" in e and "has_body" in e
    # 旧档 C 不可完整回放，进入待补清单，可按原运行及来源定位
    assert dataset["manifest"]["full_count"] == 2
    assert dataset["manifest"]["preview_only_count"] == 1
    backfill_codes = {b["snapshot_code"] for b in dataset["backfill"]}
    assert backfill_codes == {"PAGE-T-C"}
    c_backfill = next(b for b in dataset["backfill"] if b["snapshot_code"] == "PAGE-T-C")
    assert c_backfill["origin_run_id"] == run_id
    assert "src3.test.test" in c_backfill["source"]

    # 2) 导出通道发布快照数据集（原子落盘）
    result, err = export_svc.deliver_snapshot_dataset(
        channel_id=channel["id"], run_id=run_id, snapshot_ids=[id_a, id_b, id_c],
    )
    assert err == "", err
    dataset_id = result["dataset_id"]
    file_path = delivery.delivery_file_path(dataset_id)
    assert file_path and os.path.exists(file_path)

    # 已发布文件自带校验和，且独立可校验
    on_disk = delivery.load_dataset_file(dataset_id)
    ok, messages = delivery.verify_dataset(on_disk)
    assert ok, messages

    # 3) 确认与下载之间触发回收：回收已交付的 A，再回收未交付语义的 C（同样在交付内）
    entry_a, msg = snap_svc.run_action(id_a, "启动回收")
    assert entry_a is not None, msg
    assert entry_a["status"] == "已回收"
    # 回收与导出冲突：已交付正文文件必须保留，文件不变成半包
    assert entry_a.get("reclaim_conflict") is True
    assert os.path.exists(persist.data_path("snapshots", _snapshot_body_key("PAGE-T-A")))

    # 回收后交付文件仍完整可读，清单与实有正文一致
    after_reclaim = delivery.load_dataset_file(dataset_id)
    ok2, msg2 = delivery.verify_dataset(after_reclaim)
    assert ok2, msg2
    full_after = [e for e in after_reclaim["entries"] if e["replay_level"] == "full"]
    assert len(full_after) == 2 and all(e["body"] for e in full_after)

    # 运行历史取得已发布交付范围和待补清单
    scope = run_svc.delivery_scope(run_id)
    assert scope["delivery_count"] == 1
    assert scope["deliveries"][0]["dataset_id"] == dataset_id
    assert scope["backfill_count"] == 1
    assert scope["backfill"][0]["snapshot_code"] == "PAGE-T-C"

    # 4) 接收方第一次导入：重建两份可回放正文，C 进待补
    report1, problems = delivery.import_dataset(after_reclaim)
    assert report1 is not None, problems
    assert report1["imported_full"] == 2
    assert report1["backfill_required"] == 1
    replayed_codes = {r["snapshot_code"] for r in report1["replayed"]}
    assert replayed_codes == {"PAGE-T-A", "PAGE-T-B"}
    # 导入重建的页面快照条目标记为回放，不算新采集
    imported_rows = [
        r for r in store.rows("page_snapshot")
        if r.get("快照编号") in {"PAGE-T-A", "PAGE-T-B"} and r.get("replay") is True
    ]
    assert len(imported_rows) == 2

    # 5) 接收方第二次导入同一数据集：幂等，不新增回放、不把旧档算新采集
    before_rows = len(store.rows("page_snapshot"))
    report2, problems2 = delivery.import_dataset(after_reclaim)
    assert report2 is not None, problems2
    assert report2["imported_full"] == 0
    assert report2["backfill_required"] == 1  # 待补清单仍可重复查看
    assert set(report2["skipped_duplicates"]) == {"PAGE-T-A", "PAGE-T-B", "PAGE-T-C"}
    assert len(store.rows("page_snapshot")) == before_rows

    # 6) 页面快照读取同一数据集：验正文并重建可回放条目视图
    view = snap_svc.replay_from_dataset(dataset_id, "PAGE-T-A")
    assert view is not None
    assert view["replayable"] is True and view["body_verified"] is True
    view_c = snap_svc.replay_from_dataset(dataset_id, "PAGE-T-C")
    assert view_c["replayable"] is False and view_c["has_body"] is False

    # 7) 后续再清理（模拟回收 B）也不会让已完成文件变成半包
    snap_svc.run_action(id_b, "启动回收")
    final_dataset = delivery.load_dataset_file(dataset_id)
    ok3, msg3 = delivery.verify_dataset(final_dataset)
    assert ok3, msg3
    assert final_dataset["manifest"]["entry_count"] == 3
    assert delivery.delivery_file_path(dataset_id) is not None

    # domain_snapshot 含交付统计
    ps_snap = snap_svc.domain_snapshot()
    assert ps_snap["reclaim_conflicts"] >= 1
    rh_snap = run_svc.domain_snapshot()
    assert rh_snap["snapshot_deliveries"] >= 1
    assert rh_snap["snapshot_backfill_items"] >= 1


def test_manifest_always_matches_actual_bodies():
    """清单与实有正文始终一致：篡改条目的 body/has_body 会被校验拦截。"""
    from app.services.snapshot_delivery import snapshot_delivery_service as delivery

    snap_svc, archives = _seed_three_archives()
    from app.services.export_channel import ExportChannelService
    export_svc = ExportChannelService()
    channel, _ = export_svc.create_entry({
        "通道编号": "EXPO-T-2", "输出格式": "json", "目标路径": "deliveries/",
    })
    export_svc.run_action(channel["id"], "启用通道")
    from app.services.run_history import RunHistoryService
    run, _ = RunHistoryService().create_entry({
        "记录编号": "RUN-T-2", "关联计划": "PLAN-T-2", "开始时间": "2026-11-22",
    })

    result, err = export_svc.deliver_snapshot_dataset(
        channel_id=channel["id"], run_id=run["id"],
        snapshot_ids=[a["id"] for a in archives],
    )
    assert err == "", err
    dataset = delivery.load_dataset_file(result["dataset_id"])

    # 篡改：给仅预览旧档塞正文，破坏清单一致性
    tampered = json.loads(json.dumps(dataset, ensure_ascii=False))
    for e in tampered["entries"]:
        if e["replay_level"] == "preview_only":
            e["body"] = "伪造正文"
    ok, problems = delivery.verify_dataset(tampered)
    assert ok is False
    assert any("正文" in p for p in problems)

    # 篡改：删掉完整条目的正文却仍标记 full
    tampered2 = json.loads(json.dumps(dataset, ensure_ascii=False))
    for e in tampered2["entries"]:
        if e["replay_level"] == "full":
            e["body"] = None
            e["has_body"] = False
    ok2, problems2 = delivery.verify_dataset(tampered2)
    assert ok2 is False
    assert problems2


if __name__ == "__main__":
    test_full_delivery_replay_and_backfill()
    print("PASS: test_full_delivery_replay_and_backfill")
    test_manifest_always_matches_actual_bodies()
    print("PASS: test_manifest_always_matches_actual_bodies")
    print("\nALL SNAPSHOT DELIVERY TESTS PASSED")
