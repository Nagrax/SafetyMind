# SafetyMind 服务器部署指南（Ubuntu 22.04，2 核 2GiB 实测可行）

> 目标形态：systemd 守护单进程 uvicorn（127.0.0.1:8000）+ Caddy 反代自动 HTTPS。
> 内存预算：系统 ~400MB + BGE-base ~750MB + 余量，务必先建 2GB swap。
> 铁律：**单机只跑一个实例**（workers=1，不叠加第二份 uvicorn）——嵌入式 chroma
> 不支持多进程共享数据目录，并发打开会触发 HNSW 段损坏。

## 0. 前置

- Ubuntu 22.04（不要用 Windows Server 镜像）；云控制台安全组先只放行 22/80/443。
- 域名一个，A 记录指向服务器公网 IP（HTTPS 由 Caddy 自动签发）。

## 1. 系统准备（root）

```bash
adduser safetymind && usermod -aG sudo safetymind
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
apt update && apt install -y python3.12-venv git curl caddy
```

## 2. 代码与依赖

```bash
mkdir -p /opt/safetymind && chown safetymind:safetymind /opt/safetymind
su - safetymind
cd /opt/safetymind
git clone https://github.com/Nagrax/SafetyMind.git .
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 3. 配置 .env（关键步骤）

```bash
cp .env.example .env && nano .env
```

必须核对/修改的项：

| 变量 | 说明 |
|---|---|
| `ANTHROPIC_API_KEY` | 你的 LLM Key（智谱开放平台获取） |
| `ANTHROPIC_MODEL` | 建议 `glm-4.7-flash`（免费）；预算允许可用 `glm-4-plus` |
| `ADMIN_TOKEN` | **必须设置**强随机值：`openssl rand -hex 16`；不设则管理端点锁定 |
| `ACCESS_TOKEN` | **必须设置**：公网访问口令，发给使用者填进前端"设置"页；留空=任何人可用你的 Key |
| `ESCALATION_PHONE` / `ESCALATION_WEBHOOK_URL` | 值班电话 / 值班群机器人 |
| `HF_HOME` | 指向数据盘目录，如 `/opt/safetymind/hf_cache` |
| `API_HOST` | 保持 `127.0.0.1`（由 Caddy 对外；**不要**改成 0.0.0.0 绕过反代） |

## 4. systemd 守护

```bash
sudo cp deploy/safetymind.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now safetymind
curl -s http://127.0.0.1:8000/health   # 应返回 {"status":"ok",...}
```

## 5. Caddy 反代 + HTTPS

```bash
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile   # 把 your-domain.com 改成实际域名
sudo systemctl reload caddy
```

浏览器访问 `https://你的域名` → 点 ⚙ → 输入 ADMIN_TOKEN → 填值班电话/群机器人等。

## 6. 首次预热与验收清单

- [ ] `https://域名/health` 返回 ok
- [ ] 对话页发"车间着火了怎么办" → 秒回应急模板、`escalated=true`、显示上报编号
- [ ] 文档库上传一份企业规程 → 检索测试能命中
- [ ] （配置了 webhook）值班群收到升级推送
- [ ] `journalctl -u safetymind -n 50` 无 ERROR

## 7. 备份（建议每日 cron）

```bash
0 3 * * * tar -czf /opt/safetymind_backup/$(date +\%F).tgz /opt/safetymind/data
```

备份对象：`data/audit.db`（审计/工单）、`data/conversations/`（会话）、`data/kb_chroma/`（知识库）、`data/chroma/`（情景记忆）。

## 常见问题

- **启动 90 秒超时退出**：看 `journalctl -u safetymind -n 100`；多为 `.env` 缺 Key 或内存不足（确认 swap 已启用）。
- **首次对话慢**：冷启动 BGE 加载属一次性；若持续慢，确认 `SAFETYMIND_BGE=1` 与 `HF_HOME` 指向已缓存目录。
- **529 过载**：免费模型高峰限流，紧急路径不受影响（零 LLM 模板）；预算允许可切 `glm-4-plus`。
