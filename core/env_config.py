"""前端可视化的 .env 配置管理（白名单 + 掩码 + 值清洗 + 原子写）。

安全边界（Web 场景写 .env 的三道闸）：
  1. 白名单：只允许 ADMIN_ENV_KEYS 里的键，拒绝任意键写入；
  2. 掩码读取：secret 键只返回掩码（前 3 位 + ****），完整值永不出端点；
  3. 值清洗：值中的换行/引号前置空格等会破坏 .env 结构或注入其他键的字符一律拒绝。
写入策略：整文件重写但逐行保留注释与未知键（KEY=... 原位替换，新键追加），
tmp + os.replace 原子落盘。
"""
import os
import threading
from typing import Any, Dict, List

ADMIN_ENV_KEYS: Dict[str, Dict[str, Any]] = {
    "ANTHROPIC_API_KEY":      {"secret": True,  "restart": True,  "label": "LLM API Key"},
    "ANTHROPIC_BASE_URL":     {"secret": False, "restart": True,  "label": "LLM 端点（兼容 Anthropic 协议）"},
    "ANTHROPIC_MODEL":        {"secret": False, "restart": True,  "label": "模型名"},
    "ESCALATION_PHONE":       {"secret": False, "restart": False, "label": "值班电话（转人工直拨）"},
    "ESCALATION_WEBHOOK_URL": {"secret": False, "restart": False, "label": "值班群 Webhook"},
    "ADMIN_TOKEN":            {"secret": True,  "restart": False, "label": "管理密码（ADMIN_TOKEN）"},
    "ACCESS_TOKEN":           {"secret": False, "restart": False, "label": "公网访问令牌（发给使用者）"},
    "SAFETYMIND_BGE":         {"secret": False, "restart": True,  "label": "本地意图引擎", "enum": ("0", "1")},
    "SAFETYMIND_EMBEDDING":   {"secret": False, "restart": True,  "label": "嵌入模式", "enum": ("auto", "bge", "ngram")},
}


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 6:
        return value[0] + "****"
    return value[:3] + "****" + value[-2:]


class EnvConfigManager:
    """读写 .env 的白名单键。路径可被 ENV_FILE_PATH 覆盖（测试/Docker）。"""

    def __init__(self, env_path: str):
        self._path = env_path
        self._lock = threading.Lock()

    def _read_lines(self) -> List[str]:
        if not os.path.exists(self._path):
            return []
        with open(self._path, encoding="utf-8") as f:
            return f.readlines()

    def read_config(self) -> List[Dict[str, Any]]:
        """返回白名单键的当前生效值（os.environ 优先，其次 .env 文件），secret 打掩码。"""
        file_vals: Dict[str, str] = {}
        for line in self._read_lines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            file_vals[k.strip()] = v.strip().strip('"').strip("'")
        out = []
        for key, meta in ADMIN_ENV_KEYS.items():
            effective = os.environ.get(key, file_vals.get(key, ""))
            shown = _mask(effective) if meta["secret"] else effective
            out.append({
                "key": key, "label": meta["label"], "value": shown,
                "secret": meta["secret"], "restart": meta["restart"],
                "enum": list(meta.get("enum", ())) or None,
                "configured": bool(effective),
            })
        return out

    def write_config(self, values: Dict[str, str]) -> Dict[str, Any]:
        """校验 + 原子写入 + 同步 os.environ。返回 {updated, restart_required}。"""
        if not isinstance(values, dict) or not values:
            raise ValueError("values 必须为非空对象")
        cleaned: Dict[str, str] = {}
        for key, raw in values.items():
            if key not in ADMIN_ENV_KEYS:
                raise ValueError(f"不允许的配置键: {key}")
            meta = ADMIN_ENV_KEYS[key]
            v = str(raw).strip()
            # 换行检查必须针对 str 化后的值：raw 若是 int/dict 等非字符串，
            # `in raw` 会抛 TypeError 绕过端点的 ValueError→400 处理变成 500。
            if "\n" in v or "\r" in v:
                raise ValueError(f"{key} 不允许换行（防配置注入）")
            if meta.get("enum") and v and v not in meta["enum"]:
                raise ValueError(f"{key} 仅允许: {'/'.join(meta['enum'])}")
            cleaned[key] = v

        with self._lock:
            lines = self._read_lines()
            remaining = dict(cleaned)
            for i, line in enumerate(lines):
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                k = stripped.split("=", 1)[0].strip()
                if k in remaining:
                    lines[i] = f"{k}={remaining.pop(k)}\n"
            for k, v in remaining.items():  # 原文件没有的键 → 追加
                if lines and not lines[-1].endswith("\n"):
                    lines[-1] += "\n"
                lines.append(f"{k}={v}\n")
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(lines)
            os.replace(tmp, self._path)

        # 同步当前进程环境：非 restart 键立即生效；restart 键也写入，重启后由 load_dotenv 兜底
        for k, v in cleaned.items():
            os.environ[k] = v
        restart_required = [k for k in cleaned if ADMIN_ENV_KEYS[k]["restart"]]
        return {"updated": list(cleaned.keys()), "restart_required": restart_required}
