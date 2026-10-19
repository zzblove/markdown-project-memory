#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""标准库共享记忆 v2。Markdown 为事实源，SQLite 为可重建缓存。
配置: MEMORY_API_KEY / MEMORY_BASE_URL / MEMORY_EMBED_MODEL /
MEMORY_RERANK_MODEL / MEMORY_DIR。命令帮助: python scripts/mem.py -h。
search 给候选清单（片段/行数/大纲），show 做定点无损回看（--grep / --lines / --section）。
"""
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import sqlite3
import struct
import sys
import time
import urllib.error
import urllib.request
import uuid
import zlib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

VERSION = '2.4.0'
# CLI 升级不改变相同输入的嵌入与 API 响应身份；仅输入格式变化时升级这些版本。
EMBED_PROFILE_VERSION = API_CACHE_VERSION = '2.2.0'
# 使用同一规范路径，避免 Windows 8.3 短路径或目录链接破坏相对更正关系。
HERE = str(Path(__file__).resolve().parent)
ROOT = str(Path(HERE).parent)
MEM_DIR = str(Path(os.environ.get('MEMORY_DIR', os.path.join(ROOT, 'memory'))).resolve())
DB_PATH = os.path.join(MEM_DIR, '.index.sqlite')
BASE_URL = os.environ.get('MEMORY_BASE_URL', 'https://api.siliconflow.cn/v1').rstrip('/')
EMBED_MODEL = os.environ.get('MEMORY_EMBED_MODEL', 'BAAI/bge-m3')
RERANK_MODEL = os.environ.get('MEMORY_RERANK_MODEL', 'BAAI/bge-reranker-v2-m3')
FRONT_RE = re.compile(r'\A(?:\ufeff)?---[^\S\n]*\n(.*?)\n---[^\S\n]*(?:\n|$)', re.S)
HEAD_RE = re.compile(r'^(#{1,3})\s+(.+)$')
LIMIT, BATCH, RECALL = 1600, 16, 30
# 弃权阈值只适用于重排；分制必须明确配置，不能从分数正负推断。
# (a) 归一化分（0~1 区间，如 siliconflow bge-reranker-v2-m3）取 0.70；(b) 对数几率原始分取 0.0。
# 0.70 来自 2026-10-07 实测标定（bge-reranker-v2-m3，43 条中文记忆）：
#   "记忆里确实有答案"    top-1 落在 0.866 ~ 0.994（9 个查询）
#   "实体在但该属性没记"  top-1 落在 0.160 ~ 0.547（3 个查询）
#   "完全无关/域外"      top-1 = 0.000，已被过滤为 0 条
# 阈值取在 0.547 与 0.866 之间的空档。换重排服务商后分数分布可能整体平移，
# 应用 `search --no-rerank` 或直接比对原始分重新标定，不要照搬该数字。
MIN_SCORE, MIN_LOGIT = 0.70, 0.0
RERANK_SCALE = os.environ.get('MEMORY_RERANK_SCALE', 'probability')
CACHE_LIMIT, CACHE_BYTES, CACHE_TTL = 128, 1024 * 1024, 86400
DEFAULT_MAX_CHARS, DEFAULT_RERANK_LIMIT = 1500, 10
BEGIN, END = '<!-- shared-memory:BEGIN -->', '<!-- shared-memory:END -->'
PROTOCOL = '''## 项目共享记忆（memory/）

本项目所有 AI agent 通过同一 memory/ 目录共享长期记忆。Markdown 是事实源，索引是派生缓存。

### 主控与子智能体协作（仅编排任务适用）
- 主控检索并回看原文，只把本次相关、已核对的条目交给子智能体；所有角色共享项目知识，不按模型另建互不相通的记忆库。没有主控的独立会话继续按下方协议正常读写。
- 可用 `handoff --ref "memory/<条目>.md"`（可重复，默认总预算 6000 字符）输出 JSON 记忆包，保存在 memory/ 外；它包含项目根、记忆目录、完整原文和文件哈希，不代替核实。派工入口用 `--memory-brief <包文件>` 显式传入，派工前核对目录和哈希。源文件变化时重新读取/核验并生成，不复用旧包。
- `CODEX_TEAM_CHILD=1` 的受委派子智能体优先读取任务附带记忆；必要时仅补读原文或本地检索，不直接 add、更正、annotate --apply、init、reindex，也不手改 memory/。脚本拒绝这些受标记子进程的写入命令；这不替代系统权限。
- 子智能体在结果末尾返回“记忆候选”：类型、结论、适用范围、依据、实际执行的验证、未确认事项；没有则写“无”。候选不自动入库，主控去重、核验冲突及证据后统一保存/reindex。进行中的任务交接和原始日志放 memory/ 外。
- 论文原文结论、改进假设、待执行实验和实际实验结果必须区分；指标附数据版本/划分、配置、随机种子、基线及日志路径，缺失则明确说明，不把推测或子智能体自述写为已验证事实。
- 并行任务结束后主控重新检索再合并；记忆更新时向仍在工作的子智能体显式发送变更或重派受影响部分，不假定上下文自动同步。跨项目/worktree 仅在显式配置同一绝对 MEMORY_DIR 时共享。

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
'''
IGNORE_RULES = ('memory/.index.sqlite', 'memory/.index.sqlite-*', 'memory/.write.lock', 'scripts/.mem_api_key')


def _utf8_stdout():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')


def now():
    config = Path(MEM_DIR)/'.settings.json'
    if config.exists():
        settings = json.loads(config.read_text(encoding='utf-8-sig'))
        name = settings.get('timezone') if isinstance(settings,dict) else None
        if not isinstance(name,str) or not name.strip():
            raise ValueError('memory/.settings.json 需要非空 timezone')
        try:
            return dt.datetime.now(ZoneInfo(name))
        except (ZoneInfoNotFoundError,ValueError):
            raise ValueError('无法加载配置的 IANA 时区，请检查系统时区数据或 tzdata') from None
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))


def require_controller(operation):
    if os.environ.get('CODEX_TEAM_CHILD')=='1':
        raise RuntimeError(f'子智能体不能执行 {operation}；请返回记忆候选，由主控核验后写入/维护')


def _keyfile(path):
    try:
        return Path(path).read_text(encoding='utf-8').strip()
    except OSError:
        return ''


def api_key():
    for value in (os.environ.get('MEMORY_API_KEY', ''), _keyfile(Path(HERE)/'.mem_api_key'), _keyfile(Path.home()/'.mem_api_key')):
        if value.strip():
            return value.strip()
    raise RuntimeError('未配置 API key（MEMORY_API_KEY 或 ~/.mem_api_key）')


def post(path, payload):
    try:
        req = urllib.request.Request(BASE_URL+path, data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type':'application/json', 'Authorization':'Bearer '+api_key()}, method='POST')
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as error:
        raise RuntimeError(f'HTTP {error.code} {path}') from None
    except (OSError, ValueError) as error:
        raise RuntimeError(f'{path}: {type(error).__name__}') from None


def normalize(vector):
    if not vector or not all(math.isfinite(x) for x in vector):
        raise RuntimeError('嵌入 API 返回无效向量')
    norm = math.sqrt(sum(x*x for x in vector))
    if not norm or not math.isfinite(norm):
        raise RuntimeError('嵌入 API 返回零向量或溢出向量')
    return [x/norm for x in vector]


def embed(texts):
    vectors = []
    try:
        for offset in range(0, len(texts), BATCH):
            batch = [text[:2000] for text in texts[offset:offset+BATCH]]
            rows = post('/embeddings', {'model':EMBED_MODEL, 'input':batch})['data']
            rows.sort(key=lambda row: row['index'])
            if [row['index'] for row in rows] != list(range(len(batch))):
                raise RuntimeError('嵌入 API 返回条数/序号不完整')
            vectors.extend(normalize(row['embedding']) for row in rows)
        if len({len(vector) for vector in vectors}) > 1:
            raise RuntimeError('嵌入 API 返回维度不一致')
        return vectors
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError(f'嵌入响应格式错误: {type(error).__name__}') from None


def rerank(query, docs, top_n):
    try:
        rows = post('/rerank', {'model':RERANK_MODEL, 'query':query, 'documents':[x[:2000] for x in docs],
            'top_n':top_n, 'return_documents':False})['results']
        indexes = [row['index'] for row in rows]
        if not rows or len(set(indexes)) != len(indexes) or any(type(i) is not int or i<0 or i>=len(docs) for i in indexes):
            raise RuntimeError('重排 API 返回无效序号')
        if not all(math.isfinite(row['relevance_score']) for row in rows):
            raise RuntimeError('重排 API 返回无效分数')
        return rows
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError(f'重排响应格式错误: {type(error).__name__}') from None


def profile():
    return json.dumps([EMBED_PROFILE_VERSION, BASE_URL, EMBED_MODEL, LIMIT], separators=(',', ':'))


def frontmatter(text):
    match, result = FRONT_RE.match(text), {}
    if match:
        for line in match[1].splitlines():
            pair = re.match(r'^([\w-]+):\s*(.*?)\s*$', line)
            if pair:
                value = pair[2]
                if value.startswith('"'):
                    try:
                        value = json.loads(value)
                    except ValueError:
                        pass
                elif value.startswith("'") and value.endswith("'"):
                    value = value[1:-1]
                result[pair[1]] = value if isinstance(value, str) else pair[2]
    return result


def reference(value):
    path = Path(value.replace('\\', '/'))
    if not path.is_absolute():
        if path.parts and path.parts[0] == 'memory':
            path = Path(MEM_DIR).joinpath(*path.parts[1:])
        elif path.parts and path.parts[0] in ('facts','decisions','howto'):
            path = Path(MEM_DIR)/path
        else:
            legacy = (Path(ROOT)/path).resolve()
            if not legacy.is_relative_to(Path(MEM_DIR).resolve()):
                # Older external-directory references depended on the writer's ROOT.
                for index, part in enumerate(path.parts):
                    if part in ('facts', 'decisions', 'howto'):
                        legacy = Path(MEM_DIR).joinpath(*path.parts[index:]).resolve()
                        break
            path = legacy
    path = path.resolve()
    if not path.is_relative_to(Path(MEM_DIR).resolve()):
        raise ValueError('supersedes 必须指向 memory/ 内文件')
    return 'memory/' + path.relative_to(Path(MEM_DIR).resolve()).as_posix()


def memory_path(key):
    return Path(MEM_DIR).joinpath(*Path(reference(key)).parts[1:])


def supersedes_values(value):
    if not value:
        return []
    if value.startswith('['):
        try:
            items = json.loads(value)
        except ValueError:
            return []
        return items if isinstance(items, list) and all(isinstance(x, str) for x in items) else []
    return [value]


def file_date(text, path, updated=False):
    meta = frontmatter(text)
    for value in ([meta.get('updated')] if updated else [])+[meta.get('date'), Path(path).name[:10]]:
        try:
            return dt.date.fromisoformat((value or '')[:10]).isoformat()
        except ValueError:
            pass
    return dt.datetime.fromtimestamp(os.path.getmtime(path), now().tzinfo).date().isoformat()


def walk_md_files():
    found = {}
    for directory, dirs, files in os.walk(MEM_DIR):
        dirs[:] = [name for name in dirs if not name.startswith('.')]
        for name in files:
            path = Path(directory)/name
            if name.lower().endswith('.md') and path.resolve() != (Path(MEM_DIR)/'index.md').resolve():
                try:
                    found[reference(str(path))] = str(path)
                except ValueError:
                    continue
    return found


def _documents():
    result = {}
    for rel, path in sorted(walk_md_files().items()):
        try:
            raw = Path(path).read_bytes()
            text = raw.decode('utf-8-sig')
            meta = frontmatter(text)
            body = FRONT_RE.sub('', text, count=1).strip()
            title = re.search(r'^#\s+(.+)$', body, re.M)
            supersedes = []
            for item in supersedes_values(meta.get('supersedes', '')):
                if item.strip():
                    try:
                        supersedes.append(reference(item.strip()))
                    except ValueError:
                        pass
            result[rel] = dict(path=rel, absolute=path, text=text, body=body,
                hash=hashlib.sha256(raw).hexdigest(), meta=meta,
                title=title[1] if title else Path(path).stem, date=file_date(text,path),
                updated=file_date(text,path,True), status=meta.get('status','active').lower(), supersedes=supersedes,
                summary=meta.get('summary', ''), scope=meta.get('scope', ''),
                source=meta.get('source', ''), verified_at=meta.get('verified_at', ''))
        except FileNotFoundError:
            continue
    return result


def documents():
    # 与 add 的发布/更正操作协调，避免读取其尚未写完的文件或关系链。
    with write_lock():
        return _documents()


def chunk_records(text, fallback_title=''):
    """Prefer paragraph/heading boundaries and keep normal fenced blocks together."""
    rows = text.splitlines()
    match = FRONT_RE.match(text)
    offset = len(text[:match.end()].splitlines()) if match else 0
    chunks, pending, block = [], [], []
    heading, fence, has_body = fallback_title, None, False

    def emit():
        nonlocal pending, has_body
        trimmed = list(pending)
        while trimmed and not trimmed[0][1].strip():
            trimmed.pop(0)
        while trimmed and not trimmed[-1][1].strip():
            trimmed.pop()
        if trimmed and has_body:
            chunks.append(dict(heading=heading, text='\n'.join(x[1] for x in trimmed),
                               start=trimmed[0][0], end=trimmed[-1][0]))
        pending, has_body = [], False

    def pack():
        nonlocal block, pending, has_body
        if not block:
            return
        size = sum(len(line)+1 for _, line in block)
        if pending and sum(len(line)+1 for _, line in pending)+size > LIMIT:
            emit()
        # Oversized blocks fall back to line/sentence cuts; all source text stays indexed.
        for number, line in block:
            rest = line
            while len(rest) > LIMIT:
                if pending:
                    emit()
                cut = max(rest.rfind(mark, LIMIT//2, LIMIT) for mark in ('。', '. ', '; ', ' '))
                cut = cut+1 if cut >= LIMIT//2 else LIMIT
                pending, has_body = [(number, rest[:cut])], True
                emit()
                rest = rest[cut:]
            if pending and sum(len(value)+1 for _, value in pending)+len(rest)+1 > LIMIT:
                emit()
            pending.append((number, rest))
            has_body = has_body or bool(rest.strip() and not HEAD_RE.match(rest))
        block = []

    for number, line in enumerate(rows[offset:], offset+1):
        marker = re.match(r'^\s{0,3}(`{3,}|~{3,})', line)
        if not fence and HEAD_RE.match(line):
            pack()
            emit()
            heading = line.strip()
        block.append((number, line))
        if marker:
            token = marker[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence) and not line[marker.end():].strip():
                fence = None
        if not fence and not line.strip():
            pack()
    pack()
    emit()
    return chunks


def split_chunks(text, fallback_title=''):
    return [(row['heading'], row['text']) for row in chunk_records(text, fallback_title)]


def chunk_input(doc, heading, body):
    return '\n'.join((doc['title'][:100], doc.get('summary', '')[:100],
                      doc.get('scope', '')[:60], heading[:80], body))


def db_conn():
    Path(MEM_DIR).mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    try:
        conn.execute('PRAGMA busy_timeout=30000')
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, file_hash TEXT)')
        if 'profile' not in {row[1] for row in conn.execute('PRAGMA table_info(files)')}:
            conn.execute('ALTER TABLE files ADD COLUMN profile TEXT')
        conn.execute('CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY,path TEXT,heading TEXT,text TEXT,chunk_hash TEXT,vector BLOB)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path)')
        conn.execute('CREATE TABLE IF NOT EXISTS api_cache(cache_key TEXT PRIMARY KEY,kind TEXT,value BLOB,created REAL)')
        conn.commit()
        return conn
    except BaseException:
        conn.close()
        raise


def cached():
    if not Path(DB_PATH).exists():
        return {}, []
    with contextlib.closing(sqlite3.connect(Path(DB_PATH).resolve().as_uri()+'?mode=ro', uri=True, timeout=30)) as conn:
        conn.execute('BEGIN')  # files 与 chunks 读取同一个快照。
        known = {p:(h,c) for p,h,c in conn.execute('SELECT path,file_hash,profile FROM files')}
        rows = conn.execute('SELECT path,heading,text,chunk_hash,vector FROM chunks').fetchall()
        return known, rows


def _cache_key(kind, payload):
    raw = json.dumps([API_CACHE_VERSION, BASE_URL, kind, payload], ensure_ascii=False,
                     sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def _cache_read(key):
    if not Path(DB_PATH).exists():
        return None
    try:
        with contextlib.closing(sqlite3.connect(Path(DB_PATH).resolve().as_uri()+'?mode=ro', uri=True)) as conn:
            row = conn.execute('SELECT value,created FROM api_cache WHERE cache_key=?', (key,)).fetchone()
        if row and time.time()-row[1] < CACHE_TTL and len(row[0]) <= CACHE_BYTES:
            return json.loads(zlib.decompress(row[0]).decode('utf-8'))
    except (OSError, sqlite3.Error, ValueError, TypeError, zlib.error):
        pass
    return None


def _cache_write(key, kind, value):
    blob = zlib.compress(json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8'))
    if len(blob) > CACHE_BYTES:
        return
    try:
        with contextlib.closing(db_conn()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('DELETE FROM api_cache WHERE created<?', (time.time()-CACHE_TTL,))
            conn.execute('INSERT OR REPLACE INTO api_cache VALUES(?,?,?,?)', (key,kind,blob,time.time()))
            count, size = conn.execute('SELECT COUNT(*),COALESCE(SUM(length(value)),0) FROM api_cache').fetchone()
            for cache_key, length in conn.execute('SELECT cache_key,length(value) FROM api_cache ORDER BY created,cache_key').fetchall():
                if count <= CACHE_LIMIT and size <= CACHE_BYTES:
                    break
                conn.execute('DELETE FROM api_cache WHERE cache_key=?', (cache_key,))
                count, size = count-1, size-length
            conn.commit()
    except (OSError, sqlite3.Error):
        pass  # A derived cache must never prevent recall or publishing Markdown.


def query_vector(query, use_cache=True, dimension=None):
    key = _cache_key('query', [EMBED_MODEL, query, dimension])
    stored = _cache_read(key) if use_cache else None
    if stored is not None:
        try:
            return normalize(stored)
        except (RuntimeError, TypeError, ValueError):
            pass
    vector = normalize(embed([query])[0])
    if use_cache:
        _cache_write(key, 'query', vector)
    return vector


def rank_cached(query, docs, use_cache=True):
    key = _cache_key('rerank', [RERANK_MODEL, query, docs])
    stored = _cache_read(key) if use_cache else None
    if stored is not None:
        try:
            indexes = [row['index'] for row in stored]
            if (stored and len(set(indexes)) == len(indexes)
                    and all(type(i) is int and 0 <= i < len(docs) for i in indexes)
                    and all(math.isfinite(row['relevance_score']) for row in stored)):
                return stored
        except (KeyError, TypeError, ValueError):
            pass
    ranked = rerank(query, docs, len(docs))
    if use_cache:
        _cache_write(key, 'rerank', ranked)
    return ranked


def fresh(docs, known):
    return set(docs)==set(known) and all(known[p]==(d['hash'],profile()) for p,d in docs.items())


def index_coverage(docs, known):
    current = {p for p,d in docs.items() if known.get(p)==(d['hash'],profile())}
    return {'current':len(current), 'pending':len(set(docs)-current),
            'removed':len(set(known)-set(docs)), 'total':len(docs)}


def current_vectors(docs, known, rows, include_outdated=False, scope=None):
    """Exclude changed/deleted/incompatible files before any query API call."""
    allowed, links = eligible(docs,include_outdated), replacements(docs)
    if scope is not None:
        allowed = {p for p in allowed if docs[p].get('scope') == scope}
    return [row for row in rows if row[0] in docs
            and known.get(row[0])==(docs[row[0]]['hash'],profile())
            and (row[0] if include_outdated else resolve_current(row[0],links)) in allowed]


def cmd_reindex(force=False, local=False):
    require_controller('reindex')
    if not Path(MEM_DIR).is_dir():
        raise RuntimeError(f'记忆目录不存在: {MEM_DIR}')
    with contextlib.closing(db_conn()):
        pass
    if force and local:
        raise ValueError('--force 与 --local 不能同时使用；离线维护保留已有向量')
    docs, (known, rows) = documents(), cached()
    reuse = {h:b for p,_,_,h,b in rows if known.get(p,(None,None))[1]==profile()}
    pending, inputs, waiting = {}, {}, 0
    for path, doc in docs.items():
        if not force and known.get(path)==(doc['hash'],profile()):
            continue
        prepared, needed = [], {}
        for heading, body in split_chunks(doc['text'], doc['title']):
            value = chunk_input(doc,heading,body)
            digest = hashlib.sha256(value.encode('utf-8')).hexdigest()
            prepared.append((heading,body,digest))
            if force or digest not in reuse:
                needed[digest] = value
        if local and needed:
            waiting += 1
            continue
        inputs.update(needed)
        pending[path] = prepared
    keys = list(inputs)
    if keys:
        vectors = embed([inputs[key] for key in keys])  # 无任何 SQLite 写事务。
        if len(vectors)!=len(keys):
            raise RuntimeError('嵌入响应不完整；旧索引未被替换')
        for key, vector in zip(keys,vectors):
            vector = normalize(vector)
            reuse[key] = struct.pack(f'<{len(vector)}f',*vector)
    changed = removed = skipped = 0
    with contextlib.closing(db_conn()) as conn:
        for path, prepared in pending.items():
            conn.execute('BEGIN IMMEDIATE')
            try:
                current = Path(docs[path]['absolute'])
                if not current.exists() or hashlib.sha256(current.read_bytes()).hexdigest()!=docs[path]['hash']:
                    skipped += 1
                    conn.rollback()
                    continue
                old = conn.execute('SELECT file_hash,profile FROM files WHERE path=?',(path,)).fetchone()
                if old==(docs[path]['hash'],profile()) and not force:
                    conn.rollback()
                    continue
                conn.execute('DELETE FROM chunks WHERE path=?',(path,))
                conn.executemany('INSERT INTO chunks(path,heading,text,chunk_hash,vector) VALUES(?,?,?,?,?)',
                    [(path,h,b,d,reuse[d]) for h,b,d in prepared])
                conn.execute('INSERT OR REPLACE INTO files(path,file_hash,profile) VALUES(?,?,?)',(path,docs[path]['hash'],profile()))
                conn.commit()
                changed += 1
                print(f'  {path}: {len(prepared)} 段')
            except BaseException:
                conn.rollback()
                raise
        conn.execute('BEGIN IMMEDIATE')
        try:
            if force:
                conn.execute('DELETE FROM api_cache')
            if not local:
                live = walk_md_files()
                for (path,) in conn.execute('SELECT path FROM files').fetchall():
                    if path not in live:
                        conn.execute('DELETE FROM chunks WHERE path=?',(path,))
                        conn.execute('DELETE FROM files WHERE path=?',(path,))
                        removed += 1
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        count = conn.execute('SELECT COUNT(*) FROM files').fetchone()[0]
        chunks = conn.execute('SELECT COUNT(*) FROM chunks').fetchone()[0]
    print(f'完成: 变更 {changed} / 移除 {removed} / 并发修改跳过 {skipped}; 索引 {count} 文件 {chunks} 段')
    if local:
        print(f'离线维护：复用已有嵌入，{waiting} 文件仍待生成向量；未调用外部 API，未删除历史向量。')
    if skipped:
        print('提示: 计算期间文件变化，可再次 reindex；search 将使用最新 Markdown。')


def replacements(docs):
    links = {}
    for path, doc in sorted(docs.items(),key=lambda item:(item[1]['meta'].get('updated',
            item[1]['meta'].get('created_at', item[1]['updated'])),item[0])):
        for old in doc['supersedes']:
            links[old] = path
    return links


def resolve_current(path, links):
    seen = set()
    while path in links and path not in seen:
        seen.add(path)
        path = links[path]
    return path


def eligible(docs, include_outdated=False):
    links = replacements(docs)
    return {p for p,d in docs.items() if include_outdated or (d['status']!='outdated' and p not in links)}


def lexical_score(query, text):
    query, text = query.casefold().strip(), text.casefold()
    tokens = list(dict.fromkeys(re.findall(r'[\w-]+',query)))
    score = sum(token in text for token in tokens)
    if query and query in text:
        score += len(tokens)+1
    if not score:
        pairs = {part[i:i+2] for part in re.findall(r'[\u4e00-\u9fff]+',query) for i in range(len(part)-1)}
        score = sum(pair in text for pair in pairs)/max(1,len(pairs))
    return score


def headings(doc):
    return [line.strip() for line in doc['body'].splitlines() if HEAD_RE.match(line)]


def highlight(text, query, limit=200):
    # 摘要围绕命中位置取窗，避免固定截断丢掉数字、路径与哈希。
    flat = re.sub(r'\s+',' ',text).strip()
    tokens = [token for token in dict.fromkeys(re.findall(r'[\w-]+',query.casefold())) if token]
    position, matched = -1, ''
    for token in tokens:
        found = flat.casefold().find(token)
        if found>=0 and (position<0 or found<position):
            position, matched = found, token
    if position<0 and query.strip():
        found = flat.casefold().find(query.casefold().strip())
        if found>=0:
            position, matched = found, query.strip()
    if position<0:
        return flat[:limit]+('…' if len(flat)>limit else '')
    start = max(0,position-max(0,(limit-len(matched))//2))
    window = flat[start:start+limit]
    return ('…' if start else '')+window+('…' if start+limit<len(flat) else '')


def hit_lines(doc, body):
    text = doc['text'].replace('\r\n', '\n')
    body = body.replace('\r\n', '\n').strip()
    position = text.find(body) if body else -1
    if position < 0:
        return 1, len(text.splitlines())
    start = text[:position].count('\n')+1
    return start, start+len(body.splitlines())-1


def shell_path(path):
    # Search output is often copied into a shell; keep filenames literal.
    return "'"+path.replace("'", "''")+"'" if os.name == 'nt' else shlex.quote(path)


def ranked_results(results, docs, k):
    dedup = {}
    for row in results:
        if row[1] not in dedup or row[0] > dedup[row[1]][0]:
            dedup[row[1]] = row
    return sorted(dedup.values(), key=lambda row:(row[0], docs[row[1]]['updated']), reverse=True)[:k]


def format_results(query, results, docs, mode, k, abstain=False, max_chars=DEFAULT_MAX_CHARS):
    """Return the exact display and visible paths so evaluation includes its budget."""
    links = replacements(docs)
    final = ranked_results(results,docs,k)
    header = f'查询: {query[:100]}  ({mode}, {len(final)} 个候选)\n'
    warning = '无相关记忆：重排低于阈值。以下仅供核对，不能据此回答。\n' if abstain else ''
    footer = '提示: 摘要/片段只用于筛选；引用数字、路径前用 show --grep 回看原文。\n'
    omitted_note = '有候选未展示，请增大 --max-chars；需要更多候选可增大 -k。\n'
    if not final:
        return ('无相关记忆。请换关键词，或查看 memory/index.md。\n')[:max_chars], []
    output = header+warning
    visible = []
    for score,path,heading,body in final:
        shown = len(visible)
        doc = docs[path]
        start,end = hit_lines(doc,body)
        state = doc['status']+('/已被取代' if path in links else '')
        item = f'[{shown+1}] {doc["updated"]} [{state}] score={score:.3f}  {path}  行 {start}-{end}\n'
        if doc.get('scope'):
            item += '    范围: '+doc['scope']+'\n'
        if doc.get('verified_at'):
            item += '    核实: '+doc['verified_at']+'\n'
        if doc.get('source'):
            item += '    依据: '+doc['source']+'\n'
        if doc.get('summary') and len(doc['summary']) <= 180:
            item += '    摘要: '+doc['summary']+'\n'
        item += '    片段: '+highlight(body,query,120)+'\n'
        item += f'    回看: python scripts/mem.py show {shell_path(path)} --lines {start}-{end}\n'
        reserve = len(footer)+len(omitted_note)
        if len(output)+len(item)+reserve > max_chars:
            # Keep a useful path/range even when optional metadata does not fit.
            item = f'[{shown+1}] [{state}]  {path}  行 {start}-{end}\n'
            item += f'    回看: python scripts/mem.py show {shell_path(path)} --lines {start}-{end}\n'
            if len(output)+len(item)+reserve > max_chars:
                break
        output += item
        visible.append(path)
    if len(visible) < len(final):
        output += omitted_note
    output += footer
    if len(output) > max_chars or not visible:
        output = warning+'有候选，预算不足以展示完整引用。请增大 --max-chars。\n'
        visible = []
    return output[:max_chars], visible


def print_results(query, results, docs, mode, k, abstain=False, max_chars=DEFAULT_MAX_CHARS):
    output, visible = format_results(query,results,docs,mode,k,abstain,max_chars)
    print(output,end='')
    return visible


def local_candidates(query, docs, include_outdated=False, scope=None):
    links, allowed, best = replacements(docs), eligible(docs,include_outdated), {}
    if scope is not None:
        allowed = {p for p in allowed if docs[p].get('scope') == scope}
    targets = {p:p if include_outdated else resolve_current(p,links) for p in docs}
    relevant = {p for p,target in targets.items() if target in allowed} | allowed
    chunks = {p:split_chunks(docs[p]['text'],docs[p]['title']) or [(docs[p]['title'],docs[p]['body'])] for p in relevant}
    for path,doc in docs.items():
        target = targets[path]
        if target not in allowed:
            continue
        prefix = (3*lexical_score(query,doc['title'])+2*lexical_score(query,doc.get('summary',''))
                  +.5*lexical_score(query,doc.get('scope',''))+.5*lexical_score(query,doc.get('source','')))
        if query in path:
            prefix += 2
        selected = max(chunks[path], key=lambda part: lexical_score(query,part[1]))
        score = prefix+lexical_score(query,selected[1])
        if score <= 0:
            continue
        if target != path:
            selected = max(chunks[target], key=lambda part: lexical_score(query,part[1]))
        row = (score,target,selected[0],selected[1])
        if target not in best or score > best[target][0]:
            best[target] = row
    return sorted(best.values(),reverse=True)


def fuse_rankings(*rankings):
    """RRF combines ranks, never cosine/BM25/reranker score scales."""
    fused = {}
    for rows in rankings:
        seen = set()
        for rank,row in enumerate(rows):
            path = row[1]
            if path in seen:
                continue
            seen.add(path)
            if path not in fused:
                fused[path] = [0.0]+list(row[1:])
            fused[path][0] += 1/(60+rank+1)
    return sorted((tuple(row) for row in fused.values()),reverse=True)


def grep_fallback(query, k, include_outdated=False, docs=None, max_chars=DEFAULT_MAX_CHARS, scope=None):
    docs = documents() if docs is None else docs
    print_results(query,local_candidates(query,docs,include_outdated,scope),docs,
                  '当前 Markdown 关键词检索',k,max_chars=max_chars)


def strong_local_match(query, rows, docs):
    if not rows:
        return False
    exact_titles = [row for row in rows if docs[row[1]]['title'].casefold() == query.casefold()]
    if len(exact_titles) == 1:
        return rows[0][1] == exact_titles[0][1]
    identifier = re.fullmatch(r'(?:[a-fA-F0-9]{12,64}|[^\s]+[/\\][^\s]+)',query)
    exact = [row for row in rows if query.casefold() in
             (docs[row[1]]['body']+'\n'+docs[row[1]].get('source','')).casefold()]
    return bool(identifier and len(exact) == 1 and rows[0][1] == exact[0][1])


def vector_candidates(query, docs, known, rows, qv, include_outdated=False, scope=None,
                      min_vector_score=None):
    rows = current_vectors(docs,known,rows,include_outdated,scope)
    allowed, links = eligible(docs,include_outdated), replacements(docs)
    if scope is not None:
        allowed = {p for p in allowed if docs[p].get('scope') == scope}
    scored, lookup, best = [], {}, {}
    for path,heading,body,_,blob in rows:
        vector = struct.unpack(f'<{len(blob)//4}f',blob)
        if len(vector) != len(qv) or not all(math.isfinite(x) for x in vector):
            raise RuntimeError('嵌入维度变化或向量损坏；请 reindex --force')
        scored.append((sum(a*b for a,b in zip(qv,vector)),path,heading,body))
    scored.sort(reverse=True)
    for row in scored:
        lookup.setdefault(row[1],row)
    for score,path,heading,body in scored:
        target = path if include_outdated else resolve_current(path,links)
        if target not in allowed or (min_vector_score is not None and score < min_vector_score):
            continue
        if target != path:
            _,_,heading,body = lookup.get(target,(score,target,docs[target]['title'],docs[target]['body']))
        if target not in best:
            best[target] = (score,target,heading,body)
    return sorted(best.values(),reverse=True)


def mixed_candidates(query, docs, known, rows, qv, include_outdated, scope, min_vector_score, lexical=None):
    if lexical is None:
        lexical = local_candidates(query,docs,include_outdated,scope)
    vectors = vector_candidates(query,docs,known,rows,qv,include_outdated,scope,min_vector_score)
    candidates = fuse_rankings(lexical,vectors)[:RECALL]
    vector_parts = {row[1]:row[2:] for row in vectors}
    return [(score,path)+vector_parts[path]
            if path in vector_parts and lexical_score(query,vector_parts[path][1]) >= lexical_score(query,body)
            else (score,path,heading,body) for score,path,heading,body in candidates]


def search_result(query, k=3, include_outdated=False, no_rerank=False, mode='auto',
               max_chars=DEFAULT_MAX_CHARS, rerank_limit=DEFAULT_RERANK_LIMIT, scope=None,
               min_vector_score=None, rerank_scale=RERANK_SCALE, min_rerank_score=None, no_cache=False):
    """Shared retrieval pipeline for the CLI and corpus-based evaluation."""
    query = query.strip()
    if k < 1 or not query or len(query) > 2000:
        raise ValueError('查询须为 1-2000 字符，k 必须大于 0')
    if max_chars < 64 or rerank_limit < 1:
        raise ValueError('max-chars 至少为 64，rerank-limit 必须大于 0')
    if mode not in ('auto','local','hybrid') or rerank_scale not in ('probability','logit'):
        raise ValueError('无效的检索模式或重排分制')
    if min_vector_score is not None and (not math.isfinite(min_vector_score) or not -1 <= min_vector_score <= 1):
        raise ValueError('min-vector-score 必须在 -1 到 1 之间')
    threshold = min_rerank_score if min_rerank_score is not None else (MIN_SCORE if rerank_scale == 'probability' else MIN_LOGIT)
    if not math.isfinite(threshold) or (rerank_scale == 'probability' and not 0 <= threshold <= 1):
        raise ValueError('重排阈值须为有限数；probability 分制范围为 0 到 1')
    docs = documents()
    lexical = local_candidates(query,docs,include_outdated,scope)
    diagnostics, known = [], {}
    def result(rows, current, label, kind, abstain=False):
        return {'query':query, 'rows':rows, 'docs':current, 'label':label, 'kind':kind,
                'abstain':abstain, 'reranked':label=='混合检索 + 重排',
                'diagnostics':diagnostics, 'index':index_coverage(current,known)}
    def local(current, kind='fallback'):
        return result(local_candidates(query,current,include_outdated,scope),current,
                      '当前 Markdown 关键词检索',kind)
    if mode == 'local' or (mode == 'auto' and strong_local_match(query,lexical,docs)):
        return result(lexical,docs,'本地检索（未调用 API）','local')
    try:
        known, rows = cached()
        usable = current_vectors(docs,known,rows,include_outdated,scope)
        allowed = eligible(docs,include_outdated)
        if scope is not None:
            allowed = {p for p in allowed if docs[p].get('scope') == scope}
        if not allowed:
            return result([],docs,'本地范围筛选','local')
        if not usable:
            raise RuntimeError('没有当前文件的可用向量（索引未同步或模型配置变化）；请 reindex')
        dimension = len(usable[0][4])//4
        if not dimension or len(usable[0][4])%4:
            raise RuntimeError('向量损坏；请 reindex --force')
        qv = query_vector(query,not no_cache,dimension)
        candidates = mixed_candidates(query,docs,known,rows,qv,include_outdated,scope,min_vector_score,lexical)
        if not candidates:
            return local(documents())
        partial = not fresh(docs,known)
        final, weak, label, kind = candidates, False, '关键词 + 向量排名融合（候选）', 'hybrid'
        if partial:
            coverage = index_coverage(docs,known)
            diagnostics.append(f'[mem] 索引部分同步：{coverage["current"]}/{coverage["total"]} 文件可复用向量；变更文件用当前 Markdown，暂不重排')
            label, kind = '部分向量 + 当前 Markdown（候选，未重排）', 'partial'
        if not no_rerank and not partial:
            selected = candidates[:rerank_limit]
            try:
                inputs = [chunk_input(docs[p],h,b) for _,p,h,b in selected]
                ranked = rank_cached(query,inputs,not no_cache)
                final = sorted(((r['relevance_score'],)+selected[r['index']][1:] for r in ranked),reverse=True)
                if rerank_scale == 'probability' and any(not 0 <= row[0] <= 1 for row in final):
                    raise RuntimeError('重排分数不在 0-1，请核对 --rerank-scale logit')
                weak = bool(final and final[0][0] < threshold)
                final = final[:1] if weak else [row for row in final if row[0] >= threshold]
                label = '混合检索 + 重排'
            except RuntimeError as error:
                diagnostics.append(f'[mem] {error}，保留排名融合候选（相关性未验证）')
                final, weak, label = candidates, False, '排名融合（重排不可用，仅供核对）'
        latest = documents()
        if {p:d['hash'] for p,d in docs.items()} != {p:d['hash'] for p,d in latest.items()}:
            diagnostics.append('[mem] 检索期间文件变化，使用最新 Markdown 与仍有效的向量；丢弃旧重排结果')
            if current_vectors(latest,known,rows,include_outdated,scope):
                final = mixed_candidates(query,latest,known,rows,qv,include_outdated,scope,min_vector_score)
                return result(final,latest,'部分向量 + 最新 Markdown（候选，未重排）','partial')
            return local(latest)
        return result(final,docs,label,kind,weak)
    except (RuntimeError,sqlite3.Error,struct.error,IndexError) as error:
        diagnostics.append(f'[mem] {error}，降级检索当前 Markdown')
        return local(documents())


def format_search(result, k=3, max_chars=DEFAULT_MAX_CHARS):
    remaining, messages = max_chars, []
    for message in result['diagnostics']:
        limit = max(0,min(160,remaining-64))
        if limit:
            diagnostic = message[:limit-1]+'\n'
            messages.append(diagnostic)
            remaining -= len(diagnostic)
    output, visible = format_results(result['query'],result['rows'],result['docs'],result['label'],
                                     k,result['abstain'],remaining)
    return output, ''.join(messages), visible


def cmd_search(query, k=3, include_outdated=False, no_rerank=False, mode='auto',
               max_chars=DEFAULT_MAX_CHARS, rerank_limit=DEFAULT_RERANK_LIMIT, scope=None,
               min_vector_score=None, rerank_scale=RERANK_SCALE, min_rerank_score=None, no_cache=False):
    result = search_result(query,k,include_outdated,no_rerank,mode,max_chars,rerank_limit,scope,
                           min_vector_score,rerank_scale,min_rerank_score,no_cache)
    output, errors, visible = format_search(result,k,max_chars)
    print(errors,end='',file=sys.stderr)
    print(output,end='')
    return result


def cmd_show(path, grep=None, context=0, lines=None, section=None, max_chars=200000):
    """定点的无损回看：整篇、按关键词定位、按行范围或按章节。"""
    if context<0 or max_chars<1:
        raise ValueError('context 不能为负，max-chars 必须大于 0')
    candidate = memory_path(os.path.expanduser(path)).resolve()
    if not candidate.is_relative_to(Path(MEM_DIR).resolve()):
        raise ValueError(f'只允许回看记忆目录内的文件: {MEM_DIR}')
    if not candidate.is_file():
        raise ValueError(f'记忆文件不存在: {candidate}')
    text = candidate.read_text(encoding='utf-8-sig')
    rows = text.splitlines()
    total = len(rows)
    if lines:
        match = re.fullmatch(r'\s*(\d+)\s*(?:[-–—~,]\s*(\d+))?\s*',lines)
        if not match:
            raise ValueError('--lines 需要 N 或 A-B 形式')
        start = max(1,int(match.group(1)))
        end = min(total,int(match.group(2)) if match.group(2) else start)
        if start>end:
            raise ValueError(f'行范围无效（该文件共 {total} 行）')
        picked = [(number,rows[number-1]) for number in range(start,end+1)]
        label = f'行 {start}-{end}'
    elif grep:
        pattern = re.compile(re.escape(grep),re.I)
        hits = [number for number,row in enumerate(rows,1) if pattern.search(row)]
        if not hits:
            print(f'文件: {candidate}\n共 {total} 行\n未找到 "{grep}"。可换关键词，或用 --lines N-M 回看具体行。')
            return
        wanted = sorted({number for hit in hits for number in range(max(1,hit-context),min(total,hit+context)+1)})
        picked = [(number,rows[number-1]) for number in wanted]
        label = f'匹配 "{grep}" {len(hits)} 行'+(f'（±{context} 行上下文）' if context else '')
    elif section:
        found = [number for number,row in enumerate(rows,1) if HEAD_RE.match(row) and section.casefold() in row.casefold()]
        if not found:
            outline = [row.strip() for row in rows if HEAD_RE.match(row)]
            print(f'文件: {candidate}\n共 {total} 行\n未找到章节 "{section}"。可选章节: '+(' | '.join(outline[:12]) if outline else '（无标题行）'))
            return
        start = found[0]
        # 章节延伸到下一个标题之前；无后续标题时到文件末行（故哨兵取 total+1）。
        end = next((number for number in range(start+1,total+1) if HEAD_RE.match(rows[number-1])), total+1)
        picked = [(number,rows[number-1]) for number in range(start,end)]
        label = f'章节 "{rows[start-1].strip()}"'
    else:
        picked, label = list(enumerate(rows,1)), '全文'
    body = '\n'.join(f'{number:>5}| {row}' for number,row in picked)
    print(f'文件: {candidate}\n共 {total} 行  {len(text.encode("utf-8"))} 字节  [{label}]  显示 {len(picked)} 行')
    meta = frontmatter(text)
    for field,display in (('status','状态'),('scope','适用范围'),('source','依据'),('verified_at','核实日期')):
        if meta.get(field):
            print(display+': '+meta[field])
    print(body[:max_chars]+('\n…（输出被 --max-chars 截断）' if len(body)>max_chars else ''))


def metadata_patch(raw, fields):
    """Edit only requested flat fields, preserving body/unknown YAML/BOM/newlines."""
    text = raw.decode('utf-8')
    match = FRONT_RE.match(text)
    if not match:
        raise ValueError('annotate 需要已有 frontmatter；请先人工确认日期和状态')
    rows = text.splitlines(keepends=True)
    close = text[:match.end(1)].count('\n')+1
    newline = '\r\n' if rows[0].endswith('\r\n') else '\n'
    header = rows[1:close]
    for field,value in fields.items():
        matches = [i for i,line in enumerate(header) if re.match(r'^'+re.escape(field)+r':',line)]
        if len(matches)>1:
            raise ValueError(f'{field} 重复，须先人工整理')
        new_line = field+': '+json.dumps(value,ensure_ascii=False)+newline
        if matches:
            i = matches[0]
            tail = header[i].split(':',1)[1].strip()
            if tail.startswith(('|','>')) or (i+1<len(header) and header[i+1][:1].isspace() and header[i+1].strip()):
                raise ValueError(f'{field} 是多行字段，须先人工整理')
            ending = re.search(r'(\r\n|\n|\r)$',header[i])
            header[i] = new_line[:-len(newline)]+(ending[0] if ending else newline)
        else:
            header.append(new_line)
    return (rows[0]+''.join(header)+''.join(rows[close:])).encode('utf-8')


def cmd_annotate(path, summary=None, scope=None, source=None, verified_at=None,
                 expected_hash=None, apply=False):
    if apply:
        require_controller('annotate --apply')
    fields = {name:value for name,value in (('summary',summary),('scope',scope),('source',source),
                                            ('verified_at',verified_at)) if value is not None}
    if not fields or any(not isinstance(v,str) or not v.strip() or '\n' in v or '\r' in v for v in fields.values()):
        raise ValueError('请提供非空、单行的 summary/scope/source/verified-at')
    if verified_at is not None:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',verified_at):
            raise ValueError('verified-at 须为 YYYY-MM-DD；仅在重新核实后填写')
        dt.date.fromisoformat(verified_at)
    if expected_hash is not None and not re.fullmatch(r'[0-9a-f]{64}',expected_hash):
        raise ValueError('expected-hash 须为预览给出的 SHA-256')
    if apply and not expected_hash:
        raise ValueError('保存前请预览，再用 --apply --expected-hash <预览哈希>')
    target = memory_path(path)
    if target.suffix.lower()!='.md' or target.resolve()==(Path(MEM_DIR)/'index.md').resolve():
        raise ValueError('annotate 只允许 memory/ 内的 Markdown 条目，不编辑 index.md 或缓存')
    with write_lock():
        raw = target.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if expected_hash and digest != expected_hash:
            raise ValueError('文件已变化，请重新读取/预览；未覆盖其它 agent 的修改')
        updated = metadata_patch(raw,fields)
        before = frontmatter(raw.decode('utf-8-sig'))
        changes = {name:{'before':before.get(name,''),'after':value}
                   for name,value in fields.items() if before.get(name,'') != value}
        if apply and changes:
            atomic_write(target,updated)
    report = {'path':reference(str(target)), 'expected_hash':digest, 'changes':changes,
              'applied':bool(apply and changes), 'body_preserved':True}
    print(json.dumps(report,ensure_ascii=False,indent=2))
    if changes:
        print('已保存；执行 reindex（离线可用 --local）。' if apply else
              '仅预览；确认后使用 --apply --expected-hash '+digest)
    return report


def cmd_audit(limit=20, json_output=False, include_outdated=False):
    if limit<1:
        raise ValueError('limit 必须大于 0')
    docs = documents()
    paths = eligible(docs,include_outdated)
    fields = ('summary','scope','source','verified_at')
    report = {'documents':len(docs), 'reviewed':len(paths),
              'coverage':{f:sum(bool(docs[p].get(f)) for p in paths) for f in fields},
              'missing':[]}
    for path in sorted(paths):
        missing = [f for f in fields[:3] if not docs[path].get(f)]
        if missing:
            report['missing'].append({'path':path,'missing':missing,'expected_hash':docs[path]['hash']})
    if json_output:
        print(json.dumps(report,ensure_ascii=False,indent=2))
    else:
        print(f'元数据检查: {len(paths)} 个有效/选定条目（全部 {len(docs)}）；不调用 API')
        print(' / '.join(f'{f}: {report["coverage"][f]}/{len(paths)}' for f in fields))
        print('verified_at 是可选核实记录，不因补摘要而自动刷新。')
        for row in report['missing'][:limit]:
            print(row['path']+'  缺少: '+', '.join(row['missing']))
        if len(report['missing'])>limit:
            print(f'另有 {len(report["missing"])-limit} 条；--json 输出完整清单。')
    return report


def body_hash(doc):
    return hashlib.sha256(doc['body'].replace('\r\n','\n').encode('utf-8')).hexdigest()


def cmd_handoff(refs, max_chars=6000):
    if not 512<=max_chars<=16000:
        raise ValueError('handoff max-chars 需在 512 到 16000 之间')
    keys = list(dict.fromkeys(reference(ref) for ref in refs))
    if not keys or len(keys)>5:
        raise ValueError('handoff 需要 1 到 5 个显式选择的记忆条目')
    docs = documents()
    active = eligible(docs)
    entries = []
    for key in keys:
        if key not in active:
            raise ValueError(f'记忆不存在或已过时，请重新检索: {key}')
        doc = docs[key]
        entries.append({'key':key,'sha256':doc['hash'],'text':doc['text']})
    packet = {'schema_version':1,'kind':'memory-handoff','project_root':ROOT,
              'memory_dir':MEM_DIR,'created_at':now().isoformat(),
              'note':'主控显式选择的历史原文；文件哈希只验证内容新鲜度，不证明结论正确。',
              'entries':entries}
    rendered = json.dumps(packet,ensure_ascii=False,indent=2)
    if len(rendered)>max_chars:
        raise ValueError('记忆包超出总字符预算；减少条目或显式提高 --max-chars（最多 16000），不会截断原文')
    print(rendered)
    return packet


def evaluation_cases(path, docs):
    data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(data,dict) or data.get('schema_version')!=1:
        raise ValueError('评测集需要 schema_version: 1 和 cases 数组')
    cases = data.get('cases')
    if not isinstance(cases,list) or not 1<=len(cases)<=1000:
        raise ValueError('评测集需要 1-1000 个问题')
    ids, validated = set(), []
    for case in cases:
        if not isinstance(case,dict):
            raise ValueError('每个评测问题须为对象')
        identity, query = case.get('id'), case.get('query')
        if not isinstance(identity,str) or not identity.strip() or identity in ids:
            raise ValueError('评测 id 须非空且唯一')
        ids.add(identity)
        if not isinstance(query,str) or not query.strip() or len(query)>2000:
            raise ValueError(f'{identity}: query 须为 1-2000 字符')
        no_answer, expected, forbidden = case.get('no_answer',False), case.get('expected',[]), case.get('forbidden',[])
        if type(no_answer) is not bool or any(not isinstance(v,list) or any(not isinstance(p,str) for p in v)
                                               for v in (expected,forbidden)):
            raise ValueError(f'{identity}: 无效的 expected/forbidden/no_answer')
        expected, forbidden = [reference(p) for p in expected], [reference(p) for p in forbidden]
        if len(set(expected))!=len(expected) or bool(expected)==no_answer or set(expected)&set(forbidden):
            raise ValueError(f'{identity}: 有答案问题需要 expected，无答案问题不能指定 expected；标签不得重复或冲突')
        scope = case.get('scope')
        if scope is not None and (not isinstance(scope,str) or not scope.strip()):
            raise ValueError(f'{identity}: scope 须为非空字符串')
        reason = case.get('reason','')
        if not isinstance(reason,str) or not reason.strip():
            raise ValueError(f'{identity}: 请记录人工核对原文后的标签依据 reason')
        evidence = case.get('evidence',{})
        if not isinstance(evidence,dict) or set(evidence)!=set(expected):
            raise ValueError(f'{identity}: 每个 expected 需要 evidence（正文哈希与原文摘录）')
        for target in expected:
            if target not in docs or target not in eligible(docs) or (scope is not None and docs[target].get('scope')!=scope):
                raise ValueError(f'{identity}: 标签目标不存在、已过时或不在范围内: {target}')
            item = evidence[target]
            if not isinstance(item,dict) or item.get('body_sha256')!=body_hash(docs[target]):
                raise ValueError(f'{identity}: 标签正文已变化，请重新核对: {target}')
            quotes = item.get('contains')
            if not isinstance(quotes,list) or not quotes or any(not isinstance(q,str) or not q or q not in docs[target]['body'] for q in quotes):
                raise ValueError(f'{identity}: 依据摘录与原文不符: {target}')
        validated.append(dict(case,query=query.strip(),expected=expected,forbidden=forbidden,no_answer=no_answer))
    return validated


def cmd_eval(cases_file, report=None, k=3, mode='local', max_chars=DEFAULT_MAX_CHARS,
             no_rerank=False, rerank_limit=DEFAULT_RERANK_LIMIT, min_vector_score=None,
             rerank_scale=RERANK_SCALE, min_rerank_score=None, no_cache=False):
    """Evaluate actual corpus labels; default local and never build an index implicitly."""
    docs = documents()
    cases = evaluation_cases(cases_file,docs)  # Validate every label before any API call.
    if report and Path(report).resolve().is_relative_to(Path(MEM_DIR).resolve()):
        raise ValueError('评测报告请保存在 memory/ 之外，避免报告变成记忆')
    scores, timings, pipelines = [], [], {}
    corpus = {p:d['hash'] for p,d in docs.items()}
    for case in cases:
        started = time.perf_counter()
        result = search_result(case['query'],k,False,no_rerank,mode,max_chars,rerank_limit,
                               case.get('scope'),min_vector_score,rerank_scale,min_rerank_score,no_cache)
        if {p:d['hash'] for p,d in result['docs'].items()}!=corpus:
            raise ValueError('评测期间记忆已变化；本次结果未发布，请重新运行')
        _, _, visible = format_search(result,k,max_chars)
        accepted = visible if not result['abstain'] else []
        ranks = [i+1 for i,p in enumerate(accepted) if p in case['expected']]
        pipelines[result['kind']] = pipelines.get(result['kind'],0)+1
        elapsed = (time.perf_counter()-started)*1000
        timings.append(elapsed)
        scores.append({'id':case['id'],'query':case['query'],'expected':case['expected'],
                       'visible':visible,'pipeline':result['kind'],'abstained':result['abstain'],
                       'reranked':result['reranked'],'index':result['index'],
                       'hit':bool(ranks),'reciprocal_rank':1/min(ranks) if ranks else 0,
                       'recall':len(ranks)/len(case['expected']) if case['expected'] else None,
                       'no_answer':case['no_answer'],
                       'no_answer_candidates':case['no_answer'] and bool(result['rows']) and not result['abstain'],
                       'forbidden_hits':[p for p in visible if p in case['forbidden']],
                       'budget_hidden':bool(result['rows']) and not visible,
                       'diagnostics':result['diagnostics'],'latency_ms':round(elapsed,3)})
    if {p:d['hash'] for p,d in documents().items()}!=corpus:
        raise ValueError('评测期间记忆已变化；本次结果未发布，请重新运行')
    positives, negatives = [r for r in scores if not r['no_answer']], [r for r in scores if r['no_answer']]
    ordered = sorted(timings)
    metrics = {'cases':len(scores),'answerable':len(positives),'no_answer':len(negatives),
               'hit_at_k':sum(r['hit'] for r in positives)/len(positives) if positives else None,
               'mrr':sum(r['reciprocal_rank'] for r in positives)/len(positives) if positives else None,
               'recall_at_k':sum(r['recall'] for r in positives)/len(positives) if positives else None,
               'no_answer_candidate_rate':sum(r['no_answer_candidates'] for r in negatives)/len(negatives) if negatives else None,
               'forbidden_cases':sum(bool(r['forbidden_hits']) for r in scores),
               'budget_hidden_cases':sum(r['budget_hidden'] for r in scores),
               'reranked_cases':sum(r['reranked'] for r in scores),
               'latency_ms_p50':round(ordered[math.ceil(len(ordered)*.5)-1],3),
               'latency_ms_p95':round(ordered[math.ceil(len(ordered)*.95)-1],3)}
    result = {'schema_version':1,'tool_version':VERSION,'created_at':now().isoformat(),
              'settings':{'mode':mode,'k':k,'max_chars':max_chars,'no_rerank':no_rerank,
                          'rerank_limit':rerank_limit,'min_vector_score':min_vector_score,
                          'rerank_scale':rerank_scale,'min_rerank_score':min_rerank_score,
                          'embed_model':EMBED_MODEL,'rerank_model':RERANK_MODEL},
              'corpus_sha256':hashlib.sha256(json.dumps(corpus,sort_keys=True).encode('utf-8')).hexdigest(),
              'metrics':metrics,'pipelines':pipelines,'results':scores}
    if report:
        destination = Path(report).expanduser().resolve()
        destination.parent.mkdir(parents=True,exist_ok=True)
        atomic_write(destination,json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'metrics':metrics,'pipelines':pipelines},ensure_ascii=False,indent=2))
    print('命中率按实际展示预算计算；无答案候选率是未弃权候选比例，不是回答准确率。')
    if not all(r['reranked'] for r in scores):
        print('包含本地/未重排/降级/部分索引检索；本结果不能当作完整云端模型准确率。')
    if report:
        print('完整报告: '+str(destination))
    return result


def cmd_outdated(months=6):
    if months<1:
        raise ValueError('months 必须大于 0')
    cutoff, docs, marked, aging = now().date()-dt.timedelta(days=months*30), documents(), [], []
    for path,doc in docs.items():
        row = (doc['updated'],path,doc['title'])
        if doc['status']=='outdated':
            marked.append(row)
        elif dt.date.fromisoformat(doc['updated'])<cutoff:
            aging.append(row)
    print(f'记忆维护清单: {len(docs)} 文件，阈值 {months} 个月（updated/date）')
    for label,rows in (('已标记过时',marked),('超过阈值未更新',aging)):
        print(f'{label}: {len(rows)}')
        for date,path,title in sorted(rows):
            print(f'  [{date}] {path}  {title}')
    index = Path(MEM_DIR)/'index.md'
    lines = len(index.read_text(encoding='utf-8-sig').splitlines()) if index.exists() else 0
    if len(docs)>80 or lines>60:
        print(f'建议合并蒸馏: 文件 {len(docs)}，index.md {lines} 行。')


def protocol_block():
    return BEGIN+'\n'+PROTOCOL+END+'\n'


def update_protocol(existing):
    pattern = re.compile(re.escape(BEGIN)+r'.*?'+re.escape(END)+r'\n?',re.S)
    if pattern.search(existing):
        return pattern.sub(lambda _:protocol_block(),existing,count=1)
    pattern = re.compile(r'^## 项目共享记忆（memory/）[^\n]*\n.*?(?=^#{1,2} |\Z)',re.M|re.S)
    if pattern.search(existing):
        return pattern.sub(lambda _:protocol_block()+'\n',existing,count=1)
    return existing.rstrip()+('\n\n' if existing.strip() else '')+protocol_block()


def atomic_write(path, text):
    path = Path(path)
    temp = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        if isinstance(text,bytes):
            temp.write_bytes(text)
        else:
            temp.write_text(text,encoding='utf-8',newline='\n')
        os.replace(temp,path)
    finally:
        temp.unlink(missing_ok=True)


@contextlib.contextmanager
def write_lock():
    Path(MEM_DIR).mkdir(parents=True,exist_ok=True)
    with open(Path(MEM_DIR)/'.write.lock','a+b') as handle:
        if os.name=='nt':
            import msvcrt
            handle.seek(0,os.SEEK_END)
            if not handle.tell():
                handle.write(b'0')
                handle.flush()
            deadline = time.monotonic()+30
            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
                    break
                except OSError:
                    if time.monotonic()>=deadline:
                        raise RuntimeError('记忆写锁等待超时') from None
                    time.sleep(.05)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
        else:
            import fcntl
            deadline = time.monotonic()+30
            while True:
                try:
                    fcntl.flock(handle,fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic()>=deadline:
                        raise RuntimeError('记忆写锁等待超时') from None
                    time.sleep(.05)
            try:
                yield
            finally:
                fcntl.flock(handle,fcntl.LOCK_UN)


def memory_identity(kind,title,summary,body,scope='',source='',verified_at='',supersedes=None):
    return hashlib.sha256(json.dumps([kind,title,summary,body.replace('\r\n','\n').strip(),
        scope,source,verified_at,supersedes or ''],ensure_ascii=False).encode('utf-8')).hexdigest()


def cmd_add(kind,title,summary,body_file,supersedes=None,scope='',source='',verified_at='',no_dedupe=False):
    require_controller('add/correction')
    title, summary = title.strip(), summary.strip()
    if not title or not summary or '\n' in title or '\n' in summary:
        raise ValueError('标题和摘要必须非空且单行')
    if kind=='correction' and not supersedes:
        raise ValueError('correction 必须提供 --supersedes')
    if supersedes and kind!='correction':
        raise ValueError('--supersedes 仅用于 correction')
    scope, source, verified_at = scope.strip(), source.strip(), verified_at.strip()
    if any('\n' in value or '\r' in value for value in (title,summary,scope,source,verified_at)):
        raise ValueError('标题、摘要与元数据必须是单行')
    if verified_at:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',verified_at):
            raise ValueError('verified-at 需要 YYYY-MM-DD 形式')
        dt.date.fromisoformat(verified_at)
    body = Path(body_file).read_text(encoding='utf-8-sig').strip()
    if not body:
        raise ValueError('记忆正文不能为空')
    old_rel = reference(supersedes) if supersedes else None
    if kind=='correction' and not title.startswith('更正：'):
        title = '更正：'+title
    sub = {'fact':'facts','decision':'decisions','howto':'howto','correction':'facts'}[kind]
    slug = re.sub(r'[<>:"/\\|?*\x00-\x1f]','-',title).strip(' .')[:70] or '记忆'
    stamp = now()
    target = Path(MEM_DIR)/sub/f'{stamp:%Y-%m-%d-%H%M}-{slug}-{uuid.uuid4().hex[:8]}.md'
    text = f'---\ndate: {stamp.date()}\ncreated_at: {stamp.isoformat()}\nstatus: active\ntype: {kind}\n'
    for key,value in (('summary',summary),('scope',scope),('source',source),('verified_at',verified_at)):
        if value:
            text += key+': '+json.dumps(value,ensure_ascii=False)+'\n'
    if old_rel:
        text += 'supersedes: '+json.dumps(old_rel,ensure_ascii=False)+'\n'
    text += f'---\n\n# {title}\n\n{body}\n'
    with write_lock():
        docs = _documents()
        if old_rel and old_rel not in docs:
            raise ValueError('被更正文件不存在')
        identity = memory_identity(kind,title,summary,body,scope,source,verified_at,old_rel)
        if not no_dedupe:
            active = eligible(docs)
            for path,doc in docs.items():
                if path not in active:
                    continue
                previous_body = re.sub(r'\A#\s+[^\n]*(?:\n|$)', '', doc['body'], count=1).strip()
                previous = memory_identity(doc['meta'].get('type',''),doc['title'],doc.get('summary',''),
                    previous_body,doc.get('scope',''),doc.get('source',''),doc.get('verified_at',''),
                    doc['supersedes'][0] if len(doc['supersedes']) == 1 else None)
                if identity == previous:
                    print(f'复用已有记忆: {path}（完全重复，未新增文件）')
                    return path
        if old_rel:
            current = resolve_current(old_rel,replacements(docs))
            if current != old_rel:
                raise ValueError(f'目标已被更正，请先读取并更正当前条目: {current}')
        target.parent.mkdir(parents=True,exist_ok=True)
        with target.open('x',encoding='utf-8',newline='\n') as handle:
            handle.write(text)
        index = Path(MEM_DIR)/'index.md'
        if old_rel:
            old = memory_path(old_rel)
            old_text = old.read_text(encoding='utf-8-sig')
            match, fields = FRONT_RE.match(old_text), frontmatter(old_text)
            fields.update(status='outdated',updated=stamp.isoformat())
            fields.setdefault('date',file_date(old_text,old))
            head = '---\n'+'\n'.join(f'{k}: {json.dumps(v,ensure_ascii=False)}' for k,v in fields.items())+'\n---\n'
            atomic_write(old,head+(old_text[match.end():] if match else old_text))
            if index.exists():
                lines = index.read_text(encoding='utf-8-sig').splitlines()
                lines = [line+'（已被更正）' if old_rel in line and '已被更正' not in line else line for line in lines]
                atomic_write(index,'\n'.join(lines)+'\n')
        rel = reference(str(target))
        with index.open('a',encoding='utf-8',newline='\n') as handle:
            handle.write(f'\n- {title} — `{rel}` — {summary}\n')
    print(f'已保存: {rel}\n提示: 执行 python scripts/mem.py reindex')
    return rel


def cmd_init(target):
    require_controller('init')
    import shutil
    target = Path(target).expanduser().resolve()
    if not target.is_dir():
        raise ValueError(f'目标目录不存在: {target}')
    (target/'scripts').mkdir(exist_ok=True)
    destination = target/'scripts/mem.py'
    if Path(__file__).resolve()!=destination:
        shutil.copyfile(__file__,destination)
    for sub in ('decisions','facts','howto'):
        (target/'memory'/sub).mkdir(parents=True,exist_ok=True)
    index = target/'memory/index.md'
    if not index.exists():
        index.write_text('# 记忆索引\n\n新增条目仅追加；使用 mem.py add 避免并发冲突。\n',encoding='utf-8')
    agents = target/'AGENTS.md'
    atomic_write(agents,update_protocol(agents.read_text(encoding='utf-8-sig') if agents.exists() else ''))
    ignore = target/'.gitignore'
    existing = ignore.read_text(encoding='utf-8-sig') if ignore.exists() else ''
    missing = [rule for rule in IGNORE_RULES if rule not in existing.splitlines()]
    if missing:
        with ignore.open('a',encoding='utf-8',newline='\n') as handle:
            handle.write(('\n' if existing and not existing.endswith('\n') else '')+'\n'.join(missing)+'\n')
    print(f'已初始化/升级: {target}（mem {VERSION}；不复制 key，保留其它项目约定）\n下一步: reindex + doctor')


def cmd_doctor():
    docs, issues = documents(), []
    try:
        known, rows = cached()
        if not fresh(docs,known):
            coverage = index_coverage(docs,known)
            issues.append(f'索引部分/未同步: {coverage["current"]}/{coverage["total"]} 文件向量可复用，{coverage["pending"]} 待更新；执行 reindex')
    except sqlite3.Error:
        issues.append('索引旧格式/损坏: reindex（必要时 --force）')
        known, rows = {}, []
    agents = Path(ROOT)/'AGENTS.md'
    if not agents.exists() or protocol_block().strip() not in agents.read_text(encoding='utf-8-sig'):
        issues.append('项目协议版本不一致: 用最新版 mem.py init 升级')
    kit_script = Path.home()/'.shared-memory-kit/scripts/mem.py'
    if kit_script.exists() and kit_script.read_text(encoding='utf-8-sig')!=Path(__file__).read_text(encoding='utf-8-sig'):
        issues.append('项目与工具包脚本不一致: 核查版本后同步，不要覆盖项目自定义修改')
    for path,doc in docs.items():
        for field in ('date','status'):
            if not doc['meta'].get(field):
                issues.append(f'{path}: 缺少 {field}')
        if doc['status'] not in ('active','outdated'):
            issues.append(f'{path}: status 无效')
        if doc['meta'].get('supersedes') and not doc['supersedes']:
            issues.append(f'{path}: supersedes 路径无效')
        for old in doc['supersedes']:
            if old not in docs:
                issues.append(f'{path}: supersedes 目标不存在: {old}')
    links = replacements(docs)
    for path in links:
        seen, current = set(), path
        while current in links:
            if current in seen:
                issues.append(f'更正关系有环: {path}')
                break
            seen.add(current)
            current = links[current]
    try:
        api_key()
        key_state = '已配置（不显示值，未调用 API）'
    except RuntimeError:
        key_state = '未配置，search 可本地降级'
    print(f'mem {VERSION}: {len(docs)} 记忆文件，{len(rows)} 索引段\nAPI key: {key_state}\n诊断问题: {len(issues)}')
    for issue in issues:
        print('  - '+issue)
    return len(issues)


def cmd_stats():
    docs = documents()
    try:
        known, rows = cached()
    except sqlite3.Error:
        known, rows = {}, []
    print(f'mem {VERSION}: {len(docs)} 记忆文件 / 索引 {len(known)} 文件 {len(rows)} 段')
    coverage = index_coverage(docs,known)
    print('索引新鲜度: '+('已同步' if fresh(docs,known) else
          f'部分/未同步（{coverage["current"]}/{coverage["total"]} 文件可复用向量；其余用当前 Markdown）'))
    print(f'模型: {EMBED_MODEL} + {RERANK_MODEL} @ {BASE_URL}')


def search_options(s, default_mode='auto', allow_scope=True):
    s.add_argument('-k',type=int,default=3)
    s.add_argument('--no-rerank',action='store_true')
    s.add_argument('--mode',choices=('auto','local','hybrid'),default=default_mode)
    s.add_argument('--max-chars',type=int,default=DEFAULT_MAX_CHARS)
    s.add_argument('--rerank-limit',type=int,default=DEFAULT_RERANK_LIMIT)
    if allow_scope:
        s.add_argument('--scope')
    s.add_argument('--min-vector-score',type=float)
    s.add_argument('--rerank-scale',choices=('probability','logit'),default=RERANK_SCALE)
    s.add_argument('--min-rerank-score',type=float); s.add_argument('--no-cache',action='store_true')


def main():
    _utf8_stdout()
    parser = argparse.ArgumentParser(description=f'共享记忆 mem {VERSION}（标准库）')
    parser.add_argument('--version',action='version',version=VERSION)
    sub = parser.add_subparsers(dest='cmd',required=True)
    r = sub.add_parser('reindex'); r.add_argument('--force',action='store_true'); r.add_argument('--local',action='store_true')
    s = sub.add_parser('search'); s.add_argument('query'); s.add_argument('--include-outdated',action='store_true')
    search_options(s)
    e = sub.add_parser('eval',help='基于实际 Markdown 与已核对标签评测；默认 local')
    e.add_argument('cases_file'); e.add_argument('--report')
    search_options(e,'local',False)
    au = sub.add_parser('audit',help='离线检查旧记忆元数据')
    au.add_argument('--limit',type=int,default=20); au.add_argument('--json',action='store_true')
    au.add_argument('--include-outdated',action='store_true')
    an = sub.add_parser('annotate',help='预览/补充元数据，保留正文与原日期')
    an.add_argument('path'); an.add_argument('--apply',action='store_true'); an.add_argument('--expected-hash')
    for field in ('summary','scope','source','verified-at'):
        an.add_argument('--'+field)
    o = sub.add_parser('outdated'); o.add_argument('--months',type=int,default=6)
    sub.add_parser('stats'); sub.add_parser('doctor')
    i = sub.add_parser('init'); i.add_argument('target')
    h = sub.add_parser('show'); h.add_argument('path'); h.add_argument('--grep'); h.add_argument('-C','--context',type=int,default=0); h.add_argument('--lines'); h.add_argument('--section'); h.add_argument('--max-chars',type=int,default=200000)
    handoff = sub.add_parser('handoff',help='将主控显式选择的有效记忆及哈希交给子智能体；JSON 标准输出')
    handoff.add_argument('--ref',action='append',required=True)
    handoff.add_argument('--max-chars',type=int,default=6000)
    a = sub.add_parser('add'); a.add_argument('--type',choices=('fact','decision','howto','correction'),required=True)
    for name in ('title','summary','body-file'):
        a.add_argument('--'+name,required=True)
    a.add_argument('--supersedes')
    a.add_argument('--scope',default=''); a.add_argument('--source',default='')
    a.add_argument('--verified-at',default=''); a.add_argument('--no-dedupe',action='store_true')
    args = parser.parse_args()
    try:
        if args.cmd=='search': cmd_search(args.query,args.k,args.include_outdated,args.no_rerank,
            args.mode,args.max_chars,args.rerank_limit,args.scope,args.min_vector_score,
            args.rerank_scale,args.min_rerank_score,args.no_cache)
        elif args.cmd=='reindex': cmd_reindex(args.force,args.local)
        elif args.cmd=='outdated': cmd_outdated(args.months)
        elif args.cmd=='init': cmd_init(args.target)
        elif args.cmd=='add': cmd_add(args.type,args.title,args.summary,args.body_file,args.supersedes,
            args.scope,args.source,args.verified_at,args.no_dedupe)
        elif args.cmd=='show': cmd_show(args.path,args.grep,args.context,args.lines,args.section,args.max_chars)
        elif args.cmd=='handoff': cmd_handoff(args.ref,args.max_chars)
        elif args.cmd=='audit': cmd_audit(args.limit,args.json,args.include_outdated)
        elif args.cmd=='annotate': cmd_annotate(args.path,args.summary,args.scope,args.source,args.verified_at,
                                               args.expected_hash,args.apply)
        elif args.cmd=='eval': cmd_eval(args.cases_file,args.report,args.k,args.mode,args.max_chars,args.no_rerank,
                                        args.rerank_limit,args.min_vector_score,args.rerank_scale,args.min_rerank_score,args.no_cache)
        elif args.cmd=='doctor': return 1 if cmd_doctor() else 0
        else: cmd_stats()
        return 0
    except (RuntimeError,OSError,sqlite3.Error,ValueError) as error:
        print(f'[mem] 错误: {error}',file=sys.stderr)
        return 1


if __name__=='__main__':
    sys.exit(main())
