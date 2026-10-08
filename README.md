# Markdown 项目共享记忆

用 Markdown 保存项目的长期结论，让同一项目中的 AI agent 能检索、核对和补充已有知识。Markdown 是事实源，SQLite 只保存可重建的语义检索缓存。工具版本为 `2.1.0`，仅依赖 Python 标准库。

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
python scripts/mem.py search "部署环境" -k 5
python scripts/mem.py show "memory/facts/<命中文件>.md"
python scripts/mem.py show "memory/facts/<命中文件>.md" --grep "Python" -C 2
python scripts/mem.py show "memory/facts/<命中文件>.md" --lines 1-20
```

检索结果只是候选清单。引用数字、路径、哈希等信息前，必须回看原文；`--grep` 按字面文本匹配。没有相关记忆时，查看 `memory/index.md`，或使用 `rg -n -i "<关键词>" memory/`。当前代码和用户提供的新信息优先于旧记忆。

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

支持 `fact`、`decision`、`howto`、`correction` 四种类型。`add` 使用唯一文件名和进程间写锁，避免同名覆盖及并发追加丢失。形成长期结论后及时保存；不要记录密钥、密码、临时调试信息或代码已经直接表达的内容。正文临时文件用完后可移走，正式记忆保存在 `memory/`。

### 更正与历史

更正正文应包含旧文件路径及旧说法、新事实和核实依据：

```sh
python scripts/mem.py add --type correction --title "部署方案更新" --summary "新环境替代旧环境" --body-file correction-body.txt --supersedes "memory/decisions/<旧文件>.md"
python scripts/mem.py reindex
python scripts/mem.py search "部署方案" --include-outdated
```

更正会将旧条目标记为 `outdated`，保留正文并建立替代关系。默认检索有效条目，`--include-outdated` 可查看历史。

## 可选语义检索

语义检索调用外部嵌入和重排接口，接口需要兼容本工具的 `/embeddings` 与 `/rerank` 请求和响应。**调用时会向配置的服务发送查询和记忆文本片段**；敏感项目可以只用本地 Markdown 检索。

| 配置 | 默认值或用途 |
| --- | --- |
| `MEMORY_API_KEY` | API key；也支持项目 `scripts/.mem_api_key` 或用户目录 `~/.mem_api_key` |
| `MEMORY_BASE_URL` | `https://api.siliconflow.cn/v1` |
| `MEMORY_EMBED_MODEL` | `BAAI/bge-m3` |
| `MEMORY_RERANK_MODEL` | `BAAI/bge-reranker-v2-m3` |
| `MEMORY_DIR` | 默认项目 `memory/`；显式共享时设置绝对路径 |

key 优先级为环境变量、项目 key 文件、用户 key 文件。文件中只写 key，不要提交到 Git。

配置好 API 后运行：

```sh
python scripts/mem.py reindex
python scripts/mem.py search "部署环境" -k 5
python scripts/mem.py search "部署环境" --no-rerank
```

修改模型或接口后应重新建索引，必要时使用 `reindex --force`。重排失败会使用向量排序；索引过期、缺少 key 或嵌入调用失败会退回当前 Markdown。更换服务或模型后需重新评估相关性阈值；向量分数和重排分数不可直接比较。

## Git 与多人协作

提交 `scripts/mem.py`、`AGENTS.md` 和 `memory/` 中的 Markdown。不要提交 `.index.sqlite`、WAL/SHM、`.write.lock` 或 key 文件；仓库已配置对应忽略规则。

同一目录下的 agent 通过写锁协调 `add`。独立 worktree 或机器不会自动共享目录；应显式同步 Markdown，或配置共同可访问的绝对 `MEMORY_DIR`。运行中的 agent 仍需要重新检索才能读到新结论。共享写入不代表脚本自动解决 Git 合并冲突。

建议记忆提交使用 `memory:` 前缀。每月或记忆规模较大时运行 `python scripts/mem.py outdated` 检查待复核条目；它只列出建议，不会自动删除记忆。

## 验证

```sh
python -m unittest discover -s tests -v
```

测试在临时目录中检查初始化保留项目约定、并发写入、更正历史、检索降级、索引更新失败和原文查看边界。测试不读取真实 key，也不调用真实外部 API。GitHub Actions 配置在 Windows 和 Linux 上执行同一测试；运行结果以对应 Actions 记录为准。
