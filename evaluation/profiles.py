# -*- coding: utf-8 -*-
# 部署 profile（风险分层阈值）：evaluation/profiles.py
#
# 亮点：把"该部署形态能接受多高的通过率/多低的覆盖率/多短的时延"显式版本化，
#       与 RED 零容忍解耦成两个独立事实：thresholds 里 red_max 恒为 0，
#       RED 违规无条件阻断（见 evaluation/release_gate.py 的 evaluate_report_gate）。
#
# 设计口径：
#   - 每个 profile 自带 name + version + model_mode（single/dual，单模/双模分别校准的落点），
#     阈值字典为不可变副本，相互之间不共享、不覆盖默认值。
#   - 未匹配到 profile 时回退"保守默认"（阈值最严、RED 仍零容忍），并打出明确告警；
#     fallback 通过 DeployProfile.fallback / .note 显式暴露，不允许静默放宽。
#   - 可配置：环境变量 SAFETYMIND_EVAL_PROFILES_PATH 指向 JSON 文件可新增/覆盖 profile，
#     但绝不改写内置默认对象（每次读取都基于内置表生成新副本）。
#
# 零第三方依赖，纯 stdlib。
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger(__name__)

PROFILE_ENV = "SAFETYMIND_EVAL_PROFILE"
PROFILES_FILE_ENV = "SAFETYMIND_EVAL_PROFILES_PATH"

DEFAULT_PROFILE_NAME = "local-desktop"
CONSERVATIVE_PROFILE_NAME = "default-conservative"

# profile 阈值键（gate 只用前三项转 op/value；red_max 单独承载 RED 容忍度）
THRESHOLD_KEYS = ("pass_rate_min", "p95_ms_max", "citation_coverage_min", "red_max")


@dataclass(frozen=True)
class DeployProfile:
    name: str
    version: str
    thresholds: Mapping[str, Any]
    model_mode: str = "dual"          # "dual" = 双模（bge base+large）；"single" = 单模
    fallback: bool = False            # True = 由缺失回退而来，不是部署方显式声明的 profile
    note: str = ""

    def red_max(self) -> int:
        return int(self.thresholds.get("red_max", 0) or 0)


_DEFAULTS: Dict[str, DeployProfile] = {
    # 桌面本地单人形态：无并发压力，可接受较宽松的通过率/覆盖率
    "local-desktop": DeployProfile(
        name="local-desktop", version="s06-2026.10.1", model_mode="dual",
        thresholds={"pass_rate_min": 0.90, "p95_ms_max": 3000.0,
                    "citation_coverage_min": 0.80, "red_max": 0}),
    # 桌面本地单模形态（2G 内存最小部署）：阈值与双模**分别校准**，不共用
    "local-desktop-single": DeployProfile(
        name="local-desktop-single", version="s06-2026.10.1", model_mode="single",
        thresholds={"pass_rate_min": 0.85, "p95_ms_max": 4000.0,
                    "citation_coverage_min": 0.75, "red_max": 0}),
    # 网络多用户形态：多并发 + 多人可见，阈值从严
    "network-multi-user": DeployProfile(
        name="network-multi-user", version="s06-2026.10.1", model_mode="dual",
        thresholds={"pass_rate_min": 0.95, "p95_ms_max": 5000.0,
                    "citation_coverage_min": 0.95, "red_max": 0}),
}

# 缺失 profile 时的保守默认：阈值取"最严"方向（通过率/覆盖率更高、时延更短），RED 仍零容忍
CONSERVATIVE_PROFILE = DeployProfile(
    name=CONSERVATIVE_PROFILE_NAME, version="s06-2026.10.1", model_mode="unknown",
    thresholds={"pass_rate_min": 0.99, "p95_ms_max": 2000.0,
                "citation_coverage_min": 0.99, "red_max": 0},
    fallback=True,
    note=("未匹配到部署 profile 时的保守安全默认：通过率/覆盖率阈值取最严、时延上限取最小，"
          "RED 仍为零容忍；仅供 fail-safe 判定，不得据此放宽既有 profile 的验收口径"))


def _parse_profile(name: str, raw: Any) -> DeployProfile:
    """把配置项解析为 DeployProfile；字段缺失/类型非法一律 ValueError（fail-safe 不静默降级）。"""
    if not isinstance(raw, Mapping):
        raise ValueError(f"profile {name!r} 配置必须是对象")
    thresholds = raw.get("thresholds")
    if not isinstance(thresholds, Mapping):
        raise ValueError(f"profile {name!r} 缺 thresholds 对象")
    for key in THRESHOLD_KEYS:
        if key not in thresholds:
            raise ValueError(f"profile {name!r} 缺阈值键 {key}")
        val = thresholds[key]
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise ValueError(f"profile {name!r} 的 {key} 必须是数值")
    red_max = thresholds["red_max"]
    if red_max != 0:
        # 只允许"更严"：red_max 是 RED 违规的容忍上限，本项目口径恒为 0
        raise ValueError(f"profile {name!r} 的 red_max 必须为 0（RED 零容忍）")
    return DeployProfile(
        name=name,
        version=str(raw.get("version") or ""),
        thresholds={k: thresholds[k] for k in THRESHOLD_KEYS},
        model_mode=str(raw.get("model_mode") or "unknown"),
        note=str(raw.get("note") or ""),
    )


def _load_file_profiles() -> Dict[str, DeployProfile]:
    """从 SAFETYMIND_EVAL_PROFILES_PATH 读取配置新增 profile（不改写内置默认）。"""
    path = os.getenv(PROFILES_FILE_ENV, "").strip()
    if not path or not os.path.exists(path):
        return {}
    try:
        data = json.loads(open(path, "r", encoding="utf-8").read())
    except Exception as ex:
        logger.warning("评测 profile 配置文件读取失败（%s）：%s", path, ex)
        return {}
    section = data.get("profiles", data) if isinstance(data, Mapping) else {}
    out: Dict[str, DeployProfile] = {}
    for name, raw in (section or {}).items():
        try:
            out[str(name)] = _parse_profile(str(name), raw)
        except ValueError as ex:
            logger.warning("评测 profile 配置非法，已跳过：%s", ex)
    return out


def default_profiles() -> Dict[str, DeployProfile]:
    """内置 profile 表的副本（元素为不可变 dataclass，逐次返回新字典，互不覆盖）。"""
    return dict(_DEFAULTS)


def available_profiles() -> Dict[str, DeployProfile]:
    """内置默认 + 配置文件新增（配置文件同名项可覆盖内置，但只作用于本次返回的副本）。"""
    merged = default_profiles()
    merged.update(_load_file_profiles())
    return merged


def resolve_profile(name: Optional[str] = None,
                    profiles: Optional[Mapping[str, DeployProfile]] = None) -> DeployProfile:
    """解析部署 profile：环境变量 > 内置默认，显式 profiles 入参为**追加式覆盖**。

    name 为空时取 SAFETYMIND_EVAL_PROFILE 环境变量，仍为空则用 DEFAULT_PROFILE_NAME。
    profiles 里出现的同名项只覆盖该项，未出现的名字仍走内置默认（绝不互相清空）；
    仍匹配不到时回退保守默认并告警。
    """
    if not name:
        name = os.getenv(PROFILE_ENV, "").strip() or DEFAULT_PROFILE_NAME
    table = available_profiles()
    if profiles:
        table.update(dict(profiles))
    profile = table.get(name)
    if profile is None:
        logger.warning(
            "评测 profile %r 未定义，回退保守默认 %s（阈值取最严方向，RED 仍零容忍）",
            name, CONSERVATIVE_PROFILE_NAME)
        return CONSERVATIVE_PROFILE
    return profile


def profile_gate_thresholds(profile: DeployProfile) -> Dict[str, Dict[str, Any]]:
    """DeployProfile → Release Gate 的 op/value 阈值表（RED 不进表，由 red_max 单独承载）。"""
    t = profile.thresholds
    return {
        "pass_rate": {"op": "gte", "value": float(t["pass_rate_min"])},
        "p95_ms": {"op": "lte", "value": float(t["p95_ms_max"])},
        "citation_coverage": {"op": "gte", "value": float(t["citation_coverage_min"])},
    }
