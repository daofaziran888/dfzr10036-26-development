"""可派发文件分区清单 HTTP 端到端测试（仅标准库，对运行中的 8000 端口打请求）。

运行：.venv/bin/python -I test_compaction_dispatch_http.py
覆盖：候选预览 -> 双计划并发抢同一分区 -> 派发/重复派发 -> 成功/失败回执
     -> 冻结清单核对 -> 限定范围重试 -> 全量校验错误（400）
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000/api/compaction-dispatch"
TABLE_A = "ads.order_events_iceberg"
TABLE_B = "ods.payment_log_iceberg"
P_BIG = "dt=2026-11-17"
P_EXACT = "dt=2026-11-18"

passed: list[str] = []
failed: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (passed if condition else failed).append(name if condition else f"{name} {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {name} {detail}")


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


# 0. 文件明细与候选预览
print("== 0. 明细与候选 ==")
status, files = call("GET", f"/files?table={TABLE_A}")
check("明细接口200且种子文件就绪", status == 200 and files["total"] == 18, str(files.get("total")))

status, cand = call("GET", f"/candidates?table={TABLE_A}")
big = next(p for p in cand["分区清单"] if p["分区取值"] == P_BIG)
exact = next(p for p in cand["分区清单"] if p["分区取值"] == P_EXACT)
check("候选接口200", status == 200)
check("超量分区拆2单元、超大文件排除", big["候选单元数"] == 2 and big["超大文件数"] == 1)
check("恰好阈值单元 5文件/100MB",
      exact["候选单元"][0]["文件数"] == 5 and exact["候选单元"][0]["总大小MB"] == 100)

# 1. 保存计划：各种拒绝
print("== 1. 保存校验（HTTP 400） ==")
big_paths = big["候选单元"][0]["输入路径"] + big["候选单元"][1]["输入路径"]
exact_paths = exact["候选单元"][0]["输入路径"]

status, resp = call("POST", "/plans", {"table": TABLE_A, "units": []})
check("空计划返回400", status == 400 and "至少需要一个工作单元" in resp["detail"], str(resp))

status, resp = call("POST", "/plans", {
    "table": TABLE_A,
    "units": [{"分区取值": P_BIG, "输入路径": big_paths[:7]}]})  # 7 > 5
check("超文件数上限返回400", status == 400 and "最大阈值" in resp["detail"], str(resp))

status, resp = call("POST", "/plans", {
    "table": TABLE_A,
    "units": [{"分区取值": P_BIG, "输入路径": big_paths[:6]}]})  # 120MB > 100MB
check("超总大小上限返回400", status == 400 and "最大阈值" in resp["detail"], str(resp))

# 跨表
_, fb = call("GET", f"/files?table={TABLE_B}&state=available")
foreign_path = fb["items"][0]["文件路径"]
status, resp = call("POST", "/plans", {
    "table": TABLE_A,
    "units": [{"分区取值": P_BIG, "输入路径": [big_paths[0], foreign_path]}]})
check("跨表保存返回400", status == 400 and "跨表" in resp["detail"], str(resp))

# 已替换路径
_, rep = call("GET", f"/files?table={TABLE_B}&state=replaced")
status, resp = call("POST", "/plans", {
    "table": TABLE_B,
    "units": [{"分区取值": "txn_date=2026-11-18",
               "输入路径": [rep["items"][0]["文件路径"], foreign_path]}]})
check("已替换路径返回400", status == 400 and "已被成功替换" in resp["detail"], str(resp))

# 2. 双计划同时抢同一大分区
print("== 2. 并发双计划 ==")
status, plan_a = call("POST", "/plans", {"table": TABLE_A, "units": [
    {"分区取值": P_BIG, "输入路径": big["候选单元"][0]["输入路径"]},
    {"分区取值": P_BIG, "输入路径": big["候选单元"][1]["输入路径"]},
]})
check("计划A保存成功200", status == 200 and plan_a["状态"] == "待派发", str(plan_a.get("状态")))
pid_a = plan_a["id"]

status, resp = call("POST", "/plans", {"table": TABLE_A, "units": [
    {"分区取值": P_BIG, "输入路径": big["候选单元"][0]["输入路径"]}]})
check("计划B重复领取被400拒绝", status == 400 and "不能重复覆盖" in resp["detail"], str(resp))

status, plan_b = call("POST", "/plans", {"table": TABLE_A, "units": [
    {"分区取值": P_EXACT, "输入路径": exact_paths}]})
check("计划B改领其他分区成功", status == 200 and plan_b["文件总数"] == 5)
pid_b = plan_b["id"]

# 3. 派发与重复派发
print("== 3. 派发 ==")
status, d1 = call("POST", f"/plans/{pid_a}/dispatch", {"task_id": "TASK-A1"})
check("首次派发2单元", status == 200 and d1["派发数"] == 2)
check("已发出任务无重复文件",
      len({p for t in d1["本次派发"] for p in t["输入路径"]}) == 7)
status, d2 = call("POST", f"/plans/{pid_a}/dispatch", {"task_id": "TASK-A2"})
check("重复派发跳过且0新增", status == 200 and d2["派发数"] == 0 and len(d2["跳过单元"]) == 2)

# 4. 成功回执 + 越界输出拒绝 + 下次候选变化
print("== 4. 替换回执 ==")
units_a = plan_a["工作单元"]
u1, u2 = units_a[0], units_a[1]
out1 = f"s3://warehouse/ads/order_events/data/{P_BIG}/compacted-{u1['单元编号']}.parquet"
status, view = call("POST", f"/plans/{pid_a}/units/{u1['id']}/report",
                    {"output_path": out1})
check("成功回执200且单元完成", status == 200
      and next(u for u in view["工作单元"] if u["id"] == u1["id"])["状态"] == "已完成")

status, resp = call("POST", f"/plans/{pid_a}/units/{u2['id']}/report",
                    {"output_path": "s3://warehouse/other/t.parquet"})
check("越界输出400", status == 400 and "不允许重写" in resp["detail"], str(resp))

status, resp = call("POST", f"/plans/{pid_a}/units/{u1['id']}/report",
                    {"output_path": out1 + ".x"})
check("已完成单元重复回执400", status == 400 and "只有已派发" in resp["detail"], str(resp))

_, cand2 = call("GET", f"/candidates?table={TABLE_A}")
big2 = next(p for p in cand2["分区清单"] if p["分区取值"] == P_BIG)
check("回执后同分区下次候选不再含已替换文件",
      not ({f["文件路径"] for f in u1["输入清单"]}
           & {p for u in big2["候选单元"] for p in u["输入路径"]}))

# 完成第二单元
status, view = call("POST", f"/plans/{pid_a}/units/{u2['id']}/report",
                    {"output_path": f"s3://warehouse/ads/order_events/data/{P_BIG}/c2.parquet"})
check("计划整体完成", status == 200 and view["状态"] == "已完成")

# 历史输入清单仍可核对
_, detail = call("GET", f"/plans/{pid_a}")
check("冻结输入清单完整保留",
      all(len(u["输入清单"]) == u["文件数"] for u in detail["工作单元"]))

# 5. 失败与限定范围重试（用计划B的恰好阈值分区）
print("== 5. 失败重试 ==")
call("POST", f"/plans/{pid_b}/dispatch", {"task_id": "TASK-B1"})
bu = call("GET", f"/plans/{pid_b}")[1]["工作单元"][0]
frozen = [f["文件路径"] for f in bu["输入清单"]]
status, view = call("POST", f"/plans/{pid_b}/units/{bu['id']}/report",
                    {"failed": True, "reason": "executor OOM"})
bu2 = next(u for u in view["工作单元"] if u["id"] == bu["id"])
check("失败回执单元转失败", status == 200 and bu2["状态"] == "失败")
_, avail = call("GET", f"/files?table={TABLE_A}&partition={P_EXACT}&state=available")
check("失败后文件释放为可领取", len(avail["items"]) == 5)

status, retry = call("POST", f"/plans/{pid_b}/units/{bu['id']}/retry", {})
check("重试范围严格等于冻结清单", status == 200 and sorted(retry["重试范围"]) == sorted(frozen))
_, claimed = call("GET", f"/files?table={TABLE_A}&partition={P_EXACT}&state=claimed")
check("重试后文件回到claimed", len(claimed["items"]) == 5)

status, resp = call("POST", f"/plans/{pid_a}/units/{u1['id']}/retry", {})
check("已完成单元重试被400拒绝", status == 400 and "只有失败单元" in resp["detail"])

status, view = call("POST", f"/plans/{pid_b}/units/{bu['id']}/report",
                    {"output_path": f"s3://warehouse/ads/order_events/data/{P_EXACT}/cr.parquet"})
check("重试后回执成功", status == 200 and view["状态"] == "已完成")

# 6. 计划列表与404
print("== 6. 列表与404 ==")
status, listing = call("GET", f"/plans?table={TABLE_A}")
check("计划列表200且含两个计划", status == 200 and listing["total"] == 2)
status, resp = call("GET", "/plans/9999")
check("不存在计划返回404", status == 404)

print(f"\n结果：通过 {len(passed)} 项，失败 {len(failed)} 项")
if failed:
    for item in failed:
        print("  -", item)
    raise SystemExit(1)
print("HTTP 端到端全部断言通过")
