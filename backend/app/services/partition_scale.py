"""partition_scale 领域服务：分区扩缩与治理。

业务逻辑：
1. 分区策略管理：目标表分区粒度、预创建数量、保留天数
2. 分区治理链路：partition_scale → compaction（分区文件过多触发合并）
3. 失败场景：预创建分区冲突、清理策略误删、扩缩规则失效
4. 与上下游流水线：为 compaction 提供分区文件数量统计

跨模块数据流：
  partition_scale → compaction（分区策略决定压缩任务触发条件）
"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class PartitionScaleService(BaseModuleService):
    MODULE = "partition_scale"
    ENTITY = "分区策略"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["策略编号", "目标表", "分区字段", "分区粒度", "预创建数量", "保留天数", "自动清理", "扩缩规则"]
    REQUIRED_FIELDS = ["策略编号", "目标表", "分区字段"]
    STATUS_ORDER = ["草稿", "待验证", "已生效", "已停用"]
    ACTION_RULES = {"提交验证": "待验证", "标记生效": "已生效", "停用策略": "已停用"}
    NEGATIVE_ACTIONS = ["停用策略"]

    PIPELINE = "partition_governance"
    DOWNSTREAM_MODULES = ["compaction"]
    PRODUCES_ARTIFACT = "分区统计记录"

    def scale_partitions(self, entry_id: int) -> dict[str, Any]:
        """执行分区扩缩：预创建分区或清理过期分区。

        失败场景：
        - 预创建分区冲突：分区已存在
        - 清理策略误删：保留天数小于已有数据
        - 粒度不合法：分区粒度不匹配
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "reason": f"策略 {entry_id} 不存在"}
        if entry.get("status") != "已生效":
            return {"ok": False, "reason": f"策略未生效，当前状态: {entry.get('status')}"}

        pre_create = int(str(entry.get("预创建数量", "10")))
        retention_days = int(str(entry.get("保留天数", "30")))
        target_table = str(entry.get("目标表", ""))
        granularity = str(entry.get("分区粒度", "day"))

        valid_granularities = ("hour", "day", "month")
        if granularity not in valid_granularities:
            return {"ok": False, "reason": f"分区粒度 {granularity} 不在允许范围: {valid_granularities}"}

        if pre_create > 100:
            return {"ok": False, "reason": f"预创建数量 {pre_create} 过大"}

        if retention_days < 1:
            return {"ok": False, "reason": f"保留天数 {retention_days} 不可少于1"}

        # 模拟分区创建和清理
        created = []
        cleaned = 0
        total_files = 0
        for i in range(pre_create):
            part_key = f"{target_table}_{granularity}_{i:04d}"
            created.append(part_key)
            total_files += 3  # 每个分区约3个小文件
        if retention_days < 7:
            cleaned = 2  # 模拟清理过期分区

        # 更新统计。每条记录都是对应分区的一版已确认盘点；
        # 重复扩缩产生的新批次由 compaction 按版本取最新有效记录，不能累计求和。
        batch_id = f"PART_{entry_id}_{time.time_ns()}"
        stats = [{
            "partition": p,
            "files": 3,
            "table": target_table,
            "inventory_id": f"{batch_id}:{p}",
            "inventory_version": batch_id,
            "version": batch_id,
            "status": "已确认",
            "confirmed": True,
            "valid": True,
        } for p in created]
        produce_result = self.produce_artifact(stats, batch_id)

        # 盘点结果回写到已存在的待压缩任务；这里不批量创建低文件数任务。
        writeback: dict[str, Any] = {"ok": False}
        try:
            from app.services import service_for

            compaction = service_for("compaction")
            if compaction is not None:
                writeback = compaction.writeback_inventories(
                    table=target_table, create_missing=False
                )
        except Exception as exc:  # noqa: BLE001 - 回写失败不应让分区模拟结果丢失
            writeback = {"ok": False, "reason": str(exc)}

        return {
            "ok": True,
            "entry_id": entry_id,
            "table": target_table,
            "partitions_created": len(created),
            "partitions_cleaned": cleaned,
            "total_files": total_files,
            "requires_compaction": total_files > 50,
            "batch_id": batch_id,
            "produce_result": produce_result,
            "compaction_writeback": writeback,
        }

    def emit_partition_stats_for_compaction(
        self, table_name: str, target_partition: str | None = None
    ) -> dict[str, Any]:
        """向 compaction 暴露分区文件统计。

        传 ``target_partition`` 时严格返回该分区的最新有效盘点；不传时返回
        该表各有效分区的盘点列表。调用方不能再把所有分区/历史版本累加成
        单个压缩任务的输入。
        """
        from app.services.inventory import iter_inventories, resolve_inventory

        if target_partition is not None:
            resolution = resolve_inventory(table_name, str(target_partition))
            inv = resolution.get("inventory")
            if resolution.get("conflict"):
                replacement = resolution.get("replacement") or {}
                successor_inv = resolution.get("replacement_inventory") or {}
                return {
                    "ok": False,
                    "table": table_name,
                    "partition": target_partition,
                    "partition_conflict": True,
                    "reason": resolution["conflict"]["message"],
                    "replacement_partition": replacement.get("new_partition"),
                    "replacement_files": successor_inv.get("file_count"),
                    "downstream_consumer": "compaction",
                }
            if inv:
                files = int(inv.get("file_count", 0))
                return {
                    "ok": True,
                    "table": table_name,
                    "partition": target_partition,
                    "files": files,
                    "estimated_files": files,
                    "inventory_id": inv.get("inventory_id"),
                    "inventory_version": inv.get("version"),
                    "read_version": resolution.get("read_version"),
                    "needs_compaction": files >= 10,
                    "scoped": True,
                    "downstream_consumer": "compaction",
                }

        effective: dict[str, dict[str, Any]] = {}
        for inv in iter_inventories(table_name):
            if not inv.get("effective"):
                continue
            old = effective[inv["partition"]] if inv["partition"] in effective else None
            if old is None or inv.get("order", 0) >= old.get("order", 0):
                effective[inv["partition"]] = inv

        if effective:
            partitions = [
                {
                    "partition": inv["partition"],
                    "files": int(inv["file_count"]),
                    "inventory_id": inv.get("inventory_id"),
                    "inventory_version": inv.get("version"),
                    "needs_compaction": int(inv["file_count"]) >= 10,
                }
                for inv in effective.values()
            ]
            total_files = sum(int(item["files"]) for item in partitions)
            any_partition = any(item["needs_compaction"] for item in partitions)
            return {
                "ok": True,
                "table": table_name,
                "partitions": partitions,
                "partition_count": len(partitions),
                "total_files": total_files,
                # 新调用方应使用 partitions 中每个目标分区的读数；下面两个键分别
                # 保留分区级判定和旧版表级 >50 信号，避免把全表累计误用于单任务。
                "any_partition_needs_compaction": any_partition,
                "partition_needs_compaction": any_partition,
                "estimated_files": total_files,
                "needs_compaction": total_files > 50,
                "scoped": False,
                "downstream_consumer": "compaction",
            }

        rows = store.rows(self.MODULE)
        relevant = [r for r in rows if r.get("目标表") == table_name and r.get("status") == "已生效"]
        if relevant:
            pre_create = int(str(relevant[0].get("预创建数量", 10)))
            return {
                "ok": True,
                "table": table_name,
                "estimated_files": pre_create * 3,
                "needs_compaction": pre_create * 3 > 50,
                "downstream_consumer": "compaction",
            }
        return {"ok": False, "reason": f"表 {table_name} 无分区策略或有效盘点"}

    def register_inventory(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """在分区扩缩侧登记盘点，并委托 compaction 完成清单回写。"""
        from app.services import service_for

        compaction = service_for("compaction")
        assert compaction is not None
        return compaction.register_inventory(*args, **kwargs)  # type: ignore[no-any-return]

    register_partition_inventory = register_inventory
    register_files = register_inventory

    def replace_partition(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        from app.services import service_for

        compaction = service_for("compaction")
        assert compaction is not None
        return compaction.replace_partition(*args, **kwargs)  # type: ignore[no-any-return]

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["active_policies"] = sum(1 for r in store.rows(self.MODULE) if r.get("status") == "已生效")
        return base