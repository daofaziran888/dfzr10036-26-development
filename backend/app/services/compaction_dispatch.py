"""可派发文件分区清单：基于文件大小与分区阈值的小文件合并候选与派发。

领域规则（与需求逐条对应）：

1. 候选判断按「目标表 + 分区」聚合快照内数据文件，按容量阈值装箱，
   生成可派发文件分区清单（候选工作单元）。
2. 每个工作单元必须满足容量阈值：min_files <= 文件数 <= max_files 且
   min_size_mb <= 总大小 <= max_size_mb；恰好等于上限/下限都算合格。
3. 保存计划时强校验：同一计划只能写一张表；单元不允许跨分区；
   不允许重写到其他目标表（替换输出路径仍须落在本表本分区前缀下）。
4. 已被成功替换的路径（file_state=replaced）不能再次选入任何计划；
   已被其他计划领取（claimed）的路径也不能重复领取。
5. 每个单元在保存时冻结一份输入路径清单（input_manifest），
   历史快照的输入清单此后仍可核对，不受后续替换影响。
6. 派发只领取当前仍可领取的单元；失败重试只能回到本计划该单元
   尚未确认（未完成替换）的范围，不会扩大到其他单元或其他计划。
7. 回传实际替换结果后，输入文件标记为 replaced，同一分区下次生成
   候选清单时这些路径不再出现，真正改变下次可领取的文件。
"""
from __future__ import annotations

import threading
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# ---- 容量阈值（BinPack 策略参数，边界值含等号） ----
MIN_FILES = 2
MAX_FILES = 5
MIN_SIZE_MB = 10
MAX_SIZE_MB = 100

FILE_TABLE = "snapshot_data_file"
PLAN_TABLE = "compaction_plan"
UNIT_TABLE = "compaction_unit"

STATE_AVAILABLE = "available"   # 可领取
STATE_CLAIMED = "claimed"       # 已派发、尚未确认替换结果
STATE_REPLACED = "replaced"     # 已被成功替换，永不再选

UNIT_PENDING = "待派发"
UNIT_DISPATCHED = "已派发"
UNIT_COMPLETED = "已完成"
UNIT_FAILED = "失败"


class PlanValidationError(ValueError):
    """计划保存校验失败，携带可直接展示给前端的原因。"""


class CompactionDispatchService:
    """无外部依赖的内存领域服务，所有状态变更在同一把锁内原子完成。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    # ============ 基础读取 ============

    def list_files(self, table: str | None = None, partition: str | None = None,
                   state: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(FILE_TABLE)
        if table:
            rows = [r for r in rows if r.get("目标表") == table]
        if partition:
            rows = [r for r in rows if r.get("分区取值") == partition]
        if state:
            rows = [r for r in rows if r.get("file_state") == state]
        return [dict(r) for r in rows]

    def _files_by_path(self) -> dict[str, dict[str, Any]]:
        return {str(r["文件路径"]): r for r in store.rows(FILE_TABLE)}

    # ============ 候选清单生成（可派发文件分区清单） ============

    def preview_candidates(self, table: str) -> dict[str, Any]:
        """按分区聚合当前可领取的小文件，按阈值装箱出候选工作单元。

        超大文件（单文件 > max_size）不属于小文件合并范围，保留在分区不动；
        装完后凑不齐下限的余量文件保留为待处理，不派发。
        """
        with self._lock:
            rows = [
                r for r in store.rows(FILE_TABLE)
                if r.get("目标表") == table and r.get("file_state") == STATE_AVAILABLE
            ]
            partitions: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                partitions.setdefault(str(row["分区取值"]), []).append(row)

            partition_reports: list[dict[str, Any]] = []
            all_units: list[dict[str, Any]] = []
            for partition in sorted(partitions):
                files = partitions[partition]
                small = [f for f in files if float(f["文件大小MB"]) <= MAX_SIZE_MB]
                oversized = [f for f in files if float(f["文件大小MB"]) > MAX_SIZE_MB]
                packed, leftover = self._bin_pack(small)

                candidate_units = []
                for index, unit_files in enumerate(packed, start=1):
                    candidate_units.append({
                        "单元编号": f"CAND/{partition}/U{index}",
                        "目标表": table,
                        "分区取值": partition,
                        "文件数": len(unit_files),
                        "总大小MB": sum(float(f["文件大小MB"]) for f in unit_files),
                        "输入路径": [f["文件路径"] for f in unit_files],
                    })
                all_units.extend(candidate_units)

                pending_paths = [f["文件路径"] for f in leftover]
                reasons = []
                if oversized:
                    reasons.append(
                        f"{len(oversized)}个文件单文件超过{MAX_SIZE_MB}MB上限，不参与小文件合并")
                if leftover:
                    reasons.append(
                        f"{len(leftover)}个余量文件凑不齐容量下限"
                        f"（>={MIN_FILES}个且>={MIN_SIZE_MB}MB），仍待处理")
                partition_reports.append({
                    "目标表": table,
                    "分区取值": partition,
                    "分区文件总数": len(files),
                    "分区总大小MB": sum(float(f["文件大小MB"]) for f in files),
                    "可领取小文件数": len(small),
                    "超大文件数": len(oversized),
                    "候选单元数": len(candidate_units),
                    "候选单元": candidate_units,
                    "待处理路径": pending_paths,
                    "待处理原因": reasons,
                })

            return {
                "目标表": table,
                "阈值": {
                    "最小文件数": MIN_FILES, "最大文件数": MAX_FILES,
                    "最小总大小MB": MIN_SIZE_MB, "最大总大小MB": MAX_SIZE_MB,
                    "边界说明": "阈值均含等号，恰好等于上限或下限的单元判定为合格",
                },
                "分区清单": partition_reports,
                "候选单元总数": len(all_units),
            }

    @staticmethod
    def _bin_pack(files: list[dict[str, Any]]) -> tuple[list[list[dict[str, Any]]],
                                                        list[dict[str, Any]]]:
        """贪心装箱：小文件优先，逐个塞进仍能容纳它的单元。

        每个单元同时受文件数上限和总大小上限约束；装箱后不满足下限的
        单元整体退回余量（仍待处理，不派发）。
        """
        ordered = sorted(files, key=lambda f: float(f["文件大小MB"]))
        units: list[list[dict[str, Any]]] = []
        unit_sizes: list[float] = []
        for f in ordered:
            size = float(f["文件大小MB"])
            placed = False
            for idx, unit in enumerate(units):
                if (len(unit) < MAX_FILES
                        and unit_sizes[idx] + size <= MAX_SIZE_MB):
                    unit.append(f)
                    unit_sizes[idx] += size
                    placed = True
                    break
            if not placed:
                units.append([f])
                unit_sizes.append(size)

        qualified: list[list[dict[str, Any]]] = []
        leftover: list[dict[str, Any]] = []
        for unit, total in zip(units, unit_sizes):
            if len(unit) >= MIN_FILES and total >= MIN_SIZE_MB:
                qualified.append(unit)
            else:
                leftover.extend(unit)
        return qualified, leftover

    # ============ 计划保存（强校验 + 原子领取） ============

    def create_plan(self, table: str, units: list[dict[str, Any]],
                    remark: str | None = None) -> dict[str, Any]:
        """校验并保存合并计划，成功即原子领取输入文件。

        units: [{"分区取值": "...", "输入路径": [path, ...]}, ...]
        """
        with self._lock:
            if not units:
                raise PlanValidationError("计划至少需要一个工作单元")
            index = self._files_by_path()
            self._validate_units(table, units, index)

            plan_rows = store.rows(PLAN_TABLE)
            unit_rows = store.rows(UNIT_TABLE)
            plan_id = max((int(r.get("id", 0)) for r in plan_rows), default=0) + 1
            plan_no = f"PLAN-{plan_id:04d}"
            snapshot_ids = sorted({
                str(index[str(p)]["快照编号"])
                for u in units for p in u["输入路径"]
            })

            saved_units: list[dict[str, Any]] = []
            # 先做完全部校验再改状态，保证拒绝时不产生任何部分领取
            for ordinal, unit_in in enumerate(units, start=1):
                partition = str(unit_in["分区取值"]).strip()
                paths = [str(p).strip() for p in unit_in["输入路径"]]
                unit_id = max((int(r.get("id", 0)) for r in unit_rows), default=0) + 1
                unit_no = f"{plan_no}-U{ordinal:02d}"
                chosen = [index[p] for p in paths]
                total_size = sum(float(f["文件大小MB"]) for f in chosen)
                frozen = [dict(f) for f in chosen]  # 冻结输入清单，供历史核对
                saved = {
                    "id": unit_id,
                    "单元编号": unit_no,
                    "计划编号": plan_no,
                    "目标表": table,
                    "分区取值": partition,
                    "文件数": len(paths),
                    "总大小MB": total_size,
                    "状态": UNIT_PENDING,
                    "任务ID": None,
                    "输入清单": frozen,
                    "输出路径": None,
                    "失败原因": None,
                    "派发次数": 0,
                }
                unit_rows.append(saved)
                saved_units.append(saved)
                for f in chosen:
                    f["file_state"] = STATE_CLAIMED
                    f["归属计划"] = plan_no
                    f["归属单元"] = unit_no

            plan = {
                "id": plan_id,
                "计划编号": plan_no,
                "目标表": table,
                "来源快照": snapshot_ids,
                "单元数": len(saved_units),
                "文件总数": sum(u["文件数"] for u in saved_units),
                "状态": "待派发",
                "阈值": {
                    "min_files": MIN_FILES, "max_files": MAX_FILES,
                    "min_size_mb": MIN_SIZE_MB, "max_size_mb": MAX_SIZE_MB,
                },
                "备注": remark or "",
            }
            plan_rows.append(plan)
            return self.get_plan(plan_id)

    def _validate_units(self, table: str, units: list[dict[str, Any]],
                        index: dict[str, dict[str, Any]]) -> None:
        seen_paths: set[str] = set()
        for ordinal, unit_in in enumerate(units, start=1):
            partition = str(unit_in.get("分区取值", "")).strip()
            paths = [str(p).strip() for p in unit_in.get("输入路径", [])]
            label = f"第{ordinal}个单元（分区 {partition or '空'}）"
            if not partition:
                raise PlanValidationError(f"{label}缺少分区取值，单元不允许跨分区")
            if not paths:
                raise PlanValidationError(f"{label}输入路径为空")

            unit_partitions: set[str] = set()
            unit_tables: set[str] = set()
            for path in paths:
                if path in seen_paths:
                    raise PlanValidationError(f"{label}内部存在重复路径：{path}")
                seen_paths.add(path)
                f = index.get(path)
                if f is None:
                    raise PlanValidationError(f"{label}引用了快照中不存在的路径：{path}")
                # 不允许重写到其他目标表
                if f.get("目标表") != table:
                    raise PlanValidationError(
                        f"{label}路径 {path} 属于表 {f.get('目标表')}，"
                        f"与计划目标表 {table} 不一致，不允许跨表保存计划")
                unit_tables.add(str(f.get("目标表")))
                unit_partitions.add(str(f.get("分区取值")))
                if f.get("分区取值") != partition:
                    raise PlanValidationError(
                        f"{label}路径 {path} 实际位于分区 {f.get('分区取值')}，"
                        "工作单元不允许跨分区")
                # 已被成功替换的路径不能再次选入
                if f.get("file_state") == STATE_REPLACED:
                    raise PlanValidationError(
                        f"{label}路径 {path} 已被成功替换为 {f.get('替换为')}，"
                        "不能再次选入合并计划")
                # 已被其他计划领取的路径不能重复领取
                if f.get("file_state") == STATE_CLAIMED:
                    raise PlanValidationError(
                        f"{label}路径 {path} 已被计划 {f.get('归属计划')} 的 "
                        f"单元 {f.get('归属单元')} 领取，不能重复覆盖")

            if len(unit_tables) > 1:
                raise PlanValidationError(f"{label}混入了多张表的文件：{sorted(unit_tables)}")
            if len(unit_partitions) > 1:
                raise PlanValidationError(
                    f"{label}跨越了多个分区：{sorted(unit_partitions)}，单元不允许跨分区")

            # 容量阈值校验（边界含等号）
            count = len(paths)
            total = sum(float(index[p]["文件大小MB"]) for p in paths)
            if count < MIN_FILES:
                raise PlanValidationError(
                    f"{label}文件数 {count} 低于最小阈值 {MIN_FILES}")
            if count > MAX_FILES:
                raise PlanValidationError(
                    f"{label}文件数 {count} 超过最大阈值 {MAX_FILES}")
            if total < MIN_SIZE_MB:
                raise PlanValidationError(
                    f"{label}总大小 {total:g}MB 低于最小阈值 {MIN_SIZE_MB}MB")
            if total > MAX_SIZE_MB:
                raise PlanValidationError(
                    f"{label}总大小 {total:g}MB 超过最大阈值 {MAX_SIZE_MB}MB")

    # ============ 派发 / 回执 / 重试 ============

    def list_plans(self, table: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = store.rows(PLAN_TABLE)
            if table:
                rows = [r for r in rows if r.get("目标表") == table]
            return [self._plan_view(r) for r in rows]

    def get_plan(self, plan_id: int) -> dict[str, Any] | None:
        with self._lock:
            plan = store.find(PLAN_TABLE, plan_id)
            return self._plan_view(plan) if plan else None

    def _plan_view(self, plan: dict[str, Any]) -> dict[str, Any]:
        plan_no = str(plan["计划编号"])
        units = [
            dict(u) for u in store.rows(UNIT_TABLE)
            if u.get("计划编号") == plan_no
        ]
        view = dict(plan)
        view["工作单元"] = units
        view["已替换路径"] = [
            u["输出路径"] for u in units if u.get("状态") == UNIT_COMPLETED
        ]
        return view

    def dispatch(self, plan_id: int, task_id: str | None = None) -> dict[str, Any]:
        """领取计划下当前仍可领取（待派发）的单元。

        已派发/已完成/失败的单元不会重复发出，保证已发出的任务不覆盖重复文件。
        """
        with self._lock:
            plan = store.find(PLAN_TABLE, plan_id)
            if plan is None:
                raise PlanValidationError(f"合并计划 {plan_id} 不存在")
            plan_no = str(plan["计划编号"])
            units = [u for u in store.rows(UNIT_TABLE) if u.get("计划编号") == plan_no]
            dispatched, skipped = [], []
            seq = 1
            for unit in units:
                if unit["状态"] != UNIT_PENDING:
                    skipped.append({"单元编号": unit["单元编号"], "当前状态": unit["状态"]})
                    continue
                unit["状态"] = UNIT_DISPATCHED
                unit["派发次数"] = int(unit.get("派发次数", 0)) + 1
                unit["任务ID"] = task_id or f"TASK-{plan_no}-{seq:02d}"
                unit["失败原因"] = None
                seq += 1
                dispatched.append({
                    "任务ID": unit["任务ID"],
                    "单元编号": unit["单元编号"],
                    "目标表": unit["目标表"],
                    "分区取值": unit["分区取值"],
                    "输入路径": [f["文件路径"] for f in unit["输入清单"]],
                })
            self._refresh_plan_status(plan)
            return {
                "计划编号": plan_no,
                "本次派发": dispatched,
                "跳过单元": skipped,
                "派发数": len(dispatched),
            }

    def report_replacement(self, plan_id: int, unit_id: int,
                           output_path: str, failed: bool = False,
                           reason: str | None = None) -> dict[str, Any]:
        """回传实际替换结果，改变同一分区下次可领取的文件集合。"""
        with self._lock:
            plan = store.find(PLAN_TABLE, plan_id)
            unit = store.find(UNIT_TABLE, unit_id)
            if plan is None or unit is None or unit.get("计划编号") != plan["计划编号"]:
                raise PlanValidationError("计划或工作单元不存在，或单元不属于该计划")
            if unit["状态"] != UNIT_DISPATCHED:
                raise PlanValidationError(
                    f"单元 {unit['单元编号']} 当前状态为 {unit['状态']}，"
                    "只有已派发且尚未确认的单元允许回传结果")

            table = str(plan["目标表"])
            partition = str(unit["分区取值"])
            if failed:
                # 失败：释放本单元尚未确认的领取，回到本计划可重试范围
                self._release_unit_files(unit)
                unit["状态"] = UNIT_FAILED
                unit["失败原因"] = reason or "执行失败，未产生替换"
                unit["输出路径"] = None
            else:
                output_path = output_path.strip()
                if not output_path:
                    raise PlanValidationError("成功回执必须提供替换输出路径")
                # 不允许重写到其他目标表/其他分区
                if output_path in {f["文件路径"] for f in unit["输入清单"]}:
                    raise PlanValidationError("替换输出路径不能与任一输入路径相同")
                if not self._output_within_scope(output_path, unit):
                    raise PlanValidationError(
                        f"输出路径 {output_path} 不在表 {table} 的分区 {partition} "
                        "目录下，不允许重写到其他目标表或分区")
                index = self._files_by_path()
                for frozen in unit["输入清单"]:
                    f = index.get(str(frozen["文件路径"]))
                    # 只确认本计划本单元尚未确认的范围
                    if f is None or f.get("归属单元") != unit["单元编号"]:
                        raise PlanValidationError(
                            f"输入路径 {frozen['文件路径']} 已不在本单元领取范围内，"
                            "拒绝确认替换")
                    f["file_state"] = STATE_REPLACED
                    f["替换为"] = output_path
                unit["状态"] = UNIT_COMPLETED
                unit["输出路径"] = output_path
                unit["失败原因"] = None

            self._refresh_plan_status(plan)
            return self._plan_view(plan)

    def retry_unit(self, plan_id: int, unit_id: int) -> dict[str, Any]:
        """失败重试：只能重新领取本计划该单元冻结清单内、尚未确认的文件。"""
        with self._lock:
            plan = store.find(PLAN_TABLE, plan_id)
            unit = store.find(UNIT_TABLE, unit_id)
            if plan is None or unit is None or unit.get("计划编号") != plan["计划编号"]:
                raise PlanValidationError("计划或工作单元不存在，或单元不属于该计划")
            if unit["状态"] != UNIT_FAILED:
                raise PlanValidationError(
                    f"单元 {unit['单元编号']} 状态为 {unit['状态']}，只有失败单元允许重试")

            index = self._files_by_path()
            # 冻结清单是重试的唯一范围：已确认替换的文件不再重领
            frozen_paths = [str(f["文件路径"]) for f in unit["输入清单"]]
            for path in frozen_paths:
                f = index.get(path)
                if f is None:
                    raise PlanValidationError(
                        f"冻结输入路径 {path} 已从快照明细中消失，无法重试")
                if f.get("file_state") == STATE_REPLACED:
                    raise PlanValidationError(
                        f"路径 {path} 已确认替换，不属于本单元可重试范围")
                if f.get("file_state") == STATE_CLAIMED and f.get("归属单元") != unit["单元编号"]:
                    raise PlanValidationError(
                        f"路径 {path} 已被其他单元 {f.get('归属单元')} 领取，重试被拒绝")
            for path in frozen_paths:
                f = index[path]
                f["file_state"] = STATE_CLAIMED
                f["归属计划"] = plan["计划编号"]
                f["归属单元"] = unit["单元编号"]
            unit["状态"] = UNIT_DISPATCHED
            unit["派发次数"] = int(unit.get("派发次数", 0)) + 1
            unit["任务ID"] = f"RETRY-{unit['单元编号']}-{unit['派发次数']}"
            unit["失败原因"] = None
            self._refresh_plan_status(plan)
            return {
                "计划编号": plan["计划编号"],
                "重试单元": unit["单元编号"],
                "任务ID": unit["任务ID"],
                "重试范围": frozen_paths,
            }

    # ============ 内部辅助 ============

    def _release_unit_files(self, unit: dict[str, Any]) -> None:
        index = self._files_by_path()
        for frozen in unit["输入清单"]:
            f = index.get(str(frozen["文件路径"]))
            if f is not None and f.get("归属单元") == unit["单元编号"]:
                f["file_state"] = STATE_AVAILABLE
                f["归属计划"] = None
                f["归属单元"] = None

    @staticmethod
    def _output_within_scope(output_path: str, unit: dict[str, Any]) -> bool:
        """输出必须落在本单元输入文件所在的同一分区目录下。

        输入清单已在保存时校验为同表同分区，因此以输入路径的目录为锚点，
        既挡住跨表/跨分区重写，也不依赖表名与物理目录的命名约定。
        """
        inputs = unit.get("输入清单") or []
        if not inputs:
            return False
        first_dir = str(inputs[0]["文件路径"]).rsplit("/", 1)[0] + "/"
        return all(str(f["文件路径"]).rsplit("/", 1)[0] + "/" == first_dir
                   for f in inputs) and output_path.startswith(first_dir)

    def _refresh_plan_status(self, plan: dict[str, Any]) -> None:
        units = [u for u in store.rows(UNIT_TABLE)
                 if u.get("计划编号") == plan["计划编号"]]
        states = {u["状态"] for u in units}
        if states and states <= {UNIT_COMPLETED}:
            plan["状态"] = "已完成"
        elif UNIT_FAILED in states:
            plan["状态"] = "部分失败"
        elif UNIT_DISPATCHED in states:
            plan["状态"] = "派发中"
        else:
            plan["状态"] = "待派发"


dispatch_service = CompactionDispatchService()


class SnapshotDataFileService(BaseModuleService):
    """快照数据文件明细：只读资源，写入与状态变更只走派发流程。"""

    MODULE = "snapshot_data_file"
    ENTITY = "数据文件明细"
    LIST_FIELDS = ["文件路径", "目标表", "分区取值", "快照编号", "文件大小MB",
                   "记录行数", "file_state", "归属计划", "归属单元", "替换为"]
    REQUIRED_FIELDS: list[str] = []
    STATUS_ORDER = ["available", "claimed", "replaced"]
    ACTION_RULES: dict[str, str] = {}

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        raise PlanValidationError("数据文件明细由湖仓提交产生，请走可派发清单流程")

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        return None, "数据文件明细只读，文件状态仅由派发/回执流程变更"


class CompactionPlanService(BaseModuleService):
    """合并计划通用资源：列表/详情走骨架，保存与动作只走派发专用端点。"""

    MODULE = "compaction_plan"
    ENTITY = "合并计划"
    LIST_FIELDS = ["计划编号", "目标表", "来源快照", "单元数", "文件总数", "状态", "备注"]
    REQUIRED_FIELDS: list[str] = []
    STATUS_ORDER = ["待派发", "派发中", "部分失败", "已完成"]
    ACTION_RULES: dict[str, str] = {}

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        raise PlanValidationError("合并计划请通过 /api/compaction-dispatch/plans 校验保存")

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        return None, "合并计划动作请走派发/回执/重试专用端点"


class CompactionUnitService(BaseModuleService):
    """合并工作单元通用资源：只读，状态机由派发/回执/重试端点驱动。"""

    MODULE = "compaction_unit"
    ENTITY = "合并工作单元"
    LIST_FIELDS = ["单元编号", "计划编号", "目标表", "分区取值", "文件数", "总大小MB",
                   "状态", "任务ID", "派发次数", "输出路径", "失败原因"]
    REQUIRED_FIELDS: list[str] = []
    STATUS_ORDER = [UNIT_PENDING, UNIT_DISPATCHED, UNIT_COMPLETED, UNIT_FAILED]
    ACTION_RULES: dict[str, str] = {}

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        raise PlanValidationError("工作单元随合并计划一起校验保存，不接受单独登记")

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        return None, "工作单元动作请走派发/回执/重试专用端点"
