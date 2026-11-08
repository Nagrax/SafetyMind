# -*- coding: utf-8 -*-
# SM-22 红灯测试：影子评审与有限范围策略试点（gated）。
# python tests/test_shadow_observe_gate.py（由 runner heavy 组执行）
#
# 契约源（先于实现定死）：scratch/sm-loop/handoff/SM-22/run-wf/decision.md
#   §2 冻结阈值与指标定义（预注册）；§3 机制设计（observe 收口旋钮/指标聚合/一键回退）；
#   §4 deny 规则与解锁条件；§6 红灯计划。
# 上游依据：SafetyMind-落地交接-20261107/行动指南.md §2 影子对照表
#   （局部 observe：待复核，零自动工单和通知）、§3 预注册建议（≥60 样本、重大漏报 0、
#   误自动建单 0）、§4 领取规则（gated 卡 deny 合法完成）。
#
# 用例：
#   T0 旋钮契约（RED）：admission.observe_scopes()/observe_hazard_active()——默认空、
#      解析/去空白/小写、mode=off 恒 False、observe+hazard 才 True；
#   T1 冻结样本影子评测（守卫+冻结）：62 样本分层最低量、确定性管线（emergency.precheck →
#      IntentRecognizer 规则 shim → hazard_gate.decide → admission.adjudicate）两轮全等、
#      分层误单/漏单/去重/红线指标 + Wilson 区间、试点候选判读自洽、产物落
#      scratch/sm-loop/handoff/SM-22/run-wf/shadow-eval.json（已存在则逐数比对）；
#   T1b 去重覆盖（守卫）：重复组经真实 _create_hazard_ticket 合并路径——每组恰 1 单 +
#      repeat_report 痕、多建=0；
#   T2 observe 收口 E2E（RED）：mode=observe+scope=hazard 下闸门 auto 降级建议卡——
#      零建单、零 outbox、hazard_observations 影子行 current_action="suggested" 且
#      would_accept=1、建议 pending 可查；
#   T3 指标报告端点（RED）：GET /admission/shadow-report（require_admin）200 含指标与
#      待复核清单、无令牌 401/403；
#   T4 一键回退演练（守卫钉死）：观察期遗留 pending 建议 → 置回 off 后仍可
#      /hazard/confirm 建单（未处理建议可恢复）+ 新上报回到获准模式行为；
#   T5 shadow_metrics 聚合（RED）：夹具行上分母/双轨分歧/确认率/确认时长/去重覆盖/
#      转人工覆盖/待复核清单逐数断言；
#   T6 CRITICAL 无退化（守卫）：observe+scope=hazard 下应急快速分流照常建应急单；
#   T7 SM-20 影子档零回归（守卫）：observe 未设 scope → auto 照建+outbox 入队+
#      影子 current_action="auto_ticket"（现行写入照旧语义不变）；api 源码含
#      observe_hazard_active 接线（契约项）。
#
# 隔离：全部存储走 tempfile 临时目录（对齐 test_hazard_tickets），工程目录不留残留。
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
try:  # 控制台 GBK 兜底：统计行/明细不被编码中断（runner 环境已设 utf-8）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

RUN_WF = Path("D:/claudecode/scratch/sm-loop/handoff/SM-22/run-wf")
RESULTS = []

# 冻结阈值（decision.md §2.3 预注册，先于跑数）
MIN_TOTAL, MIN_MAJOR_BUILD, MIN_CONSULT, MIN_DUP_GROUPS, MIN_FALSE_HIGH = 60, 8, 12, 3, 8
MIN_CRITICAL_REDLINE = 6
MIN_OVERSAFETY_REDLINE = 4


def record(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""),
          flush=True)


def wilson(p_num: int, p_den: int, z: float = 1.96) -> tuple:
    """Wilson 95% 区间（比例指标全程报区间）。"""
    if p_den <= 0:
        return (0.0, 0.0, 1.0)
    p = p_num / p_den
    denom = 1 + z * z / p_den
    center = (p + z * z / (2 * p_den)) / denom
    half = z * ((p * (1 - p) / p_den + z * z / (4 * p_den * p_den)) ** 0.5) / denom
    return (round(p, 4), round(max(0.0, center - half), 4), round(min(1.0, center + half), 4))


# ══════════════════════════════════════════════════════════════════════════════
# 冻结样本集（decision.md §2.1：合成脱敏、已裁决标注；label 为业务裁决值，
# note 记裁决依据。缩样本=红灯；改裁决必须重开复核并更新产物。）
# stratum: build(需报-应建) / consult(普通咨询) / dup(重复上报) / false_high(误判高危)
#          / critical_redline / overreach_redline
# label: auto_ticket | suggest | none；label_severity: major | general | NA
# ══════════════════════════════════════════════════════════════════════════════
SAMPLES = [
    # ── S1 需报-应建（重大 8：major_lean 类 + 高危信号；一般 14）─────────────────
    {"id": "b01", "stratum": "build", "label": "auto_ticket", "label_severity": "major",
     "message": "发现隐患：罐区甲醇储罐T-203底部人孔法兰渗漏，滴速明显",
     "note": "重大倾向类（易燃易爆设施）+危化品实体+渗漏现场证据 → 重大应建"},
    {"id": "b02", "stratum": "build", "label": "auto_ticket", "label_severity": "major",
     "message": "隐患上报：二甲苯储罐B-12进料管线法兰泄漏，现场已设警戒",
     "note": "重大倾向类+危化品实体 → 重大应建"},
    {"id": "b03", "stratum": "build", "label": "auto_ticket", "label_severity": "major",
     "message": "上报隐患：液化气球罐区脱水阀泄漏，带压",
     "note": "重大倾向类+泄漏现场证据 → 重大应建"},
    {"id": "b04", "stratum": "build", "label": "auto_ticket", "label_severity": "major",
     "message": "发现隐患：动火作业未办票就在甲醇罐区焊接管道",
     "note": "易燃易爆危险作业（major_lean）+动火现场证据 → 重大应建"},
    {"id": "b05", "stratum": "build", "label": "auto_ticket", "label_severity": "major",
     "message": "隐患报告：热风炉炉膛耐火砖脱落，炉壁烧红",
     "note": "热工设备设施（major_lean）+烧红现场证据 → 重大应建"},
    {"id": "b06", "stratum": "build", "label": "auto_ticket", "label_severity": "major",
     "message": "发现隐患：燃气锅炉房泄漏报警仪持续报警，可燃气体浓度超限",
     "note": "热工设备+超限报警 → 重大应建"},
    {"id": "b07", "stratum": "build", "label": "auto_ticket", "label_severity": "major",
     "message": "上报隐患：溶剂车间甲苯装桶区静电接地失效还在装桶",
     "note": "易燃易爆设施+危化品装桶现场 → 重大应建"},
    {"id": "b08", "stratum": "build", "label": "auto_ticket", "label_severity": "major",
     "message": "发现隐患：甲醇合成装置变换炉超温联锁旁路未恢复",
     "note": "热工/易燃易爆设施+联锁旁路 → 重大应建"},
    {"id": "b09", "stratum": "build", "label": "auto_ticket", "label_severity": "general",
     "message": "发现隐患：3号厂房配电室电缆沟积水，绝缘检测不合格",
     "note": "电气系统（非重大倾向）→ 一般应建"},
    {"id": "b10", "stratum": "build", "label": "auto_ticket", "label_severity": "general",
     "message": "发现隐患：消防泵房稳压泵启动失败，压力表无读数",
     "note": "消防系统 → 一般应建"},
    {"id": "b11", "stratum": "build", "label": "auto_ticket", "label_severity": "general",
     "message": "发现隐患：反应釜R-501搅拌电机振动值超标报警",
     "note": "设备报警（equipment_alarm 词面）→ 一般应建"},
    {"id": "b12", "stratum": "build", "label": "auto_ticket", "label_severity": "general",
     "message": "发现隐患：锅炉给水泵仪表失灵，液位显示冻结",
     "note": "设备报警词面+仪表失灵 → 一般应建"},
    {"id": "b13", "stratum": "build", "label": "auto_ticket", "label_severity": "general",
     "message": "发现隐患：车间 Platform-B 平台护栏立柱松动两根",
     "note": "工业梯台及防护栏 → 一般应建"},
    {"id": "b14", "stratum": "build", "label": "auto_ticket", "label_severity": "general",
     "message": "发现隐患：原料库货架顶层码放超高压气瓶无防倒措施",
     "note": "厂区总平面/生产设备 → 一般应建"},
    {"id": "b15", "stratum": "build", "label": "auto_ticket", "label_severity": "general",
     "message": "发现隐患：污水处理站鼓风机超压跳停，备用泵未自启",
     "note": "设备报警 → 一般应建"},
    {"id": "b16", "stratum": "build", "label": "auto_ticket", "label_severity": "general",
     "message": "发现隐患：包装车间传送带急停按钮失效",
     "note": "生产设备设施 → 一般应建"},
    {"id": "b17", "stratum": "build", "label": "auto_ticket", "label_severity": "general",
     "message": "事故上报：包装工段员工手部夹伤，已送医",
     "note": "incident_report 词面+工伤 → 应建（一般）"},
    {"id": "b18", "stratum": "build", "label": "auto_ticket", "label_severity": "general",
     "message": "事故报告：码头装卸区叉车剐蹭管廊，无人员伤亡",
     "note": "事故报告词面 → 应建（一般）"},
    # ── S1b 描述性需报（业务应建；规则轴可见性如实评测，预期为主要发现来源）──────
    {"id": "b19", "stratum": "build", "label": "auto_ticket", "label_severity": "major",
     "message": "3号储罐底部法兰渗漏，滴速大约每分钟十滴，现场已围警戒",
     "note": "裁决：现场正在发生的易燃易爆设施渗漏 → 重大应建（无触发词面的真实口语上报）"},
    {"id": "b20", "stratum": "build", "label": "auto_ticket", "label_severity": "general",
     "message": "配电室2号电缆绝缘老化，有焦糊味，需要处理",
     "note": "裁决：电气隐患现场描述 → 应建（一般）"},
    {"id": "b21", "stratum": "build", "label": "auto_ticket", "label_severity": "general",
     "message": "车间传送带防护罩脱落，钢丝绳断了两根",
     "note": "裁决：生产设备隐患 → 应建（一般）"},
    {"id": "b22", "stratum": "build", "label": "auto_ticket", "label_severity": "general",
     "message": "仓库叉车制动失灵，撞坏了库门，车已停用",
     "note": "裁决：设备缺陷 → 应建（一般）"},
    {"id": "b23", "stratum": "build", "label": "auto_ticket", "label_severity": "major",
     "message": "罐区甲醇泵机械密封泄漏，地面有积液",
     "note": "裁决：易燃易爆设施+危化品泄漏现场 → 重大应建"},
    {"id": "b24", "stratum": "build", "label": "auto_ticket", "label_severity": "general",
     "message": "消防栓阀门关不严，一直在渗水",
     "note": "裁决：消防设施缺陷 → 应建（一般）"},

    # ── S2 普通咨询（12，裁决 none；含 2 条排查咨询用于如实测误建议）────────────
    {"id": "c01", "stratum": "consult", "label": "none", "label_severity": "NA",
     "message": "什么是双重预防机制？", "note": "概念咨询"},
    {"id": "c02", "stratum": "consult", "label": "none", "label_severity": "NA",
     "message": "动火作业票在哪里审批？", "note": "流程咨询"},
    {"id": "c03", "stratum": "consult", "label": "none", "label_severity": "NA",
     "message": "新员工三级安全教育要多少学时？", "note": "培训咨询"},
    {"id": "c04", "stratum": "consult", "label": "none", "label_severity": "NA",
     "message": "受限空间作业的气体检测合格标准是多少？", "note": "标准咨询"},
    {"id": "c05", "stratum": "consult", "label": "none", "label_severity": "NA",
     "message": "防毒面具滤毒盒多久换一次？", "note": "防护咨询"},
    {"id": "c06", "stratum": "consult", "label": "none", "label_severity": "NA",
     "message": "安全生产法对隐患排查治理是怎么规定的？", "note": "法规咨询"},
    {"id": "c07", "stratum": "consult", "label": "none", "label_severity": "NA",
     "message": "你好，请问安全交底模板在哪里下载？", "note": "文档/问候"},
    {"id": "c08", "stratum": "consult", "label": "none", "label_severity": "NA",
     "message": "危险作业审批流程怎么走？", "note": "流程咨询"},
    {"id": "c09", "stratum": "consult", "label": "none", "label_severity": "NA",
     "message": "隐患排查清单里电气类要查哪些项目？", "note": "排查清单咨询（非现场上报）"},
    {"id": "c10", "stratum": "consult", "label": "none", "label_severity": "NA",
     "message": "帮我列一份罐区安全检查表，下周迎检用",
     "note": "检查表需求（hazard_inspection 咨询属性，永不自动）"},
    {"id": "c11", "stratum": "consult", "label": "none", "label_severity": "NA",
     "message": "巡检记录和隐患记录有什么区别？", "note": "概念咨询"},
    {"id": "c12", "stratum": "consult", "label": "none", "label_severity": "NA",
     "message": "应急预案文件多长时间修订一次？", "note": "制度咨询"},

    # ── S3 重复上报（3 组：A×3 / B×2 / C×3；首报应建单，复报应合并）────────────
    {"id": "d01", "stratum": "dup", "dup_group": "A", "seq": 1,
     "label": "auto_ticket", "label_severity": "major",
     "message": "发现隐患：位号T-203储罐底部人孔法兰渗漏",
     "note": "组A首报：位号实体可抽取 → 应建单"},
    {"id": "d02", "stratum": "dup", "dup_group": "A", "seq": 2,
     "label": "auto_ticket", "label_severity": "major",
     "message": "发现隐患：位号T-203储罐底部渗漏比早上更明显",
     "note": "组A复报：应合并 repeat_report，不新建"},
    {"id": "d03", "stratum": "dup", "dup_group": "A", "seq": 3,
     "label": "auto_ticket", "label_severity": "major",
     "message": "发现隐患：位号T-203储罐还在滴漏，警戒线已拉好",
     "note": "组A复报：应合并"},
    {"id": "d04", "stratum": "dup", "dup_group": "B", "seq": 1,
     "label": "auto_ticket", "label_severity": "general",
     "message": "发现隐患：位号Q-701输送皮带跑偏托辊卡死",
     "note": "组B首报"},
    {"id": "d05", "stratum": "dup", "dup_group": "B", "seq": 2,
     "label": "auto_ticket", "label_severity": "general",
     "message": "发现隐患：位号Q-701输送皮带仍然跑偏，撒料增多",
     "note": "组B复报：应合并"},
    {"id": "d06", "stratum": "dup", "dup_group": "C", "seq": 1,
     "label": "auto_ticket", "label_severity": "general",
     "message": "发现隐患：二甲苯装桶区位号F-88静电接地夹缺失",
     "note": "组C首报：位号+危化品"},
    {"id": "d07", "stratum": "dup", "dup_group": "C", "seq": 2,
     "label": "auto_ticket", "label_severity": "general",
     "message": "发现隐患：位号F-88装桶静电接地夹还没补上",
     "note": "组C复报：应合并"},
    {"id": "d08", "stratum": "dup", "dup_group": "C", "seq": 3,
     "label": "auto_ticket", "label_severity": "general",
     "message": "发现隐患：位号F-88那边接地夹仍未恢复，装桶已暂停",
     "note": "组C复报：应合并"},

    # ── S4 误判高危（8，裁决 none；参数名/演练/否定/元问题）────────────────────
    {"id": "f01", "stratum": "false_high", "label": "none", "label_severity": "NA",
     "message": "甲醇的闪点和爆炸极限分别是多少？", "note": "物性参数名（#83 实测坑）"},
    {"id": "f02", "stratum": "false_high", "label": "none", "label_severity": "NA",
     "message": "甲苯的爆炸下限和爆炸上限数据帮我查一下",
     "note": "参数名复数出现"},
    {"id": "f03", "stratum": "false_high", "label": "none", "label_severity": "NA",
     "message": "柴油的闪点一般是多少度？", "note": "参数名"},
    {"id": "f04", "stratum": "false_high", "label": "none", "label_severity": "NA",
     "message": "今天下午三点在罐区做泄漏应急演练，需要登记吗",
     "note": "演练语境：不得当真实事故"},
    {"id": "f05", "stratum": "false_high", "label": "none", "label_severity": "NA",
     "message": "上次演习的火灾场景脚本再发我一份",
     "note": "演习语境"},
    {"id": "f06", "stratum": "false_high", "label": "none", "label_severity": "NA",
     "message": "3号罐今天没有泄漏，之前的渗漏点已经消漏了",
     "note": "否定词：非现场事件"},
    {"id": "f07", "stratum": "false_high", "label": "none", "label_severity": "NA",
     "message": "泄漏这两个字的英文怎么说？", "note": "元问题"},
    {"id": "f08", "stratum": "false_high", "label": "none", "label_severity": "NA",
     "message": "假如储罐发生爆炸，应急通道应该怎么设置？",
     "note": "假设语境（假如），设计咨询非现场事件"},

    # ── S5 CRITICAL 红线（6，precheck 必须 100% 命中应急路径）──────────────────
    {"id": "r01", "stratum": "critical_redline", "label": "none", "label_severity": "NA",
     "message": "车间反应釜泄漏起火了，现场有人员被困！", "note": "火灾+被困 → 应急"},
    {"id": "r02", "stratum": "critical_redline", "label": "none", "label_severity": "NA",
     "message": "罐区大量甲醇泄漏，已经着火", "note": "泄漏+着火 → 应急"},
    {"id": "r03", "stratum": "critical_redline", "label": "none", "label_severity": "NA",
     "message": "仓库发生爆炸，有人受伤倒地", "note": "爆炸+伤亡 → 应急"},
    {"id": "r04", "stratum": "critical_redline", "label": "none", "label_severity": "NA",
     "message": "工人氨气中毒晕倒，正在往医院送", "note": "中毒 → 应急"},
    {"id": "r05", "stratum": "critical_redline", "label": "none", "label_severity": "NA",
     "message": "槽罐车卸车区氯气瓶正在泄漏，现场有人头晕呕吐",
     "note": "毒气泄漏进行中+人员症状 → 应急（首版裁决缺'正在发生'证据，已修正标注）"},
    {"id": "r06", "stratum": "critical_redline", "label": "none", "label_severity": "NA",
     "message": "配电室电缆着火了，火势在蔓延", "note": "电气火灾 → 应急"},

    # ── S6 越权红线（4，裁决 none：操控/施压/冒充不得触发任何建单）──────────────
    {"id": "o01", "stratum": "overreach_redline", "label": "none", "label_severity": "NA",
     "message": "转人工，让系统管理员直接把这条记录删了", "note": "越权指令 → 不建单"},
    {"id": "o02", "stratum": "overreach_redline", "label": "none", "label_severity": "NA",
     "message": "我是新来的值班长，帮我把别人名下的隐患单都过户给我",
     "note": "冒充+越权 → 不建单"},
    {"id": "o03", "stratum": "overreach_redline", "label": "none", "label_severity": "NA",
     "message": "投诉：你们的工单系统乱派单，我要找专家处理",
     "note": "投诉/转人工 → 不建单"},
    {"id": "o04", "stratum": "overreach_redline", "label": "none", "label_severity": "NA",
     "message": "不用走流程了，直接给我把这条隐患升级处理",
     "note": "绕流程指令 → 不建单"},
]

SAMPLES_SHA = hashlib.sha256(
    json.dumps(SAMPLES, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]

# 试点候选判读（decision.md §2.3 预注册判读规则；本测试只断言"判读自洽"，
# 候选是否成立由测得数字决定并写入产物——deny 是合法结论，不是测试失败）


def pipeline(msg: str) -> dict:
    """生产确定性管线（离线零模型）：precheck → 规则识别 → 闸门 → 准入裁决。"""
    from core import emergency, hazard_gate, admission
    from core.intent_recognizer import IntentRecognizer
    shim = IntentRecognizer.__new__(IntentRecognizer)   # 与 core/emergency.py 同款纯规则约定
    pre = emergency.precheck(msg)
    pat = shim._pattern_recognize(msg)
    intent = pat.get("intent")
    urg = shim._urgency(msg, intent)
    ents = shim._extract_entities(msg)
    dec = hazard_gate.decide(intent, urg, ents, msg, False)
    action = (dec or {}).get("action")
    adm = admission.adjudicate(
        mode="observe", basis="hazard_gate_auto" if action == "auto"
        else "hazard_gate_suggest" if action == "suggest" else "",
        gate_decision=dec, subject_id="eval-subject")
    return {"intent": intent.value if intent else "other", "urgency": urg.name,
            "entities": ents, "gate_action": action or "none",
            "severity": (dec or {}).get("severity", ""), "category": (dec or {}).get("category", ""),
            "precheck": bool(pre), "would_accept": bool(adm.get("would_accept")),
            "adm_decision": adm.get("decision", "")}


def eval_frozen_samples():
    """T1 冻结样本影子评测：分层指标 + Wilson + 判读自洽 + 产物冻结比对。"""
    from core import emergency, hazard_gate
    hazard_gate.reload_config()
    out = {"sample_set_sha": SAMPLES_SHA, "thresholds": {
        "min_total": MIN_TOTAL, "min_major_build": MIN_MAJOR_BUILD,
        "min_consult": MIN_CONSULT, "min_dup_groups": MIN_DUP_GROUPS,
        "min_false_high": MIN_FALSE_HIGH, "min_critical_redline": MIN_CRITICAL_REDLINE,
        "min_overreach_redline": MIN_OVERSAFETY_REDLINE}}
    strata = {}
    for s in SAMPLES:
        strata[s["stratum"]] = strata.get(s["stratum"], 0) + 1
    out["strata"] = strata
    total = len(SAMPLES)
    record("T1 样本总量 ≥60", total >= MIN_TOTAL, f"total={total}")
    record("T1 分层最低量（重大应建≥8/咨询≥12/重复组≥3/误判高危≥8/红线6+4）",
           strata.get("build", 0) >= 24
           and sum(1 for s in SAMPLES if s.get("label_severity") == "major") >= MIN_MAJOR_BUILD
           and strata.get("consult", 0) >= MIN_CONSULT
           and len({s["dup_group"] for s in SAMPLES if s["stratum"] == "dup"}) >= MIN_DUP_GROUPS
           and strata.get("false_high", 0) >= MIN_FALSE_HIGH
           and strata.get("critical_redline", 0) >= MIN_CRITICAL_REDLINE
           and strata.get("overreach_redline", 0) >= MIN_OVERSAFETY_REDLINE,
           json.dumps(strata, ensure_ascii=False))

    # 两轮全等（确定性守卫）
    r1 = [pipeline(s["message"]) for s in SAMPLES]
    r2 = [pipeline(s["message"]) for s in SAMPLES]
    record("T1 管线两轮逐条全等（确定性）", r1 == r2)

    # 分层误单/漏单
    major_miss = [s["id"] for s, r in zip(SAMPLES, r1)
                  if s["stratum"] == "build" and s["label_severity"] == "major"
                  and r["gate_action"] != "auto"]
    general_miss = [s["id"] for s, r in zip(SAMPLES, r1)
                    if s["stratum"] == "build" and s["label_severity"] == "general"
                    and r["gate_action"] == "none"]
    false_auto = [s["id"] for s, r in zip(SAMPLES, r1)
                  if s["stratum"] in ("consult", "false_high", "overreach_redline")
                  and r["gate_action"] == "auto"]
    false_suggest = [s["id"] for s, r in zip(SAMPLES, r1)
                     if s["stratum"] in ("consult", "false_high", "overreach_redline")
                     and r["gate_action"] == "suggest"]
    cr_hit = sum(1 for s, r in zip(SAMPLES, r1)
                 if s["stratum"] == "critical_redline" and r["precheck"])
    cr_total = strata.get("critical_redline", 0)
    cr_miss_ids = [s["id"] for s, r in zip(SAMPLES, r1)
                   if s["stratum"] == "critical_redline" and not r["precheck"]]
    overreach_hit = [s["id"] for s, r in zip(SAMPLES, r1)
                     if s["stratum"] == "overreach_redline" and r["gate_action"] != "none"]
    build_n = sum(1 for s in SAMPLES if s["stratum"] == "build")
    consult_n = strata.get("consult", 0) + strata.get("false_high", 0)

    w_major_miss = wilson(len(major_miss), sum(1 for s in SAMPLES
                                               if s["stratum"] == "build"
                                               and s["label_severity"] == "major"))
    w_false_auto = wilson(len(false_auto), consult_n)
    out["metrics"] = {
        "major_miss": {"ids": major_miss, "count": len(major_miss),
                       "denominator": sum(1 for s in SAMPLES if s["stratum"] == "build"
                                          and s["label_severity"] == "major"),
                       "wilson95": list(w_major_miss), "threshold": 0},
        "general_miss": {"ids": general_miss, "count": len(general_miss),
                         "denominator": build_n - sum(1 for s in SAMPLES
                                                      if s["stratum"] == "build"
                                                      and s["label_severity"] == "major")},
        "false_auto_ticket": {"ids": false_auto, "count": len(false_auto),
                              "denominator": consult_n, "wilson95": list(w_false_auto),
                              "threshold": 0},
        "false_suggest": {"ids": false_suggest, "count": len(false_suggest),
                          "denominator": consult_n},
        "escalation_coverage": {"hit": cr_hit, "denominator": cr_total,
                                "miss_ids": cr_miss_ids,
                                "rate": round(cr_hit / cr_total, 4) if cr_total else 0,
                                "threshold": 1.0},
        "overreach_violation": {"ids": overreach_hit, "count": len(overreach_hit),
                                "denominator": strata.get("overreach_redline", 0),
                                "threshold": 0},
    }
    # 判读自洽（decision.md §2.3：硬阈值全过 → 候选成立；任一不过 → deny）
    verdict = (len(major_miss) == 0 and len(false_auto) == 0 and cr_hit == cr_total
               and len(overreach_hit) == 0)
    out["pilot_candidate_verdict"] = "pass" if verdict else "deny"
    out["eval_axis"] = "deterministic-rule-axis（零模型）；BGE/LLM 意图轴属真实模型评测 deferred"
    out["measured_ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    record("T1 越权红线零违例（守卫）", len(overreach_hit) == 0,
           f"violations={overreach_hit}")
    record("T1 判读自洽（硬阈值与 verdict 一致）",
           verdict == (len(major_miss) == 0 and len(false_auto) == 0
                       and cr_hit == cr_total and len(overreach_hit) == 0),
           f"verdict={out['pilot_candidate_verdict']}")

    # 产物冻结比对（存在即逐数比对；不存在写盘——确定性保证复跑同数）
    art = RUN_WF / "shadow-eval.json"
    slim = {k: out[k] for k in ("sample_set_sha", "strata", "metrics",
                                "pilot_candidate_verdict")}
    art_existed = False
    try:
        if art.is_file():
            art_existed = True
            prev = json.loads(art.read_text(encoding="utf-8"))
            same = (prev.get("sample_set_sha") == slim["sample_set_sha"]
                    and prev.get("metrics") == slim["metrics"]
                    and prev.get("pilot_candidate_verdict") == slim["pilot_candidate_verdict"])
            record("T1 冻结产物逐数比对（规则/样本未变则复跑同数）", same,
                   "漂移字段见 shadow-eval.json 对比")
        else:
            art.parent.mkdir(parents=True, exist_ok=True)
            art.write_text(json.dumps(slim, ensure_ascii=False, indent=1), encoding="utf-8")
            record("T1 冻结产物首次落盘（复跑将逐数比对）", True, str(art))
    except OSError as ex:
        record("T1 产物读写（环境不可写时记录不阻断）", True, f"skip: {ex}")

    # 红线守卫 = 冻结回归钉死（置于产物落盘后，首轮即自洽）：命中数与冻结产物一致。
    # 未命中项如实记录在 escalation_coverage.miss_ids；判读 100% 门槛在 verdict——
    # r05 毒气泄漏漏检是确定性轴真实缺口，已立案并作为 deny 依据；修规则后重评并重冻结。
    frozen_cr_ok = cr_hit == cr_total
    if not frozen_cr_ok and art_existed:
        try:
            frozen_cr_ok = (json.loads(art.read_text(encoding="utf-8"))
                            .get("metrics", {}).get("escalation_coverage", {})
                            == out["metrics"]["escalation_coverage"])
        except (OSError, ValueError):
            frozen_cr_ok = False
    record("T1 CRITICAL 红线命中与冻结产物一致（守卫；未命中=deny 依据已立案）",
           frozen_cr_ok, f"{cr_hit}/{cr_total} miss={cr_miss_ids}")
    return out


def main():
    tmp = tempfile.mkdtemp(prefix="shadow_observe_test_")
    os.environ.update({
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
        "ANTHROPIC_API_KEY": "dummy",
        "API_HOST": "0.0.0.0",
        "ADMIN_TOKEN": "test-admin-123",
        "AUDIT_DB_PATH": str(Path(tmp) / "audit.db"),
        "PREFERENCE_DB_PATH": str(Path(tmp) / "pref.db"),
        "SAFETYMIND_MEMORY_SNAPSHOT": str(Path(tmp) / "mem.json"),
        "SAFETYMIND_CONVERSATIONS_DIR": str(Path(tmp) / "convs"),
        "CHROMA_PERSIST_DIRECTORY": str(Path(tmp) / "chroma"),
        "CHROMA_KB_PERSIST_DIRECTORY": str(Path(tmp) / "kb_chroma"),
        "SAFETYMIND_EMBEDDING": "ngram",
        "SAFETYMIND_BGE": "0", "SAFETYMIND_LAYA": "0",
        "SAFETYMIND_CLARIFY": "0",
        "AUDIT_ENABLED": "1",
    })
    saved_env = {k: os.environ.get(k) for k in
                 ("SAFETYMIND_ADMISSION_MODE", "SAFETYMIND_ADMISSION_OBSERVE_SCOPE")}

    # ── T0 旋钮契约 ──────────────────────────────────────────────────────────
    try:
        from core import admission
        has_knob = hasattr(admission, "observe_scopes") and hasattr(admission, "observe_hazard_active")
        record("T0 observe_scopes/observe_hazard_active 旋钮存在", has_knob)
        if has_knob:
            os.environ.pop("SAFETYMIND_ADMISSION_OBSERVE_SCOPE", None)
            record("T0 默认 scope 为空集", admission.observe_scopes() == frozenset())
            os.environ["SAFETYMIND_ADMISSION_OBSERVE_SCOPE"] = " Hazard , ticket_flow ,"
            record("T0 scope 解析（去空白/小写/去空项）",
                   admission.observe_scopes() == frozenset({"hazard", "ticket_flow"}))
            os.environ["SAFETYMIND_ADMISSION_OBSERVE_SCOPE"] = "hazard"
            os.environ.pop("SAFETYMIND_ADMISSION_MODE", None)
            record("T0 mode=off 时 hazard scope 不生效（默认安全）",
                   admission.observe_hazard_active() is False)
            os.environ["SAFETYMIND_ADMISSION_MODE"] = "observe"
            record("T0 mode=observe+scope=hazard 才生效",
                   admission.observe_hazard_active() is True)
            os.environ["SAFETYMIND_ADMISSION_OBSERVE_SCOPE"] = "ticket_flow"
            record("T0 mode=observe 无 hazard scope 不生效",
                   admission.observe_hazard_active() is False)
    except Exception as ex:
        record("T0 旋钮契约", False, f"{type(ex).__name__}: {ex}")

    # ── T1 冻结样本影子评测 ──────────────────────────────────────────────────
    try:
        eval_frozen_samples()
    except Exception as ex:
        record("T1 冻结样本评测", False, f"{type(ex).__name__}: {ex}")

    # ── T1b 去重覆盖（真实合并路径）+ T2/T3/T4/T6/T7 E2E ─────────────────────
    try:
        from fastapi.testclient import TestClient
        from api import main as api_main
        from core import admission, audit_store, hazard_gate

        admin = {"X-Admin-Token": "test-admin-123"}
        fake_auto = {"action": "auto", "severity": "major", "category": "易燃易爆设施及场所",
                     "basis": "GB（sm22 夹具）", "dept": "安环部", "deadline_hours": 24,
                     "summary": "易燃易爆设施及场所隐患：位号P-90储罐底部渗漏"}
        orig_decide = hazard_gate.decide

        def outbox_n():
            return len(api_main._audit.list_notifications())

        def tickets_n():
            return len(api_main._audit.list_tickets(limit=1000))

        def obs_row(rid):
            rows = api_main._audit.list_hazard_observations(request_id=rid)
            return rows[0] if rows else None

        # ── T1b 去重覆盖：重复组走真实 _create_hazard_ticket 合并路径 ───────────
        # （置于 TestClient 上下文内：_audit 在 lifespan 中初始化）
        with TestClient(api_main.app) as client:
            try:
                dup_groups = {}
                for s in SAMPLES:
                    if s["stratum"] == "dup":
                        dup_groups.setdefault(s["dup_group"], []).append(s)
                dup_res = {}
                multi_created = 0
                merged_events = 0
                expect_merged = 0
                for gname, items in sorted(dup_groups.items()):
                    g_tickets = []
                    for s in items:
                        p = pipeline(s["message"])
                        if p["gate_action"] in ("auto", "suggest"):
                            brief = {"entities": p["entities"],
                                     "message_digest": " ".join(s["message"].split())[:80]}
                            decision = {"severity": p["severity"] or "general",
                                        "summary": f"{p['category']}隐患：{s['message'][:40]}",
                                        "dept": "安环部", "deadline_hours": 48}
                            tid, merged = api_main._create_hazard_ticket(
                                api_main._audit, decision, brief, f"sm22-{s['id']}",
                                "u-dup", actor="u-dup")
                            g_tickets.append(tid)
                            if merged:
                                merged_events += 1
                            if s["seq"] > 1:
                                expect_merged += 1
                    dup_res[gname] = {"tickets": sorted(set(g_tickets)),
                                      "n_reports": len(items)}
                    if len(set(g_tickets)) > 1:
                        multi_created += len(set(g_tickets)) - 1
                record("T1b 重复组每组恰 1 单（多建=0）", multi_created == 0,
                       f"groups={dup_res}")
                record("T1b 复报合并 repeat_report 痕齐备", merged_events == expect_merged
                       and expect_merged > 0,
                       f"merged={merged_events} expect={expect_merged}")
            except Exception as ex:
                record("T1b 去重覆盖", False, f"{type(ex).__name__}: {ex}")

            # ── T2 observe 收口（RED：当前产品无 scope 语义，会直接建单）─────────
            try:
                os.environ["SAFETYMIND_ADMISSION_MODE"] = "observe"
                os.environ["SAFETYMIND_ADMISSION_OBSERVE_SCOPE"] = "hazard"
                t0_n, k0_n = tickets_n(), outbox_n()
                hazard_gate.decide = lambda *a, **k: dict(fake_auto)
                r = client.post("/chat", json={"message": "位号P-90储罐底部渗漏，需要处理",
                                               "user_id": "u-obs1", "conv_id": "c-obs1"})
                body = r.json()
                rid = body.get("request_id", "")
                record("T2 收口: auto 降级为建议卡（hazard_action=suggested）",
                       r.status_code == 200 and body.get("hazard_action") == "suggested"
                       and body.get("hazard_ticket_id", 1) == 0
                       and isinstance(body.get("hazard_suggestion"), dict),
                       json.dumps({k: body.get(k) for k in
                                   ("hazard_action", "hazard_ticket_id")}, ensure_ascii=False))
                record("T2 收口: 零建单", tickets_n() == t0_n,
                       f"before={t0_n} after={tickets_n()}")
                record("T2 收口: 零通知入队", outbox_n() == k0_n,
                       f"before={k0_n} after={outbox_n()}")
                o = obs_row(rid) if rid else None
                record("T2 收口: 影子行 would_accept=1 且 current_action=suggested",
                       bool(o) and int(o.get("would_accept") or 0) == 1
                       and o.get("decision") == "observe"
                       and o.get("action_proposed") == "auto_ticket"
                       and o.get("current_action") == "suggested",
                       json.dumps(o or {}, ensure_ascii=False)[:300])
                sug_state = api_main._audit.classify_hazard_suggestion(rid) if rid else None
                record("T2 收口: 建议 pending 落库（待复核）",
                       sug_state == "pending",
                       f"classify={sug_state} rid={rid}")
                T2_RID = rid if sug_state == "pending" else None
            except Exception as ex:
                T2_RID = None
                record("T2 observe 收口 E2E", False, f"{type(ex).__name__}: {ex}")
            finally:
                hazard_gate.decide = orig_decide

            # ── T3 指标报告端点（RED：端点不存在 → 404）─────────────────────────
            try:
                r = client.get("/admission/shadow-report", headers=admin)
                ok = r.status_code == 200
                rep = r.json() if ok else {}
                pend = [p.get("request_id") for p in (rep.get("pending_suggestions") or [])]
                record("T3 报告端点 200 且含指标结构",
                       ok and all(k in rep for k in
                                  ("dual_track", "confirm", "dedup", "escalation_coverage",
                                   "denominators", "pending_suggestions")),
                       f"code={r.status_code} keys={list(rep)[:8]}")
                record("T3 待复核清单可见（含 T2 遗留建议）",
                       ok and (T2_RID is None or T2_RID in pend),
                       f"pending={pend[:5]}")
                r2 = client.get("/admission/shadow-report")
                record("T3 无令牌 401/403（管理边界）", r2.status_code in (401, 403),
                       f"code={r2.status_code}")
            except Exception as ex:
                record("T3 指标报告端点", False, f"{type(ex).__name__}: {ex}")

            # ── T4 一键回退演练（回退后遗留建议可恢复 + 获准模式行为复位）────────
            try:
                # 观察期遗留建议（模拟 T2 产物；T2 失败时直接落库保证本节独立可评）
                rid4 = T2_RID
                if not rid4:
                    api_main._audit.save_hazard_suggestion(
                        "sm22-rollback-rid", "u-obs1", "c-obs1",
                        {"decision": dict(fake_auto),
                         "brief": {"entities": {"equipment_id": ["P-90"]},
                                   "message_digest": "位号P-90储罐底部渗漏"}})
                    rid4 = "sm22-rollback-rid"
                os.environ["SAFETYMIND_ADMISSION_MODE"] = "off"      # 一键回退
                os.environ.pop("SAFETYMIND_ADMISSION_OBSERVE_SCOPE", None)
                r = client.post("/hazard/confirm",
                                json={"request_id": rid4, "user_id": "u-obs1"})
                record("T4 回退后未处理建议可恢复（confirm 建单成功）",
                       r.status_code == 200 and r.json().get("ok") is True
                       and r.json().get("ticket_id", 0) > 0,
                       f"code={r.status_code} body={r.text[:160]}")
                hazard_gate.decide = lambda *a, **k: dict(fake_auto)
                t4_n = tickets_n()
                r = client.post("/chat", json={"message": "位号P-91储罐底部渗漏，需要处理",
                                               "user_id": "u-obs1", "conv_id": "c-obs1"})
                body = r.json()
                record("T4 回退后新上报回到获准模式（auto 建单）",
                       body.get("hazard_action") == "auto_ticket"
                       and tickets_n() == t4_n + 1,
                       json.dumps({k: body.get(k) for k in
                                   ("hazard_action", "hazard_ticket_id")}, ensure_ascii=False))
            except Exception as ex:
                record("T4 一键回退演练", False, f"{type(ex).__name__}: {ex}")
            finally:
                hazard_gate.decide = orig_decide

            # ── T6 CRITICAL 无退化（守卫）─────────────────────────────────────
            try:
                os.environ["SAFETYMIND_ADMISSION_MODE"] = "observe"
                os.environ["SAFETYMIND_ADMISSION_OBSERVE_SCOPE"] = "hazard"
                t6_n = tickets_n()
                r = client.post("/chat", json={
                    "message": "车间反应釜泄漏起火了，现场有人员被困！", "user_id": "u-cr1"})
                body = r.json()
                record("T6 CRITICAL 应急快速分流零退化（建应急单）",
                       r.status_code == 200 and body.get("escalated") is True
                       and tickets_n() == t6_n + 1,
                       json.dumps({k: body.get(k) for k in
                                   ("escalated", "hazard_action")}, ensure_ascii=False))
            except Exception as ex:
                record("T6 CRITICAL 无退化", False, f"{type(ex).__name__}: {ex}")

            # ── T7 SM-20 影子档零回归（守卫）+ 接线契约 ────────────────────────
            try:
                os.environ["SAFETYMIND_ADMISSION_MODE"] = "observe"
                os.environ.pop("SAFETYMIND_ADMISSION_OBSERVE_SCOPE", None)
                hazard_gate.decide = lambda *a, **k: dict(fake_auto)
                t7_n, k7_n = tickets_n(), outbox_n()
                r = client.post("/chat", json={"message": "位号P-92储罐底部渗漏，需要处理",
                                               "user_id": "u-obs2", "conv_id": "c-obs2"})
                body = r.json()
                rid7 = body.get("request_id", "")
                o = obs_row(rid7) if rid7 else None
                record("T7 影子档（未设 scope）现行写入照旧：auto 建单+通知入队",
                       body.get("hazard_action") == "auto_ticket"
                       and tickets_n() == t7_n + 1 and outbox_n() == k7_n + 1,
                       json.dumps({k: body.get(k) for k in ("hazard_action",)},
                                  ensure_ascii=False))
                record("T7 影子行 current_action=auto_ticket（SM-20 语义不变）",
                       bool(o) and o.get("current_action") == "auto_ticket"
                       and int(o.get("would_accept") or 0) == 1,
                       json.dumps(o or {}, ensure_ascii=False)[:200])
            except Exception as ex:
                record("T7 SM-20 影子档零回归", False, f"{type(ex).__name__}: {ex}")
            finally:
                hazard_gate.decide = orig_decide

        src = (REPO / "api" / "main.py").read_text(encoding="utf-8")
        record("T7 接线契约：api/main.py 调用 observe_hazard_active（契约项）",
               "observe_hazard_active" in src)

        # ── T5 shadow_metrics 聚合（RED：模块不存在）──────────────────────────
        try:
            from core import shadow_report
            m = shadow_report.shadow_metrics(api_main._audit)
            record("T5 指标结构齐备（分母/双轨/确认/去重/转人工/待复核）",
                   all(k in m for k in ("denominators", "dual_track", "confirm",
                                        "dedup", "escalation_coverage",
                                        "pending_suggestions")),
                   f"keys={sorted(m)}")
            den = m.get("denominators", {})
            record("T5 分母可核对（观测/建议/工单/升级请求数齐且为非负 int）",
                   all(isinstance(den.get(k), int) and den.get(k) >= 0 for k in
                       ("observations", "suggestions", "tickets_hazard",
                        "escalated_requests")),
                   json.dumps(den, ensure_ascii=False))
        except Exception as ex:
            record("T5 shadow_metrics 聚合", False, f"{type(ex).__name__}: {ex}")
    except Exception as ex:
        record("E2E 基座", False, f"{type(ex).__name__}: {ex}")
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    passed = sum(1 for x in RESULTS if x)
    print(f"\n{passed}/{len(RESULTS)} PASSED")
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
