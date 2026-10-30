# -*- coding: utf-8 -*-
# K04 corpus.acquisition 回归测试（standalone，零业务依赖）：
# 用法: python tests/test_corpus_acquisition.py
# 覆盖方案 §2.3/§4.1/§6/§9：
#   本地 HTTP 文件服务端到端：下载 → 流式 sha256 → 格式签名校验 → 内容寻址落盘 → 台阶推进；
#   断点续传（中断后续传，总传输字节小于全量）；幂等（不新增映射/不重复落盘）；
#   重复分享只遍历一次；失败如实（500/内容与 mime 不符/sha256 篡改）；
#   容量守卫（单文件上限/总预算/D 盘闲置）保留待办与大小；台阶跳级报错；
#   凭据脱敏（含 cookie/token 的 URL 不进库/报告）。
import hashlib
import json
import os
import shutil
import sys
import threading
import urllib.parse
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import corpus.acquisition as acquisition  # noqa: E402
from corpus import paths  # noqa: E402
from corpus.manifest import CorpusManifest, _SECRET_RE  # noqa: E402

RESULTS = []
SCRATCH = Path("D:/claudecode/scratch/sm-corpus-20261019")  # 一切临时产物只落 D 盘（铁律 7）
SAMPLES = SCRATCH / "k04-samples"
_TMPS = []
COUNTER = {"requests": 0, "full_gets": 0}
BASE = ""


def record(name, ok, detail=""):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


# ── 本地 HTTP 文件服务（支持 Range/HEAD，即网盘替身）────────────────────────────
class _Handler(BaseHTTPRequestHandler):
    root_dir = None
    counter = None

    def log_message(self, *a):  # 静音
        pass

    def log_error(self, *a):
        pass

    def do_HEAD(self):
        self._serve(head_only=True)

    def do_GET(self):
        self._serve(head_only=False)

    def _plain(self, status, body=b"", ctype="text/plain"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _serve(self, head_only):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        if self.counter is not None:
            self.counter["requests"] += 1
            if not head_only and not (self.headers.get("Range")):
                self.counter["full_gets"] += 1
        if path == "/error":
            return self._plain(500, b"server error")
        if path == "/mismatch":  # 内容与 mime 不符：文本却声明 PDF
            return self._plain(200, b"plain text pretending to be a pdf", ctype="application/pdf")
        fp = Path(self.root_dir) / path.lstrip("/")
        if not fp.is_file():
            return self._plain(404, b"")
        st = fp.stat()
        size = st.st_size
        etag = f'"{int(st.st_mtime)}-{size}"'
        last_mod = self.date_time_string(int(st.st_mtime))
        start, end = 0, size - 1
        status = 200
        rng = self.headers.get("Range")
        if rng:
            import re as _re
            m = _re.match(r"bytes=(\d+)-(\d*)", rng)
            if m:
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
                status = 206
        end = min(end, size - 1)
        length = max(end - start + 1, 0)
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", last_mod)
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head_only:
            return
        try:
            with fp.open("rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    buf = f.read(min(65536, remaining))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    remaining -= len(buf)
        except Exception:  # 客户端提前断开（断点续传测试）不算服务端失败
            pass


def start_server():
    global BASE
    SAMPLES.mkdir(parents=True, exist_ok=True)
    _Handler.root_dir = SAMPLES.as_posix()
    _Handler.counter = COUNTER
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    BASE = f"http://127.0.0.1:{srv.server_address[1]}"
    return srv


# ── 样本与工具 ───────────────────────────────────────────────────────────────
def build_samples():
    SAMPLES.mkdir(parents=True, exist_ok=True)
    body = bytes((i * 37 + 11) % 256 for i in range(300_000))
    (SAMPLES / "sample.pdf").write_bytes(b"%PDF-1.4\n" + body)
    (SAMPLES / "note.txt").write_bytes("安全生产资料样例文本\n".encode("utf-8") * 20)


class _AbortResponse:
    """模拟传输中断：读满 limit 字节后抛错（断点续传测试）。"""

    def __init__(self, resp, limit):
        self._r = resp
        self._limit = limit
        self._seen = 0

    def __getattr__(self, name):
        return getattr(self._r, name)

    def read(self, n):
        if self._seen >= self._limit:
            raise IOError("injected truncation (test)")
        want = min(n, self._limit - self._seen)
        buf = self._r.read(want)
        self._seen += len(buf)
        if len(buf) < want:
            raise IOError("injected truncation (test)")
        return buf

    def close(self):
        self._r.close()


class _AbortTransport:
    """只对「第一次无 Range 的 GET」注入中断，其余（探测/续传）正常透传。"""

    def __init__(self, base, abort_after):
        self.base = base
        self.abort_after = abort_after
        self.armed = True

    def __call__(self, url, *, method="GET", headers=None, timeout=None):
        resp = self.base(url, method=method, headers=headers, timeout=timeout)
        if method == "GET" and not (headers or {}).get("Range") and self.armed:
            self.armed = False
            return _AbortResponse(resp, self.abort_after)
        return resp


def tmp_dir(prefix):
    SCRATCH.mkdir(parents=True, exist_ok=True)
    d = Path(tempfile.mkdtemp(prefix=prefix, dir=str(SCRATCH)))
    _TMPS.append(d)
    return d


def set_root(root):
    prev = os.environ.get("SAFETYMIND_CORPUS_ROOT")
    os.environ["SAFETYMIND_CORPUS_ROOT"] = str(root)
    return prev


def restore_root(prev):
    if prev is None:
        os.environ.pop("SAFETYMIND_CORPUS_ROOT", None)
    else:
        os.environ["SAFETYMIND_CORPUS_ROOT"] = prev


def new_env(prefix="k04_"):
    tmp = tmp_dir(prefix)
    prev = set_root(tmp)
    return tmp, prev


def new_downloader(prefix="k04_", **kwargs):
    tmp, prev = new_env(prefix)
    return acquisition.Downloader(root=tmp, **kwargs), tmp, prev


def count_originals(root):
    base = Path(root) / "originals" / "sha256"
    return sum(1 for p in base.rglob("*") if p.is_file()) if base.exists() else 0


def list_parts(root):
    return sorted((Path(root) / "staging" / "downloads").glob("*.part"))


def source_of(dl, item_id):
    rows = [s for s in dl.manifest.list_sources() if s["item_id"] == item_id]
    return rows[-1] if rows else {}


def raises_value_error(fn):
    try:
        fn()
        return False
    except ValueError:
        return True


def cleanup(m, tmp, prev):
    try:
        m.close()
    except Exception:
        pass
    restore_root(prev)
    shutil.rmtree(tmp, ignore_errors=True)


# ── 1. 端到端 ────────────────────────────────────────────────────────────────
def test_end_to_end():
    data = (SAMPLES / "sample.pdf").read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    dl, tmp, prev = new_downloader("e2e_")
    try:
        r = dl.download(f"{BASE}/sample.pdf", item_id="k04-e2e-1", target_name="sample.pdf",
                        mime_hint="application/pdf", title="端到端样例")
        record("端到端 status=verified", r["status"] == "verified", f"status={r['status']}")
        record("流式 sha256 与本地一致", r.get("sha256") == sha, f"{r.get('sha256')} vs {sha}")
        record("字节数与本地一致", r.get("bytes") == len(data), f"{r.get('bytes')}")
        dest = Path(r.get("path", ""))
        record("内容寻址落盘 originals/sha256/<前2位>/<sha256>",
               dest.is_file() and dest.as_posix().endswith(f"originals/sha256/{sha[:2]}/{sha}"),
               f"path={r.get('path')}")
        record("原件落 D 盘", dest.as_posix().startswith("D:/"), f"path={r.get('path')}")
        record("落盘内容与源一致", dest.read_bytes() == data)
        src = source_of(dl, "k04-e2e-1")
        record("台阶推进到 file_downloaded_verified",
               src.get("ladder") == ["link_exported", "share_accessible", "file_downloaded_verified"],
               f"ladder={src.get('ladder')}")
        record("acquisition_status=acquired_verified",
               src.get("acquisition_status") == "acquired_verified", f"{src.get('acquisition_status')}")
        record("条目→文件映射建立", len(dl.manifest.list_mappings("k04-e2e-1")) == 1)
        record("成功后清理 .part 与 meta", list_parts(tmp) == [])
        recon = dl.manifest.reconciliation(provider="toolbox_catalog")
        record("对账按 file_downloaded_verified 计数已取得文件",
               recon["acquired_files_verified"] == 1 and recon["verified"] == 1,
               f"{recon['acquired_files_verified']}/{recon['verified']}")
    finally:
        cleanup(dl.manifest, tmp, prev)


# ── 2. 断点续传 ──────────────────────────────────────────────────────────────
def test_resume():
    data = (SAMPLES / "sample.pdf").read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    tmp, prev = new_env("resume_")
    dl = acquisition.Downloader(root=tmp, transport=_AbortTransport(acquisition.urllib_transport, 100_000))
    try:
        r1 = dl.download(f"{BASE}/sample.pdf", item_id="k04-resume-1", target_name="sample.pdf",
                         mime_hint="application/pdf")
        parts = list_parts(tmp)
        record("中断不记 verified（如实为 pending）",
               r1["status"] == "pending" and "transfer_interrupted" in r1.get("reason", ""),
               f"status={r1['status']} reason={r1.get('reason')}")
        record("中断保留 .part 于断点处",
               len(parts) == 1 and parts[0].stat().st_size == 100_000,
               f"parts={[p.stat().st_size for p in parts]}")
        src = source_of(dl, "k04-resume-1")
        record("中断后未置 file_downloaded_verified",
               "file_downloaded_verified" not in (src.get("ladder") or []), f"ladder={src.get('ladder')}")
        record("中断后仍留有 meta（可续传）",
               len(list((Path(tmp) / "staging" / "downloads").glob("*.meta.json"))) == 1)

        dl._transport = acquisition.urllib_transport  # 恢复网络
        r2 = dl.download(f"{BASE}/sample.pdf", item_id="k04-resume-1", target_name="sample.pdf",
                         mime_hint="application/pdf")
        record("续传后 status=verified", r2["status"] == "verified", f"status={r2['status']}")
        record("续传后 sha256 正确", r2.get("sha256") == sha, f"{r2.get('sha256')}")
        record("续传传输字节 < 全量（证明没重下）",
               r2.get("transferred_bytes", 10 ** 9) < len(data),
               f"resume={r2.get('transferred_bytes')} total={len(data)}")
        record("两次传输合计 < 2× 全量",
               r1.get("transferred_bytes", 0) + r2.get("transferred_bytes", 0) < 2 * len(data),
               f"sum={r1.get('transferred_bytes', 0) + r2.get('transferred_bytes', 0)}")
    finally:
        cleanup(dl.manifest, tmp, prev)


# ── 3. 幂等 ──────────────────────────────────────────────────────────────────
def test_idempotent():
    dl, tmp, prev = new_downloader("idem_")
    try:
        r1 = dl.download(f"{BASE}/sample.pdf", item_id="k04-idem", target_name="sample.pdf",
                         mime_hint="application/pdf")
        r2 = dl.download(f"{BASE}/sample.pdf", item_id="k04-idem", target_name="sample.pdf",
                         mime_hint="application/pdf")
        record("重复调用复用（status=reused）",
               r1["status"] == "verified" and r2["status"] == "reused",
               f"{r1['status']}/{r2['status']}")
        record("同(item_id,url) 只下载一次", dl.downloads_started == 1, f"{dl.downloads_started}")
        record("不新增映射行", len(dl.manifest.list_mappings("k04-idem")) == 1)
        record("不重复落盘（原件唯一）", count_originals(tmp) == 1, f"{count_originals(tmp)}")
    finally:
        cleanup(dl.manifest, tmp, prev)


# ── 4. 重复分享只遍历一次 ────────────────────────────────────────────────────
def test_shared_share_once():
    dl, tmp, prev = new_downloader("share_")
    try:
        before = COUNTER["full_gets"]
        for iid in ("k04-sh-1", "k04-sh-2", "k04-sh-3"):
            dl.download(f"{BASE}/sample.pdf", item_id=iid, target_name="sample.pdf",
                        mime_hint="application/pdf")
        record("3 条目指向同一分享 → 只下载 1 次", dl.downloads_started == 1,
               f"downloads={dl.downloads_started}")
        record("服务端只收到 1 次完整 GET", COUNTER["full_gets"] - before == 1,
               f"delta={COUNTER['full_gets'] - before}")
        record("映射保留 3 条（谱系不合并）", len(dl.manifest.list_mappings()) == 3,
               f"{len(dl.manifest.list_mappings())}")
        record("原件仍只 1 份", count_originals(tmp) == 1, f"{count_originals(tmp)}")
        shares = dl.registry.unique_shares()
        record("unique_shares 归并为 1 个分享 3 条目",
               len(shares) == 1 and len(shares[0]["items"]) == 3,
               f"shares={shares}")
        plan = dl.registry.entry_asset_plan(["k04-sh-2"])
        record("entry_asset_plan 复用已有资产",
               plan["reusable"] and not plan["to_download"] and len(plan["items"]["k04-sh-2"]) == 1,
               f"{plan}")
    finally:
        cleanup(dl.manifest, tmp, prev)


# ── 5. 失败如实 ──────────────────────────────────────────────────────────────
def test_failures_honest():
    dl, tmp, prev = new_downloader("fail_")
    try:
        r500 = dl.download(f"{BASE}/error", item_id="k04-f-500", target_name="x.pdf",
                           mime_hint="application/pdf")
        record("HTTP 500 → invalid_access_blocked",
               r500["status"] == "invalid_access_blocked" and r500.get("reason") == "http_500",
               f"{r500}")
        record("500 后状态如实且未记 verified",
               source_of(dl, "k04-f-500").get("acquisition_status") == "invalid_access_blocked"
               and "file_downloaded_verified" not in (source_of(dl, "k04-f-500").get("ladder") or []))

        rm = dl.download(f"{BASE}/mismatch", item_id="k04-f-mime", target_name="fake.pdf",
                         mime_hint="application/pdf")
        record("内容与 mime 不符 → parse_failed",
               rm["status"] == "parse_failed" and "signature_mismatch" in rm.get("reason", ""),
               f"{rm}")
        record("不符内容不落原件", count_originals(tmp) == 0, f"{count_originals(tmp)}")
        record("不符后 parse_status=unparseable",
               source_of(dl, "k04-f-mime").get("parse_status") == "unparseable")

        rt = dl.download(f"{BASE}/sample.pdf", item_id="k04-f-sha", target_name="sample.pdf",
                         mime_hint="application/pdf", expected_sha256="0" * 64)
        record("sha256 不符（篡改）→ parse_failed",
               rt["status"] == "parse_failed" and "sha256_mismatch" in rt.get("reason", ""), f"{rt}")
        record("三项失败均未记 file_downloaded_verified",
               all("file_downloaded_verified" not in (source_of(dl, i).get("ladder") or [])
                   for i in ("k04-f-500", "k04-f-mime", "k04-f-sha")))
        recon = dl.manifest.reconciliation(provider="toolbox_catalog")
        record("失败项进 blocked 桶且不计已取得文件",
               recon["blocked"] == 3 and recon["acquired_files_verified"] == 0,
               f"blocked={recon['blocked']} verified={recon['acquired_files_verified']}")
        record("失败项无映射行", len(dl.manifest.list_mappings()) == 0)
    finally:
        cleanup(dl.manifest, tmp, prev)


# ── 6. 容量守卫 ──────────────────────────────────────────────────────────────
def test_capacity_guard():
    data = (SAMPLES / "sample.pdf").read_bytes()
    tmp, prev = new_env("cap_")
    m = CorpusManifest(paths.manifest_db(tmp))
    try:
        dl1 = acquisition.Downloader(root=tmp, manifest=m, max_bytes=10)
        r1 = dl1.download(f"{BASE}/sample.pdf", item_id="cap-1", target_name="sample.pdf",
                          mime_hint="application/pdf")
        record("超单文件上限 → capacity_blocked",
               r1["status"] == "capacity_blocked" and r1["reason"] == "single_file_over_limit", f"{r1}")
        s1 = [s for s in m.list_sources() if s["item_id"] == "cap-1"][0]
        record("状态记为 capacity_blocked",
               s1["acquisition_status"] == "capacity_blocked", f"{s1['acquisition_status']}")
        record("保留待办与大小（pending_bytes）",
               int(s1["unknown_fields"].get("pending_bytes", 0)) == len(data),
               f"{s1['unknown_fields']}")

        dl2 = acquisition.Downloader(root=tmp, manifest=m, total_budget_bytes=10)
        r2 = dl2.download(f"{BASE}/sample.pdf", item_id="cap-2", target_name="sample.pdf",
                          mime_hint="application/pdf")
        record("超总预算 → capacity_blocked",
               r2["status"] == "capacity_blocked" and r2["reason"] == "total_budget_exceeded", f"{r2}")

        dl3 = acquisition.Downloader(root=tmp, manifest=m, disk_free_guard_bytes=10 ** 15)
        probes_before = dl3.probes
        r3 = dl3.download(f"{BASE}/sample.pdf", item_id="cap-3", target_name="sample.pdf",
                          mime_hint="application/pdf")
        record("D 盘闲置不足 → capacity_blocked",
               r3["status"] == "capacity_blocked" and r3["reason"] == "disk_free_below_guard", f"{r3}")
        record("闲置不足时不发起下载", dl3.downloads_started == 0 and dl3.probes == probes_before)
        record("容量阻塞不落 .part", list_parts(tmp) == [])
        record("容量阻塞不落原件", count_originals(tmp) == 0)
        recon = m.reconciliation(provider="toolbox_catalog")
        record("容量阻塞项在 blocked 桶、未被改 done",
               recon["blocked"] == 3 and recon["todo"] == 0 and recon["verified"] == 0,
               f"blocked={recon['blocked']} todo={recon['todo']}")
    finally:
        m.close()
        restore_root(prev)
        shutil.rmtree(tmp, ignore_errors=True)


# ── 7. 台阶状态机 ────────────────────────────────────────────────────────────
def test_ladder_machine():
    tmp, prev = new_env("ladder_")
    dl = acquisition.Downloader(root=tmp)
    try:
        dl.manifest.upsert_source("toolbox_catalog", "lad-x", title="台阶样例")
        record("跳级报错（空台阶直接记 share_accessible）",
               raises_value_error(lambda: dl.mark_ladder("lad-x", "share_accessible")))
        record("未知台阶报错",
               raises_value_error(lambda: dl.mark_ladder("lad-x", "bogus_step")))
        dl.mark_ladder("lad-x", "link_exported")
        record("无资产不得记 file_downloaded_verified（跳级+无资产双重拒绝）",
               raises_value_error(lambda: dl.mark_ladder("lad-x", "file_downloaded_verified",
                                                         asset_id="0" * 64)))
        dl.mark_ladder("lad-x", "share_accessible")
        record("仍拒绝无资产的 file_downloaded_verified",
               raises_value_error(lambda: dl.mark_ladder("lad-x", "file_downloaded_verified",
                                                         asset_id="0" * 64)))
        aid = dl.manifest.register_asset(b"%PDF-1.4 ladder-sample", mime="application/pdf")
        dl.mark_ladder("lad-x", "file_downloaded_verified", asset_id=aid)
        row = source_of(dl, "lad-x")
        record("逐级推进后台阶齐全",
               row["ladder"] == ["link_exported", "share_accessible", "file_downloaded_verified"],
               f"{row['ladder']}")
        record("file_downloaded_verified 后 acquisition_status=acquired_verified",
               row["acquisition_status"] == "acquired_verified", f"{row['acquisition_status']}")
        record("重复记同一台阶幂等",
               dl.mark_ladder("lad-x", "link_exported") == row["ladder"])
        dl.mark_ladder("lad-x", "body_parsed")
        row2 = source_of(dl, "lad-x")
        record("body_parsed 后 parse_status=parsed",
               row2["parse_status"] == "parsed" and row2["acquisition_status"] == "parsed",
               f"{row2['parse_status']}/{row2['acquisition_status']}")
    finally:
        cleanup(dl.manifest, tmp, prev)


# ── 8. 凭据脱敏 ──────────────────────────────────────────────────────────────
def test_credential_redaction():
    secret = "SECRETTOKEN123"
    tmp, prev = new_env("cred_")
    dl = acquisition.Downloader(root=tmp)
    try:
        url = f"{BASE}/sample.pdf?token={secret}&x=1"
        record("redact 抹除 URL 参数凭据",
               secret not in acquisition.redact(url) and not _SECRET_RE.search(acquisition.redact(url)),
               acquisition.redact(url))
        record("redact 处理 kv 与提取码形式",
               "REDACTED" in acquisition.redact("cookie=abc; token=xyz")
               and secret not in acquisition.redact(f"url?pwd={secret}")
               and not _SECRET_RE.search(acquisition.redact("访问码：abcd")))
        r = dl.download(url, item_id="cred-1", target_name="sample.pdf", mime_hint="application/pdf")
        record("含凭据 URL 仍可正常取件", r["status"] == "verified", f"{r['status']}")
        record("返回的 url_ref 已脱敏",
               secret not in r["url_ref"] and not _SECRET_RE.search(r["url_ref"]), r["url_ref"])
        row = source_of(dl, "cred-1")
        record("link_ref 入库前已脱敏",
               secret not in row["link_ref"] and not _SECRET_RE.search(row["link_ref"]), row["link_ref"])
        record("raw 中不含凭据", secret not in json.dumps(row["raw"], ensure_ascii=False))
        blob = (tmp / "manifest.sqlite").read_bytes()
        wal = tmp / "manifest.sqlite-wal"
        if wal.exists():
            blob += wal.read_bytes()
        record("凭据未进入 manifest 库文件（含 WAL）", secret.encode() not in blob)
    finally:
        cleanup(dl.manifest, tmp, prev)


# ── 9. 内容寻址落盘 ──────────────────────────────────────────────────────────
def test_save_original_dedupe():
    data = (SAMPLES / "sample.pdf").read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    tmp, prev = new_env("save_")
    try:
        m = CorpusManifest(paths.manifest_db(tmp))
        a = acquisition.save_original(SAMPLES / "sample.pdf", mime="application/pdf",
                                      manifest=m, root=tmp)
        b = acquisition.save_original(SAMPLES / "sample.pdf", mime="application/pdf",
                                      manifest=m, root=tmp)
        record("save_original 内容寻址 sha256",
               a["asset_id"] == sha and a["sha256"] == sha and a["bytes"] == len(data), f"{a}")
        record("同内容第二次不重复落盘（deduped）",
               b["asset_id"] == sha and b["deduped"] is True and count_originals(tmp) == 1,
               f"{b}")
        record("save_original 落 D 盘",
               Path(a["path"]).as_posix().startswith("D:/"), a["path"])
        record("字节输入路径同样可用",
               acquisition.save_original(data, mime="application/pdf", manifest=m, root=tmp)["asset_id"] == sha)
        m.close()
    finally:
        restore_root(prev)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    build_samples()
    srv = start_server()
    try:
        for fn in [test_end_to_end, test_resume, test_idempotent, test_shared_share_once,
                   test_failures_honest, test_capacity_guard, test_ladder_machine,
                   test_credential_redaction, test_save_original_dedupe]:
            try:
                fn()
            except Exception as ex:
                record(fn.__name__, False, f"异常: {type(ex).__name__}: {ex}")
    finally:
        srv.shutdown()
    passed, total = sum(RESULTS), len(RESULTS)
    print(f"\n{passed}/{total} passed")
    for d in _TMPS:
        shutil.rmtree(d, ignore_errors=True)
    sys.exit(0 if passed == total else 1)
