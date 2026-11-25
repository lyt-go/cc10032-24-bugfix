"""压缩盘点、预检、领取和任务详情的专用 HTTP 接口。

通用 CRUD/动作仍由 resource.py 提供；这里只承载盘点一致性场景需要的领域入口。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.services import service_for
from app.services.inventory import iter_inventories
from app.store import store

router = APIRouter(prefix="/api/compaction", tags=["compaction-inventory"])


def _service():
    service = service_for("compaction")
    assert service is not None
    return service


@router.get("/inventories")
def list_inventories(table: str | None = None, all_versions: bool = False) -> dict[str, Any]:
    rows = iter_inventories(table)
    if not all_versions:
        # 每个表/分区只暴露最新有效版本。
        effective: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            if not row.get("effective"):
                continue
            key = (row["table"], row["partition"])
            old = effective.get(key)
            if old is None or row.get("order", 0) >= old.get("order", 0):
                effective[key] = row
        rows = list(effective.values())
    return {"ok": True, "items": [{k: v for k, v in row.items() if k != "raw"} for row in rows]}


@router.post("/inventories")
async def register_inventory(request: Request) -> dict[str, Any]:
    payload = await request.json()
    return _service().register_inventory(payload)


@router.post("/inventories/{inventory_id}/confirm")
def confirm_inventory(inventory_id: str) -> dict[str, Any]:
    return _service().confirm_inventory(inventory_id)


@router.post("/replacements")
async def replace_partition(request: Request) -> dict[str, Any]:
    payload = await request.json()
    return _service().replace_partition(payload)


@router.post("/sync")
async def sync_inventories(request: Request) -> dict[str, Any]:
    table = None
    create_missing = False
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if isinstance(payload, dict):
        table = payload.get("table") or payload.get("目标表")
        create_missing = bool(payload.get("create_missing", False))
    return _service().writeback_inventories(table=table, create_missing=create_missing)


@router.get("/qualifications")
def qualifications(table: str | None = None) -> dict[str, Any]:
    items = _service().list_task_qualifications(table=table)
    return {"ok": True, "items": items, "total": len(items)}


@router.get("/claimable")
def claimable(table: str | None = None) -> dict[str, Any]:
    items = _service().list_claimable_tasks(table=table)
    return {"ok": True, "items": items, "total": len(items)}


@router.post("/claim-next")
async def claim_next(request: Request) -> dict[str, Any]:
    table = None
    worker = None
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if isinstance(payload, dict):
        table = payload.get("table") or payload.get("目标表")
        worker = payload.get("worker") or payload.get("领取人")
    return _service().claim_next_compaction(table=table, worker=worker)


@router.get("/{entry_id}/detail")
def task_detail(entry_id: int) -> dict[str, Any]:
    detail = _service().get_task_detail(entry_id)
    if detail is None:
        return {"ok": False, "message": f"压缩任务 {entry_id} 不存在"}
    return {"ok": True, "entry": detail}


@router.post("/{entry_id}/adopt-replacement")
async def adopt_replacement(entry_id: int, request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    partition = payload.get("replacement_partition") or payload.get("接替分区") if isinstance(payload, dict) else None
    execute = bool(payload.get("execute", False)) if isinstance(payload, dict) else False
    return _service().adopt_replacement(entry_id, partition, execute=execute)


@router.post("/{entry_id}/precheck")
async def precheck(entry_id: int, request: Request) -> dict[str, Any]:
    try:
        options = await request.json()
    except Exception:
        options = {}
    if not isinstance(options, dict):
        options = {}
    return _service().precheck_compaction(entry_id, **options)


@router.post("/{entry_id}/execute")
async def execute(entry_id: int, request: Request) -> dict[str, Any]:
    try:
        options = await request.json()
    except Exception:
        options = {}
    if not isinstance(options, dict):
        options = {}
    token = options.get("precheck_token") or options.get("token")
    return _service().execute_compaction(entry_id, precheck_token=token, **options)


@router.post("/{entry_id}/rollback")
async def rollback(entry_id: int, request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    reason = payload.get("reason", "") if isinstance(payload, dict) else ""
    return _service().rollback_compaction(entry_id, reason=reason)
