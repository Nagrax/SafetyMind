# BGE 原型分类器纯函数单测（不依赖 torch/模型下载）：python tests/test_bge_proto.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.intent_bge import build_bank, fuse_ens, fuse_with_pattern, HAND, HAND2
from core.intent_recognizer import IntentCategory, _TEMPLATES
from core.laya_recognizer import ZH_KEY, ZH_SHORT

DEFS = {"equipment_alarm": "设备报警处置", "work_permit": "办票审批", "greeting": "问候",
        "feedback": "正面反馈", "escalation": "转人工", "other": "无关"}
DEFS = {c.value: DEFS.get(c.value, f"{c.value}定义") for c in IntentCategory}

def test_bank_covers_all_classes():
    bank = build_bank(IntentCategory, _TEMPLATES, ZH_KEY, ZH_SHORT, DEFS)
    assert set(bank.keys()) == {c.value for c in IntentCategory}
    for v, lst in bank.items():
        assert len(lst) >= 2, f"{v} 原型示例不足: {len(lst)}"
    print("PASS bank_covers_all_classes")

def test_bank_no_cross_class_dup_keys():
    bank = build_bank(IntentCategory, _TEMPLATES, ZH_KEY, ZH_SHORT, DEFS)
    all_texts = [t for lst in bank.values() for t in lst]
    assert len(all_texts) == len(set(all_texts)), "示例库存在完全重复文本"
    print("PASS bank_no_cross_class_dup_keys")

def test_fuse_ens_weighted():
    s = fuse_ens([{"a": 0.6, "b": 0.4}, {"a": 0.2, "b": 0.8}], [0.7, 0.3])
    assert abs(s["a"] - (0.7*0.6 + 0.3*0.2)) < 1e-9
    assert abs(s["b"] - (0.7*0.4 + 0.3*0.8)) < 1e-9
    print("PASS fuse_ens_weighted")

def test_fuse_with_pattern():
    ens = {"a": 0.55, "b": 0.45}
    cls, conf = fuse_with_pattern(ens, None, 0.0)
    assert cls == "a" and abs(conf - 0.55) < 1e-9
    # pattern 弱补能翻盘
    cls2, conf2 = fuse_with_pattern(ens, "b", 1.0, weight=0.2)
    assert cls2 == "b", (cls2, conf2)  # 0.45+0.2 > 0.55
    print("PASS fuse_with_pattern")

def test_fuse_excludes_generic():
    cls, _ = fuse_with_pattern({"a": 0.5, "b": 0.5}, "general_consult", 0.5, exclude={"general_consult"})
    assert cls in ("a", "b")
    print("PASS fuse_excludes_generic")

if __name__ == "__main__":
    test_bank_covers_all_classes()
    test_bank_no_cross_class_dup_keys()
    test_fuse_ens_weighted()
    test_fuse_with_pattern()
    test_fuse_excludes_generic()
    print("\nALL 5 TESTS PASSED")
