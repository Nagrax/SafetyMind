# -*- coding: utf-8 -*-
"""物性参数名识别：把"爆炸极限"这类**参数名**与"正在爆炸"这类**现场事件**区分开。

## 为什么单独一个模块

紧急度判定有**两处**用它：
1. `core/intent_recognizer._urgency`（决定 urgency=CRITICAL，进而走 orchestrator 的
   CRITICAL 模板）；
2. `core/emergency.precheck`（/chat 入口的零 LLM 快路径）。
两处各自维护一份词表必然漂移，所以集中在这里，两边 import。

## 真实缺陷（2026-10-29 事实稳定性评测发现）

"甲醇的闪点和爆炸极限分别是多少？" 5 次都没有给出数值：一次被入口快路径当紧急事态分流
（还建了应急工单），另一次被 intent 判成 CRITICAL 走了模板响应。根因就是"爆炸极限"里的
"爆炸"命中了 CRITICAL 词表。

## 判定口径

关键词的**每一处**出现都以参数名开头 → 视为参数名（不是现场证据）；
只要有一处不是（"爆炸极限是多少？现场已经爆炸了"）→ 仍按现场事件处理。
"""

# 参数名（物性参数，不是现场事件）
PARAM_PHRASES = (
    "爆炸极限", "爆炸下限", "爆炸上限", "爆炸浓度", "爆炸范围", "爆炸危险",
    "爆炸性", "闪点", "燃点", "自燃点",
)


def keyword_only_in_param_phrase(text: str, keyword: str) -> bool:
    """关键词的所有出现都只是参数名的一部分时返回 True（→ 不应计为现场证据）。"""
    msg = text or ""
    start = 0
    while True:
        idx = msg.find(keyword, start)
        if idx < 0:
            return True
        tail = msg[idx:idx + 6]
        if not any(tail.startswith(p) for p in PARAM_PHRASES):
            return False
        start = idx + len(keyword)


def live_keyword_hits(text: str, keywords) -> list:
    """过滤掉"只作为参数名出现"的关键词命中，返回真正算现场证据的那些。"""
    return [kw for kw in keywords if kw in (text or "")
            and not keyword_only_in_param_phrase(text, kw)]
