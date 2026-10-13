# Markdown 项目共享记忆

用 Markdown 保存项目的长期结论，让同一项目中的 AI agent 能检索、核对和补充已有知识。Markdown 是事实源，SQLite 只保存可重建的检索与 API 响应缓存。工具版本为 `2.2.0`，仅依赖 Python 标准库。

适合记录技术决策、环境约束和解决复杂问题的完整步骤。它不自动保存聊天全文；agent 按 `AGENTS.md` 协议主动读写。不同机器或 worktree 通过 Git 同步 Markdown，各自重建检索缓存。

## 文件结构

```text
AGENTS.md                 agent 的读取、核对、写入和维护协议
scripts/mem.py            命令行工具
memory/index.md           可直接阅读的记忆目录
memory/decisions/         技术决策与被否方案
memory/facts/             已核实的环境事实
memory/howto/             有效的解决步骤
tests/test_mem.py         不调用外部 API 的隔离测试
```

本仓库 `memory/` 中已有的服务修复记录是本项目的历史。对另一个项目执行 `init` 只安装脚本、目录和协议，不会复制这些历史记录或 API key。独立的 SQLite MCP 服务及其压缩包不包含在这套方案中。

## 安装到项目

需要 Python 3.10 或更高版本；当前本机验证环境是 Python 3.11。下载或克隆本仓库，在仓库根目录运行：

```sh
python scripts/mem.py init "<已存在的目标项目绝对路径>"
```

然后进入目标项目的根目录：

```sh
python scripts/mem.py search "项目环境和开发约定" -k 5
python scripts/mem.py doctor
```

`init` 会安装或更新 `scripts/mem.py`，在 `AGENTS.md` 中更新记忆协议，保留协议之外的项目约定、已有记忆和已有忽略规则。若目标项目自行修改过 `scripts/mem.py`，升级前应先比较修改，因为脚本本身会被替换。

没有语义索引或没有可用 API 时，`search` 自动检索当前 Markdown。`doctor` 会提示索引未同步；这不妨碍离线查看、写入或关键词检索。

## 日常使用

### 先检索，再读原文

```sh
python scripts/mem.py search "部署环境"
python scripts/mem.py search "部署环境" --mode local
python scripts/mem.py search "部署环境" -k 5 --max-chars 4000
python scripts/mem.py show "memory/facts/<命中文件>.md"
python scripts/mem.py show "memory/facts/<命中文件>.md" --grep "Python" -C 2
python scripts/mem.py show "memory/facts/<命中文件>.md" --lines 1-20
```

默认展示最多 3 个候选，搜索输出和简短诊断合计最多 1500 字符（不是精确 token 数）。每个候选给出原文行范围；先阅读命中段和适用条件，依据不足时再展开上下文或全文。预算不足会明确提示仍有候选，不会伪装成“无相关记忆”。

检索结果只是候选清单。引用数字、路径、哈希等信息前，必须回看原文；`--grep` 按字面文本匹配。没有相关记忆时，查看 `memory/index.md`，或使用 `rg -n -i "<关键词>" memory/`。当前代码和用户提供的新信息优先于旧记忆。旧记录没有摘要元数据时仍使用原文片段，不需要批量改写历史。

### 写入长期结论

先把正文写入 UTF-8 文本文件，例如 `decision-body.txt`：

```text
背景：为什么需要做出这个决定。
结论：选用的方案与适用范围。
理由：支持方案的已核实依据。
被否方案：考虑过的替代方案，以及不采用的原因。
```

```sh
python scripts/mem.py add --type decision --title "部署方案" --summary "记录部署选择与原因" --body-file decision-body.txt
python scripts/mem.py reindex
```

支持 `fact`、`decision`、`howto`、`correction` 四种类型。摘要保存在 frontmatter 并参与检索。可增加 `--scope production`、`--source "部署清单 revision 3"` 和 `--verified-at 2026-10-13`，让不同环境的事实及其依据明确区分；日期需为 `YYYY-MM-DD`。

`add` 在同一个进程间写锁内检查重复、创建唯一文件并更新目录，避免并发覆盖或丢失。同类型、标题、摘要、正文、范围、依据、核实日期和更正目标都相同的有效记录会返回已有路径；不同范围或依据保留独立记录。需要显式追加同内容时使用 `--no-dedupe`。去重不会刷新未重新核实的旧日期。

形成长期结论后及时保存；不要记录密钥、密码、临时调试信息或代码已经直接表达的内容。正文临时文件用完后可移走，正式记忆保存在 `memory/`。

### 更正与历史

更正正文应包含旧文件路径及旧说法、新事实和核实依据：

```sh
python scripts/mem.py add --type correction --title "部署方案更新" --summary "新环境替代旧环境" --body-file correction-body.txt --supersedes "memory/decisions/<旧文件>.md"
python scripts/mem.py reindex
python scripts/mem.py search "部署方案" --include-outdated
```

更正会将旧条目标记为 `outdated`，保留正文并建立替代关系。默认检索有效条目，`--include-outdated` 可查看历史。

新引用统一保存为 `memory/facts/...` 等记忆键，相对于实际记忆目录，跨工作目录共享时保持相同身份。单个 `supersedes` 路径不会按逗号拆分；手写多个目标使用 JSON 数组。相同更正可复用；目标已被另一条更正替代时，工具会给出当前条目路径，应先读取最新结论再更正。绝对路径必须留在记忆目录内。

## 检索模式与预算

| 参数 | 行为 |
| --- | --- |
| `--mode auto`（默认） | 先做本地检索；唯一精确标题或明确路径/哈希匹配走本地，否则在新鲜语义索引上融合关键词与向量候选 |
| `--mode local` | 直接检索当前 Markdown；不读取 key，不调用嵌入或重排 API |
| `--mode hybrid` | 有可用索引时执行关键词/向量排名融合，适合验证完整流程 |
| `--scope production` | 精确筛选范围；不提供时检索整个共享目录，范围不用于隔离作者 |
| `--no-rerank` | 保留排名融合候选，跳过重排；仍可能调用查询嵌入接口 |
| `--rerank-limit 10`（默认） | 最多重排 10 个不同文件的相关片段；`-k` 是展示上限，不会突破此调用预算 |
| `--max-chars 1500`（默认） | 限制总搜索输出字符数，返回摘要/片段与完整引用入口 |
| `--min-vector-score` | 可选余弦阈值（-1 到 1）；默认不把重排阈值套用到向量 |
| `--rerank-scale probability`（默认） | 重排分须在 0 到 1，默认弃权阈值 0.70；`logit` 使用原始分，默认阈值 0 |
| `--min-rerank-score` | 覆盖当前分制的重排弃权阈值，需要按实际查询标定 |
| `--no-cache` | 不读写 API 响应缓存，用于检查接口或模型变化 |

关键词和向量各自排序后通过 RRF 按名次融合，不直接比较不同来源的分数。每路按文件去重后再限制召回，避免长文件的许多片段占满名额。分块优先保留段落、标题章节和普通代码块；超长块按行/句子继续拆分，所有原文仍保留。

重排低于阈值时明确提示“无相关记忆”，保留一个低置信候选供人工核对。重排不可用时返回排名融合候选并提示相关性未验证；它们不代表答案。中文、版本号、路径和无答案查询应使用自己的样例验证，调整阈值不能替代原文复核。

## 可选语义检索

语义检索调用外部嵌入和重排接口，接口需要兼容本工具的 `/embeddings` 与 `/rerank` 请求和响应。**调用时会向配置的服务发送查询和记忆文本片段**；敏感项目可以只用本地 Markdown 检索。

| 配置 | 默认值或用途 |
| --- | --- |
| `MEMORY_API_KEY` | API key；也支持项目 `scripts/.mem_api_key` 或用户目录 `~/.mem_api_key` |
| `MEMORY_BASE_URL` | `https://api.siliconflow.cn/v1` |
| `MEMORY_EMBED_MODEL` | `BAAI/bge-m3` |
| `MEMORY_RERANK_MODEL` | `BAAI/bge-reranker-v2-m3` |
| `MEMORY_RERANK_SCALE` | `probability`；使用原始重排分的服务配置为 `logit` |
| `MEMORY_DIR` | 默认项目 `memory/`；显式共享时设置绝对路径 |

key 优先级为环境变量、项目 key 文件、用户 key 文件。文件中只写 key，不要提交到 Git。

配置好 API 后运行：

```sh
python scripts/mem.py reindex
python scripts/mem.py search "部署环境" -k 5
python scripts/mem.py search "部署环境" --no-rerank
```

修改嵌入模型或接口后应重新建索引，必要时使用 `reindex --force`；成功强制重建也会清空 API 缓存。重排失败保留关键词/向量排名融合；索引过期、缺少 key 或嵌入调用失败会退回当前 Markdown。更换服务或模型后需重新评估相关性阈值；余弦、融合分和重排分不能直接比较。

重复查询的向量和同查询/同候选内容的重排响应缓存在 `.index.sqlite` 内。缓存键包含服务、模型和实际请求内容；查询向量额外区分维度。缓存有效期 24 小时，最多 128 条，压缩后的响应总量最多 1 MiB（不是整个 SQLite 文件的磁盘上限）。失败不缓存，缓存写入失败不阻断检索。

不缓存最终候选清单：每次读取当前 Markdown，新写入或更正的内容在索引未更新时仍能从本地检索；请求进行中有其他 agent 修改内容时也会转回最新 Markdown。正文变化后旧重排响应不会匹配新内容。

完全离线维护可运行 `python scripts/mem.py reindex --local`。本地检索直接读取 Markdown，这个命令只初始化可重建缓存，不生成向量、不删除已有向量；恢复接口后再执行普通 `reindex`。

## Git 与多人协作

提交 `scripts/mem.py`、`AGENTS.md` 和 `memory/` 中的 Markdown。不要提交 `.index.sqlite`、WAL/SHM、`.write.lock` 或 key 文件；仓库已配置对应忽略规则。

同一目录下的 agent 通过写锁协调 `add`。独立 worktree 或机器不会自动共享目录；应显式同步 Markdown，或配置共同可访问的绝对 `MEMORY_DIR`。运行中的 agent 仍需要重新检索才能读到新结论。共享写入不代表脚本自动解决 Git 合并冲突。

建议记忆提交使用 `memory:` 前缀。每月或记忆规模较大时运行 `python scripts/mem.py outdated` 检查待复核条目；它只列出建议，不会自动删除记忆。

## 验证

```sh
python -m unittest discover -s tests -v
```

测试在临时目录中检查初始化保留项目约定、并发写入与去重、更正历史和逗号路径、跨工作目录共享、检索降级、索引更新失败、缓存隔离/失效/容量、分数分制、输出预算、CRLF 行定位和代码块边界。`tests/fixtures/retrieval_cases.json` 包含 36 条中文、精确名称、范围、更正与无匹配检索样例。

测试不读取真实 key，也不调用真实外部 API。缓存与云端检索路径使用模拟响应验证接口流程，这些样例不等于真实嵌入模型的准确率测评。GitHub Actions 配置在 Windows 和 Linux 上执行同一测试；运行结果以对应 Actions 记录为准。
