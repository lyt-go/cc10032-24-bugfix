"""分区盘点解析与压缩清单回写的共享领域逻辑。

这里不把同一目标表下所有分区、所有历史盘点累加，而是：

1. 按 ``表 + 分区`` 精确匹配；
2. 只读取当前有效且已确认的盘点版本；
3. 已替换/已撤下的版本仅作为历史和冲突依据；
4. 未确认的新盘点不会覆盖已确认读数。
"""
from __future__ import annotations

import hashlib
import time
from typing import Any, Iterable

from app.store import store

COMPACTION_THRESHOLD = 10

_TABLE_KEYS = ("table", "table_name", "目标表", "表名", "源表名称")
_PARTITION_KEYS = (
    "partition", "partition_name", "partition_key", "partition_id",
    "target_partition", "target_partition_name",
    "目标分区", "分区", "分区名", "分区名称", "分区标识", "分区ID",
)
_COUNT_KEYS = (
    "effective_files", "file_count", "files_count", "total_files", "files", "count",
    "small_files", "small_file_count", "num_files", "file_num", "file_total", "total_file_count",
    "file_list", "file_paths",
    "有效文件数", "盘点文件数", "文件总数", "小文件数", "小文件数量", "文件数量", "文件数",
)
_ID_KEYS = (
    "inventory_id", "inventory_no", "manifest_id", "盘点编号", "盘点ID", "盘点id", "盘点单号",
)
_VERSION_KEYS = (
    "inventory_version", "version_no", "version_id", "version", "rev",
    "盘点版本号", "盘点版本", "版本号", "版本",
)
_TIME_KEYS = ("confirmed_at", "registered_at", "ts", "created_at", "登记时间", "确认时间")

_ACTIVE_STATUS = {
    "active", "current", "effective", "confirmed", "valid", "ready",
    "有效", "生效", "已生效", "已确认", "确认", "当前", "当前有效", "最新", "正常", "就绪",
}
_INACTIVE_STATUS = {
    "replaced", "superseded", "old", "previous", "removed", "deleted",
    "dropped", "scaled_in", "scale_in", "inactive", "invalid", "draft",
    "pending", "unconfirmed", "cancelled", "canceled", "0",
    "已替换", "被替换", "已替代", "替代", "旧盘点", "已撤下", "撤下", "已删除", "删除",
    "已失效", "失效", "已缩容", "缩容", "草稿", "待确认", "未确认", "已取消",
}

_REPLACEMENT_KEYS = (
    "replacement_partition", "successor_partition", "replace_partition",
    "new_partition", "to_partition", "replacement", "replace_with",
    "next_partition", "next", "接替分区", "替代分区", "代替分区", "新分区",
)
_OLD_PARTITION_KEYS = (
    "old_partition", "source_partition", "from_partition", "replaced_partition",
    "原分区", "旧分区", "被接替分区",
)


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def as_int(value: Any, default: int = 0) -> int:
    """宽容地把 '18'、18.0、'18个' 转成整数。"""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    digits = ""
    for ch in text:
        if ch.isdigit():
            digits += ch
        elif digits:
            break
    return int(digits) if digits else default


def _first(row: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in row and row.get(key) not in (None, ""):
            return row.get(key)
    return None


def _as_str(value: Any) -> str:
    return "" if value is None else str(value)


def _looks_like_inventory(row: dict[str, Any]) -> bool:
    if not isinstance(row, dict):
        return False
    has_table = _first(row, _TABLE_KEYS) is not None
    has_partition = _first(row, _PARTITION_KEYS) is not None
    has_count = _first(row, _COUNT_KEYS) is not None
    return has_table and has_partition and (has_count or _has_individual_file(row))


def _has_individual_file(row: dict[str, Any]) -> bool:
    return any(key in row for key in ("file_name", "filename", "path", "文件路径", "文件名"))


def normalize_inventory(row: dict[str, Any], source: str, order: int = 0) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None

    # 允许 partition_scale 行内嵌一版或多版盘点。
    nested: list[dict[str, Any]] = []
    for key in ("inventory", "盘点", "effective_inventory", "盘点结果"):
        value = row.get(key)
        if isinstance(value, dict):
            nested.append(value)
    for key in ("inventories", "盘点记录", "盘点版本列表", "history"):
        value = row.get(key)
        if isinstance(value, list):
            nested.extend(v for v in value if isinstance(v, dict))
    if nested:
        table = _first(row, _TABLE_KEYS)
        partition = _first(row, _PARTITION_KEYS)
        normalized_nested = []
        for index, item in enumerate(nested):
            merged = dict(item)
            if table is not None:
                merged.setdefault("table", table)
            if partition is not None:
                merged.setdefault("partition", partition)
            normalized = normalize_inventory(merged, f"{source}.nested", order + index)
            if normalized:
                normalized_nested.append(normalized)
        if normalized_nested:
            return max(normalized_nested, key=lambda x: _version_sort_key(x, order))

    if not _looks_like_inventory(row):
        return None

    table = _as_str(_first(row, _TABLE_KEYS)).strip()
    partition = _as_str(_first(row, _PARTITION_KEYS)).strip()
    if not table or not partition:
        return None

    raw_count = _first(row, _COUNT_KEYS)
    if isinstance(raw_count, (list, tuple, set, frozenset)):
        file_count = len(raw_count)
    elif raw_count is None and _has_individual_file(row):
        file_count = 1
    else:
        file_count = as_int(raw_count)

    status_value = _as_str(row.get("status") or row.get("盘点状态") or row.get("state")).strip()
    status_lower = status_value.casefold()
    explicit_valid = None
    for valid_key in (
        "valid", "is_valid", "有效", "是否有效", "effective", "is_effective",
        "current", "is_current", "当前有效", "latest", "is_latest",
        "active", "is_active", "生效",
    ):
        if valid_key in row:
            explicit_valid = row.get(valid_key)
            break
    explicit_confirmed = None
    for confirmed_key in ("confirmed", "is_confirmed", "已确认", "是否确认"):
        if confirmed_key in row:
            explicit_confirmed = row.get(confirmed_key)
            break

    inactive = status_lower in _INACTIVE_STATUS or status_value in _INACTIVE_STATUS
    active_status = status_lower in _ACTIVE_STATUS or status_value in _ACTIVE_STATUS
    if explicit_valid is False or explicit_valid == "false" or explicit_valid == 0:
        inactive = True
    if explicit_valid is True or explicit_valid == "true" or explicit_valid == 1:
        active_status = True
    if any(row.get(flag) is True or row.get(flag) == "true"
           for flag in ("deleted", "removed", "replaced", "superseded", "已撤下", "已替换")):
        inactive = True

    confirmed: bool | None
    if explicit_confirmed is None:
        if status_value in ("待确认", "未确认") or status_lower in ("pending", "unconfirmed", "draft"):
            confirmed = False
        elif status_value in _ACTIVE_STATUS or status_lower in _ACTIVE_STATUS:
            confirmed = True
        else:
            # 兼容旧流水线制品：没有确认标记时视为已确认的统计结果。
            confirmed = True
    else:
        confirmed = explicit_confirmed is True or explicit_confirmed == "true" or explicit_confirmed == 1

    is_valid = not inactive and explicit_valid is not False
    inventory_id = _as_str(_first(row, _ID_KEYS)) or f"{source}:{table}:{partition}:{order}"
    version_value = _first(row, _VERSION_KEYS)
    version_text = _as_str(version_value) or f"seq-{order}"

    replacement_partition = _as_str(_first(row, _REPLACEMENT_KEYS)).strip()
    replaces_partition = _as_str(_first(row, (
        "replaces_partition", "replaces", "replaced_partition",
        "old_partition", "source_partition", "from_partition",
        "接替原分区", "被接替分区",
    )) or row.get("接替原分区") or "").strip()

    result = {
        "inventory_id": inventory_id,
        "table": table,
        "partition": partition,
        "file_count": file_count,
        "version": version_text,
        "status": status_value or ("已确认" if confirmed else "待确认"),
        "confirmed": bool(confirmed),
        "valid": bool(is_valid),
        "effective": bool(is_valid and confirmed),
        "source": source,
        "order": order,
        "registered_at": _as_str(_first(row, _TIME_KEYS)) or now_text(),
        "replacement_partition": replacement_partition or None,
        "replaces_partition": replaces_partition or None,
        "raw": row,
    }
    return result


def _version_sort_key(inv: dict[str, Any], fallback_order: int = 0) -> tuple[int, int, float, int]:
    version = str(inv.get("version", ""))
    numeric = as_int(version, default=-1)
    ts = 0.0
    raw = inv.get("raw") if isinstance(inv.get("raw"), dict) else {}
    for key in _TIME_KEYS:
        value = raw.get(key)
        if isinstance(value, (int, float)):
            ts = float(value)
            break
    return numeric if numeric >= 0 else -1, as_int(inv.get("order"), fallback_order), ts, as_int(inv.get("order"), fallback_order)


def _store_list_attrs() -> list[tuple[str, Any]]:
    candidates = [
        "_partition_inventories", "partition_inventories",
        "_inventory_records", "inventory_records", "inventories", "_inventories",
        "_partition_assets", "partition_assets", "_inventory_manifest",
    ]
    found: list[tuple[str, Any]] = []
    for name in candidates:
        if hasattr(store, name):
            found.append((name, getattr(store, name)))
    for name, value in vars(store).items():
        if name in candidates or not isinstance(value, (list, dict)):
            continue
        lowered = name.lower()
        if "inventory" in lowered or "manifest" in lowered or "asset" in lowered:
            found.append((name, value))
    return found


def _iter_container_rows(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                yield item
    elif isinstance(value, dict):
        for item in value.values():
            if isinstance(item, dict):
                yield item
            elif isinstance(item, list):
                yield from (x for x in item if isinstance(x, dict))


def iter_source_rows() -> list[tuple[dict[str, Any], str]]:
    """返回所有可能承载盘点或分区接替关系的原始行。"""
    rows: list[tuple[dict[str, Any], str]] = []

    for attr_name, container in _store_list_attrs():
        for row in _iter_container_rows(container):
            rows.append((row, f"store.{attr_name}"))

    pipeline = store.pipeline("partition_governance")
    if pipeline is not None:
        for row in getattr(pipeline, "_artifacts", []):
            if isinstance(row, dict):
                rows.append((row, "pipeline.partition_governance"))

    for module in store.module_names():
        if module == "compaction":
            # 压缩任务行本身保存的是计数快照，不能反向当成最新盘点来源。
            continue
        try:
            module_rows = store.rows(module)
        except Exception:
            continue
        for row in module_rows:
            if isinstance(row, dict):
                rows.append((row, f"module.{module}"))
    return rows


def iter_inventories(table_name: str | None = None) -> list[dict[str, Any]]:
    """从盘点仓库、流水线制品和分区策略行中汇总所有版本。"""
    inventories: list[dict[str, Any]] = []
    for order, (row, source) in enumerate(iter_source_rows()):
        normalized = normalize_inventory(row, source, order)
        if normalized is not None:
            if table_name is None or normalized["table"] == str(table_name):
                inventories.append(normalized)
    return inventories


def _mapping_from_row(row: dict[str, Any], source: str) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    table = _as_str(_first(row, _TABLE_KEYS)).strip()
    new_partition = _as_str(_first(row, _REPLACEMENT_KEYS) or row.get("to") or row.get("new")).strip()
    old_partition = _as_str(
        _first(row, _OLD_PARTITION_KEYS)
        or row.get("from")
        or row.get("old")
        or (_first(row, _PARTITION_KEYS) if new_partition else None)
    ).strip()
    if not table or not old_partition or not new_partition:
        return None
    return {
        "table": table,
        "old_partition": old_partition,
        "new_partition": new_partition,
        "status": _as_str(row.get("status") or row.get("接替状态") or "已生效"),
        "confirmed": row.get("confirmed", row.get("已确认", True)) is not False,
        "source": source,
        "raw": row,
    }


def iter_replacements(table_name: str | None = None) -> list[dict[str, Any]]:
    mappings: list[dict[str, Any]] = []
    for row in store.list_partition_replacements():
        mapping = _mapping_from_row(row, "store._partition_replacements")
        if mapping:
            mappings.append(mapping)

    # 即使撤下记录没有文件数字、无法被规范成“盘点”，也要识别其中的接替关系。
    for row, source in iter_source_rows():
        mapping = _mapping_from_row(row, source)
        if mapping:
            mappings.append(mapping)

    for inv in iter_inventories(table_name):
        raw = inv.get("raw") if isinstance(inv.get("raw"), dict) else {}
        if inv.get("replacement_partition"):
            mappings.append({
                "table": inv["table"],
                "old_partition": inv["partition"],
                "new_partition": inv["replacement_partition"],
                "status": inv["status"],
                "confirmed": inv["confirmed"],
                "source": inv["source"],
                "raw": raw,
            })
        if inv.get("replaces_partition"):
            mappings.append({
                "table": inv["table"],
                "old_partition": inv["replaces_partition"],
                "new_partition": inv["partition"],
                "status": inv["status"],
                "confirmed": inv["confirmed"],
                "source": inv["source"],
                "raw": raw,
            })

    deduped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for mapping in mappings:
        key = (
            str(mapping.get("table")),
            str(mapping.get("old_partition")),
            str(mapping.get("new_partition")),
            str(mapping.get("source")),
        )
        deduped[key] = mapping
    mappings = list(deduped.values())

    if table_name is not None:
        mappings = [m for m in mappings if m["table"] == str(table_name)]
    return mappings


def active_replacement(table: str, partition: str) -> dict[str, Any] | None:
    mappings = [
        m for m in iter_replacements(table)
        if m["old_partition"] == partition and m.get("confirmed") is not False
    ]
    return mappings[-1] if mappings else None


def resolve_inventory(table: str, partition: str) -> dict[str, Any]:
    """解析目标分区当前有效盘点，并附带接替分区的盘点/冲突信息。"""
    all_for_partition = [
        inv for inv in iter_inventories(table)
        if inv["partition"] == partition
    ]
    effective = [inv for inv in all_for_partition if inv["effective"]]
    effective.sort(key=_version_sort_key)
    current = effective[-1] if effective else None

    replacement_mapping = None if current else active_replacement(table, partition)
    replacement_inventory = None
    replacement_effective = False
    if replacement_mapping:
        successor = replacement_mapping["new_partition"]
        successor_rows = [
            inv for inv in iter_inventories(table)
            if inv["partition"] == successor
        ]
        successor_effective_rows = [inv for inv in successor_rows if inv["effective"]]
        successor_effective_rows.sort(key=_version_sort_key)
        if successor_effective_rows:
            replacement_inventory = successor_effective_rows[-1]
            replacement_effective = True
        elif successor_rows:
            successor_rows.sort(key=_version_sort_key)
            replacement_inventory = successor_rows[-1]

    conflict = None
    if not current and replacement_mapping:
        successor = replacement_mapping["new_partition"]
        if replacement_effective and replacement_inventory:
            conflict = {
                "code": "partition_replaced_adoptable",
                "message": (
                    f"原分区 {partition} 已撤下，由 {successor} 接替；"
                    f"{successor} 已有已确认盘点 {replacement_inventory['file_count']} 个文件，"
                    "原任务不会自动扩大采用范围，可显式改派到接替分区。"
                ),
            }
        elif replacement_inventory:
            conflict = {
                "code": "partition_replaced_unconfirmed",
                "message": (
                    f"原分区 {partition} 已撤下并由 {successor} 接替；"
                    f"接替分区登记的 {replacement_inventory['file_count']} 个文件尚未确认，"
                    "预检并发期间只能读取已确认盘点，本次不执行原任务。"
                ),
            }
        else:
            conflict = {
                "code": "partition_removed",
                "message": f"原分区 {partition} 已撤下，且未找到接替分区 {replacement_mapping['new_partition']} 的有效盘点，本次不执行。",
            }

    read_version = None
    if current:
        raw = {k: v for k, v in current.items() if k != "raw"}
        read_version = hashlib.sha1(
            repr(sorted(raw.items(), key=lambda x: x[0])).encode("utf-8")
        ).hexdigest()[:12]

    return {
        "table": table,
        "partition": partition,
        "inventory": current,
        "effective_inventory": current,
        "all_versions": sorted(all_for_partition, key=_version_sort_key),
        "replacement": replacement_mapping,
        "replacement_inventory": replacement_inventory,
        "replacement_effective": replacement_effective,
        "conflict": conflict,
        "read_version": read_version,
        "threshold": COMPACTION_THRESHOLD,
    }


def scope_snapshot(resolution: dict[str, Any], inventory: dict[str, Any] | None = None) -> dict[str, Any]:
    inv = inventory or resolution.get("inventory") or {}
    replacement = resolution.get("replacement") or {}
    return {
        "table": resolution.get("table"),
        "partition": (inv or {}).get("partition", resolution.get("partition")),
        "requested_partition": resolution.get("partition"),
        "inventory_id": (inv or {}).get("inventory_id"),
        "version": (inv or {}).get("version"),
        "file_count": (inv or {}).get("file_count"),
        "source": (inv or {}).get("source"),
        "read_version": resolution.get("read_version"),
        "replacement_partition": replacement.get("new_partition"),
        "captured_at": now_text(),
    }


def scope_text(scope: dict[str, Any]) -> str:
    table = scope.get("table") or ""
    partition = scope.get("partition") or ""
    inv_id = scope.get("inventory_id") or "未记录盘点"
    version = scope.get("version") or "历史版本"
    count = scope.get("file_count")
    return f"{table} / {partition} / {inv_id} / {version} / {count}个文件"
