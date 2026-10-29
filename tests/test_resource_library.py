# -*- coding: utf-8 -*-
# 资源库（MSDS 结构化查询 + 资料目录）单元测试：导入/去重/查询/热门/目录检索/
# handler 契约/损坏文件重建，以及经 MCPToolManager 全链路注册调用。
# 用法: python tests/test_resource_library.py
import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def fresh_lib():
    from mcp.resource_library import ResourceLibrary
    data_dir = Path(__file__).resolve().parents[1] / "data"  # 测试产物留在 D 盘 data 下，锚定仓库根
    tmp = tempfile.mkdtemp(prefix="reslib_test_", dir=str(data_dir))
    return ResourceLibrary(base_dir=tmp), tmp


def test_seed_autoload():
    lib, _ = fresh_lib()
    s = lib.stats()
    record("种子数据自动加载(≥5种化学品)", s["msds_total"] >= 5, f"stats={s}")


def test_lookup_by_alias_and_cas():
    lib, _ = fresh_lib()
    r1 = lib.lookup(name="烧碱")           # 别名前缀
    r2 = lib.lookup(cas="64-17-5")         # CAS 精确
    record("别名前缀命中(烧碱→氢氧化钠)",
           r1.get("found") and r1["name"] == "氢氧化钠", f"got={r1.get('name')}")
    record("CAS 精确命中(64-17-5→乙醇)",
           r2.get("found") and r2["name"] == "乙醇", f"got={r2.get('name')}")


def test_lookup_section():
    lib, _ = fresh_lib()
    r1 = lib.lookup(name="乙醇", section="泄漏应急处理")
    r2 = lib.lookup(name="乙醇", section=6)
    r3 = lib.lookup(name="乙醇", section="999")
    record("按章节名取段", r1.get("section") == 6 and "泄漏" in r1.get("content", ""), f"got={r1.get('content','')[:20]}")
    record("按章节号取段", r2.get("section") == 6)
    record("非法章节返回错误", "error" in r3)


def test_lookup_miss_and_validation():
    lib, _ = fresh_lib()
    r1 = lib.lookup(name="三氟化溴")
    r2 = lib.lookup()
    record("未命中返回 found=False+提示", r1.get("found") is False and "hint" in r1)
    record("空参数返回错误", "error" in r2)


def test_import_fill_vs_upsert():
    lib, _ = fresh_lib()
    item = {"name": "乙醇", "cas": "64-17-5",
            "sections": {"1": "覆盖后的企业标识"}}
    r_fill = lib.import_msds([item], mode="fill")
    r_up = lib.import_msds([dict(item, name="新化学品", cas="9999-99-9")], mode="upsert")
    after = lib.lookup(cas="64-17-5")
    record("fill 模式不覆盖已有", r_fill["skipped"] == 1 and after["sections"]["1"] != "覆盖后的企业标识")
    record("upsert 新增计数", r_up["added"] == 1 and r_up["batches"] >= 1)
    record("fill 拒绝非法 mode", "error" in lib.import_msds([], mode="bad"))


def test_dedupe():
    lib, _ = fresh_lib()
    # 重复的真实来源：无 CAS 条目（键 name:xxx）与后续带 CAS 的同物条目
    lib.import_msds([
        {"name": "甲醇", "cas": "", "sections": {"3": "又一条无CAS"}},
        {"name": "甲醇", "cas": "67-56-1", "sections": {"2": "有CAS版"}},
    ], mode="upsert")
    before = lib.stats()["msds_total"]
    r = lib.dedupe()
    kept = lib.lookup(cas="67-56-1")
    record("无CAS与带CAS同物条目合并且字段互补",
           r["removed"] >= 1 and lib.stats()["msds_total"] == before - r["removed"]
           and "又一条无CAS" in kept.get("sections", {}).get("3", "")
           and kept.get("sections", {}).get("2") == "有CAS版"
           and kept.get("name") == "甲醇",
           f"dedupe={r}, kept={ {k: kept.get(k) for k in ('name','cas')} }")


def test_hot():
    lib, _ = fresh_lib()
    for _ in range(12):
        lib.lookup(cas="64-17-5")
    hot = lib.hot(top_k=3)
    record("热度统计与排序", hot and hot[0]["cas"] == "64-17-5" and hot[0]["count"] >= 12, f"hot={hot}")


def test_resources():
    lib, _ = fresh_lib()
    base_total = lib.stats()["resources_total"]  # 种子目录可能已自动加载
    lib.add_resource({"title": "动火作业检查表", "category": "documents",
                      "description": "动火前检查", "tags": ["动火"]})
    lib.add_resource({"title": "动火作业检查表", "category": "documents",
                      "description": "更新后的描述", "tags": ["动火"]})
    bad = lib.add_resource({"title": "x", "category": "not_a_cat"})
    hit = lib.search_resources(query="动火", category="documents")
    miss = lib.search_resources(query="量子力学")
    record("条目新增+同标题更新不重复", lib.stats()["resources_total"] == base_total + 1)
    record("非法分类被拒绝", "error" in bad)
    record("关键词+分类过滤命中", hit and hit[0]["title"] == "动火作业检查表")
    record("未命中返回空列表", miss == [])


def test_handler_contract():
    lib, _ = fresh_lib()
    loop = asyncio.new_event_loop()
    try:
        h1 = loop.run_until_complete(lib.msds_handler({"name": "盐酸"}, None))
        h2 = loop.run_until_complete(lib.msds_handler({"action": "hot"}, None))
        h3 = loop.run_until_complete(lib.msds_handler({"action": "stats"}, None))
        h4 = loop.run_until_complete(lib.msds_handler({"action": "nope"}, None))
        h5 = loop.run_until_complete(lib.resource_handler({"query": "动火"}, None))
        h6 = loop.run_until_complete(lib.resource_handler({"query": "量子力学"}, None))
        record("msds_handler lookup", h1.get("found") is True)
        record("msds_handler hot", "items" in h2)
        record("msds_handler stats", "msds_total" in h3)
        record("msds_handler 未知action报错", "error" in h4)
        record("resource_handler 结构", "items" in h5)
        record("resource_handler 未命中带提示", h6["items"] == [] and "hint" in h6)
    finally:
        loop.close()


def test_corrupt_recovery():
    lib, tmp = fresh_lib()
    (Path(tmp) / "resources.json").write_text("{broken json", encoding="utf-8")
    lib2 = type(lib)(base_dir=tmp)
    record("损坏文件归档重建不抛异常", isinstance(lib2.stats()["resources_total"], int)
           and (Path(tmp) / "resources.json.corrupt").exists())


def test_ledger_checklist():
    lib, _ = fresh_lib()
    lib.add_resource({"title": "年度安全培训计划模板", "category": "documents",
                      "description": "培训计划", "tags": ["培训", "计划"]})
    r1 = lib.checklist("危化品台账")     # 别名命中 hazardous_chemicals
    r2 = lib.checklist("培训")           # 名称命中 training
    r3 = lib.checklist("不存在的台账")
    r4 = lib.checklist("")
    r5 = loop = None
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        r5 = loop.run_until_complete(lib.ledger_handler({"action": "list"}, None))
        r6 = loop.run_until_complete(lib.ledger_handler({"action": "bad"}, None))
    finally:
        loop.close()
    record("别名命中台账清单", r1.get("found") and r1["ledger"] == "危险化学品管理台账",
           f"got={r1.get('ledger')}")
    record("清单含条目与摘要", r1["summary"]["total"] >= 3 and r1["summary"]["missing"] >= 1)
    record("清单支持中文别名(培训)", r2.get("found") and r2["ledger"] == "安全培训教育台账")
    record("命中条目自动匹配资源库", any(i["matched"] for i in r2["items"]),
           f"items={[(i['name'], bool(i['matched'])) for i in r2['items']]}")
    record("缺失条目标 missing", all(i["missing"] == (i["matched"] is None) for i in r2["items"]))
    record("未知台账返回 available 列表", r3.get("found") is False and len(r3["available"]) >= 5)
    record("空参数返回错误+可用列表", "error" in r4 and "available" in r4)
    record("ledger_handler list", r5 and "ledgers" in r5)
    record("ledger_handler 未知action报错", "error" in r6)


def test_strict_alignment_and_routing():
    """空白终审修复员引入的严格对齐匹配与 id 前缀路由修复的回归守卫。

    宽匹配会把只共享一个通用词的条目算作已入库（培训签到表 挂到 培训计划），
    掩盖真实缺件；严格匹配要求 category 一致 + 台账项全部 tags 精确命中 +
    名称主干 ≥2 字连续重合。另：hazard 是 hazardous_chemicals 的前缀，
    双向子串路由会把「危化品台账」误送隐患台账。
    """
    lib, _ = fresh_lib()
    # 路由：简称与英文 id 都必须送到正确的 bundle
    r1 = lib.checklist("危化品台账")
    r2 = lib.checklist("hazard")
    record("危化品台账简称路由到危化品bundle", r1.get("found") and r1["ledger"] == "危险化学品管理台账",
           f"got={r1.get('ledger')}")
    record("hazard 精确路由到隐患bundle", r2.get("found") and r2["ledger"] == "隐患排查治理台账",
           f"got={r2.get('ledger')}")
    # 严格匹配：对齐条目命中，只共享通用词的条目必须仍标 missing
    cl = lib.checklist("培训台账")
    by_name = {i["name"]: i for i in cl["items"]}
    record("对齐条目命中(培训计划)", by_name.get("年度安全培训计划", {}).get("matched") is not None)
    record("仅共享通用词不算命中(签到表)",
           by_name.get("培训签到表", {}).get("missing") is True,
           f"got={by_name.get('培训签到表', {}).get('matched')}")


def test_tool_manager_wiring():
    """模拟 api/main.py 的注册方式，验证经 MCPToolManager 全链路调用。"""
    from mcp.tool_manager import MCPToolManager, Tool
    lib, _ = fresh_lib()
    tm = MCPToolManager(api_key="test-key")
    tm.register(Tool(
        name="msds_lookup", description="t", handler=lib.msds_handler,
        schema={"type": "object", "properties": {"name": {"type": "string"}}},
        cache_ttl=0.0,
    ))
    tm.register(Tool(
        name="resource_search", description="t", handler=lib.resource_handler,
        schema={"type": "object", "properties": {"query": {"type": "string"}}},
    ))
    loop = asyncio.new_event_loop()
    try:
        r1 = loop.run_until_complete(tm.call("msds_lookup", {"name": "双氧水"}))
        r2 = loop.run_until_complete(tm.call("resource_search", {"query": "动火"}))
        r3 = loop.run_until_complete(tm.call("not_exist", {}))
        record("经工具管理器调用 msds_lookup", r1.success and r1.data.get("found") is True,
               f"error={r1.error}")
        record("经工具管理器调用 resource_search", r2.success and "items" in r2.data)
        record("未注册工具返回错误", r3.success is False and "工具不存在" in (r3.error or ""))
    finally:
        loop.close()


def test_main_py_registers_tools():
    """静态检查 main.py 注册代码存在（完整启动测试由 test_frontend_endpoints.py 覆盖）。"""
    src = (Path(__file__).resolve().parents[1] / "api" / "main.py").read_text(encoding="utf-8")
    record("main.py 注册 msds_lookup", '"msds_lookup"' in src and "ResourceLibrary()" in src)
    record("main.py 注册 resource_search", '"resource_search"' in src)
    record("main.py 注册 ledger_checklist", '"ledger_checklist"' in src)


if __name__ == "__main__":
    for fn in [test_seed_autoload, test_lookup_by_alias_and_cas, test_lookup_section,
               test_lookup_miss_and_validation, test_import_fill_vs_upsert, test_dedupe,
               test_hot, test_resources, test_handler_contract, test_corrupt_recovery,
               test_ledger_checklist, test_strict_alignment_and_routing,
               test_tool_manager_wiring, test_main_py_registers_tools]:
        try:
            fn()
        except Exception as ex:
            record(fn.__name__, False, f"异常: {ex}")
    passed, total = sum(RESULTS), len(RESULTS)
    print(f"\n{passed}/{total} passed")
    # 清理临时目录
    data_dir = Path(__file__).resolve().parents[1] / "data"
    for d in data_dir.glob("reslib_test_*"):
        shutil.rmtree(d, ignore_errors=True)
    sys.exit(0 if passed == total else 1)
