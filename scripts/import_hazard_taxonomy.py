"""从《隐患排查依据大全》xlsx 生成隐患定级台账配置（hazard_taxonomy）。

产出两份同内容 JSON：
  - data/resource_library/hazard_taxonomy.json  运行时配置（data/ 不入库，可现场改）
  - mcp/hazard_taxonomy_seed.json               仓库种子（随 git 分发，运行时缺省兜底）

聚合口径：按 大类 × 中类 统计 一般/重大 条数并抽取代表检查依据（该中类最常
出现的依据条款），供 core/hazard_gate.py 做规则定级与简报依据引用：
  - 14 个大类中，重大隐患集中于 易燃易爆危险作业 / 易燃易爆设施及场所 /
    热工设备设施及场所 三类（合计 125/132 条），作为"重大倾向"标记；
  - 大类关键词 = 手策同义词表 + 台账中类名（自动提取）；
  - 匹配优先级 = 手策表顺序（危险作业 > 易燃易爆场所 > 热工 > …），
    避免泛词类（如 电气/生产设备）抢走高危类的命中。

用法：.venv/Scripts/python.exe scripts/import_hazard_taxonomy.py [xlsx路径]
"""
import json
import pathlib
import sys
from collections import Counter

import openpyxl

_ROOT = pathlib.Path(__file__).parent.parent.resolve()

# 大类关键词（手策同义词，含匹配优先级：越靠前越优先命中）。
# 泛词类靠后，防止"车间配电箱打火"被 车间作业环境 抢走 电气系统 的命中。
CATEGORY_SYNONYMS = [
    ("易燃易爆危险作业", ["动火", "焊接", "切割", "气割", "受限空间", "有限空间",
                        "盲板", "抽堵", "临时用电", "高处作业", "吊装作业"]),
    ("易燃易爆设施及场所", ["罐区", "储罐", "危化品库", "易燃易爆", "防爆", "喷漆房",
                          "液化气", "气瓶", "溶剂", "加油", "库房"]),
    ("热工设备设施及场所", ["锅炉", "热工", "蒸汽", "加热炉", "导热油", "烘房", "熔炼"]),
    ("特种设备", ["特种设备", "压力容器", "压力管道", "起重", "天车", "电梯", "叉车"]),
    ("消防系统", ["消防", "灭火", "消火栓", "喷淋", "灭火器", "消防泵", "火灾报警"]),
    ("电气系统", ["电气", "配电", "电缆", "电线", "照明", "接地", "静电",
               "变压器", "开关柜", "漏电", "短路"]),
    ("工业梯台及防护栏", ["梯台", "护栏", "栏杆", "爬梯", "直梯", "防护栏", "钢平台"]),
    ("生产设备设施", ["生产设备", "机械设备", "反应器", "反应釜", "压缩机",
                   "泵", "搅拌", "传动", "输送", "阀门", "法兰", "管道裂纹", "裂纹"]),
    ("采暖与通风系统", ["采暖", "通风", "排风", "除尘", "新风"]),
    ("车间作业环境", ["作业环境", "安全通道", "疏散通道", "标识", "噪声", "车间环境"]),
    ("厂区总平面布置", ["总平面", "厂区布置", "消防车道", "防火间距", "平面布置"]),
    ("职业安全卫生", ["职业健康", "职业病", "职业禁忌", "劳动防护"]),
    ("防雷", ["防雷", "避雷", "雷击"]),
    ("选址", ["选址", "厂址"]),
]
# 重大倾向大类（xlsx 实测 125/132 条重大集中于此；命中且伴随高危信号即建议"重大"）
MAJOR_LEAN = ["易燃易爆危险作业", "易燃易爆设施及场所", "热工设备设施及场所"]


def main() -> None:
    xlsx = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else \
        _ROOT / "data" / "sources" / "2026《隐患排查依据大全》查找版 (1).xlsx"
    wb = openpyxl.load_workbook(xlsx, read_only=True)
    ws = wb.worksheets[0]
    # 聚合结构：大类 → 中类 → {major, general, basis Counter}
    agg = {}
    total = 0
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i < 2:  # 第 1 行标题、第 2 行表头
            continue
        cat = str(row[1] or "").replace("\xa0", " ").strip()
        sub = str(row[2] or "").replace("\xa0", " ").strip()
        basis = str(row[5] or "").replace("\xa0", " ").strip()
        level = str(row[6] or "").strip()
        if not cat or not sub or level not in ("一般隐患", "重大隐患"):
            continue
        total += 1
        slot = agg.setdefault(cat, {}).setdefault(sub, {"major": 0, "general": 0, "basis": Counter()})
        slot["major" if level == "重大隐患" else "general"] += 1
        if basis:
            slot["basis"][basis] += 1

    categories = []
    for name, synonyms in CATEGORY_SYNONYMS:
        subs = agg.get(name)
        if not subs:
            continue
        sub_list = []
        for sub_name, v in sorted(subs.items(), key=lambda kv: -(kv[1]["major"] + kv[1]["general"])):
            top_basis = v["basis"].most_common(1)[0][0] if v["basis"] else ""
            sub_list.append({"name": sub_name, "major": v["major"], "general": v["general"],
                             "basis": top_basis})
        # 代表依据：取大类内出现频次最高的条款（简报引用用，一条足够）
        basis_counter = Counter()
        for v in subs.values():
            for b, n in v["basis"].items():
                basis_counter[b] += n
        categories.append({
            "name": name,
            "keywords": sorted(set(synonyms) | {s for s in subs if len(s) >= 2}),
            "major_lean": name in MAJOR_LEAN,
            "major_count": sum(v["major"] for v in subs.values()),
            "general_count": sum(v["general"] for v in subs.values()),
            "basis_sample": basis_counter.most_common(1)[0][0] if basis_counter else "",
            "sub_categories": sub_list,
        })

    out = {
        "source": "2026《隐患排查依据大全》查找版",
        "row_count": total,
        "category_count": len(categories),
        "note": "重大倾向=易燃易爆危险作业/易燃易爆设施及场所/热工设备设施及场所；"
                "keywords 含手策同义词与台账中类名，匹配按数组顺序取首个命中",
        "categories": categories,
    }

    runtime = _ROOT / "data" / "resource_library" / "hazard_taxonomy.json"
    runtime.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    seed = _ROOT / "mcp" / "hazard_taxonomy_seed.json"
    seed.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    major_total = sum(c["major_count"] for c in categories)
    print(f"台账 {total} 行 / {len(categories)} 大类 / 重大 {major_total} 条")
    for c in categories:
        print(f"  {c['name']}: 一般 {c['general_count']} / 重大 {c['major_count']}"
              f"{'（重大倾向）' if c['major_lean'] else ''}")
    print(f"已写入: {runtime}")
    print(f"已写入: {seed}")


if __name__ == "__main__":
    main()
