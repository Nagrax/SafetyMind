# -*- coding: utf-8 -*-
"""K08 通用资料库评测（冻结 120 项 + 开发集 30 项 + A/B0/B80/B300 四配置对比）。

方案依据：内部设计文档（私有，不随仓库分发） §8.2 / §8.3。
本包只做评测：用例集（cases.json）、评测骨架（runner.py）、纪律守卫。
不改 corpus/** 与产品代码。

对外入口：
  run_ab / compare_configs / load_cases / load_document /
  assert_frozen_untouched / select_corpus_thresholds / validate_cases
"""
from evaluation.corpus_eval.runner import (  # noqa: F401
    CONFIGS,
    CORPUS_CONFIGS,
    DEFAULT_REPORT_DIR,
    GROUPS,
    INCREMENT_GAIN_TARGET_PP,
    PASS_TARGET_WHEN_B0_ABOVE,
    QUOTA,
    VALID_SPLITS,
    assert_frozen_untouched,
    compare_configs,
    load_cases,
    load_document,
    run_ab,
    select_corpus_thresholds,
    validate_cases,
)

__all__ = [
    "CONFIGS", "CORPUS_CONFIGS", "DEFAULT_REPORT_DIR", "GROUPS",
    "INCREMENT_GAIN_TARGET_PP", "PASS_TARGET_WHEN_B0_ABOVE", "QUOTA", "VALID_SPLITS",
    "assert_frozen_untouched", "compare_configs", "load_cases", "load_document",
    "run_ab", "select_corpus_thresholds", "validate_cases",
]
