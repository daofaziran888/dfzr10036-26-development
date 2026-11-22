"""可派发文件分区清单路由：候选预览、计划保存、派发、替换回执与失败重试。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.compaction_dispatch import (
    PlanValidationError,
    dispatch_service,
)

router = APIRouter(prefix="/api/compaction-dispatch", tags=["可派发文件分区清单"])


class UnitSpec(BaseModel):
    分区取值: str
    输入路径: list[str]


class PlanPayload(BaseModel):
    table: str
    units: list[UnitSpec]
    remark: str | None = None


class DispatchPayload(BaseModel):
    task_id: str | None = None


class ReplacementPayload(BaseModel):
    output_path: str = ""
    failed: bool = False
    reason: str | None = None


def _guard(call):
    try:
        return call()
    except PlanValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/files")
def list_files(table: str | None = None, partition: str | None = None,
               state: str | None = None) -> dict[str, Any]:
    """快照内数据文件明细，可按表/分区/文件状态过滤。"""
    items = dispatch_service.list_files(table, partition, state)
    return {"items": items, "total": len(items)}


@router.get("/candidates")
def preview_candidates(table: str) -> dict[str, Any]:
    """按阈值生成可派发文件分区清单（候选工作单元），不落库、不领取。"""
    return dispatch_service.preview_candidates(table)


@router.post("/plans")
def create_plan(payload: PlanPayload) -> dict[str, Any]:
    """校验并保存合并计划；成功即原子领取输入文件。"""
    units = [u.model_dump() for u in payload.units]
    return _guard(lambda: dispatch_service.create_plan(payload.table, units, payload.remark))


@router.get("/plans")
def list_plans(table: str | None = None) -> dict[str, Any]:
    items = dispatch_service.list_plans(table)
    return {"items": items, "total": len(items)}


@router.get("/plans/{plan_id}")
def get_plan(plan_id: int) -> dict[str, Any]:
    plan = dispatch_service.get_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"合并计划 {plan_id} 不存在")
    return plan


@router.post("/plans/{plan_id}/dispatch")
def dispatch_plan(plan_id: int, payload: DispatchPayload) -> dict[str, Any]:
    """领取计划下待派发单元；已派发/已完成/失败单元自动跳过，不重复覆盖。"""
    return _guard(lambda: dispatch_service.dispatch(plan_id, payload.task_id))


@router.post("/plans/{plan_id}/units/{unit_id}/report")
def report_replacement(plan_id: int, unit_id: int,
                       payload: ReplacementPayload) -> dict[str, Any]:
    """回传实际替换结果：成功则输入文件标记 replaced，失败则回到可重试范围。"""
    return _guard(lambda: dispatch_service.report_replacement(
        plan_id, unit_id, payload.output_path, payload.failed, payload.reason))


@router.post("/plans/{plan_id}/units/{unit_id}/retry")
def retry_unit(plan_id: int, unit_id: int) -> dict[str, Any]:
    """失败重试，范围严格限定在本计划本单元冻结清单内尚未确认的文件。"""
    return _guard(lambda: dispatch_service.retry_unit(plan_id, unit_id))
