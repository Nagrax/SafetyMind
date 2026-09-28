"""轻量压测：对运行中的 SafetyMind 服务做并发冒烟压测。

用法（在仓库根目录）:
    .venv/Scripts/python.exe benchmarks/stress_light.py            # 默认参数
    .venv/Scripts/python.exe benchmarks/stress_light.py --chat 2   # 指定真实 /chat 条数

设计约束：
- 主要压力打在零 LLM 依赖的 GET 端点（/health /knowledge/stats /skills /conversations），
  不消耗上游 LLM 配额；
- /chat 真实调用默认 2 条、并发 2（会消耗 bigmodel.cn LLM 配额，勿随意调大）；
- 压测后必须复查 /health，确认服务仍存活；任何 5xx/连接错误都如实打印，不做粉饰。
"""

import argparse
import concurrent.futures as cf
import statistics
import sys
import time

import httpx


def pct(xs: list, p: int):
    if not xs:
        return None
    s = sorted(xs)
    k = min(len(s) - 1, max(0, round(p / 100 * (len(s) - 1))))
    return s[k]


def run_phase(client: httpx.Client, name: str, method: str, url: str,
              n: int, conc: int, **kw) -> int:
    lat, errs = [], []

    def one(_i):
        t0 = time.perf_counter()
        try:
            r = client.request(method, url, **kw)
            if r.status_code >= 400:
                errs.append(f"HTTP {r.status_code}")
                return None
            return (time.perf_counter() - t0) * 1000
        except Exception as e:  # 连接错误/超时都是压测的有效观测
            errs.append(type(e).__name__)
            return None

    with cf.ThreadPoolExecutor(max_workers=conc) as ex:
        lat = [v for v in ex.map(one, range(n)) if v is not None]

    ok_n, fail_n = len(lat), len(errs)
    if ok_n:
        print(f"[{name}] {n}请求/并发{conc}: 成功{ok_n} 失败{fail_n} "
              f"p50={statistics.median(lat):.0f}ms "
              f"p95={pct(lat, 95):.0f}ms max={max(lat):.0f}ms")
    else:
        print(f"[{name}] {n}请求/并发{conc}: 全部失败 {errs[:3]}")
    if errs:
        print(f"  错误样本: {errs[:5]}")
    return fail_n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--chat", type=int, default=2,
                    help="真实 /chat 条数（消耗上游 LLM 配额）")
    args = ap.parse_args()

    limits = httpx.Limits(max_connections=64, max_keepalive_connections=32)
    total_fail = 0
    with httpx.Client(base_url=args.base, timeout=60.0, limits=limits) as c:
        # 预热（BGE 权重/连接池）
        try:
            c.get("/health").raise_for_status()
        except Exception as e:
            print(f"服务不可达: {e}")
            return 2

        t0 = time.perf_counter()
        total_fail += run_phase(c, "1.健康检查", "GET", "/health", 200, 20)
        total_fail += run_phase(c, "2.知识库统计", "GET", "/knowledge/stats", 120, 12)
        total_fail += run_phase(c, "3.技能列表", "GET", "/skills", 60, 10)
        total_fail += run_phase(c, "4.会话列表", "GET", "/conversations", 60, 10)

        if args.chat > 0:
            # 真实会话并发（消耗 LLM 配额）：验证并发下不崩溃、能返回回答
            questions = ["动火作业票怎么办理", "高处作业审批流程是什么",
                         "受限空间作业注意事项", "发现燃气泄漏怎么处置"]
            n_chat = min(args.chat, len(questions))
            if args.chat > len(questions):
                print(f"[提示] --chat={args.chat} 超出内置问题数 {len(questions)}，"
                      f"本次实际执行 {n_chat} 条（问题池固定，不做扩充）")
            t1 = time.perf_counter()
            chat_lat, chat_err = [], []
            def one_chat(i):
                t = time.perf_counter()
                try:
                    r = c.post("/chat", json={"message": questions[i % len(questions)],
                                              "user_id": "stress_test"})
                    if r.status_code >= 400:
                        chat_err.append(f"HTTP {r.status_code}")
                        return None
                    body = r.json()
                    resp = (body.get("response") or "").strip()
                    if not resp:
                        chat_err.append("空回答")
                        return None
                    return (time.perf_counter() - t) * 1000
                except Exception as e:
                    chat_err.append(type(e).__name__)
                    return None
            with cf.ThreadPoolExecutor(max_workers=n_chat) as ex:
                chat_lat = [v for v in ex.map(one_chat, range(n_chat)) if v is not None]
            if chat_lat:
                print(f"[5.真实会话/chat] {n_chat}条/并发{n_chat}: 成功{len(chat_lat)} "
                      f"失败{len(chat_err)} p50={statistics.median(chat_lat):.0f}ms "
                      f"max={max(chat_lat):.0f}ms")
            else:
                print(f"[5.真实会话/chat] 全部失败: {chat_err[:3]}")
            total_fail += len(chat_err)
            print(f"  （真实会话阶段耗时 {time.perf_counter()-t1:.1f}s，回答非空才算成功）")

        print(f"压测总耗时 {time.perf_counter()-t0:.1f}s")

        # 压测后存活复查
        try:
            c.get("/health").raise_for_status()
            print("压测后 /health 复查: 正常")
        except Exception as e:
            print(f"压测后 /health 复查: 异常 {e} —— 服务在压测中失去响应")
            total_fail += 1

    return 1 if total_fail else 0


if __name__ == "__main__":
    sys.exit(main())
