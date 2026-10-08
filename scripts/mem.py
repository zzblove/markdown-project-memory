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
import sqlite3
import struct
import sys
import time
import urllib.error
import urllib.request
import uuid

VERSION = '2.1.1'
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
# 弃权阈值。重排器 archetype 未知，两种口径都取保守值：
# (a) 归一化分（0~1 区间，如 siliconflow bge-reranker-v2-m3）取 0.70；(b) 对数几率原始分取 0.0。
# 0.70 来自 2026-10-07 实测标定（bge-reranker-v2-m3，43 条中文记忆）：
#   "记忆里确实有答案"    top-1 落在 0.866 ~ 0.994（9 个查询）
#   "实体在但该属性没记"  top-1 落在 0.160 ~ 0.547（3 个查询）
#   "完全无关/域外"      top-1 = 0.000，已被过滤为 0 条
# 阈值取在 0.547 与 0.866 之间的空档。换重排服务商后分数分布可能整体平移，
# 应用 `search --no-rerank` 或直接比对原始分重新标定，不要照搬该数字。
MIN_SCORE, MIN_LOGIT = 0.70, 0.0
BEGIN, END = '<!-- shared-memory:BEGIN -->', '<!-- shared-memory:END -->'
PROTOCOL = '''## 项目共享记忆（memory/）

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
'''
IGNORE_RULES = ('memory/.index.sqlite', 'memory/.index.sqlite-*', 'memory/.write.lock', 'scripts/.mem_api_key')


def _utf8_stdout():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')


def now():
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))


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
    if not norm:
        raise RuntimeError('嵌入 API 返回零向量')
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
        if not rows or len(set(indexes)) != len(indexes) or any(i<0 or i>=len(docs) for i in indexes):
            raise RuntimeError('重排 API 返回无效序号')
        if not all(math.isfinite(row['relevance_score']) for row in rows):
            raise RuntimeError('重排 API 返回无效分数')
        return rows
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError(f'重排响应格式错误: {type(error).__name__}') from None


def profile():
    return json.dumps([VERSION, BASE_URL, EMBED_MODEL, LIMIT], separators=(',', ':'))


def frontmatter(text):
    match, result = FRONT_RE.match(text), {}
    if match:
        for line in match[1].splitlines():
            pair = re.match(r'^([\w-]+):\s*(.*?)\s*$', line)
            if pair:
                result[pair[1]] = pair[2].strip('"\'')
    return result


def reference(value):
    path = Path(value.replace('\\', '/'))
    if not path.is_absolute():
        path = Path(MEM_DIR)/path if path.parts and path.parts[0] in ('facts','decisions','howto') else Path(ROOT)/path
    path = path.resolve()
    if not path.is_relative_to(Path(MEM_DIR).resolve()):
        raise ValueError('supersedes 必须指向 memory/ 内文件')
    return os.path.relpath(path, ROOT).replace('\\', '/')


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
                found[os.path.relpath(path, ROOT).replace('\\', '/')] = str(path)
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
            for item in meta.get('supersedes', '').strip('[]').split(','):
                if item.strip():
                    try:
                        supersedes.append(reference(item.strip().strip('"\'')))
                    except ValueError:
                        pass
            result[rel] = dict(path=rel, absolute=path, text=text, body=body,
                hash=hashlib.sha256(raw).hexdigest(), meta=meta,
                title=title[1] if title else Path(path).stem, date=file_date(text,path),
                updated=file_date(text,path,True), status=meta.get('status','active').lower(), supersedes=supersedes)
        except FileNotFoundError:
            continue
    return result


def documents():
    # 与 add 的发布/更正操作协调，避免读取其尚未写完的文件或关系链。
    with write_lock():
        return _documents()


def split_chunks(text, fallback_title=''):
    text = FRONT_RE.sub('', text, count=1).strip()
    chunks, heading, lines = [], fallback_title, []
    def flush():
        body = '\n'.join(lines).strip()
        if any(line.strip() and not HEAD_RE.match(line) for line in body.splitlines()):
            chunks.extend((heading, body[i:i+LIMIT]) for i in range(0,len(body),LIMIT))
    for line in text.splitlines():
        if HEAD_RE.match(line):
            flush()
            heading, lines = line.strip(), [line]
        else:
            lines.append(line)
    flush()
    return chunks


def chunk_input(doc, heading, body):
    return f"{doc['title'][:200]}\n{heading[:100]}\n{body}"


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


def fresh(docs, known):
    return set(docs)==set(known) and all(known[p]==(d['hash'],profile()) for p,d in docs.items())


def cmd_reindex(force=False):
    if not Path(MEM_DIR).is_dir():
        raise RuntimeError(f'记忆目录不存在: {MEM_DIR}')
    with contextlib.closing(db_conn()):
        pass
    docs, (known, rows) = documents(), cached()
    reuse = {h:b for p,_,_,h,b in rows if known.get(p,(None,None))[1]==profile()}
    pending, inputs = {}, {}
    for path, doc in docs.items():
        if not force and known.get(path)==(doc['hash'],profile()):
            continue
        prepared = []
        for heading, body in split_chunks(doc['text'], doc['title']):
            value = chunk_input(doc,heading,body)
            digest = hashlib.sha256(value.encode('utf-8')).hexdigest()
            prepared.append((heading,body,digest))
            if force or digest not in reuse:
                inputs[digest] = value
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
    if skipped:
        print('提示: 计算期间文件变化，可再次 reindex；search 将使用最新 Markdown。')


def replacements(docs):
    links = {}
    for path, doc in sorted(docs.items(),key=lambda item:(item[1]['updated'],item[0])):
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


def print_results(query, results, docs, mode, k, abstain=False):
    links, dedup = replacements(docs), {}
    for score,path,heading,body in results:
        if path not in dedup or score>dedup[path][0]:
            dedup[path] = (score,path,heading,body)
    final = sorted(dedup.values(),key=lambda row:(row[0],docs[row[1]]['updated']),reverse=True)[:k]
    print(f'查询: {query}  ({mode}, {len(final)} 个文件)')
    if final and abstain:
        print('[mem] 无相关记忆：top-1 分数低于弃权阈值，判为"记忆里没有这一项"。')
        print('[mem] 以下候选仅因语义相近而出现，可能只是谈到了同一对象；不要据此回答，需要时人工核对。')
    for i,(score,path,heading,body) in enumerate(final,1):
        doc = docs[path]
        state = doc['status']+('/已被取代' if path in links else '')
        # 行数与 show 用同一口径（splitlines），保证 --lines 可直接照抄。
        print(f"[{i}] {doc['updated']} [{state}] score={score:.3f}  {path}  行数={len(doc['text'].splitlines())}  字节={len(doc['text'].encode('utf-8'))}")
        if heading.strip():
            print('    命中章节: '+heading.strip())
        print('    片段: '+highlight(body,query))
        outline = headings(doc)
        if outline:
            print('    大纲: '+' | '.join(outline[:8])+(' | …' if len(outline)>8 else ''))
        print("    回看: python scripts/mem.py show \""+doc['absolute']+'"')
        if doc['supersedes']:
            print('    更正替代: '+', '.join(doc['supersedes']))
        if path in links:
            print('    当前条目: '+resolve_current(path,links))
    if not final:
        print('无相关记忆。请换/缩短关键词重试；仍无结果时用 rg -n -i "<关键词>" memory/ 检索，或查看 memory/index.md。')
    print('提示: 以上是候选清单；引用数字、哈希、行号、路径前，必须用 show --grep 回看原文。')


def grep_fallback(query, k, include_outdated=False, docs=None):
    docs = documents() if docs is None else docs
    allowed, links, results = eligible(docs,include_outdated), replacements(docs), []
    for path,doc in docs.items():
        score = lexical_score(query,doc['title']+'\n'+doc['body'])
        target = path if include_outdated else resolve_current(path,links)
        if score>0 and target in allowed:
            results.append((score,target,docs[target]['title'],docs[target]['body']))
    print_results(query,results,docs,'当前 Markdown 关键词检索',k)


def cmd_search(query, k=5, include_outdated=False, no_rerank=False):
    if k<1 or not query.strip():
        raise ValueError('查询不能为空，k 必须大于 0')
    docs = documents()
    try:
        known, rows = cached()
        if not fresh(docs,known):
            raise RuntimeError('索引未同步或格式/模型配置变化；请 reindex')
        if not rows:
            raise RuntimeError('语义索引为空')
        qv, scored = normalize(embed([query])[0]), []
        for path,heading,body,_,blob in rows:
            vector = struct.unpack(f'<{len(blob)//4}f',blob)
            if len(vector)!=len(qv):
                raise RuntimeError('嵌入维度变化；请 reindex --force')
            scored.append((sum(a*b for a,b in zip(qv,vector)),path,heading,body))
        links, allowed = replacements(docs), eligible(docs,include_outdated)
        ordered, lookup, expanded = sorted(scored,reverse=True), {}, {}
        for row in ordered:
            lookup.setdefault(row[1],row)
        for score,path,heading,body in ordered:
            target = path if include_outdated else resolve_current(path,links)
            if target not in allowed:
                continue
            if target!=path:
                _,_,heading,body = lookup.get(target,(score,target,docs[target]['title'],docs[target]['body']))
            key = (target,heading,body)
            if key not in expanded or score>expanded[key][0]:
                expanded[key] = (score,target,heading,body)
        # 先排除无效历史、沿更正链映射，再截断召回，防止历史块挤占名额。
        candidates = sorted(expanded.values(),reverse=True)[:RECALL]
        if not candidates:
            return grep_fallback(query,k,include_outdated,docs)
        vector_mode, final, weak = False, [], False
        if no_rerank:
            print('[mem] --no-rerank：使用纯向量分数排序（余弦相似度，与重排分不可比）',file=sys.stderr)
            final, vector_mode, mode = candidates, True, '向量排序（--no-rerank）'
        else:
            try:
                ranked = rerank(query,[chunk_input(docs[p],h,b) for _,p,h,b in candidates],len(candidates))
                final = [(r['relevance_score'],)+candidates[r['index']][1:] for r in ranked]
                mode = '向量召回 + 重排，按文件去重'
            except RuntimeError as error:
                print(f'[mem] {error}，使用向量排序',file=sys.stderr)
                final, vector_mode, mode = candidates, True, '向量排序，按文件去重（阈值未校准）'
        latest = documents()
        if {p:d['hash'] for p,d in docs.items()}!={p:d['hash'] for p,d in latest.items()}:
            print('[mem] 检索期间文件变化，使用最新 Markdown',file=sys.stderr)
            return grep_fallback(query,k,include_outdated,latest)
        if final:
            ordered = sorted(final,reverse=True)
            top = ordered[0][0]
            # 分数区间因服务商而异；阈值下方收敛为 top-1，避免把"最不差的几条"当依据。
            durable, weak = (MIN_LOGIT if top<=0 else MIN_SCORE), ordered[0][0]<(MIN_LOGIT if top<=0 else MIN_SCORE)
            final = ordered[:1] if weak else [row for row in ordered if row[0]>=durable]
        print_results(query,final,docs,mode,k,weak and not vector_mode)
    except (RuntimeError,sqlite3.Error,struct.error,IndexError) as error:
        print(f'[mem] {error}，降级检索当前 Markdown',file=sys.stderr)
        return grep_fallback(query,k,include_outdated)


def cmd_show(path, grep=None, context=0, lines=None, section=None, max_chars=200000):
    """定点的无损回看：整篇、按关键词定位、按行范围或按章节。"""
    if context<0 or max_chars<1:
        raise ValueError('context 不能为负，max-chars 必须大于 0')
    candidate = Path(os.path.expanduser(path))
    if not candidate.is_absolute():
        candidate = Path(ROOT)/candidate
    candidate = candidate.resolve()
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
    print(body[:max_chars]+('\n…（输出被 --max-chars 截断）' if len(body)>max_chars else ''))


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


def cmd_add(kind,title,summary,body_file,supersedes=None):
    title, summary = title.strip(), summary.strip()
    if not title or not summary or '\n' in title or '\n' in summary:
        raise ValueError('标题和摘要必须非空且单行')
    if kind=='correction' and not supersedes:
        raise ValueError('correction 必须提供 --supersedes')
    if supersedes and kind!='correction':
        raise ValueError('--supersedes 仅用于 correction')
    body = Path(body_file).read_text(encoding='utf-8-sig').strip()
    if not body:
        raise ValueError('记忆正文不能为空')
    old_rel = reference(supersedes) if supersedes else None
    if old_rel and not (Path(ROOT)/old_rel).is_file():
        raise ValueError('被更正文件不存在')
    if kind=='correction' and not title.startswith('更正：'):
        title = '更正：'+title
    sub = {'fact':'facts','decision':'decisions','howto':'howto','correction':'facts'}[kind]
    slug = re.sub(r'[<>:"/\\|?*\x00-\x1f]','-',title).strip(' .')[:70] or '记忆'
    stamp = now()
    target = Path(MEM_DIR)/sub/f'{stamp:%Y-%m-%d-%H%M}-{slug}-{uuid.uuid4().hex[:8]}.md'
    text = f'---\ndate: {stamp.date()}\nstatus: active\ntype: {kind}\n'
    if old_rel:
        text += f'supersedes: {old_rel}\n'
    text += f'---\n\n# {title}\n\n{body}\n'
    with write_lock():
        target.parent.mkdir(parents=True,exist_ok=True)
        with target.open('x',encoding='utf-8',newline='\n') as handle:
            handle.write(text)
        index = Path(MEM_DIR)/'index.md'
        if old_rel:
            old = Path(ROOT)/old_rel
            old_text = old.read_text(encoding='utf-8-sig')
            match, fields = FRONT_RE.match(old_text), frontmatter(old_text)
            fields.update(status='outdated',updated=str(stamp.date()))
            fields.setdefault('date',file_date(old_text,old))
            head = '---\n'+'\n'.join(f'{k}: {v}' for k,v in fields.items())+'\n---\n'
            atomic_write(old,head+(old_text[match.end():] if match else old_text))
            if index.exists():
                lines = index.read_text(encoding='utf-8-sig').splitlines()
                lines = [line+'（已被更正）' if old_rel in line and '已被更正' not in line else line for line in lines]
                atomic_write(index,'\n'.join(lines)+'\n')
        rel = os.path.relpath(target,ROOT).replace('\\','/')
        with index.open('a',encoding='utf-8',newline='\n') as handle:
            handle.write(f'\n- {title} — `{rel}` — {summary}\n')
    print(f'已保存: {rel}\n提示: 执行 python scripts/mem.py reindex')
    return rel


def cmd_init(target):
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
            issues.append('索引未同步/格式模型变化: reindex')
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
    print('索引新鲜度: '+('已同步' if fresh(docs,known) else '未同步（search 使用 Markdown）'))
    print(f'模型: {EMBED_MODEL} + {RERANK_MODEL} @ {BASE_URL}')


def main():
    _utf8_stdout()
    parser = argparse.ArgumentParser(description=f'共享记忆 mem {VERSION}（标准库）')
    parser.add_argument('--version',action='version',version=VERSION)
    sub = parser.add_subparsers(dest='cmd',required=True)
    r = sub.add_parser('reindex'); r.add_argument('--force',action='store_true')
    s = sub.add_parser('search'); s.add_argument('query'); s.add_argument('-k',type=int,default=5); s.add_argument('--include-outdated',action='store_true'); s.add_argument('--no-rerank',action='store_true')
    o = sub.add_parser('outdated'); o.add_argument('--months',type=int,default=6)
    sub.add_parser('stats'); sub.add_parser('doctor')
    i = sub.add_parser('init'); i.add_argument('target')
    h = sub.add_parser('show'); h.add_argument('path'); h.add_argument('--grep'); h.add_argument('-C','--context',type=int,default=0); h.add_argument('--lines'); h.add_argument('--section'); h.add_argument('--max-chars',type=int,default=200000)
    a = sub.add_parser('add'); a.add_argument('--type',choices=('fact','decision','howto','correction'),required=True)
    for name in ('title','summary','body-file'):
        a.add_argument('--'+name,required=True)
    a.add_argument('--supersedes')
    args = parser.parse_args()
    try:
        if args.cmd=='search': cmd_search(args.query,args.k,args.include_outdated,args.no_rerank)
        elif args.cmd=='reindex': cmd_reindex(args.force)
        elif args.cmd=='outdated': cmd_outdated(args.months)
        elif args.cmd=='init': cmd_init(args.target)
        elif args.cmd=='add': cmd_add(args.type,args.title,args.summary,args.body_file,args.supersedes)
        elif args.cmd=='show': cmd_show(args.path,args.grep,args.context,args.lines,args.section,args.max_chars)
        elif args.cmd=='doctor': return 1 if cmd_doctor() else 0
        else: cmd_stats()
        return 0
    except (RuntimeError,OSError,sqlite3.Error,ValueError) as error:
        print(f'[mem] 错误: {error}',file=sys.stderr)
        return 1


if __name__=='__main__':
    sys.exit(main())
