# 前端-后端接线测试：桌面/Web 模式下前端按钮背后的关键端点是否真实可用
# 用法: python tests/test_frontend_endpoints.py（启动完整 app，覆盖 lifespan 初始化）
# 覆盖：/health（连接状态）、GET+POST /skills（已加载能力/重新加载按钮）、
#       /knowledge/stats（知识库页统计）、/api/python/* 前缀别名（桌面模式前端实际路径）、
#       静态首页（frontend/dist 是否随仓库可用）、
#       Markdown 渲染函数（frontend/src/lib/markdown.js，node 实测 + dist 产物标记）。
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def check_markdown_renderer():
    """Markdown 渲染函数检查（2026-09-28 新增）。

    背景：夜检截图 04-真实会话1-动火作业票.png 暴露回答气泡 Markdown 渲染缺口
    （`---` 分隔线原样显示、标题内 **加粗** 不解析）。这里用 node 直接执行
    frontend/src/lib/markdown.js（与 dist 打包同源模块）断言解析契约，并检查
    dist 产物确含渲染标记，防止"只改 src 不重建 dist"的回归。
    """
    import json
    import subprocess

    root = Path(__file__).resolve().parents[1]
    md_js = root / "frontend" / "src" / "lib" / "markdown.js"
    app_vue = root / "frontend" / "src" / "App.vue"
    dist_assets = root / "frontend" / "dist" / "assets"

    probe = (
        "import { parseMarkdown, inlineTokens } from 'file:///" + md_js.as_posix() + "';\n"
        "const doc = ['# 一级标题', '', '## 二级标题', '', '### 三级标题', '',"
        " '- 无序一', '- 无序二', '', '1. 有序一', '2. 有序二', '',"
        " '---', '', '正文**加粗**与`行内代码`', '', '```py', 'print(1)', '```'].join('\\n');\n"
        "const blocks = parseMarkdown(doc);\n"
        "const out = {\n"
        "  types: blocks.map(b => b.t),\n"
        "  hLevels: blocks.filter(b => b.t === 'h').map(b => b.level),\n"
        "  ulItems: (blocks.find(b => b.t === 'ul') || {items: []}).items.length,\n"
        "  olItems: (blocks.find(b => b.t === 'ol') || {items: []}).items.length,\n"
        "  hrAfterParas: parseMarkdown('上文\\n\\n---\\n\\n下文').some(b => b.t === 'hr'),\n"
        "  dashesNotUl: !parseMarkdown('- - -').some(b => b.t === 'ul'),\n"
        "  bold: inlineTokens('前**加粗**后').some(t => t.t === 'b' && t.v === '加粗'),\n"
        "  code: inlineTokens('运行`cmd`看').some(t => t.t === 'c' && t.v === 'cmd'),\n"
        "  plainNoLoss: inlineTokens('无标记纯文本').some(t => t.t === 'text' && t.v === '无标记纯文本'),\n"
        "  fence: (blocks.find(b => b.t === 'code') || {lines: []}).lines.join('\\n'),\n"
        "};\n"
        "console.log(JSON.stringify(out));\n"
    )
    if not md_js.exists():
        record("markdown.js 存在", False, str(md_js))
        return
    try:
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", probe],
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        assert proc.returncode == 0, f"node exit={proc.returncode} stderr={proc.stderr[:200]}"
        data = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception as e:  # node 不可用/解析失败都是红灯，绝不静默跳过
        record("node 实测 markdown.js 解析契约", False, str(e))
        return

    types = set(data["types"])
    record("块级解析含 标题/列表/段落/分隔线/代码",
           {"h", "ul", "ol", "p", "hr", "code"} <= types, f"types={data['types']}")
    record("标题级别 1/2/3 级", data["hLevels"] == [1, 2, 3], f"hLevels={data['hLevels']}")
    record("无序/有序列表条目", data["ulItems"] == 2 and data["olItems"] == 2,
           f"ul={data['ulItems']} ol={data['olItems']}")
    record("--- 分隔线产出 hr 块", data["hrAfterParas"] is True, f"hrAfterParas={data['hrAfterParas']}")
    record("- - - 识别为分隔线而非列表", data["dashesNotUl"] is True, f"dashesNotUl={data['dashesNotUl']}")
    record("**加粗** 行内解析", data["bold"] is True)
    record("`行内代码` 行内解析", data["code"] is True)
    record("纯文本无丢失", data["plainNoLoss"] is True)
    record("围栏代码块内容原样保留", data["fence"] == "print(1)", f"fence={data['fence']!r}")

    dist_files = sorted(dist_assets.glob("index-*.js")) if dist_assets.exists() else []
    bundle = dist_files[-1].read_text(encoding="utf-8", errors="ignore") if dist_files else ""
    record("dist 已构建且含渲染标记(md-hr/md-inline-code)",
           bool(dist_files) and "md-hr" in bundle and "md-inline-code" in bundle,
           f"assets={[p.name for p in dist_files]}")

    vue = app_vue.read_text(encoding="utf-8") if app_vue.exists() else ""
    import re as _re
    heading_branch = _re.search(r"<component\b[^>]*block\.t === 'h'.*?</component>", vue, _re.S)
    heading_ok = bool(heading_branch) and "inlineTokens(block.text)" in heading_branch.group(0)
    record("标题分支走行内解析(标题内**加粗**可渲染)",
           "item.role === 'assistant'" in vue and heading_ok,
           "App.vue 标题分支需用 inlineTokens(block.text) 渲染")
    record("用户消息保持纯文本逐行插值",
           "item.content.split" in vue, "App.vue 用户分支需保持 content.split 纯文本渲染")


def main():
    check_markdown_renderer()

    from fastapi.testclient import TestClient
    from api.main import app

    with TestClient(app) as client:  # with 语句触发 lifespan（真实初始化记忆/知识库/Skills）
        r = client.get("/health")
        record("GET /health 连接状态", r.status_code == 200, f"code={r.status_code}")

        r = client.get("/skills")
        ok = r.status_code == 200 and r.json().get("count", 0) >= 1
        record("GET /skills 已加载能力", ok, f"code={r.status_code} body={r.text[:120]}")

        r = client.post("/skills/reload")
        ok = r.status_code == 200 and r.json().get("count", 0) >= 1
        record("POST /skills/reload 重新加载按钮", ok, f"code={r.status_code} body={r.text[:120]}")

        r = client.get("/knowledge/stats")
        ok = r.status_code == 200 and "total_chunks" in r.json()
        record("GET /knowledge/stats 知识库统计", ok, f"code={r.status_code} body={r.text[:120]}")

        # 桌面模式前端走 /api/python 前缀（与 Nginx 反代路径一致），别名必须同权可用
        r = client.get("/api/python/skills")
        record("GET /api/python/skills 别名", r.status_code == 200, f"code={r.status_code}")
        r = client.post("/api/python/skills/reload")
        record("POST /api/python/skills/reload 别名", r.status_code == 200, f"code={r.status_code}")

        # 会话历史：写入（借 /chat 太慢，直接调存储层）→ 列表 → 恢复 → 删除
        from memory.conversation_store import ConversationStore
        store = ConversationStore("data/conversations")
        store.append_message("endpoint_test", "c_hist_1", "user", "动火作业票怎么办理")
        store.append_message("endpoint_test", "c_hist_1", "assistant", "办理流程：...", escalated=False, request_id="r9")
        r = client.get("/conversations?user_id=endpoint_test")
        ok = r.status_code == 200 and any(c["conv_id"] == "c_hist_1" for c in r.json().get("conversations", []))
        record("GET /conversations 历史列表", ok, f"code={r.status_code} body={r.text[:140]}")
        r = client.get("/conversations/c_hist_1/messages?user_id=endpoint_test")
        ok = r.status_code == 200 and len(r.json().get("messages", [])) == 2
        record("GET /conversations/{id}/messages 隔天恢复", ok, f"code={r.status_code}")
        r = client.delete("/conversations/c_hist_1?user_id=endpoint_test")
        ok = r.status_code == 200 and r.json().get("deleted") is True
        record("DELETE /conversations/{id}", ok, f"code={r.status_code}")

        r = client.get("/config")
        record("GET /config 转人工电话配置", r.status_code == 200 and "escalation_phone" in r.json(),
               f"code={r.status_code} body={r.text[:120]}")

        # 评测页加载时自动拉取最近一次评测报告：
        # 200 且含 pass_rate 字段（已有报告），或明确的空态结构（available=false，冷启动）
        r = client.get("/eval/last")
        body = r.json() if r.status_code == 200 else {}
        ok = r.status_code == 200 and ("pass_rate" in body or body.get("available") is False)
        record("GET /eval/last 最近评测报告/空态", ok, f"code={r.status_code} body={r.text[:120]}")
        r = client.get("/api/python/eval/last")
        record("GET /api/python/eval/last 别名", r.status_code == 200, f"code={r.status_code}")

        r = client.get("/")
        record("GET / 静态首页(frontend/dist)", r.status_code == 200 and "<div id=" in r.text,
               f"code={r.status_code} len={len(r.text)}")

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
