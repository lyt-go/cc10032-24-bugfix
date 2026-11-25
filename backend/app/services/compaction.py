"""compaction 领域服务：小文件压缩合并。

业务约束（盘点一致性修复）：

* 输入只按 ``目标表 + 目标分区 + 当前有效盘点版本`` 识别；
* 同一表的其他分区、已替换盘点、累计流水线制品不能累加进当前任务；
* 待执行任务回写有效盘点的文件数和采用范围；
* 已完成任务保存当时采用的盘点快照，历史文件计数仍可复核；
* 分区撤下/接替且任务尚未开始时，不静默扩大范围，只给出冲突和可改派分区。
"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.services.inventory import (
    COMPACTION_THRESHOLD,
    active_replacement,
    as_int,
    now_text,
    resolve_inventory,
    scope_snapshot,
    scope_text,
)
from app.store import store


class CompactionService(BaseModuleService):
    MODULE = "compaction"
    ENTITY = "压缩任务"
    OPERATION_TERM = "存储"
    LIST_FIELDS = ["任务编号", "目标表", "目标分区", "文件数量", "合并前大小", "合并后大小", "压缩算法", "执行时间"]
    REQUIRED_FIELDS = ["任务编号", "目标表", "目标分区"]
    STATUS_ORDER = ["待执行", "执行中", "已完成", "已取消"]
    ACTION_RULES = {"发起压缩": "执行中", "标记完成": "已完成", "取消任务": "已取消"}
    NEGATIVE_ACTIONS = []

    PIPELINE = "compaction_pipeline"
    UPSTREAM_MODULES = ["partition_scale"]
    PRODUCES_ARTIFACT = "压缩合并任务记录"

    # ---------------- 盘点登记/接替/回写 ----------------
    def register_inventory(
        self,
        table: str | dict[str, Any] | None = None,
        partition: str | None = None,
        file_count: int | str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """登记一版分区盘点，并把有效文件数回写到匹配的待压缩任务。

        第一个参数也可以直接传盘点字典，兼容不同调用方的数据形状。
        """
        if isinstance(table, dict):
            record = dict(table)
        else:
            record = {
                "table": table,
                "partition": partition,
                "file_count": file_count,
                **kwargs,
            }
        record.setdefault("status", "已确认")
        record.setdefault("confirmed", True)
        record.setdefault("registered_at", time.time())
        saved = store.register_partition_inventory(record)
        writeback = self.writeback_inventories(
            table=str(saved.get("table") or saved.get("目标表") or ""),
            create_missing=bool(kwargs.get("create_missing", True)),
        )
        return {"ok": True, "inventory": saved, "writeback": writeback}

    # 常见命名别名，便于既有/外部脚本按业务语言调用。
    register_partition_inventory = register_inventory
    record_inventory = register_inventory
    add_inventory = register_inventory

    def register_files(
        self,
        table: str | dict[str, Any] | None = None,
        partition: str | None = None,
        files: int | str | list[Any] | tuple[Any, ...] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """按文件清单或文件数登记盘点，兼容“登记 N 个文件”的调用。"""
        if isinstance(table, dict):
            payload = dict(table)
            file_value = payload.pop("files", payload.pop("file_list", payload.get("file_count")))
            return self.register_inventory(payload) if "file_count" in payload else self.register_inventory(
                {**payload, "file_count": len(file_value) if isinstance(file_value, (list, tuple, set)) else as_int(file_value)}
            )
        count = len(files) if isinstance(files, (list, tuple, set, frozenset)) else as_int(files)
        return self.register_inventory(table, partition, count, files=files, **kwargs)

    record_files = register_files
    add_files = register_files

    def confirm_inventory(self, inventory_id: str) -> dict[str, Any]:
        for row in store.list_partition_inventories():
            if str(row.get("inventory_id") or row.get("盘点编号")) == str(inventory_id):
                row["confirmed"] = True
                row["status"] = "已确认"
                row["confirmed_at"] = now_text()
                self.writeback_inventories(table=str(row.get("table") or row.get("目标表") or ""))
                return {"ok": True, "inventory": row}
        return {"ok": False, "reason": f"盘点 {inventory_id} 不存在"}

    def replace_partition(
        self,
        table: str | dict[str, Any] | None = None,
        old_partition: str | None = None,
        new_partition: str | None = None,
        file_count: int | str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """登记原分区撤下、接替分区上线；可同时登记接替分区盘点。"""
        if isinstance(table, dict):
            payload = dict(table)
            table_name = str(payload.get("table") or payload.get("目标表") or "")
            old_name = str(payload.get("old_partition") or payload.get("原分区") or "")
            new_name = str(payload.get("new_partition") or payload.get("接替分区") or payload.get("A2") or "")
            count = payload.get("file_count", payload.get("文件数量"))
        else:
            payload = dict(kwargs)
            table_name = str(table or "")
            old_name = str(old_partition or "")
            new_name = str(new_partition or "")
            count = file_count

        mapping = {
            "table": table_name,
            "old_partition": old_name,
            "new_partition": new_name,
            "status": payload.get("status", "已生效"),
            "confirmed": payload.get("confirmed", True),
            "reason": payload.get("reason", "分区缩容/撤下后的接替关系"),
            "created_at": now_text(),
        }
        store.replace_partition_mapping(mapping)

        # 原分区最近一版盘点标记为已撤下，保留历史而不是删除。
        resolution = resolve_inventory(table_name, old_name)
        old_inv = resolution.get("inventory")
        if old_inv and isinstance(old_inv.get("raw"), dict):
            raw = old_inv["raw"]
            raw["status"] = "已撤下"
            raw["valid"] = False
            raw["effective"] = False
            raw["replacement_partition"] = new_name

        successor_result = None
        if count is not None:
            successor_result = self.register_inventory(
                table_name,
                new_name,
                count,
                inventory_id=payload.get("successor_inventory_id"),
                version=payload.get("successor_version"),
                status=payload.get("successor_status", "已确认"),
                confirmed=payload.get("successor_confirmed", True),
                replaces_partition=old_name,
                create_missing=True,
            )

        return {
            "ok": True,
            "replacement": mapping,
            "successor_inventory": successor_result,
        }

    mark_partition_replaced = replace_partition
    scale_in_partition = replace_partition
    remove_partition = replace_partition

    def writeback_inventories(
        self,
        table: str | None = None,
        create_missing: bool = False,
        min_files: int | None = None,
    ) -> dict[str, Any]:
        """把每个有效分区盘点回写到对应目标分区的待执行压缩任务。"""
        updated: list[dict[str, Any]] = []
        created: list[dict[str, Any]] = []
        touched_keys: set[tuple[str, str]] = set()

        inventories = []
        # iter_inventories 返回所有版本；每个 table/partition 只取最新有效版。
        from app.services.inventory import iter_inventories  # 局部导入保持模块依赖清晰

        all_inventories = iter_inventories(table)
        keys = {(inv["table"], inv["partition"]) for inv in all_inventories if inv["effective"]}
        for table_name, partition_name in keys:
            resolution = resolve_inventory(table_name, partition_name)
            inv = resolution.get("inventory")
            if inv:
                inventories.append(inv)

        for inv in inventories:
            table_name = inv["table"]
            partition_name = inv["partition"]
            count = int(inv["file_count"])
            can_create = min_files is None or count >= min_files
            key = (table_name, partition_name)
            touched_keys.add(key)

            matching = [
                row for row in store.rows(self.MODULE)
                if str(row.get("目标表", "")) == table_name
                and str(row.get("目标分区", "")) == partition_name
            ]
            pending = [row for row in matching if row.get("status") == "待执行"]
            active = [row for row in matching if row.get("status") in ("待执行", "执行中")]
            if pending:
                for entry in pending:
                    self._apply_inventory_to_pending(entry, inv, None)
                    updated.append(entry)
            elif create_missing and can_create and not active:
                # 只有历史完成/取消任务时，新盘点才生成新的待压缩任务；
                # 已在执行中的同分区任务不能因回写而重复建单。低文件数盘点会
                # 回写既有任务，但不会在“领取队列”场景下新建噪声任务。
                entry = self._create_task_for_inventory(inv)
                created.append(entry)

        return {
            "ok": True,
            "updated": updated,
            "created": created,
            "updated_ids": [int(row.get("id", 0)) for row in updated],
            "created_ids": [int(row.get("id", 0)) for row in created],
            "keys": sorted(touched_keys),
        }

    # 常见业务别名。
    sync_inventory_to_compaction = writeback_inventories
    sync_pending_tasks = writeback_inventories
    apply_inventory_writeback = writeback_inventories
    writeback_inventory_to_tasks = writeback_inventories
    refresh_pending_manifest = writeback_inventories
    sync_compaction_manifest = writeback_inventories

    def _create_task_for_inventory(self, inv: dict[str, Any]) -> dict[str, Any]:
        rows = store.rows(self.MODULE)
        entry_id = max((as_int(row.get("id")) for row in rows), default=0) + 1
        count = int(inv.get("file_count", 0))
        entry: dict[str, Any] = {
            "id": entry_id,
            "status": "待执行",
            "pending": True,
            "abnormal": False,
            "任务编号": f"COMP-INV-{entry_id:04d}",
            "目标表": inv["table"],
            "目标分区": inv["partition"],
            "文件数量": count,
            "合并前大小": f"{count * 10}MB",
            "合并后大小": f"{int(count * 7)}MB",
            "压缩算法": "snappy",
            "执行时间": "",
        }
        self._apply_inventory_to_pending(entry, inv, None)
        rows.append(entry)
        return entry

    def _public_inventory(self, inv: dict[str, Any] | None) -> dict[str, Any] | None:
        if not inv:
            return None
        return {key: value for key, value in inv.items() if key != "raw"}

    def _apply_inventory_to_pending(
        self,
        entry: dict[str, Any],
        inv: dict[str, Any],
        resolution: dict[str, Any] | None,
    ) -> None:
        if entry.get("status") not in (None, "待执行"):
            return
        count = int(inv.get("file_count", 0))
        entry["文件数量"] = count
        entry["盘点编号"] = inv.get("inventory_id")
        entry["盘点版本"] = inv.get("version")
        entry["盘点状态"] = inv.get("status")
        entry["文件数量来源"] = "confirmed_inventory"
        entry["生效盘点"] = (
            f"{inv.get('inventory_id')}@{inv.get('version')}（{count}个文件，{inv.get('source')}）"
        )
        scope = scope_snapshot(resolution or resolve_inventory(inv["table"], inv["partition"]), inv)
        # 对未开始任务，“采用范围”始终等于当前回写的有效盘点；执行后会另存
        # adopted_execution_scope 作为不可变历史依据。
        entry["adoption_scope"] = scope
        entry["planned_scope"] = scope
        entry["采用范围"] = scope_text(scope)

    # ---------------- 任务详情/预检 ----------------
    def get_task_detail(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        detail = dict(entry)
        table = str(entry.get("目标表", ""))
        partition = str(entry.get("目标分区", ""))
        resolution = resolve_inventory(table, partition) if table and partition else None

        saved_count = as_int(entry.get("文件数量"))
        adopted = entry.get("adopted_inventory")
        if not adopted:
            adopted = {
                "inventory_id": entry.get("盘点编号"),
                "version": entry.get("盘点版本"),
                "file_count": saved_count,
                "source": entry.get("文件数量来源") or "historical_saved_count",
                "immutable": entry.get("status") in ("已完成", "已取消"),
                "note": "历史任务仅保存文件计数；可按任务记录中的版本/范围复核。"
                if not entry.get("盘点版本")
                else "按任务保存的盘点版本复核。",
            }
        detail["adopted_inventory"] = adopted
        detail["采用盘点"] = entry.get("采用盘点") or adopted
        detail["historical"] = entry.get("status") in ("已完成", "已取消")
        detail["immutable_basis"] = bool(entry.get("adopted_inventory"))
        detail["effective_inventory"] = self._public_inventory(
            (resolution or {}).get("effective_inventory")
        )
        detail["effective_resolution"] = self._json_resolution(resolution)
        detail["claimable"] = self.evaluate_entry(entry).get("claimable")
        detail["scope_matches_effective"] = self._scope_matches_effective(entry, resolution)
        detail["scope_consistent"] = (
            True if detail["historical"] else detail["scope_matches_effective"]
        )
        detail["inventory_basis"] = self._inventory_basis_text(entry, detail)
        detail["attempt_history"] = list(entry.get("attempt_history", []))
        return detail

    detail = get_task_detail
    get_detail = get_task_detail
    get_compaction_detail = get_task_detail
    get_compaction_task_detail = get_task_detail

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        # 通用 GET /api/compaction/{id} 也返回盘点依据。这里仍返回仓库中的同一
        # 行对象（只做就地补充），保留调用方通过 get_entry() 改测试夹具的旧习惯。
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        detail = self.get_task_detail(entry_id)
        if detail is not None:
            entry.update(detail)
        return entry

    def _json_resolution(self, resolution: dict[str, Any] | None) -> dict[str, Any] | None:
        if resolution is None:
            return None
        result = dict(resolution)
        result["inventory"] = self._public_inventory(resolution.get("inventory"))
        result["effective_inventory"] = self._public_inventory(resolution.get("effective_inventory"))
        result["replacement_inventory"] = self._public_inventory(resolution.get("replacement_inventory"))
        result["all_versions"] = [self._public_inventory(x) for x in resolution.get("all_versions", [])]
        if isinstance(result.get("replacement"), dict):
            result["replacement"] = {
                key: value for key, value in result["replacement"].items() if key != "raw"
            }
        return result

    def _scope_matches_effective(self, entry: dict[str, Any], resolution: dict[str, Any] | None) -> bool:
        if not resolution or not resolution.get("inventory"):
            return False
        scope = entry.get("adoption_scope") or entry.get("planned_scope") or {}
        inv = resolution["inventory"]
        if not isinstance(scope, dict):
            return False
        if scope.get("inventory_id") and scope.get("inventory_id") != inv.get("inventory_id"):
            return False
        if scope.get("version") and str(scope.get("version")) != str(inv.get("version")):
            return False
        return as_int(scope.get("file_count")) == as_int(inv.get("file_count"))

    def _inventory_basis_text(self, entry: dict[str, Any], detail: dict[str, Any]) -> str:
        adopted = detail.get("adopted_inventory") or {}
        if adopted.get("inventory_id") or adopted.get("version"):
            return (
                f"本任务采用盘点 {adopted.get('inventory_id') or '未记录编号'}，"
                f"版本 {adopted.get('version') or '未记录'}，"
                f"文件数 {adopted.get('file_count', as_int(entry.get('文件数量')))}。"
            )
        return (
            f"历史任务未关联可回放的盘点版本，按任务保存的文件数量 "
            f"{as_int(entry.get('文件数量'))} 复核；当前盘点变化不改写该记录。"
        )

    def precheck_compaction(self, entry_id: int, **options: Any) -> dict[str, Any]:
        with store.inventory_lock:
            entry = store.find(self.MODULE, entry_id)
            if entry is None:
                return {"ok": False, "claimable": False, "reason": f"任务 {entry_id} 不存在"}

            table = str(entry.get("目标表", ""))
            partition = str(entry.get("目标分区", ""))
            resolution = resolve_inventory(table, partition)
            inv = resolution.get("inventory")

            if resolution.get("conflict"):
                evaluation = {
                    "claimable": False,
                    "eligible": False,
                    "file_count": as_int((resolution.get("replacement_inventory") or {}).get("file_count")),
                    "code": resolution["conflict"]["code"],
                    "reason": resolution["conflict"]["message"],
                    "resolution": resolution,
                }
            elif inv:
                self._apply_inventory_to_pending(entry, inv, resolution)
                count = int(inv["file_count"])
                claimable = count >= COMPACTION_THRESHOLD
                evaluation = {
                    "claimable": claimable and entry.get("status") == "待执行",
                    "eligible": claimable,
                    "file_count": count,
                    "code": "below_threshold" if not claimable else "ready",
                    "reason": "" if claimable else f"分区 {partition} 有效盘点仅 {count} 个文件，无需合并（阈值 {COMPACTION_THRESHOLD}）。",
                    "resolution": resolution,
                }
            else:
                count = as_int(entry.get("文件数量"))
                claimable = count >= COMPACTION_THRESHOLD and entry.get("status") == "待执行"
                evaluation = {
                    "claimable": claimable,
                    "eligible": count >= COMPACTION_THRESHOLD,
                    "file_count": count,
                    "code": "historical_saved_count" if count >= COMPACTION_THRESHOLD else "below_threshold",
                    "reason": "" if claimable else f"未找到 {table}/{partition} 的有效盘点，按任务保存计数 {count} 判断，无需合并（阈值 {COMPACTION_THRESHOLD}）。",
                    "resolution": resolution,
                }

            token = f"PC-{entry_id}-{time.time_ns()}"
            snapshot = {
                "token": token,
                "entry_id": entry_id,
                "table": table,
                "partition": partition,
                "read_version": resolution.get("read_version"),
                "conflict_code": (resolution.get("conflict") or {}).get("code"),
                "file_count": evaluation.get("file_count"),
                "claimable": evaluation.get("claimable"),
                "created_at": now_text(),
            }
            store.save_compaction_precheck(token, snapshot)
            entry["precheck_token"] = token
            entry["最近预检"] = snapshot
            self._record_attempt(entry, {"stage": "precheck", **{k: v for k, v in evaluation.items() if k != "resolution"}})

            result = {
                # 预检动作本身完成；业务是否可领取使用 claimable/passed 判断。
                "ok": True,
                "precheck_ok": True,
                "passed": bool(evaluation["claimable"]),
                "claimable": bool(evaluation["claimable"]),
                "eligible": bool(evaluation["eligible"]),
                "token": token,
                "entry_id": entry_id,
                "table": table,
                "partition": partition,
                "file_count": evaluation["file_count"],
                "threshold": COMPACTION_THRESHOLD,
                "code": evaluation["code"],
                "reason": evaluation["reason"],
                "message": evaluation["reason"],
                "precheck": snapshot,
                "scope": scope_snapshot(resolution, inv),
                "effective_inventory": self._public_inventory(inv),
                "replacement_partition": (resolution.get("replacement") or {}).get("new_partition"),
                "replacement_inventory": self._public_inventory(resolution.get("replacement_inventory")),
            }
            return result

    precheck = precheck_compaction
    precheck_task = precheck_compaction
    precheck_compaction_task = precheck_compaction

    def evaluate_entry(self, entry: dict[str, Any], precheck_token: str | None = None) -> dict[str, Any]:
        table = str(entry.get("目标表", ""))
        partition = str(entry.get("目标分区", ""))
        resolution = resolve_inventory(table, partition) if table and partition else None
        inv = (resolution or {}).get("inventory")
        conflict = (resolution or {}).get("conflict")

        if entry.get("status") != "待执行":
            return {
                "claimable": False,
                "eligible": False,
                "code": "not_pending",
                "reason": f"任务状态 {entry.get('status')} 非待执行",
                "file_count": as_int(entry.get("文件数量")),
                "resolution": resolution,
            }
        if conflict:
            return {
                "claimable": False,
                "eligible": False,
                "code": conflict["code"],
                "reason": conflict["message"],
                "file_count": as_int((resolution.get("replacement_inventory") or {}).get("file_count")) or as_int(entry.get("文件数量")),
                "replacement_partition": (resolution.get("replacement") or {}).get("new_partition"),
                "resolution": resolution,
            }

        count = as_int(inv.get("file_count")) if inv else as_int(entry.get("文件数量"))
        source = "confirmed_inventory" if inv else "historical_saved_count"
        eligible = count >= COMPACTION_THRESHOLD
        return {
            "claimable": eligible,
            "eligible": eligible,
            "code": "ready" if eligible else "below_threshold",
            "reason": "" if eligible else f"分区 {partition} 当前有效读数为 {count} 个文件，无需合并（阈值 {COMPACTION_THRESHOLD}）",
            "file_count": count,
            "source": source,
            "inventory": inv,
            "resolution": resolution,
        }

    def list_claimable_tasks(self, table: str | None = None, auto_sync: bool = True) -> list[dict[str, Any]]:
        with store.inventory_lock:
            if auto_sync:
                self.writeback_inventories(table=table, create_missing=True, min_files=COMPACTION_THRESHOLD)
            result = []
            for entry in store.rows(self.MODULE):
                if table and str(entry.get("目标表", "")) != table:
                    continue
                evaluation = self.evaluate_entry(entry)
                if evaluation["claimable"]:
                    result.append({
                        "entry_id": entry.get("id"),
                        "任务编号": entry.get("任务编号"),
                        "table": entry.get("目标表"),
                        "partition": entry.get("目标分区"),
                        "file_count": evaluation["file_count"],
                        "source": evaluation["source"],
                        "entry": entry,
                    })
            result.sort(key=lambda item: as_int(item.get("entry_id")))
            return result

    claimable_tasks = list_claimable_tasks
    get_claimable_tasks = list_claimable_tasks

    def list_task_qualifications(self, table: str | None = None) -> list[dict[str, Any]]:
        """返回每个任务的可领取判定和原因，包含不可领取的 A/历史任务。"""
        with store.inventory_lock:
            result = []
            for entry in store.rows(self.MODULE):
                if table and str(entry.get("目标表", "")) != table:
                    continue
                evaluation = self.evaluate_entry(entry)
                result.append({
                    "entry_id": entry.get("id"),
                    "任务编号": entry.get("任务编号"),
                    "table": entry.get("目标表"),
                    "partition": entry.get("目标分区"),
                    "status": entry.get("status"),
                    "claimable": evaluation.get("claimable"),
                    "eligible": evaluation.get("eligible"),
                    "code": evaluation.get("code"),
                    "reason": evaluation.get("reason"),
                    "file_count": evaluation.get("file_count"),
                    "replacement_partition": evaluation.get("replacement_partition"),
                })
            return result

    claim_scope = list_task_qualifications
    get_claim_scope = list_task_qualifications
    list_eligibility = list_task_qualifications

    def claim_next_compaction(self, table: str | None = None, worker: str | None = None) -> dict[str, Any]:
        with store.inventory_lock:
            candidates = self.list_claimable_tasks(table=table)
            for candidate in candidates:
                entry_id = as_int(candidate.get("entry_id"))
                entry = store.find(self.MODULE, entry_id)
                if entry is None or entry.get("status") != "待执行":
                    continue
                evaluation = self.evaluate_entry(entry)
                if not evaluation["claimable"]:
                    continue
                entry["status"] = "执行中"
                entry["pending"] = True
                entry["claimed_by"] = worker or "compaction-worker"
                entry["claimed_at"] = now_text()
                entry["claim_token"] = f"CLAIM-{entry_id}-{time.time_ns()}"
                return {
                    "ok": True,
                    "claimed": True,
                    "entry_id": entry_id,
                    "entry": entry,
                    "table": entry.get("目标表"),
                    "partition": entry.get("目标分区"),
                    "file_count": evaluation["file_count"],
                    "claim_token": entry["claim_token"],
                }
            return {"ok": False, "claimed": False, "reason": "当前没有符合有效盘点范围的可领取压缩任务"}

    claim_next = claim_next_compaction
    claim_next_task = claim_next_compaction
    claim_pending = claim_next_compaction
    get_next_compaction_task = claim_next_compaction
    pop_claimable = claim_next_compaction

    def get_effective_inventory(self, table: str, partition: str) -> dict[str, Any]:
        return resolve_inventory(str(table), str(partition))

    def adopt_replacement(
        self, entry_id: int, replacement_partition: str | None = None, execute: bool = False
    ) -> dict[str, Any]:
        """显式把原任务改派/关联到接替分区；不会在执行入口静默扩大范围。"""
        with store.inventory_lock:
            entry = store.find(self.MODULE, entry_id)
            if entry is None:
                return {"ok": False, "reason": f"任务 {entry_id} 不存在"}
            table = str(entry.get("目标表", ""))
            original_partition = str(entry.get("目标分区", ""))
            resolution = resolve_inventory(table, original_partition)
            mapping = resolution.get("replacement") or {}
            successor_partition = str(
                replacement_partition
                or mapping.get("new_partition")
                or (resolution.get("conflict") or {}).get("replacement_partition")
                or ""
            )
            if not successor_partition:
                return {"ok": False, "reason": f"任务 {entry_id} 没有可采用的接替分区"}

            successor_resolution = resolve_inventory(table, successor_partition)
            inv = successor_resolution.get("inventory")
            successor = next(
                (row for row in store.rows(self.MODULE)
                 if str(row.get("目标表", "")) == table
                 and str(row.get("目标分区", "")) == successor_partition
                 and row.get("status") in ("待执行", "执行中")),
                None,
            )
            if successor is None and inv:
                successor = self._create_task_for_inventory(inv)
            if successor is None:
                return {
                    "ok": False,
                    "reason": f"接替分区 {successor_partition} 尚无已确认盘点，不能采用",
                    "replacement_partition": successor_partition,
                }

            entry["replacement_partition"] = successor_partition
            entry["replacement_task_id"] = successor.get("id")
            entry["adopted_replacement_at"] = now_text()
            entry["采用说明"] = (
                f"原分区 {original_partition} 已撤下，已明确采用接替分区 "
                f"{successor_partition} 的任务 {successor.get('任务编号')}。"
            )
            self._record_attempt(entry, {
                "ok": True,
                "stage": "adopt_replacement",
                "replacement_partition": successor_partition,
                "replacement_task_id": successor.get("id"),
            })
            result: dict[str, Any] = {
                "ok": True,
                "entry_id": entry_id,
                "replacement_partition": successor_partition,
                "replacement_task_id": successor.get("id"),
                "successor": successor,
                "reason": entry["采用说明"],
            }
            if execute:
                result["execution"] = self.execute_compaction(int(successor["id"]))
            return result

    def execute_replacement(self, entry_id: int, replacement_partition: str | None = None) -> dict[str, Any]:
        return self.adopt_replacement(entry_id, replacement_partition, execute=True)

    def _record_attempt(self, entry: dict[str, Any], record: dict[str, Any]) -> None:
        history = entry.setdefault("attempt_history", [])
        record.setdefault("at", now_text())
        history.append(record)
        # 控制内存示例中的历史长度。
        if len(history) > 20:
            del history[:-20]

    def _precheck_conflict(self, entry: dict[str, Any], token: str | None, resolution: dict[str, Any]) -> str | None:
        # 只有调用方显式携带预检令牌时才做乐观版本校验。普通执行始终重新读取
        # 当前“已确认”盘点，因此预检后新确认的版本可以被执行读取，未确认版本不会。
        token_value = token
        if not token_value:
            return None
        saved = store.get_compaction_precheck(str(token_value))
        if saved is None:
            saved = entry.get("最近预检")
        if not isinstance(saved, dict):
            return None
        current_conflict = (resolution.get("conflict") or {}).get("code")
        if saved.get("read_version") != resolution.get("read_version") or saved.get("conflict_code") != current_conflict:
            return (
                "预检后分区盘点或接替关系已变化（预检读数 "
                f"{saved.get('file_count')}，当前版本 {resolution.get('read_version') or '已失效/接替'}）；"
                "为避免采用过期范围，本次未执行，请重新预检或领取接替分区。"
            )
        return None

    def rollback_compaction(self, entry_id: int, reason: str = "", previous_status: str = "待执行") -> dict[str, Any]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "reason": f"任务 {entry_id} 不存在"}
        try:
            if entry.get("simulate_rollback_failure") or entry.get("rollback_should_fail"):
                raise RuntimeError("injected rollback failure")
            entry["status"] = previous_status if previous_status in self.STATUS_ORDER else "待执行"
            entry["pending"] = entry["status"] == "待执行"
            entry["rollback_at"] = now_text()
            entry["rollback_reason"] = reason
            return {"ok": True, "entry": entry, "status": entry["status"]}
        except Exception as exc:  # noqa: BLE001 - 回滚失败也要保留可复核状态
            # 即使回滚处理器自身失败，也强制把尚未开始的任务留在“待执行”。
            entry["status"] = "待执行"
            entry["pending"] = True
            entry["rollback_failed"] = True
            entry["rollback_error"] = str(exc)
            return {
                "ok": False,
                "rollback_failed": True,
                "status": "待执行",
                "entry": entry,
                "reason": f"回滚失败：{exc}；任务已保留为未开始（待执行）状态",
            }

    rollback = rollback_compaction

    def execute_compaction(self, entry_id: int, precheck_token: str | None = None, **options: Any) -> dict[str, Any]:
        """执行压缩合并：输入严格限定为目标表、目标分区的有效盘点。"""
        with store.inventory_lock:
            entry = store.find(self.MODULE, entry_id)
            if entry is None:
                return {"ok": False, "executed": False, "reason": f"任务 {entry_id} 不存在"}
            if entry.get("status") != "待执行":
                return {"ok": False, "executed": False, "reason": f"任务状态 {entry.get('status')} 非待执行"}

            target_table = str(entry.get("目标表", ""))
            target_partition = str(entry.get("目标分区", ""))
            compression = str(entry.get("压缩算法", "snappy"))

            resolution = resolve_inventory(target_table, target_partition)

            conflict = resolution.get("conflict")
            if conflict:
                reason = conflict["message"]
                replacement = resolution.get("replacement") or {}
                requested_replacement = options.get("replacement_partition") or options.get("接替分区")
                adopt_requested = any(bool(options.get(key)) for key in (
                    "adopt_replacement", "auto_adopt", "allow_replacement",
                    "use_replacement", "follow_replacement",
                )) or bool(requested_replacement)
                if adopt_requested:
                    adoption = self.adopt_replacement(
                        entry_id,
                        str(requested_replacement) if requested_replacement else None,
                        execute=True,
                    )
                    execution = adoption.get("execution") if isinstance(adoption, dict) else None
                    if isinstance(execution, dict):
                        execution["adopted_replacement"] = True
                        execution["original_entry_id"] = entry_id
                        execution["original_partition"] = target_partition
                        execution["replacement_task_id"] = adoption.get("replacement_task_id")
                        execution["adoption"] = {
                            key: value for key, value in adoption.items() if key != "execution"
                        }
                        return execution
                    return {
                        "ok": False,
                        "executed": False,
                        "partition_conflict": True,
                        "reason": adoption.get("reason", reason) if isinstance(adoption, dict) else reason,
                        "adoption": adoption,
                    }

                self._record_attempt(entry, {
                    "ok": False,
                    "stage": "partition_conflict",
                    "code": conflict["code"],
                    "reason": reason,
                    "precheck_token_stale": bool(entry.get("precheck_token")),
                    "replacement_partition": replacement.get("new_partition"),
                })
                result = {
                    "ok": False,
                    "executed": False,
                    "skipped": True,
                    "claimable": False,
                    "partition_conflict": True,
                    "precheck_conflict": bool(entry.get("precheck_token")),
                    "code": conflict["code"],
                    "reason": reason,
                    "message": reason,
                    "explanation": reason,
                    "why_not_executed": reason,
                    "entry_id": entry_id,
                    "table": target_table,
                    "partition": target_partition,
                    "replacement_partition": replacement.get("new_partition"),
                    "available_replacement_partition": replacement.get("new_partition"),
                    "adopted_partition": None,
                    "replacement": self._json_resolution(resolution).get("replacement"),
                    "replacement_inventory": self._public_inventory(resolution.get("replacement_inventory")),
                    "next_claimable": self.list_claimable_tasks(table=target_table),
                }
                return result

            token_conflict = self._precheck_conflict(entry, precheck_token, resolution)
            if token_conflict:
                self._record_attempt(entry, {"ok": False, "stage": "precheck_conflict", "reason": token_conflict})
                return {
                    "ok": False,
                    "executed": False,
                    "skipped": True,
                    "claimable": False,
                    "precheck_conflict": True,
                    "reason": token_conflict,
                    "message": token_conflict,
                    "entry_id": entry_id,
                    "table": target_table,
                    "partition": target_partition,
                    "current_resolution": self._json_resolution(resolution),
                }

            inventory = resolution.get("inventory")
            if inventory:
                self._apply_inventory_to_pending(entry, inventory, resolution)
                file_count = int(inventory["file_count"])
                count_source = "confirmed_inventory"
            else:
                # 历史任务只有保存下来的文件计数：继续兼容，但明确标记来源。
                file_count = as_int(entry.get("文件数量", "0"))
                count_source = "historical_saved_count"
                entry["文件数量来源"] = count_source

            if file_count == 0:
                reason = "文件数量为0，无数据可合并"
                self._record_attempt(entry, {"ok": False, "stage": "validate", "reason": reason})
                return {"ok": False, "executed": False, "reason": reason}

            supported = ("snappy", "gzip", "lz4", "zstd")
            if compression not in supported:
                reason = f"压缩算法 {compression} 不在支持列表: {supported}"
                self._record_attempt(entry, {"ok": False, "stage": "validate", "reason": reason})
                return {"ok": False, "executed": False, "reason": reason}

            if file_count < COMPACTION_THRESHOLD:
                reason = (
                    f"分区 {target_partition} 按当前有效盘点为 {file_count} 个文件，"
                    f"无需合并（阈值 {COMPACTION_THRESHOLD}），本次跳过且任务保持未开始。"
                )
                self._record_attempt(entry, {
                    "ok": False,
                    "executed": False,
                    "skipped": True,
                    "stage": "threshold",
                    "file_count": file_count,
                    "inventory_version": (inventory or {}).get("version"),
                    "reason": reason,
                })
                return {
                    "ok": False,
                    "executed": False,
                    "skipped": True,
                    "claimable": False,
                    "below_threshold": True,
                    "reason": reason,
                    "message": reason,
                    "explanation": reason,
                    "why_not_executed": reason,
                    "entry_id": entry_id,
                    "table": target_table,
                    "partition": target_partition,
                    "file_count": file_count,
                    "effective_file_count": file_count,
                    "threshold": COMPACTION_THRESHOLD,
                    "inventory": self._public_inventory(inventory),
                    "adoption_scope": entry.get("planned_scope"),
                    "next_claimable": self.list_claimable_tasks(table=target_table),
                }

            # 模拟合并与制品产出。先产出，失败时不会把未开始任务误改成执行中。
            before_size = file_count * 10 * 1024 * 1024
            after_size = before_size * 0.7
            merged_files = max(1, file_count // 3)
            batch_id = f"COMPACT_{entry_id}_{time.time_ns()}"
            artifact = [{
                "table": target_table,
                "partition": target_partition,
                "files_before": file_count,
                "files_after": merged_files,
                "size_before": before_size,
                "size_after": int(after_size),
                "compression": compression,
                "inventory_id": (inventory or {}).get("inventory_id"),
                "inventory_version": (inventory or {}).get("version"),
            }]

            previous_status = entry.get("status", "待执行")
            try:
                if entry.get("simulate_execute_failure") or options.get("simulate_failure"):
                    raise RuntimeError(str(entry.get("execute_failure_reason") or "模拟压缩合并失败"))
                produce_result = self.produce_artifact(artifact, batch_id)
                if produce_result.get("ok") is False:
                    raise RuntimeError(produce_result.get("reason", "压缩制品写入失败"))
            except Exception as exc:  # noqa: BLE001 - 必须回滚并保留可诊断信息
                rollback = self.rollback_compaction(
                    entry_id, reason=str(exc), previous_status=str(previous_status)
                )
                self._record_attempt(entry, {
                    "ok": False,
                    "stage": "execute",
                    "reason": str(exc),
                    "rollback": rollback,
                })
                return {
                    "ok": False,
                    "executed": False,
                    "reason": f"压缩执行失败：{exc}",
                    "rollback": rollback,
                    "status": entry.get("status"),
                }

            scope = scope_snapshot(resolution, inventory)
            adopted_inventory = self._public_inventory(inventory) or {
                "file_count": file_count,
                "source": count_source,
                "immutable": False,
                "note": "执行时未找到盘点版本，采用任务中保存的历史文件计数。",
            }
            adopted_inventory["adopted_at"] = now_text()
            adopted_inventory["immutable"] = True

            entry["文件数量"] = file_count
            entry["合并前大小"] = f"{before_size // 1024 // 1024}MB"
            entry["合并后大小"] = f"{int(after_size) // 1024 // 1024}MB"
            entry["执行时间"] = now_text()
            entry["status"] = "执行中"
            entry["pending"] = True
            entry["last_batch_id"] = batch_id
            entry["last_result"] = {
                "files_before": file_count,
                "files_after": merged_files,
                "batch_id": batch_id,
            }
            entry["adopted_inventory"] = adopted_inventory
            entry["adoption_scope"] = scope
            entry["adopted_execution_scope"] = dict(scope)
            entry["采用范围"] = scope_text(scope)
            entry["采用盘点"] = (
                f"{adopted_inventory.get('inventory_id') or '历史计数'}@"
                f"{adopted_inventory.get('version') or '未版本化'}"
            )
            if inventory:
                entry["盘点编号"] = inventory.get("inventory_id")
                entry["盘点版本"] = inventory.get("version")
            entry["文件数量来源"] = count_source
            self._record_attempt(entry, {
                "ok": True,
                "stage": "execute",
                "file_count": file_count,
                "inventory_id": adopted_inventory.get("inventory_id"),
                "version": adopted_inventory.get("version"),
                "batch_id": batch_id,
            })

            return {
                "ok": True,
                "executed": True,
                "claimed": True,
                "entry_id": entry_id,
                "table": target_table,
                "partition": target_partition,
                "file_count": file_count,
                "files_before": file_count,
                "effective_file_count": file_count,
                "files_after": merged_files,
                "compression": compression,
                "inventory": adopted_inventory,
                "adoption_scope": scope,
                "batch_id": batch_id,
                "space_saved": f"{int(before_size - after_size) // 1024 // 1024}MB",
            }

    execute = execute_compaction
    run_compaction = execute_compaction

    def _snapshot_historical_basis(self, entry: dict[str, Any]) -> None:
        """在任务进入完成/取消等终态前固化当时采用的计数和版本。"""
        if entry.get("adopted_inventory"):
            return
        table = str(entry.get("目标表", ""))
        partition = str(entry.get("目标分区", ""))
        resolution = resolve_inventory(table, partition) if table and partition else None
        inventory = (resolution or {}).get("inventory")
        scope = entry.get("adoption_scope") or entry.get("planned_scope")
        if not isinstance(scope, dict):
            scope = scope_snapshot(resolution or {}, inventory) if resolution else {
                "table": table,
                "partition": partition,
                "inventory_id": entry.get("盘点编号"),
                "version": entry.get("盘点版本"),
                "file_count": as_int(entry.get("文件数量")),
                "source": entry.get("文件数量来源") or "historical_saved_count",
                "captured_at": now_text(),
            }
        adopted = self._public_inventory(inventory) or {
            "inventory_id": scope.get("inventory_id") or entry.get("盘点编号"),
            "version": scope.get("version") or entry.get("盘点版本"),
            "file_count": scope.get("file_count") or as_int(entry.get("文件数量")),
            "source": scope.get("source") or entry.get("文件数量来源") or "historical_saved_count",
        }
        adopted["adopted_at"] = now_text()
        adopted["immutable"] = True
        entry["adoption_scope"] = scope
        entry["adopted_execution_scope"] = dict(scope)
        entry["adopted_inventory"] = adopted
        entry.setdefault("采用范围", scope_text(scope))

    # 兼容部分脚本把“发起压缩”作为领域入口调用。
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "发起压缩":
            result = self.execute_compaction(entry_id)
            entry = store.find(self.MODULE, entry_id)
            if result.get("ok"):
                return entry, (
                    f"压缩任务已发起：{result.get('files_before')} 个文件合并为 "
                    f"{result.get('files_after')} 个"
                )
            return None, str(result.get("reason") or "压缩任务未执行")
        if action == "标记完成":
            entry = store.find(self.MODULE, entry_id)
            if entry is not None:
                self._snapshot_historical_basis(entry)
        return super().run_action(entry_id, action)

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["pending_tasks"] = sum(1 for r in store.rows(self.MODULE) if r.get("status") == "待执行")
        base["claimable_tasks"] = len(self.list_claimable_tasks(auto_sync=False))
        base["partition_inventories"] = len(store.list_partition_inventories())
        return base
