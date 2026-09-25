# BGE 原型分类器（SOTA 配置）：中文短描述+全定义+模板+手写示例 → 原型均值 → 余弦 softmax
# 证据: laya_eval/RESULTS.md — test n=1039 acc 92.6%（large+base 0.7/0.3 集成），零死类
# 环境开关: SAFETYMIND_BGE=1（默认 off）；SAFETYMIND_BGE_LARGE=1 加载 large 并与 base 0.7/0.3 集成
import os
import math
from typing import Callable, Dict, Optional, Tuple

INSTR = ""  # 实测指令前缀有害（dev 82.5 vs 82.9）

# ── 示例库（AI 生成自标注；模板 54 + 手写 Round1 95 + Round2 错误驱动定向 20）──
HAND = {
 "equipment_alarm": ["造粒机模头温度报警响了，现在要怎么操作？","中间罐液位高报警连锁启动了，先做哪一步？","循环水泵出口压力低报警，岗位工该怎么应对？","一氧化碳探头报警持续响，值班人员处置顺序是什么？","蒸汽管网压力骤降报警，中控要做什么？"],
 "equipment_maintenance": ["罗茨风机齿轮箱异响，检修方案怎么定？","板式换热器压差变大，什么时候安排清洗检修？","年度大修中阀门解体检查的重点是什么？","减速机油封渗油，日常保养怎么处理？","仪表风压缩机保养周期表怎么编制？"],
 "hazard_report": ["楼梯间堆了废纸箱，我要把这个隐患报上去","管道保温层破损了一块，隐患上报给谁？","车间门口的应急灯坏了，怎么报隐患？","地沟盖板缺了一块，这个隐患怎么走流程？","灭火器压力指针在红区，我上报个隐患"],
 "hazard_inspection": ["防爆区域的隐患排查要重点查什么？","设备巡检和隐患排查有什么区别？","月度排查表里电气部分要填哪些项？","新员工参与隐患排查要注意什么？","雨季前排查重点是哪几项？"],
 "incident_report": ["昨晚发生一起叉车撞人事故，上报流程怎么走？","职工手部被压伤属于什么等级事故，怎么报？","发生化学灼伤事故后，报告时限是多久？","外包单位出了未遂事故，要不要按事故上报？","一起轻微火灾事故的调查上报程序是什么？"],
 "emergency_response": ["液氯钢瓶针阀泄漏正在扩散，现场怎么应急？","仓库起火有两人被困，先做什么后做什么？","有人氮气窒息倒在坑内，现场救援怎么组织？","丙烯罐区管线刺漏喷料，紧急处置措施？","配电室电缆着火冒浓烟，第一步处置是什么？"],
 "work_permit": ["罐区旁要动火焊接，动火作业票怎么申请？","进反应釜清理作业，受限空间票怎么办？","临时用电作业票的有效期和延期怎么规定？","高处作业票审批要哪些部门签字？","吊装作业票办理前要做哪些准备？"],
 "regulation_query": ["安全生产法第一百零二条的内容是什么？","危险化学品目录2015版在哪里查？","GB30871对受限空间作业的规定是哪几条？","生产经营项目场所发包的安全管理条款怎么查？","特种作业人员的安全培训考核规定出自哪个文件？"],
 "chemical_safety": ["双氧水和丙酮能不能放在一个库房？","液碱储罐的材质选择有什么安全要求？","甲醇的闪点和储存温度要求是什么？","危化品废弃包装桶怎么处置管理？","硝酸与有机物存放的安全距离要求？"],
 "training_cert": ["压力容器操作证复审要提前多久申请？","车间级安全培训一般包含哪些内容？","安全管理员证年审需要什么材料？","新入职员工厂级培训学时是多少？","转岗人员的重新培训要求是什么？"],
 "inspection_audit": ["省里标准化评审前，班组要做哪些准备？","外审老师要查隐患整改闭环资料怎么整理？","迎检前现场标识牌要按什么标准布置？","安全生产标准化三级升二级的流程是什么？","检查组来之前的自评报告怎么写？"],
 "occupational_health": ["噪声岗位员工几年做一次听力检查？","接触苯的员工职业健康体检查什么项目？","粉尘岗位的职业病危害告知卡写什么？","职业健康监护档案保存多少年？","岗前体检发现职业禁忌证怎么办？"],
 "ppe_inquiry": ["打磨作业应该戴什么类型的护目镜？","接触氢氟酸用什么材质的防护手套？","防尘口罩的更换周期是多久？","喷漆作业选什么型号的防毒面具？","全身式安全带的使用期限和检查要求？"],
 "safety_document": ["变更管理程序的修订流程怎么规定？","操作规程的编写大纲包括哪些章节？","专项应急预案和现场处置方案怎么衔接？","安全生产责任制度的发文和宣贯怎么走？","受控文件的作废回收怎么管理？"],
 "general_consult": ["安全检查表法是什么意思？","能本原理在安全管理中怎么理解？","事故致因理论主要有哪几类？","安全冗余设计是个什么概念？","风险分级管控和隐患排查是什么关系？"],
}
HAND2 = {
 "other": ["讲一段长城的历史", "帮我算一下美元汇率", "明天有没有足球比赛", "推荐几首好听的歌", "帮我写一段商品广告词", "今天股市收盘多少"],
 "greeting": ["哈喽哈喽", "各位下午好呀", "冒个泡", "路过问声好", "签到"],
 "escalation": ["这个必须转你们主管处理", "叫你们领导出面解释", "我不接受机器答复，转人工"],
 "regulation_query": ["《生产安全事故报告和调查处理条例》的适用范围是哪几条？", "事故等级划分的法律依据是哪个条款？", "查一下安全生产条例里事故报告时限的条文", "重大危险源辨识标准的发布文号是什么？"],
 "occupational_health": ["职业健康检查机构的资质要求是什么？", "职业中毒的诊断鉴定去哪个机构？"],
}
HAND5 = {
 "other": ["陪我聊会天", "讲个笑话听听", "现在几点了", "帮我推荐部电影", "咱们随便聊聊"],
 "feedback": ["赞一个，讲得很好", "按你说的方案办", "收到，很有帮助", "不错不错，就是这个意思"],
 "regulation_query": ["体检周期在《职业病防治法》里是怎么写的？", "安全评价的备案要求出自哪个法规条文？", "安全生产费用提取的法规依据是哪一条？", "安全设施设计审查的法规条文在哪里"],
 "occupational_health": ["车间要组织接害岗位员工做职业健康体检", "员工的职业健康复查流程怎么走", "职业健康体检发现异常后的调岗怎么处理"],
 "inspection_audit": ["迎检前隐患整改台账怎么准备", "标准化评审现场的定置管理要求", "外部检查前的汇报材料清单怎么列"],
 "hazard_inspection": ["隐患分级挂牌督办的标准是什么", "排查问题整改闭环的流程是什么"],
 "safety_document": ["检维修规程修订的启动条件和审批流程", "操作规程与现场不符时怎么修订"],
 "ppe_inquiry": ["防静电工作服的检测周期是多少", "护耳器的报废和更换标准", "接触苯岗位的滤毒盒型号怎么选"],
 "equipment_alarm": ["可燃气体探测器报警但确认无泄漏，按报警处置流程操作"],
 "work_permit": ["动火作业前气体分析的时间间隔和票面要求"],
}
HAND3 = {
 "other": ["讲首歌听", "你会写代码吗", "帮我算下这道数学题", "明天天气适合晾被子吗", "给我讲个鬼故事", "推荐个手机型号"],
 "feedback": ["这就去办，谢谢", "赞一个", "OK的，收到", "按你说的办", "好使，谢了"],
 "greeting": ["你好，想咨询个事", "冒昧问一下", "在吗？想问点事"],
 "escalation": ["这事必须人工处理，别的我不认"],
 "regulation_query": ["职业病防治法对体检周期的规定是第几条？", "体检相关条款在职业病防治法哪一条？", "查一下危化品储存标准的条文编号"],
 "occupational_health": ["车间噪声大，员工的职业健康检查多久做一次？", "接害岗位的职业健康监护怎么做？"],
 "safety_document": ["检维修规程的修订由谁审批？", "隐患排查治理制度的编写大纲有哪些？", "编制检修规程时要注意什么？"],
 "equipment_maintenance": ["检修规程执行中发现漏点，先隔离还是先报修？", "检维修外包队伍的资质怎么审？"],
 "hazard_inspection": ["隐患排查制度和标准化考核的关系是什么？", "迎检前的自查排查表怎么设计？"],
 "inspection_audit": ["标准化考核里对培训学时的评分标准是什么？", "考核标准里培训档案占几分？", "检查组要的双重预防机制运行材料清单"],
 "training_cert": ["特种作业人员体检查什么，是培训取证的条件吗？", "培训档案归哪个部门管理？"],
 "equipment_alarm": ["可燃气体报警器响了但没看到泄漏，这报警怎么处置？", "DCS温度报警响起，处理的是报警不是现场火情"],
 "hazard_report": ["检修时发现漏点还没处理，先报个隐患登记", "迎检自查发现的隐患要不要立即上报登记？"],
}
GENERIC_NOISE = {"general_consult"}

def build_bank(intent_category_cls, templates, zh_key, zh_short, definitions) -> Dict[str, list]:
    """构建示例库: 模板 + 手写 Round1/Round2 + (交互类回退) 短描述/定义。返回 {intent_value: [texts]}"""
    en2zh = {v: k for k, v in zh_key.items()}
    bank = {}
    for c in intent_category_cls:
        v = c.value
        entries = list(templates.get(c, []))
        entries += HAND.get(v, []) + HAND2.get(v, []) + HAND3.get(v, []) + HAND5.get(v, [])
        if not entries:  # 交互类回退
            zh = en2zh[v]
            entries = [zh_short[zh], definitions[v]]
        bank[v] = [t[:64] for t in entries]
    return bank

def fuse_ens(probs_list, weights):
    """多模型概率加权集成。纯函数。"""
    score = {}
    for probs, w in zip(probs_list, weights):
        for k, v in (probs or {}).items():
            score[k] = score.get(k, 0.0) + w * float(v)
    return score

def fuse_with_pattern(ens_score: Dict[str, float], pattern_cls: Optional[str],
                      pattern_conf: float, weight: float = 0.15,
                      exclude: Optional[set] = None) -> Tuple[Optional[str], float]:
    """BGE 集成分 + pattern 弱补（消融坐标下降最优: pattern 权重 0.1-0.2）。"""
    exclude = exclude or set()
    score = dict(ens_score)
    total = sum(score.values())
    if pattern_cls and pattern_cls not in exclude:
        c = max(0.0, min(1.0, float(pattern_conf)))
        score[pattern_cls] = score.get(pattern_cls, 0.0) + weight * c
        total += weight * c
    if not score or total <= 0:
        return None, 0.0
    best = max(score, key=score.get)
    return best, score[best] / total


class BGEProtoClassifier:
    """bge 原型分类器（懒加载）。large+base 集成或单 base。"""

    def __init__(self, temperature: float = 0.03, tau_direct: float = 0.80,
                 use_large: bool = False, device: str = "cpu"):
        self.temperature = temperature
        self.tau_direct = tau_direct
        self.use_large = use_large
        self.device = device
        self._models = None  # [(name, tok, model)]

    @classmethod
    def from_env(cls) -> Optional["BGEProtoClassifier"]:
        if os.getenv("SAFETYMIND_BGE", "off") != "1":
            return None
        return cls(
            temperature=float(os.getenv("SAFETYMIND_BGE_TEMP", "0.03")),
            tau_direct=float(os.getenv("SAFETYMIND_BGE_TAU", "0.80")),
            use_large=os.getenv("SAFETYMIND_BGE_LARGE", "0") == "1",
            device=os.getenv("SAFETYMIND_BGE_DEVICE", "cpu"),
        )

    def _get_models(self):
        if self._models is None:
            os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
            os.environ.setdefault("HF_HOME", r"D:\hf_cache")
            import torch
            from transformers import AutoTokenizer, AutoModel
            from core.intent_recognizer import _TEMPLATES, IntentCategory
            from core.laya_recognizer import ZH_KEY, ZH_SHORT
            bank = build_bank(IntentCategory, _TEMPLATES, ZH_KEY, ZH_SHORT, self._definitions())
            names = ["BAAI/bge-base-zh-v1.5"] + (["BAAI/bge-large-zh-v1.5"] if self.use_large else [])
            self._weights = [0.5] + ([0.5] if self.use_large else [])
            self._models = []
            self._protos = []
            for name in names:
                tok = AutoTokenizer.from_pretrained(name)
                mdl = AutoModel.from_pretrained(name); mdl.eval(); mdl.to(self.device)
                texts, pcls = [], []
                for v, lst in bank.items():
                    for t in lst:
                        texts.append(t); pcls.append(v)
                E = self._embed(texts, tok, mdl)
                cat_vals = [c.value for c in IntentCategory]
                protos = torch.zeros(len(cat_vals), E.shape[1]); cnt = {}
                for j, cls in enumerate(pcls):
                    protos[cat_vals.index(cls)] += E[j]; cnt[cls] = cnt.get(cls, 0) + 1
                for cls in cat_vals: protos[cat_vals.index(cls)] /= max(1, cnt[cls])
                protos = torch.nn.functional.normalize(protos, dim=-1)
                self._models.append((name, tok, mdl))
                self._protos.append(protos)
            self._cat_vals = [c.value for c in IntentCategory]
        return self._models

    def _definitions(self) -> Dict[str, str]:
        # 与 runner.DEF 同源的类定义（复制关键描述，避免引 runner 的 argparse 副作用）
        return {
            "equipment_alarm": "设备或工艺报警处置：DCS仪表报警正在响、超温超压超液位、联锁动作的即时应对",
            "equipment_maintenance": "检维修作业：设备检修计划安排、维修流程、大修中修保养、试车验收",
            "hazard_report": "隐患上报：发现尚未造成事故的具体隐患，走上报流程（去报）",
            "work_permit": "特殊作业办票：动火受限空间高处吊装等作业的办票审批延期气体分析要求",
            "hazard_inspection": "隐患排查：如何开展排查工作查什么项目频次计划排查表台账（查的方法）",
            "incident_report": "事故上报：已发生并造成损害的事故的报告流程时限材料",
            "emergency_response": "应急处置：正在发生的泄漏着火中毒坍塌等紧急事态的现场处置与急救",
            "chemical_safety": "危化品管理：储存要求相容性MSDS使用与防泄漏设施",
            "training_cert": "培训取证：安全培训学时特种作业证取证复审补办报名考试",
            "inspection_audit": "迎检考核：标准化考核评审上级检查外审的迎检准备与整改报告",
            "occupational_health": "职业健康：体检职业病危害因素申报告知监护档案",
            "regulation_query": "法规标准查询：国家法律法规国标行标具体条文内容发布施行信息",
            "ppe_inquiry": "防护用品选用：PPE选型型号级别佩戴检测周期报废更换",
            "safety_document": "制度文档：企业内部操作规程管理制度预案文档的编写修订受控",
            "general_consult": "一般安全咨询：安全理念概念方法学的理解性咨询",
            "greeting": "问候寒暄打招呼找人不涉业务", "feedback": "正面反馈满意感谢点赞",
            "escalation": "转人工升级：要求人工找领导专家投诉不接受机器答复", "other": "无关内容生活闲聊",
        }

    @staticmethod
    def _mean_pool(lh, mask):
        mask = mask.unsqueeze(-1).float()
        return (lh * mask).sum(1) / mask.sum(1).clamp(min=1e-9)

    def _embed(self, texts, tok, model, bs=32, max_len=128):
        import torch
        outs = []
        with torch.no_grad():
            for i in range(0, len(texts), bs):
                enc = tok(texts[i:i+bs], padding=True, truncation=True, max_length=max_len, return_tensors="pt").to(self.device)
                emb = self._mean_pool(model(**enc).last_hidden_state, enc["attention_mask"])
                outs.append(torch.nn.functional.normalize(emb, dim=-1))
        return torch.cat(outs)

    def probs(self, message: str) -> Dict[str, float]:
        """集成概率 {intent_value: prob}。"""
        models = self._get_models()
        import torch
        ens = None
        for (name, tok, mdl), w in zip(models, self._weights):
            E = self._embed([message], tok, mdl)
            sims = (E @ self._protos[models.index((name, tok, mdl))].T)[0]
            p = torch.softmax(sims / self.temperature, dim=-1)
            if ens is None:
                ens = {v: w * float(p[j]) for j, v in enumerate(self._cat_vals)}
            else:
                for j, v in enumerate(self._cat_vals):
                    ens[v] += w * float(p[j])
        return ens

    def _try_sync(self, message: str, pattern_fn: Callable[[str], Dict]) -> Optional[Tuple[str, float]]:
        try:
            ens = self.probs(message)
        except Exception:
            return None
        pat = pattern_fn(message)
        pat_cls = pat.get("intent")
        if hasattr(pat_cls, "value"):
            pat_cls = pat_cls.value
        cls, conf = fuse_with_pattern(ens, pat_cls, pat.get("confidence", 0.0),
                                      weight=0.15, exclude=GENERIC_NOISE)
        if cls is None:
            return None
        return cls, conf

    async def try_recognize(self, message: str, pattern_fn: Callable[[str], Dict]) -> Optional[Tuple[str, float]]:
        import asyncio
        return await asyncio.to_thread(self._try_sync, message, pattern_fn)
