# 轻量向量检索器（无 chroma）：bge 嵌入 + numpy 余弦
# 用途: 真实业务知识库（数千-万级片段）的可靠检索，规避 chromadb Windows 段损坏问题
# 用法: python tools/simple_vector_rag.py --build   （构建/刷新索引）
#       python tools/simple_vector_rag.py --query "受限空间作业要先检测什么"
import os, sys, json, time, argparse
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.chdir(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from dotenv import load_dotenv
load_dotenv(".env")

import numpy as np

STORE_DIR = os.path.join("data", "vector_store")
STORE_VEC = os.path.join(STORE_DIR, "embeddings.npy")
STORE_DOC = os.path.join(STORE_DIR, "docs.json")

def get_ef(mode=None):
    mode = mode or os.getenv("SAFETYMIND_EMBEDDING", "auto")
    if mode in ("auto", "bge"):
        try:
            from core.bge_embedding import BgeEmbeddingFunction
            return BgeEmbeddingFunction(), mode
        except Exception as ex:
            print(f"BGE 不可用，回退 n-gram: {ex}")
    from core.local_embedding import LocalEmbeddingFunction
    return LocalEmbeddingFunction(), "ngram"

def build(mode=None):
    seed = json.load(open("data/knowledge_seed/xlsx_docs.json", encoding="utf-8"))
    ef, used_mode = get_ef(mode)
    t0 = time.monotonic()
    texts = [d["content"] for d in seed]
    vecs = np.array(ef.embed_documents(texts), dtype=np.float32)
    os.makedirs(STORE_DIR, exist_ok=True)
    np.save(STORE_VEC, vecs)
    json.dump(seed, open(STORE_DOC, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"索引构建: {len(seed)} 片段 × {vecs.shape[1]} 维, 嵌入模式={used_mode}, 耗时 {time.monotonic()-t0:.0f}s")

def search(query, top_k=3, mode=None):
    ef, used_mode = get_ef(mode)
    vecs = np.load(STORE_VEC)
    docs = json.load(open(STORE_DOC, encoding="utf-8"))
    qv = np.array(ef.embed_query([query])[0], dtype=np.float32)
    # 余弦相似度（向量已归一化时等价点积；做一次归一化防御）
    qn = qv / (np.linalg.norm(qv) + 1e-9)
    vn = vecs / (np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9)
    sims = vn @ qn
    top = np.argsort(-sims)[:top_k]
    return [(docs[i], float(sims[i])) for i in top]

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--mode", default=os.getenv("SAFETYMIND_EMBEDDING", "auto"))
    ap.add_argument("--query")
    ap.add_argument("--top-k", type=int, default=3)
    args = ap.parse_args()
    if args.build:
        build(args.mode)
    elif args.query:
        t0 = time.monotonic()
        results = search(args.query, args.top_k, args.mode)
        ms = (time.monotonic()-t0)*1000
        for d, s in results:
            print(f"[{s:.3f}] {d['title']}\n    {d['content'][:120]}")
        print(f"({ms:.0f}ms)")
    else:
        ap.print_help()
