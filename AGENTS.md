<!-- shared-memory:BEGIN -->
## 项目共享记忆（memory/）

本项目所有 AI agent 通过同一 memory/ 目录共享长期记忆。Markdown 是事实源，索引是派生缓存。

### 读（接到任务的第一个动作）
1. 在项目根目录执行 `python scripts/mem.py search "<任务描述或关键词>" -k 5`。输出是**候选清单**：路径、命中片段、行号范围与文件标题大纲。
2. 命中后**不要只凭摘要下结论**：用 `python scripts/mem.py show <路径> --lines A-B` 或 `show <路径> --grep "<关键词>" -C 2` 回看原文。引用数字、指标、SHA/哈希、行号、页码、路径、表格数值前必须 `--grep` 复核。
3. 工具明确输出"无相关记忆"时即为检索无结果，改用 `rg -n -i "<关键词>" memory/` 并查看 `memory/index.md`，不要勉强套用低分候选。
4. 代码和用户说法优先于记忆。更正关系用 supersedes，默认只返回有效条目，历史加 `--include-outdated`；`search --no-rerank` 可用纯向量分数排序（分数区间为余弦相似度，与重排分不可比）。
5. `python scripts/mem.py doctor` 检查索引、协议与元数据。换 worktree/机器默认不是同一记忆目录，共享需显式设置绝对 MEMORY_DIR。

### 写（形成长期结论后立即落盘）
- 技术决策放 decisions/，含背景、结论、理由、被否方案；环境事实放 facts/；耗时问题的有效完整步骤放 howto/。
- 一个主题一个文件，frontmatter 含 date、status: active；修订同步 updated。
- 优先用 `python scripts/mem.py add --type fact --title "<主题>" --summary "<摘要>" --body-file "<正文文件>"`；type 可为 fact/decision/howto/correction。
- add 使用 YYYY-MM-DD-HHMM-主题-随机后缀.md、独占创建、写锁追加索引。手动写入必须唯一命名、UTF-8、末尾追加；禁止整文件覆盖来追加 index.md。
- 更正正文含旧说法（路径与原文）、新事实、依据；type: correction、supersedes: <旧相对路径>。add 加 `--supersedes "<旧路径>"` 会标记旧文件 outdated、更新 updated、标注旧索引行，保留旧正文。
- 写完执行 `python scripts/mem.py reindex`；失败不影响已落盘记忆，search 会从当前 Markdown 检索；API 恢复后再建索引。
- 禁止记录临时调试信息、代码直接可见内容、密钥和密码。

### 维护
- 每月一次；或记忆超过 80 文件、index.md 超过 60 行时运行 `python scripts/mem.py outdated`，以 updated（无则 date）计算阈值。
- 合并同主题（保留演进）→ 标记 outdated → 仅删除临时内容或已有完整更正替代的错误内容；决策不删。
- 维护后 reindex；memory/ 随代码提交，前缀 memory:。SQLite、WAL/SHM 与锁文件不提交。
<!-- shared-memory:END -->
