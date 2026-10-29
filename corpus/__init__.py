"""通用安全工程资料库（corpus）——资料获取、解析、检索接入与效果验证。

代码在仓库内（本包），数据在仓库外：默认根目录 D:/SafetyMindData/corpus，
大文件与代码分离（见 docs/资料库建设决策与Agent实施方案_2026-10-19.md §5）。

分层：
  - manifest（来源/资产/版本/正文块/模板契约与状态机）
  - parsers（DOCX/XLSX/PPTX/PDF/旧PPT识别/压缩包 → 完整段落、页、表格、定位）
  - chunking（token 计量切块，约 180~220 token，超长句硬切）
  - fts（FTS5 中文预分词 + BM25 + 标准号/CAS 精确字段）
  - catalog（短目录卡：evidence_kind=catalog，与正文分集合）
  - build（导入真实 API 库路径，唯一 build 标记，激活上限）
"""

__all__ = ["paths"]
