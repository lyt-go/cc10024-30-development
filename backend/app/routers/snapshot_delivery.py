"""快照数据集交付接口：构建、发布、下载、导入回放、完整性校验与运行交付范围。

路径前缀 /api/snapshot-delivery 为固定路径，需在动态资源路由 /api/{module} 之前注册。
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from app.services.snapshot_delivery import (
    DATASET_KIND,
    snapshot_delivery_service as delivery,
)
from app.services import service_for
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/snapshot-delivery", tags=["快照数据集交付"])


class BuildPayload(BaseModel):
    run_id: int
    snapshot_ids: list[int] | None = None
    channel_id: int | None = None
    remark: str = ""


class PublishPayload(BaseModel):
    run_id: int
    snapshot_ids: list[int] = Field(default_factory=list)
    channel_id: int
    remark: str = ""


class ImportPayload(BaseModel):
    dataset: dict[str, Any]


class AttachBodyPayload(BaseModel):
    raw_dom: str
    page_url: str = ""


@router.post("/snapshots/{snapshot_id}/body")
def attach_snapshot_body(snapshot_id: int, payload: AttachBodyPayload) -> dict[str, Any]:
    """为页面快照附上采集完整正文；未附正文的条目在交付时按预览旧档处理。"""
    snap_svc = service_for("page_snapshot")
    result, err = snap_svc.attach_body(snapshot_id, payload.raw_dom, payload.page_url)
    if err:
        raise HTTPException(status_code=400, detail=err)
    return {"ok": True, "message": "完整正文已存档", "result": result}


@router.post("/build")
def build_dataset(payload: BuildPayload) -> dict[str, Any]:
    """构建数据集预览（不落盘）：让存档方先确认每个条目的正文/摘要/来源/理由。"""
    dataset, err = delivery.build_dataset(
        run_id=payload.run_id,
        snapshot_ids=payload.snapshot_ids or None,
        channel_id=payload.channel_id,
        remark=payload.remark,
    )
    if err:
        raise HTTPException(status_code=400, detail=err)
    return {"ok": True, "dataset": dataset}


@router.post("/publish")
def publish_dataset(payload: PublishPayload) -> dict[str, Any]:
    """导出通道写快照数据集：构建并原子发布不可变交付文件。"""
    export_svc = service_for("export_channel")
    result, err = export_svc.deliver_snapshot_dataset(
        channel_id=payload.channel_id,
        run_id=payload.run_id,
        snapshot_ids=payload.snapshot_ids or None,
        remark=payload.remark,
    )
    if err:
        raise HTTPException(status_code=400, detail=err)
    return {"ok": True, "message": "快照数据集已发布", "delivery": result}


@router.get("/deliveries")
def list_deliveries(run_id: int | None = Query(default=None)) -> dict[str, Any]:
    """交付登记索引：已发布的交付范围清单。"""
    records = delivery.list_deliveries(run_id=run_id)
    return {"items": records, "total": len(records)}


@router.get("/runs/{run_id}/scope")
def run_delivery_scope(run_id: int) -> dict[str, Any]:
    """运行历史：取得该运行已发布的交付范围和待补清单。"""
    run_svc = service_for("run_history")
    return run_svc.delivery_scope(run_id)


@router.get("/deliveries/{dataset_id}")
def get_dataset(dataset_id: str) -> dict[str, Any]:
    """读取交付数据集全文（接收方据此导入回放，文件自包含）。"""
    dataset = delivery.load_dataset_file(dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail=f"交付数据集 {dataset_id} 不存在")
    return dataset


@router.get("/deliveries/{dataset_id}/download")
def download_dataset(dataset_id: str) -> FileResponse:
    """下载交付文件：独立可回放的 JSON 数据集。"""
    path = delivery.delivery_file_path(dataset_id)
    if path is None:
        raise HTTPException(status_code=404, detail=f"交付数据集 {dataset_id} 不存在")
    return FileResponse(
        path,
        media_type="application/json; charset=utf-8",
        filename=f"{dataset_id}.json",
    )


@router.get("/deliveries/{dataset_id}/verify")
def verify_dataset(dataset_id: str) -> dict[str, Any]:
    """核对已发布数据集：清单与实有正文是否一致、校验和是否齐全。"""
    dataset = delivery.load_dataset_file(dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail=f"交付数据集 {dataset_id} 不存在")
    ok, messages = delivery.verify_dataset(dataset)
    return {"ok": ok, "dataset_id": dataset_id, "messages": messages}


@router.get("/deliveries/{dataset_id}/entries/{entry_key}/replay")
def replay_entry(dataset_id: str, entry_key: str) -> dict[str, Any]:
    """页面快照读取同一数据集：验正文并重建可回放条目视图。"""
    result = delivery.replay_entry(dataset_id, entry_key)
    if result is None:
        raise HTTPException(status_code=404, detail="数据集或条目不存在")
    return result


@router.post("/import")
def import_dataset(payload: ImportPayload) -> dict[str, Any]:
    """接收方导入数据集（直接提交数据集 JSON），重复导入幂等。"""
    dataset = payload.dataset
    if dataset.get("kind") != DATASET_KIND:
        raise HTTPException(status_code=400, detail="不是合法的快照数据集（kind 标识不符）")
    report, problems = delivery.import_dataset(dataset)
    if report is None:
        raise HTTPException(status_code=400, detail=f"数据集校验失败：{'；'.join(problems)}")
    return {"ok": True, "message": "导入完成", "report": report}


@router.post("/import/file")
async def import_dataset_file(request: Request) -> dict[str, Any]:
    """接收方按交付文件原样导入：请求体即下载得到的 JSON 文件字节，可独立回放。"""
    raw = (await request.body()).decode("utf-8")
    try:
        dataset = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"交付文件解析失败：{exc}")
    if dataset.get("kind") != DATASET_KIND:
        raise HTTPException(status_code=400, detail="不是合法的快照数据集（kind 标识不符）")
    report, problems = delivery.import_dataset(dataset)
    if report is None:
        raise HTTPException(status_code=400, detail=f"数据集校验失败：{'；'.join(problems)}")
    return {"ok": True, "message": "文件导入完成", "report": report}
