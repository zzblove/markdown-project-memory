<!-- shared-memory:BEGIN -->
## 项目共享记忆（memory/）

本项目所有 AI agent 通过同一 memory/ 目录共享长期记忆。Markdown 是事实源，索引是派生缓存。

### 读（接到任务的第一个动作）
1. 在项目根目录执行 `python scripts/mem.py search "<任务描述或关键词>"`。默认先展示 3 个候选、总输出最多 1500 字符；需要更多用 `-k 5 --max-chars 4000`。`--mode local` 完全本地检索，不读取 key 或调用 API。
2. 摘要只用于筛选：先用候选给出的 `show --lines A-B` 阅读命中段、适用范围和依据；条件不清楚时展开上下文或全文。引用数字、指标、SHA/哈希、路径、表格数值前用 `show --grep "<关键词>" -C 2` 复核。
3. 工具明确输出"无相关记忆"时即为检索无结果，改用 `rg -n -i "<关键词>" memory/` 并查看 `memory/index.md`，不要勉强套用低分候选。
4. 代码和用户说法优先于记忆。更正关系用 supersedes，默认只返回有效条目，历史加 `--include-outdated`；`--no-rerank` 保留关键词/向量排名融合。向量、融合名次和重排分不可比较，低置信候选不能作为答案。
5. `python scripts/mem.py doctor` 检查索引、协议与元数据。索引部分同步时，未变化文件仍复用向量，变更文件用当前 Markdown；候选未重排，不能当作已验证答案。换 worktree/机器默认不是同一记忆目录，共享需显式设置绝对 MEMORY_DIR。

### 写（形成长期结论后立即落盘）
- 技术决策放 decisions/，含背景、结论、理由、被否方案；环境事实放 facts/；耗时问题的有效完整步骤放 howto/。
- 一个主题一个文件，frontmatter 含 date、status: active、summary；可用 --scope 区分环境/模块、--source 记录依据、--verified-at 记录核实日期。范围不是作者隔离，所有 agent 默认检索整个共享目录。
- 优先用 `python scripts/mem.py add --type fact --title "<主题>" --summary "<摘要>" --body-file "<正文文件>"`；type 可为 fact/decision/howto/correction。
- add 在同一写锁内检查完全重复内容并发布。相同类型、标题、摘要、正文、范围和依据的有效记忆复用已有文件；不同条件保留独立记录。使用 --no-dedupe 可显式追加。手动写入必须唯一命名、UTF-8、末尾追加。
- 更正正文含旧说法、路径、新事实与依据。supersedes 使用 memory/ 开头的稳定记忆键，支持 JSON 字符串或数组；逗号属于单个路径。add 标记旧文件 outdated、更新 updated，保留旧正文。目标已经被取代时先读取当前条目再更正。
- 写完执行 `python scripts/mem.py reindex`；失败不影响已落盘记忆。离线维护用 `reindex --local` 复用兼容的已有嵌入，其余文件待恢复 API 后生成向量；不删除历史向量。API 缓存只复用请求响应，不缓存最终候选清单；每次检索仍核对当前 Markdown。
- 禁止记录临时调试信息、代码直接可见内容、密钥和密码。

### 维护
- 每月一次；或记忆超过 80 文件、index.md 超过 60 行时运行 `python scripts/mem.py outdated`，以 updated（无则 date）计算阈值。
- 用 `audit` 检查摘要、范围、依据缺失。`annotate <路径> --summary ... --scope ... --source ...` 默认仅预览；确认后加 `--apply --expected-hash <预览哈希>`，冲突时重新读取。保留正文、原日期和未知元数据；只在真正核实事实后填写 verified_at。
- 用 `eval <问题集.json> --report <memory/外的报告.json>` 验证真实问题，默认 local，不自动建云端索引。标签须含原文摘录与正文哈希；私有问题与报告留在本机。无答案仍出现候选要单独检查，不能只看命中率。
- 合并同主题（保留演进）→ 标记 outdated → 仅删除临时内容或已有完整更正替代的错误内容；决策不删。
- 维护后 reindex；memory/ 随代码提交，前缀 memory:。SQLite、WAL/SHM 与锁文件不提交。
<!-- shared-memory:END -->
