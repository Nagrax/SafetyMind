# -*- coding: utf-8 -*-
"""单臂执行器（供 eval_kb_checklist_ab.py 以独立进程调用，避免 importlib.reload 的坑）。

用法:
  <venv python> benchmarks/_kb_style_worker.py --kb-path <dir> --style <""|checklist> --out <json>
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "benchmarks"))

from eval_reasoning_ab import QUESTIONS  # noqa: E402
from eval_kb_expansion import INSPECTION_QS  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb-path", required=True)
    ap.add_argument("--style", default="")
    ap.add_argument("--out", required=True)
    ap.add_argument("--n-general", type=int, default=6)
    args = ap.parse_args()

    os.environ["CHROMA_KB_PERSIST_DIRECTORY"] = args.kb_path
    os.environ["SAFETYMIND_TEMPERATURE"] = "0"
    os.environ["SAFETYMIND_RESPONSE_CACHE"] = "0"
    if args.style:
        os.environ["SAFETYMIND_KB_INJECT_STYLE"] = args.style
    else:
        os.environ.pop("SAFETYMIND_KB_INJECT_STYLE", None)

    from fastapi.testclient import TestClient
    import api.main as m

    questions = ([("general", q) for _, _, q in QUESTIONS[:args.n_general]]
                 + [("inspection", q) for q in INSPECTION_QS])
    rows = []
    with TestClient(m.app) as c:
        c.get("/health")
        for i, (tag, q) in enumerate(questions):
            t = time.perf_counter()
            r = c.post("/chat", json={"message": q, "user_id": f"style-{args.style or 'default'}-{i}"})
            b = r.json() if r.status_code == 200 else {}
            rows.append({"arm": args.style or "default", "tag": tag, "q": q,
                         "ans": b.get("response") or "",
                         "cites": len(b.get("citations") or []),
                         "ms": round((time.perf_counter() - t) * 1000, 0)})
            print(f"[{args.style or 'default':10s} {tag:10s}] {rows[-1]['ms']:6.0f}ms "
                  f"{len(rows[-1]['ans']):5d}字 cites={rows[-1]['cites']}", flush=True)

    Path(args.out).write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    print(f"written {args.out}")


if __name__ == "__main__":
    os.environ.setdefault("SAFETYMIND_LLM_PROTOCOL", "openai")
    main()
