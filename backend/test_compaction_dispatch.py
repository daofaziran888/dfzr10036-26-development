"""可派发文件分区清单端到端领域测试（标准库断言，无需 pytest）。

运行：cd backend && .venv/bin/python -I test_compaction_dispatch.py
覆盖：
1. 超量分区按阈值拆成多个合格单元，超大文件不参与小文件合并
2. 恰好等于上限（文件数=5、总大小=100MB）和恰好等于下限的判定
3. 欠阈值分区仍待处理、已替换路径不再进入候选
4. 两个计划同时抢同一大分区：第二个计划被拒绝，不覆盖重复文件
5. 重复派发跳过已发出单元；成功回执后同分区下次可领取文件发生变化
6. 失败重试只回到本计划本单元尚未确认的范围；冻结输入清单可核对
7. 跨分区/跨表/超容量/输出越界等保存校验
"""
from __future__ import annotations

from app.seed import SEED_ROWS
from app.services.compaction_dispatch import (
    MAX_FILES,
    MAX_SIZE_MB,
    MIN_FILES,
    MIN_SIZE_MB,
    STATE_AVAILABLE,
    STATE_CLAIMED,
    STATE_REPLACED,
    UNIT_COMPLETED,
    UNIT_DISPATCHED,
    UNIT_FAILED,
    UNIT_PENDING,
    PlanValidationError,
    dispatch_service,
)
from app.store import store

TABLE_A = "ads.order_events_iceberg"
TABLE_B = "ods.payment_log_iceberg"
P_BIG = "dt=2026-11-17"
P_EXACT = "dt=2026-11-18"
P_NORMAL = "dt=2026-11-19"
P_SMALL = "dt=2026-11-20"

passed: list[str] = []
failed: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        passed.append(name)
        print(f"  PASS  {name}")
    else:
        failed.append(f"{name} {detail}")
        print(f"  FAIL  {name} {detail}")


def reset_store() -> None:
    # 直接用种子重建内存仓库，保证每组用例起点一致
    store._tables = {name: [dict(row) for row in rows]
                     for name, rows in SEED_ROWS.items()}


def paths(table: str, partition: str, state: str | None = None) -> list[str]:
    return [r["文件路径"] for r in dispatch_service.list_files(table, partition, state)]


def expect_error(name: str, call, *fragments: str) -> None:
    try:
        call()
    except PlanValidationError as exc:
        ok = all(frag in str(exc) for frag in fragments)
        check(name, ok, f"报错信息={exc}")
        return
    check(name, False, "应当被拒绝但保存成功")


# ---------------------------------------------------------------- 1. 候选清单
print("== 1. 候选判断：超量分区 / 恰好阈值 / 欠阈值 / 已替换路径 ==")
reset_store()
cand = dispatch_service.preview_candidates(TABLE_A)
by_part = {p["分区取值"]: p for p in cand["分区清单"]}

big = by_part[P_BIG]
check("超量分区生成2个候选单元", big["候选单元数"] == 2, str(big))
check("超量分区200MB超大文件不参与合并", big["超大文件数"] == 1)
check("超量分区7个小文件全部入箱无余量",
      sorted(sum((u["输入路径"] for u in big["候选单元"]), [])) ==
      sorted(paths(TABLE_A, P_BIG, STATE_AVAILABLE)[:7])
      and not big["待处理路径"])
for unit in big["候选单元"]:
    check(f"超量单元满足全部容量阈值({unit['单元编号']})",
          MIN_FILES <= unit["文件数"] <= MAX_FILES
          and MIN_SIZE_MB <= unit["总大小MB"] <= MAX_SIZE_MB)
check("超量分区存在文件数恰好顶到上限5的单元",
      any(u["文件数"] == MAX_FILES for u in big["候选单元"]))

exact = by_part[P_EXACT]
check("恰好等于阈值分区只生成1个单元", exact["候选单元数"] == 1)
u = exact["候选单元"][0]
check("该单元文件数恰好=上限5", u["文件数"] == 5)
check("该单元总大小恰好=上限100MB", u["总大小MB"] == 100)

normal = by_part[P_NORMAL]
check("普通合格分区生成1个单元", normal["候选单元数"] == 1
      and normal["候选单元"][0]["文件数"] == 3
      and normal["候选单元"][0]["总大小MB"] == 60)

small = by_part[P_SMALL]
check("欠阈值分区不生成候选单元", small["候选单元数"] == 0)
check("欠阈值文件保留为待处理", len(small["待处理路径"]) == 2
      and "凑不齐容量下限" in " ".join(small["待处理原因"]))

# 第二张表：已替换路径不能再次进入候选
cand_b = dispatch_service.preview_candidates(TABLE_B)
pb = cand_b["分区清单"][0]
check("第二张表候选不含已替换路径",
      all("part-00001" not in p for u in pb["候选单元"] for p in u["输入路径"]))
check("第二张表3个可领取小文件生成1个单元",
      pb["候选单元数"] == 1 and pb["候选单元"][0]["文件数"] == 3)

# ---------------------------------------------------------------- 2. 保存校验
print("== 2. 计划保存校验：跨分区 / 跨表 / 已替换 / 超容量 ==")
reset_store()
big_paths = [p for p in paths(TABLE_A, P_BIG, STATE_AVAILABLE) if "big" not in p]
exact_paths = paths(TABLE_A, P_EXACT, STATE_AVAILABLE)
other_table_paths = paths(TABLE_B, "txn_date=2026-11-18", STATE_AVAILABLE)
replaced_paths = paths(TABLE_B, "txn_date=2026-11-18", STATE_REPLACED)

expect_error("拒绝跨分区单元",
             lambda: dispatch_service.create_plan(
                 TABLE_A, [{"分区取值": P_BIG,
                            "输入路径": [big_paths[0], exact_paths[0]]}]),
             "不允许跨分区")
expect_error("拒绝重写到其他目标表",
             lambda: dispatch_service.create_plan(
                 TABLE_A, [{"分区取值": P_BIG, "输入路径": [big_paths[0], other_table_paths[0]]}]),
             "不一致", "跨表")
expect_error("拒绝选入已成功替换的路径",
             lambda: dispatch_service.create_plan(
                 TABLE_B, [{"分区取值": "txn_date=2026-11-18",
                            "输入路径": [replaced_paths[0], other_table_paths[0]]}]),
             "已被成功替换")
expect_error("拒绝文件数超过上限的单元（7>5）",
             lambda: dispatch_service.create_plan(
                 TABLE_A, [{"分区取值": P_BIG, "输入路径": big_paths}]),
             "超过最大阈值")
# 用大分区内 6 个同分区小文件（6*20=120MB > 100MB）触发总大小上限，避免跨分区先报错
expect_error("拒绝总大小超过上限的单元（同分区120MB>100MB）",
             lambda: dispatch_service.create_plan(
                 TABLE_A, [{"分区取值": P_BIG, "输入路径": big_paths[:6]}]),
             "超过最大阈值")
expect_error("拒绝总大小低于下限的单元",
             lambda: dispatch_service.create_plan(
                 TABLE_A, [{"分区取值": P_SMALL,
                            "输入路径": paths(TABLE_A, P_SMALL, STATE_AVAILABLE)}]),
             "低于最小阈值")
expect_error("拒绝空计划",
             lambda: dispatch_service.create_plan(TABLE_A, []), "至少需要一个工作单元")

# 被拒绝后文件仍全部可领取，没有产生部分领取
check("校验拒绝不产生部分领取",
      all(r["file_state"] == STATE_AVAILABLE
          for r in dispatch_service.list_files(TABLE_A)))

# ---------------------------------------------------------------- 3. 并发双计划
print("== 3. 两个合并计划同时选中同一大分区 ==")
reset_store()
cand = dispatch_service.preview_candidates(TABLE_A)
big_units = [u for p in cand["分区清单"] if p["分区取值"] == P_BIG
             for u in p["候选单元"]]
plan_a = dispatch_service.create_plan(TABLE_A, [
    {"分区取值": P_BIG, "输入路径": big_units[0]["输入路径"]},
    {"分区取值": P_BIG, "输入路径": big_units[1]["输入路径"]},
])
check("计划A保存成功并原子领取", plan_a["文件总数"] == 7 and plan_a["状态"] == "待派发")
check("计划A的输入文件全部变为claimed",
      all(r["file_state"] == STATE_CLAIMED for r in dispatch_service.list_files(TABLE_A, P_BIG)
          if "big" not in r["文件路径"]))

expect_error("计划B重复领取同一批文件被拒绝",
             lambda: dispatch_service.create_plan(
                 TABLE_A, [{"分区取值": P_BIG, "输入路径": big_units[0]["输入路径"]}]),
             "已被计划", "领取")
check("计划B失败后计划A领取关系不变",
      all(r.get("归属计划") == plan_a["计划编号"]
          for r in dispatch_service.list_files(TABLE_A, P_BIG, STATE_CLAIMED)))

# 计划B改领尚未被占用的分区可以成功
exact_paths = paths(TABLE_A, P_EXACT, STATE_AVAILABLE)
plan_b = dispatch_service.create_plan(
    TABLE_A, [{"分区取值": P_EXACT, "输入路径": exact_paths}])
check("计划B领取另一分区成功", plan_b["文件总数"] == 5)
check("两张计划覆盖的文件零交集",
      not ({f["文件路径"] for u in plan_a["工作单元"] for f in u["输入清单"]}
           & {f["文件路径"] for u in plan_b["工作单元"] for f in u["输入清单"]}))

# ---------------------------------------------------------------- 4. 派发与重复派发
print("== 4. 派发领取与重复派发跳过 ==")
d1 = dispatch_service.dispatch(plan_a["id"], task_id="TASK-A1")
check("计划A首次派发2个单元", d1["派发数"] == 2 and len(d1["本次派发"]) == 2)
check("派发任务输入路径互不重复",
      len({p for t in d1["本次派发"] for p in t["输入路径"]}) == 7)
d2 = dispatch_service.dispatch(plan_a["id"], task_id="TASK-A2")
check("二次派发不再覆盖已发出单元", d2["派发数"] == 0
      and len(d2["跳过单元"]) == 2
      and all(s["当前状态"] == UNIT_DISPATCHED for s in d2["跳过单元"]))

# ---------------------------------------------------------------- 5. 成功回执
print("== 5. 替换回执改变同分区下次可领取文件 ==")
units_a = {u["id"]: u for u in dispatch_service.get_plan(plan_a["id"])["工作单元"]}
first_uid = next(iter(units_a))
first_unit = units_a[first_uid]
out_path = ("s3://warehouse/ads/order_events/data/"
            f"{P_BIG}/compacted-{first_unit['单元编号']}.parquet")
view = dispatch_service.report_replacement(plan_a["id"], first_uid, out_path)
check("回执后单元变为已完成",
      next(u for u in view["工作单元"] if u["id"] == first_uid)["状态"] == UNIT_COMPLETED)
done_files = dispatch_service.list_files(TABLE_A, P_BIG, STATE_REPLACED)
check("输入文件全部标记replaced并记录替换输出",
      len(done_files) == first_unit["文件数"]
      and all(r["替换为"] == out_path for r in done_files))

# 越界输出路径必须被拒绝（用另一个已派发单元试）
second_uid = next(uid for uid in units_a if uid != first_uid)
expect_error("拒绝写到其他目标表的输出路径",
             lambda: dispatch_service.report_replacement(
                 plan_a["id"], second_uid,
                 "s3://warehouse/other_db/other_table/data/x.parquet"),
             "不允许重写")

# 历史输入清单仍可核对（冻结清单不随文件状态变化）
frozen = next(u for u in dispatch_service.get_plan(plan_a["id"])["工作单元"]
              if u["id"] == first_uid)
check("历史快照输入清单仍可核对",
      len(frozen["输入清单"]) == first_unit["文件数"]
      and all(f["文件路径"] for f in frozen["输入清单"]))

# 同分区下次候选：已替换路径消失，只剩第二单元仍在领取中的文件
cand2 = dispatch_service.preview_candidates(TABLE_A)
big2 = next(p for p in cand2["分区清单"] if p["分区取值"] == P_BIG)
replaced_set = {r["文件路径"] for r in done_files}
check("下次候选不再出现已替换路径",
      not replaced_set & {p for u in big2["候选单元"] for p in u["输入路径"]})
# 第二单元仍 claimed，可用小文件为0 → 无候选、无待处理小文件（超大文件仍在）
check("仍在领取中的文件不会被再次派发", big2["候选单元数"] == 0
      and big2["超大文件数"] == 1)

# 完成第二单元后计划整体完成
view = dispatch_service.report_replacement(
    plan_a["id"], second_uid,
    f"s3://warehouse/ads/order_events/data/{P_BIG}/compacted-u2.parquet")
check("全部单元完成后计划状态为已完成", view["状态"] == "已完成")
expect_error("已完成单元不能重复回执",
             lambda: dispatch_service.report_replacement(
                 plan_a["id"], first_uid, out_path + ".dup"),
             "只有已派发")


# ---------------------------------------------------------------- 6. 失败与重试
print("== 6. 失败释放与限定范围的重试 ==")
reset_store()
cand = dispatch_service.preview_candidates(TABLE_A)
exact_unit = next(u for p in cand["分区清单"] if p["分区取值"] == P_EXACT
                  for u in p["候选单元"])
plan = dispatch_service.create_plan(
    TABLE_A, [{"分区取值": P_EXACT, "输入路径": exact_unit["输入路径"]}])
dispatch_service.dispatch(plan["id"], task_id="TASK-FAIL")
uid = plan["工作单元"][0]["id"]
view = dispatch_service.report_replacement(
    plan["id"], uid, "", failed=True, reason="executor OOM")
unit_after_fail = view["工作单元"][0]
check("失败回执后单元状态=失败", unit_after_fail["状态"] == UNIT_FAILED)
check("失败释放的文件重新可领取",
      all(r["file_state"] == STATE_AVAILABLE
          for r in dispatch_service.list_files(TABLE_A, P_EXACT)))
check("计划状态反映部分失败", view["状态"] == "部分失败")

# 重试只能回到本单元冻结清单范围
retry = dispatch_service.retry_unit(plan["id"], uid)
check("重试范围等于冻结输入清单",
      sorted(retry["重试范围"]) == sorted(exact_unit["输入路径"]))
check("重试后文件重新claimed且归属本单元",
      all(r["file_state"] == STATE_CLAIMED and r["归属单元"] == unit_after_fail["单元编号"]
          for r in dispatch_service.list_files(TABLE_A, P_EXACT)))

# 重试成功后再次重试被拒绝
dispatch_service.report_replacement(
    plan["id"], uid,
    f"s3://warehouse/ads/order_events/data/{P_EXACT}/compacted-retry.parquet")
expect_error("已完成单元不允许再重试",
             lambda: dispatch_service.retry_unit(plan["id"], uid), "只有失败单元")

# 非失败状态（待派发）不能重试
reset_store()
plan2 = dispatch_service.create_plan(
    TABLE_A, [{"分区取值": P_NORMAL,
               "输入路径": paths(TABLE_A, P_NORMAL, STATE_AVAILABLE)}])
expect_error("待派发单元不能直接重试",
             lambda: dispatch_service.retry_unit(plan2["id"], plan2["工作单元"][0]["id"]),
             "只有失败单元")

# ---------------------------------------------------------------- 7. 汇总
print(f"\n结果：通过 {len(passed)} 项，失败 {len(failed)} 项")
if failed:
    for item in failed:
        print("  -", item)
    raise SystemExit(1)
print("全部断言通过")
