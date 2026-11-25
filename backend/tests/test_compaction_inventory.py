"""压缩合并盘点范围、版本兼容和分区接替的回归测试。"""
from __future__ import annotations

import pytest

from app.services.compaction import CompactionService
from app.store import store

TABLE = "__test_compaction_inventory__"


@pytest.fixture
def service():
    cp = CompactionService()
    store._partition_inventories.clear()
    store._partition_replacements.clear()
    store._compaction_prechecks.clear()
    store._tables["compaction"] = [
        row for row in store.rows("compaction") if row.get("目标表") != TABLE
    ]
    yield cp
    store._tables["compaction"] = [
        row for row in store.rows("compaction") if row.get("目标表") != TABLE
    ]
    pipeline = store.pipeline("partition_governance")
    if pipeline is not None:
        pipeline._artifacts = [
            row for row in pipeline._artifacts
            if not (isinstance(row, dict) and row.get("table") == TABLE)
        ]
    store._partition_inventories.clear()
    store._partition_replacements.clear()
    store._compaction_prechecks.clear()


def add_task(task_id: int, partition: str, count: object, status: str = "待执行") -> dict:
    row = {
        "id": task_id,
        "status": status,
        "pending": status == "待执行",
        "abnormal": False,
        "任务编号": f"TEST-COMP-{task_id}",
        "目标表": TABLE,
        "目标分区": partition,
        "文件数量": count,
        "压缩算法": "snappy",
    }
    store.rows("compaction").append(row)
    return row


def test_replaced_and_other_partition_inventories_do_not_accumulate(service):
    a = add_task(9101, "A", "60")
    b = add_task(9102, "B", "60")

    service.register_inventory(
        TABLE, "A", 60, inventory_id="A-old", version=1,
        status="已替换", confirmed=False, valid=False, create_missing=False,
    )
    service.register_inventory(TABLE, "A", 3, inventory_id="A-new", version=2, create_missing=False)
    service.register_inventory(TABLE, "B", 60, inventory_id="B-1", version=1, create_missing=False)
    service.writeback_inventories(table=TABLE)

    assert int(a["文件数量"]) == 3
    assert int(b["文件数量"]) == 60

    result = service.execute_compaction(9101)
    assert result["ok"] is False
    assert result["skipped"] is True
    assert result["file_count"] == 3
    assert a["status"] == "待执行"


def test_removed_partition_conflicts_and_replacement_is_claimable(service):
    a = add_task(9201, "A", 3)
    b = add_task(9202, "B", 60)
    service.register_inventory(TABLE, "A", 3, inventory_id="A-new", version=2, create_missing=False)
    service.register_inventory(TABLE, "B", 60, inventory_id="B-1", version=1, create_missing=False)
    low = service.execute_compaction(9201)
    assert low["skipped"] is True and low["file_count"] == 3
    service.precheck_compaction(9201)

    service.replace_partition(TABLE, "A", "A2", 18)

    result = service.execute_compaction(9201)
    assert result["ok"] is False
    assert result["executed"] is False
    assert result["partition_conflict"] is True
    assert "A" in result["reason"] and "A2" in result["reason"]
    assert result["replacement_partition"] == "A2"
    assert a["status"] == "待执行"

    claimable = service.list_claimable_tasks(table=TABLE)
    assert [(item["partition"], item["file_count"]) for item in claimable] == [
        ("B", 60),
        ("A2", 18),
    ]

    claimed = service.claim_next_compaction(table=TABLE)
    assert claimed["ok"] is True
    assert claimed["partition"] == "B"
    assert [
        item["partition"] for item in service.list_claimable_tasks(table=TABLE)
    ] == ["A2"]

    detail = service.get_task_detail(9201)
    assert detail["adopted_inventory"]["file_count"] == 3
    assert any(item.get("stage") == "threshold" for item in detail["attempt_history"])
    assert any(item.get("stage") == "partition_conflict" for item in detail["attempt_history"])


def test_pipeline_artifacts_are_scoped_by_partition_and_latest_version(service):
    add_task(9401, "PA", "3")
    pipeline = store.pipeline("partition_governance") or store.create_pipeline(
        "partition_governance", ["partition_scale"], "compaction"
    )
    pipeline._artifacts.extend([
        {"table": TABLE, "partition": "PA", "files": 60, "version": 1, "status": "replaced", "valid": False},
        {"table": TABLE, "partition": "PA", "files": 3, "version": 2, "status": "active", "valid": True},
        {"table": TABLE, "partition": "PB", "files": 60, "version": 1, "status": "active", "valid": True},
    ])

    result = service.execute_compaction(9401)
    assert result["ok"] is False
    assert result["file_count"] == 3
    assert "无需合并" in result["reason"]


def test_historical_completed_task_keeps_original_inventory(service):
    old = add_task(9301, "H", 80, status="已完成")
    old["执行时间"] = "2026-09-03"
    old["adopted_inventory"] = {"inventory_id": "H-inv", "version": "v9", "file_count": 80}
    service.register_inventory(TABLE, "H", 1, inventory_id="H-new", version="v10", create_missing=False)

    detail = service.get_task_detail(9301)
    assert detail["historical"] is True
    assert detail["adopted_inventory"]["version"] == "v9"
    assert detail["adopted_inventory"]["file_count"] == 80
    assert old["文件数量"] == 80
