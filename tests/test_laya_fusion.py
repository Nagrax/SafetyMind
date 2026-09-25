# LayaFuser 纯函数单测（不依赖 torch/模型）：python tests/test_laya_fusion.py
# 覆盖：温度校准、加权融合、泛化噪声排除、键映射完备性
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.laya_recognizer import calibrate_probs, fuse, ZH_KEY, GENERIC_NOISE

def test_calibrate_sums_to_one():
    p = {"a": 0.6, "b": 0.3, "c": 0.1}
    c = calibrate_probs(p, 1.5)
    assert abs(sum(c.values()) - 1.0) < 1e-9, c
    print("PASS calibrate_sums_to_one")

def test_calibrate_monotonic_order():
    p = {"a": 0.6, "b": 0.3, "c": 0.1}
    c = calibrate_probs(p, 2.0)
    assert c["a"] > c["b"] > c["c"], c
    print("PASS calibrate_monotonic_order")

def test_calibrate_high_t_flattens():
    p = {"a": 0.9, "b": 0.1}
    c = calibrate_probs(p, 5.0)
    assert c["a"] < 0.9 and c["b"] > 0.1, c  # T>1 拉平过自信
    s = calibrate_probs(p, 0.1)
    assert s["a"] > 0.9, s                    # T<1 锐化
    print("PASS calibrate_high_t_flattens")

def test_fuse_weighted_math():
    probs = {"a": 0.8, "b": 0.2}
    cls, conf = fuse(probs, None, 0.0, weight=0.5)
    assert cls == "a"
    assert abs(conf - (0.5 * 0.8) / (0.5 * 1.0)) < 1e-9, conf
    # pattern 一票可翻盘
    cls2, _ = fuse(probs, "b", 1.0, weight=0.5)
    s_a = 0.5 * 0.8
    s_b = 0.5 * 0.2 + 0.5 * 1.0
    assert cls2 == ("a" if s_a > s_b else "b")
    print("PASS fuse_weighted_math")

def test_fuse_excludes_generic_noise():
    # pattern 的 general_consult 来自泛化问句伪置信度，必须被排除
    probs = {"a": 0.7, "b": 0.3}
    cls, _ = fuse(probs, "general_consult", 0.5, weight=0.5, exclude=GENERIC_NOISE)
    assert cls == "a", cls
    print("PASS fuse_excludes_generic_noise")

def test_fuse_empty_cases():
    assert fuse({}, None, 0.0) == (None, 0.0)
    assert fuse({}, "x", 0.0) == (None, 0.0)  # conf=0 的 pattern 不产生质量
    cls, _ = fuse({}, "x", 0.5)
    assert cls == "x"
    print("PASS fuse_empty_cases")

def test_zh_key_covers_all_19():
    from core.intent_recognizer import IntentCategory
    vals = {c.value for c in IntentCategory}
    assert set(ZH_KEY.values()) == vals, (vals - set(ZH_KEY.values()), set(ZH_KEY.values()) - vals)
    assert len(ZH_KEY) == len(vals)  # 无重复中文键
    print("PASS zh_key_covers_all_19")

if __name__ == "__main__":
    test_calibrate_sums_to_one()
    test_calibrate_monotonic_order()
    test_calibrate_high_t_flattens()
    test_fuse_weighted_math()
    test_fuse_excludes_generic_noise()
    test_fuse_empty_cases()
    test_zh_key_covers_all_19()
    print("\nALL 7 TESTS PASSED")
