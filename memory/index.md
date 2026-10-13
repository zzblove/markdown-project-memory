# 记忆索引

新增条目仅追加；使用 mem.py add 避免并发冲突。

- 共享记忆服务修复方案 — `memory/decisions/2026-10-07-2320-共享记忆服务修复方案-ff04b19a.md` — 单机原子事务、后台向量化和摘要版本更新，测试隔离真实数据

- 共享记忆服务修复验证环境 — `memory/facts/2026-10-07-2347-共享记忆服务修复验证环境-9c2bdd9f.md` — Windows Python 3.11 隔离回归、MCP 链路和自启脚本验证通过

- Markdown 方案 Git 发布边界 — `memory/decisions/2026-10-08-0913-Markdown 方案 Git 发布边界-de34c873.md` — 发布协议、脚本与 Markdown，排除缓存、凭据及独立服务包

- Markdown 方案 GitHub 发布位置 — `memory/facts/2026-10-08-0921-Markdown 方案 GitHub 发布位置-ff5a7956.md` — zzblove 私有仓库 markdown-project-memory，main 分支同步 Markdown（已被更正）

- Windows 短路径造成记忆更正断链的修复 — `memory/howto/2026-10-08-0924-Windows 短路径造成记忆更正断链的修复-d5e6d470.md` — 统一真实路径身份并验证短路径与目录链接，避免更正关系失效

- 更正：Markdown 方案仓库改为公开 — `memory/facts/2026-10-08-1037-更正：Markdown 方案仓库改为公开-35840c0e.md` — 按用户要求改为 PUBLIC，并通过匿名访问确认公开状态

- 共享记忆检索优化与多 agent 兼容约束 — `memory/decisions/2026-10-13-1030-共享记忆检索优化与多 agent 兼容约束-44572750.md` — 保留 Markdown 与写锁，优化混合检索、缓存、证据读取及输出预算

- 2.2.0 共享记忆优化验证环境 — `memory/facts/2026-10-13-1106-2.2.0 共享记忆优化验证环境-629686de.md` — Windows 离线回归 35 项通过，36 条检索样例与本地 HTTP 接口验证通过

- 2.2.0 优化草稿 PR 与双平台验证 — `memory/facts/2026-10-13-1118-2.2.0 优化草稿 PR 与双平台验证-4ade4288.md` — 草稿 PR 1 已建立，实现提交通过 Windows/Linux 六组 Python 检查
