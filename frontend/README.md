# SafetyMind Frontend

Vue 3 + Vite 单页前端：对话、技能查看/热加载、知识库统计、检索演示、评测面板、监控面板。

- **无需构建即可运行**：`frontend/dist/` 为预构建产物，由后端 `api/main.py` 同源伺服（`/` 与 `/api/python/*`），`desktop.pyw` / `python -m api.main` / Docker 均直接使用。
- **开发模式**：`npm install && npm run dev`，Vite 代理把 `/api/python` 转发到本地 8000 端口（见 `vite.config.js`）。
- **重新构建**：`npm run build` 后产物落在 `dist/`，随仓库提交以保持桌面开箱即用。
- 后端地址可在页面右上角设置中修改（localStorage 持久化）。
