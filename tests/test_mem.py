"""Isolated, offline regression tests for the Markdown memory CLI."""
import concurrent.futures
import contextlib
import hashlib
import importlib.util
import http.server
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import struct
import sys
import tempfile
import threading
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / 'scripts' / 'mem.py'


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'scripts').mkdir()
        self.script = self.root / 'scripts' / 'mem.py'
        shutil.copyfile(SOURCE, self.script)
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith('MEMORY_')}
        with mock.patch.dict(os.environ, environment, clear=True):
            spec = importlib.util.spec_from_file_location('isolated_mem', self.script)
            self.mem = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.mem)
        self.real_post = self.mem.post
        self.no_network = mock.patch.object(
            self.mem, 'post', side_effect=AssertionError('External API forbidden in tests'))
        self.no_network.start()
        self.addCleanup(self.no_network.stop)
        self.no_keys = mock.patch.object(
            self.mem, 'api_key', side_effect=RuntimeError('No test API key'))
        self.no_keys.start()
        self.addCleanup(self.no_keys.stop)
        self.capture(self.mem.cmd_init, str(self.root))

    def capture(self, function, *args, **kwargs):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            result = function(*args, **kwargs)
        return result, output.getvalue(), errors.getvalue()

    def add(self, body='Deployment uses Python 3.11.', title='Deployment',
            kind='fact', supersedes=None, **metadata):
        body_file = self.root / 'body.txt'
        body_file.write_text(body, encoding='utf-8')
        path, _, _ = self.capture(self.mem.cmd_add, kind, title, 'Verified environment',
                                  str(body_file), supersedes, **metadata)
        return path

    def index(self):
        with mock.patch.object(self.mem, 'embed', side_effect=lambda texts: [[1., 0.]] * len(texts)):
            self.capture(self.mem.cmd_reindex)

    def test_init_preserves_project_rules_memories_and_ignore_rules(self):
        target = self.root / 'other'
        target.mkdir()
        (target / 'AGENTS.md').write_text('# Project rules\nRun existing tests.\n', encoding='utf-8')
        (target / '.gitignore').write_text('build/\n', encoding='utf-8')
        (target / 'memory').mkdir()
        (target / 'memory' / 'index.md').write_text('# Existing history\n', encoding='utf-8')
        # A source key must never be copied by init.
        (self.root / 'scripts' / '.mem_api_key').write_text('test-placeholder', encoding='utf-8')
        self.capture(self.mem.cmd_init, str(target))
        before = (target / 'AGENTS.md').read_text(encoding='utf-8')
        self.capture(self.mem.cmd_init, str(target))
        self.assertEqual(before, (target / 'AGENTS.md').read_text(encoding='utf-8'))
        self.assertIn('Run existing tests.', before)
        self.assertEqual(before.count(self.mem.BEGIN), 1)
        self.assertEqual((target / 'memory' / 'index.md').read_text(encoding='utf-8'),
                         '# Existing history\n')
        rules = (target / '.gitignore').read_text(encoding='utf-8').splitlines()
        self.assertIn('build/', rules)
        self.assertTrue(all(rules.count(rule) == 1 for rule in self.mem.IGNORE_RULES))
        self.assertFalse((target / 'scripts' / '.mem_api_key').exists())

    def test_concurrent_cli_adds_keep_all_files_and_index_entries(self):
        body = self.root / 'concurrent-body.txt'
        body.write_text('A durable concurrency decision.', encoding='utf-8')
        env = {key: value for key, value in os.environ.items() if not key.startswith('MEMORY_')}
        command = [sys.executable, str(self.script), 'add', '--type', 'decision',
                   '--title', 'Same title', '--summary', 'Concurrent write', '--body-file', str(body),
                   '--no-dedupe']
        def write_one(_):
            return subprocess.run(command, cwd=self.root, env=env, capture_output=True,
                                  encoding='utf-8', timeout=40)
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(write_one, range(6)))
        for result in results:
            self.assertEqual(result.returncode, 0, result.stderr)
        docs = self.mem.documents()
        self.assertEqual(len(docs), 6)
        index = (self.root / 'memory' / 'index.md').read_text(encoding='utf-8')
        for path, doc in docs.items():
            self.assertEqual(index.count(path), 1)
            self.assertIn('A durable concurrency decision.', doc['body'])

    def test_correction_preserves_history_and_redirects_default_search(self):
        old = self.add('Deployment used LegacyMarker Python 3.10.')
        new = self.add('LegacyMarker is replaced; deployment uses Python 3.11.',
                       kind='correction', supersedes=old)
        docs = self.mem.documents()
        self.assertEqual(docs[old]['status'], 'outdated')
        self.assertIn('Python 3.10', docs[old]['body'])
        self.assertEqual(docs[new]['supersedes'], [old])
        _, output, _ = self.capture(self.mem.cmd_search, 'LegacyMarker')
        self.assertIn(new, output)
        self.assertNotIn('  ' + old + '  ', output)
        _, history, _ = self.capture(self.mem.cmd_search, 'LegacyMarker', include_outdated=True)
        self.assertIn('  ' + old + '  ', history)
        self.assertIn('  ' + new + '  ', history)

    def test_alias_paths_keep_correction_links_canonical(self):
        if os.name == 'nt':
            import ctypes
            buffer = ctypes.create_unicode_buffer(32768)
            length = ctypes.windll.kernel32.GetShortPathNameW(str(self.script), buffer, len(buffer))
            if not length or buffer.value == str(self.script):
                self.skipTest('Windows short paths unavailable on this volume')
            alias_script = Path(buffer.value)
        else:
            alias_root = self.root / 'project-alias'
            alias_root.symlink_to(self.root, target_is_directory=True)
            alias_script = alias_root / 'scripts' / 'mem.py'
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith('MEMORY_')}
        environment['MEMORY_DIR'] = str(alias_script.parent.parent / 'memory')
        with mock.patch.dict(os.environ, environment, clear=True):
            spec = importlib.util.spec_from_file_location('alias_mem', alias_script)
            alias = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(alias)
        self.assertEqual(alias.ROOT, str(self.root.resolve()))
        self.assertEqual(alias.MEM_DIR, str((self.root / 'memory').resolve()))
        body = self.root / 'alias-body.txt'
        body.write_text('AliasMarker verified deployment.', encoding='utf-8')
        old, _, _ = self.capture(alias.cmd_add, 'fact', 'Alias environment', 'Initial', str(body))
        new, _, _ = self.capture(alias.cmd_add, 'correction', 'Alias environment', 'Updated', str(body), old)
        self.assertEqual(alias.documents()[new]['supersedes'], [old])
        with mock.patch.object(alias, 'post', side_effect=AssertionError('External API forbidden')):
            _, output, _ = self.capture(alias.cmd_search, 'AliasMarker')
        self.assertIn('  ' + new + '  ', output)
        self.assertNotIn('  ' + old + '  ', output)

    def test_stale_index_search_reads_latest_markdown_without_api(self):
        path = self.add()
        self.index()
        target = self.root / path
        target.write_text(target.read_text(encoding='utf-8').replace('3.11', 'LatestMarker 3.12'),
                          encoding='utf-8')
        with mock.patch.object(self.mem, 'embed', side_effect=AssertionError('Stale index must not call API')):
            _, output, errors = self.capture(self.mem.cmd_search, 'LatestMarker')
        self.assertIn('LatestMarker 3.12', output)
        self.assertIn('Markdown', errors)

    def test_embedding_failure_falls_back_to_current_markdown(self):
        path = self.add('Deployment contains OfflineMarker.')
        self.index()
        with mock.patch.object(self.mem, 'embed', side_effect=RuntimeError('Service unavailable')):
            _, output, errors = self.capture(self.mem.cmd_search, 'OfflineMarker')
        self.assertIn(path, output)
        self.assertIn('OfflineMarker', output)
        self.assertIn('Service unavailable', errors)

    def test_failed_reindex_keeps_previous_cache_and_source(self):
        path = self.add()
        self.index()
        old_cache = self.mem.cached()
        source = self.root / path
        source.write_text(source.read_text(encoding='utf-8') + '\nNew durable fact.\n', encoding='utf-8')
        with mock.patch.object(self.mem, 'embed', side_effect=RuntimeError('Service unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'Service unavailable'):
                self.capture(self.mem.cmd_reindex)
        self.assertEqual(self.mem.cached(), old_cache)
        self.assertIn('New durable fact.', source.read_text(encoding='utf-8'))
        self.assertFalse(self.mem.fresh(self.mem.documents(), old_cache[0]))

    def test_unchanged_reindex_reuses_embeddings(self):
        self.add()
        self.index()
        with mock.patch.object(self.mem, 'embed', side_effect=AssertionError('Unchanged files need no API')):
            self.capture(self.mem.cmd_reindex)
        known, rows = self.mem.cached()
        self.assertTrue(self.mem.fresh(self.mem.documents(), known))
        self.assertTrue(rows)

    def test_show_uses_literal_keywords_and_rejects_outside_paths(self):
        path = self.add('Literal a.b marker.\nDo not match axb.\n')
        _, output, _ = self.capture(self.mem.cmd_show, path, grep='a.b')
        self.assertIn('Literal a.b marker.', output)
        self.assertNotIn('Do not match axb.', output)
        with self.assertRaises(ValueError):
            self.capture(self.mem.cmd_show, str(self.root / 'AGENTS.md'))

    def test_summary_scope_and_evidence_are_stored_and_searchable(self):
        body = self.root / 'summary.txt'
        body.write_text('The verified configuration is recorded here.', encoding='utf-8')
        path, _, _ = self.capture(self.mem.cmd_add, 'fact', 'Config', 'SummaryOnlyMarker: "verified"',
            str(body), scope='production', source='docs/config: "revision 3"', verified_at='2026-10-13')
        doc = self.mem.documents()[path]
        self.assertEqual(doc['summary'], 'SummaryOnlyMarker: "verified"')
        self.assertEqual(doc['source'], 'docs/config: "revision 3"')
        self.assertEqual(doc['scope'], 'production')
        _, output, _ = self.capture(self.mem.cmd_search, 'SummaryOnlyMarker', mode='local')
        self.assertIn(path, output)
        self.assertIn('2026-10-13', output)

    def test_local_search_and_exact_title_do_not_read_keys_or_call_models(self):
        path = self.add(title='服务认证约定')
        self.index()
        with mock.patch.object(self.mem, 'api_key', side_effect=AssertionError('No key reads')), \
             mock.patch.object(self.mem, 'cached', side_effect=AssertionError('No vector reads')), \
             mock.patch.object(self.mem, 'embed', side_effect=AssertionError('No model calls')):
            _, local, _ = self.capture(self.mem.cmd_search, 'Python', mode='local')
            _, automatic, _ = self.capture(self.mem.cmd_search, '服务认证约定')
        self.assertIn(path, local)
        self.assertIn(path, automatic)

    def test_vector_scores_are_not_filtered_by_probability_threshold(self):
        first = self.add('First evidence.', title='First')
        second = self.add('Second evidence.', title='Second')
        self.index()
        with contextlib.closing(self.mem.db_conn()) as conn:
            for path, vector in ((first, [.8,.6]), (second, [.6,.8])):
                conn.execute('UPDATE chunks SET vector=? WHERE path=?', (struct.pack('<2f', *vector), path))
        with mock.patch.object(self.mem, 'embed', return_value=[[1.,0.]]):
            _, output, _ = self.capture(self.mem.cmd_search, 'UnknownSemanticQuery', k=2,
                no_rerank=True, mode='hybrid', max_chars=4000)
            _, filtered, _ = self.capture(self.mem.cmd_search, 'UnknownSemanticQuery', k=2,
                no_rerank=True, mode='hybrid', min_vector_score=.7, max_chars=4000)
        self.assertIn(first, output)
        self.assertIn(second, output)
        self.assertIn(first, filtered)
        self.assertNotIn(second, filtered)

    def test_logit_and_probability_thresholds_are_explicit(self):
        path = self.add()
        self.index()
        with mock.patch.object(self.mem, 'embed', return_value=[[1.,0.]]), \
             mock.patch.object(self.mem, 'rerank', return_value=[{'index':0,'relevance_score':.3}]):
            _, logits, _ = self.capture(self.mem.cmd_search, 'semantic question',
                mode='hybrid', rerank_scale='logit')
            _, probability, _ = self.capture(self.mem.cmd_search, 'semantic question',
                mode='hybrid', rerank_scale='probability')
        self.assertIn(path, logits)
        self.assertNotIn('无相关记忆', logits)
        self.assertIn('无相关记忆', probability)
        with self.assertRaises(ValueError):
            self.capture(self.mem.cmd_search, 'query', min_rerank_score=2.)

    def test_hybrid_preserves_exact_keyword_outside_vector_recall(self):
        target = self.add('Build identifier ExactBuildMarker.', title='Build identifier')
        for number in range(31):
            self.add(f'Other topic number {number}.', title=f'Other {number}')
        self.index()
        with contextlib.closing(self.mem.db_conn()) as conn:
            conn.execute('UPDATE chunks SET vector=? WHERE path=?', (struct.pack('<2f', -1.,0.),target))
        with mock.patch.object(self.mem, 'embed', return_value=[[1.,0.]]):
            _, output, _ = self.capture(self.mem.cmd_search, 'ExactBuildMarker', k=1,
                no_rerank=True, mode='hybrid')
        self.assertIn(target, output)

    def test_rerank_quota_applies_after_file_deduplication(self):
        self.add('\n\n'.join(f'## Section {i}\nEvidence for section {i}.' for i in range(40)), title='Long')
        other = self.add('A separate memory.', title='Other')
        self.index()
        with mock.patch.object(self.mem, 'embed', return_value=[[1.,0.]]), \
             mock.patch.object(self.mem, 'rerank', side_effect=lambda q,docs,n:
                 [{'index':i,'relevance_score':.95} for i in range(n)]) as ranking:
            _, output, _ = self.capture(self.mem.cmd_search, 'semantic question', k=2,
                mode='hybrid', rerank_limit=2, max_chars=4000)
        self.assertEqual(len(ranking.call_args.args[1]), 2)
        self.assertIn(other, output)

    def test_query_and_rerank_cache_reuse_without_caching_final_results(self):
        path = self.add('CacheOriginalMarker.', title='Cache evidence')
        self.index()
        with mock.patch.object(self.mem, 'embed', return_value=[[1.,0.]]) as embedding, \
             mock.patch.object(self.mem, 'rerank', return_value=[{'index':0,'relevance_score':.95}]) as ranking:
            self.capture(self.mem.cmd_search, 'semantic question', mode='hybrid')
            self.capture(self.mem.cmd_search, 'semantic question', mode='hybrid')
            self.assertEqual(embedding.call_count, 1)
            self.assertEqual(ranking.call_count, 1)
            source = self.root / path
            source.write_text(source.read_text(encoding='utf-8').replace('CacheOriginalMarker','CacheLatestMarker'), encoding='utf-8')
            self.index()
            _, output, _ = self.capture(self.mem.cmd_search, 'semantic question', mode='hybrid')
            self.assertEqual(embedding.call_count, 1)
            self.assertEqual(ranking.call_count, 2)
            self.assertIn('CacheLatestMarker', output)
            self.assertNotIn('CacheOriginalMarker', output)
            self.capture(self.mem.cmd_search, 'semantic question', mode='hybrid', no_cache=True)
            self.assertEqual(embedding.call_count, 2)
            self.assertEqual(ranking.call_count, 3)

    def test_api_cache_separates_models_urls_and_dimensions(self):
        with mock.patch.object(self.mem, 'embed', return_value=[[1.,0.]]) as embedding:
            self.mem.query_vector('same query', dimension=2)
            self.mem.query_vector('same query', dimension=2)
            with mock.patch.object(self.mem, 'EMBED_MODEL', 'another-model'):
                self.mem.query_vector('same query', dimension=2)
            with mock.patch.object(self.mem, 'BASE_URL', 'https://example.invalid/v1'):
                self.mem.query_vector('same query', dimension=2)
            self.mem.query_vector('same query', dimension=3)
        self.assertEqual(embedding.call_count, 4)

    def test_api_cache_is_bounded_expires_and_does_not_store_failures(self):
        with mock.patch.object(self.mem, 'CACHE_LIMIT', 2), mock.patch.object(self.mem, 'CACHE_BYTES', 500), \
             mock.patch.object(self.mem.time, 'time', return_value=1000.):
            for number in range(4):
                self.mem._cache_write(str(number), 'test', {'value': number})
            with contextlib.closing(self.mem.db_conn()) as conn:
                count, size = conn.execute('SELECT COUNT(*),SUM(length(value)) FROM api_cache').fetchone()
            self.assertEqual(count, 2)
            self.assertLessEqual(size, 500)
            self.assertIsNotNone(self.mem._cache_read('3'))
        with mock.patch.object(self.mem.time, 'time', return_value=1000.+self.mem.CACHE_TTL+1):
            self.assertIsNone(self.mem._cache_read('3'))
        with mock.patch.object(self.mem, 'embed', side_effect=RuntimeError('Unavailable')) as embedding:
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    self.mem.query_vector('failed query')
        self.assertEqual(embedding.call_count, 2)

    def test_output_budget_includes_diagnostics_and_never_reports_budget_as_no_match(self):
        for number in range(6):
            self.add(f'BudgetMarker evidence {number}.', title=f'Budget {number}')
        for budget in (64,128,512,1500):
            with self.subTest(budget=budget):
                _, output, errors = self.capture(self.mem.cmd_search, 'BudgetMarker', k=6, max_chars=budget)
                self.assertLessEqual(len(output)+len(errors), budget)
                self.assertNotIn('无相关记忆', output)
                self.assertIn('候选', output)

    def test_source_line_ranges_work_with_crlf_and_fenced_headings(self):
        body = '# Guide\n\n'+('Background paragraph. '*60)+'\n\n```python\n# Not a new section\nx = 123\n```\n\n## Verification\nLineRangeMarker is verified.\n'
        path = self.add(body, title='Guide')
        source = self.root / path
        text = source.read_text(encoding='utf-8')
        source.write_bytes(text.replace('\n','\r\n').encode('utf-8'))
        doc = self.mem.documents()[path]
        chunks = self.mem.chunk_records(doc['text'],doc['title'])
        code = next(row for row in chunks if 'x = 123' in row['text'])
        self.assertIn('```python\n# Not a new section\nx = 123\n```', code['text'])
        self.assertNotEqual(code['heading'], '# Not a new section')
        hit = next(row for row in chunks if 'LineRangeMarker' in row['text'])
        start,end = self.mem.hit_lines(doc,hit['text'])
        _, output, _ = self.capture(self.mem.cmd_show,path,lines=f'{start}-{end}')
        self.assertIn('LineRangeMarker', output)
        self.assertNotIn('Background paragraph.', output)

    def test_long_lines_are_completely_indexed_and_api_inputs_are_bounded(self):
        text = '# Long line\n\n'+('A'*3400)+' TailMarker\n'
        chunks = self.mem.split_chunks(text,'Long line')
        self.assertEqual(sum(body.count('A') for _,body in chunks), 3400)
        self.assertTrue(any('TailMarker' in body for _,body in chunks))
        doc = dict(title='T'*300,summary='S'*300,scope='X'*200)
        for heading,body in chunks:
            self.assertLessEqual(len(self.mem.chunk_input(doc,heading,body)), 2000)

    def test_correction_paths_with_commas_and_json_lists_keep_links(self):
        old = self.add('CommaLegacyMarker was the old choice.', title='Cache, deployment')
        new = self.add('The verified new choice.', title='Updated',kind='correction',supersedes=old)
        self.assertEqual(self.mem.documents()[new]['supersedes'], [old])
        _, output, _ = self.capture(self.mem.cmd_search,'CommaLegacyMarker',mode='local')
        self.assertIn(new, output)
        self.assertNotIn('  '+old+'  ', output)
        source = self.root / new
        source.write_text(source.read_text(encoding='utf-8').replace(
            'supersedes: '+json.dumps(old,ensure_ascii=False),
            'supersedes: '+json.dumps([old],ensure_ascii=False)),encoding='utf-8')
        self.assertEqual(self.mem.documents()[new]['supersedes'], [old])

    def test_shared_directory_has_stable_keys_across_project_roots(self):
        shared = self.root / 'shared'
        modules = []
        for relative in ('project-a','nested/project-b'):
            root = self.root / relative
            script = root / 'scripts/mem.py'
            script.parent.mkdir(parents=True)
            shutil.copyfile(SOURCE,script)
            environment = {key:value for key,value in os.environ.items() if not key.startswith('MEMORY_')}
            environment['MEMORY_DIR'] = str(shared)
            with mock.patch.dict(os.environ,environment,clear=True):
                spec = importlib.util.spec_from_file_location('shared_mem',script)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            module.post = mock.Mock(side_effect=AssertionError('No external API'))
            modules.append(module)
        first, second = modules
        body = self.root / 'shared-body.txt'
        body.write_text('SharedLegacyMarker was the old value.',encoding='utf-8')
        old, _, _ = self.capture(first.cmd_add,'fact','Shared','Original',str(body))
        body.write_text('The verified new value.',encoding='utf-8')
        new, _, _ = self.capture(first.cmd_add,'correction','Shared','Updated',str(body),old)
        self.assertEqual(second.documents()[new]['supersedes'], [old])
        _, output, _ = self.capture(second.cmd_search,'SharedLegacyMarker',mode='local')
        self.assertIn(new, output)
        _, original, _ = self.capture(second.cmd_show,new)
        self.assertIn('verified new value', original)
        with mock.patch.object(first,'embed',side_effect=lambda texts:[[1.,0.]]*len(texts)):
            self.capture(first.cmd_reindex)
        self.assertTrue(second.fresh(second.documents(),second.cached()[0]))

    def test_deduplication_keeps_different_scope_source_and_verification(self):
        original = self.add(scope='production',source='config/v1',verified_at='2026-10-13')
        identical = self.add(scope='production',source='config/v1',verified_at='2026-10-13')
        self.assertEqual(original,identical)
        different_scope = self.add(scope='development',source='config/v1',verified_at='2026-10-13')
        different_source = self.add(scope='production',source='config/v2',verified_at='2026-10-13')
        self.assertEqual(len({original,different_scope,different_source}),3)

    def test_concurrent_identical_writes_publish_one_memory(self):
        body = self.root / 'same-body.txt'
        body.write_text('One durable shared fact.',encoding='utf-8')
        command = [sys.executable,str(self.script),'add','--type','fact','--title','Shared',
                   '--summary','Same conclusion','--body-file',str(body)]
        environment = {key:value for key,value in os.environ.items() if not key.startswith('MEMORY_')}
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _:subprocess.run(command,cwd=self.root,env=environment,
                capture_output=True,encoding='utf-8',timeout=40),range(6)))
        for result in results:
            self.assertEqual(result.returncode,0,result.stderr)
        docs = self.mem.documents()
        self.assertEqual(len(docs),1)
        index = (self.root / 'memory/index.md').read_text(encoding='utf-8')
        self.assertEqual(index.count(next(iter(docs))),1)

    def test_correction_of_replaced_target_requires_current_entry(self):
        old = self.add('Old value.')
        current = self.add('New value.',kind='correction',supersedes=old)
        with self.assertRaisesRegex(ValueError,'当前条目'):
            self.add('Conflicting value.',kind='correction',supersedes=old)
        self.assertEqual(len(self.mem.documents()),2)
        self.assertEqual(self.mem.resolve_current(old,self.mem.replacements(self.mem.documents())),current)

    def test_offline_reindex_preserves_existing_vectors(self):
        self.add()
        self.index()
        before = self.mem.cached()
        with mock.patch.object(self.mem,'embed',side_effect=AssertionError('No embedding call')):
            self.capture(self.mem.cmd_reindex,local=True)
        self.assertEqual(self.mem.cached(),before)

    def test_chinese_retrieval_cases(self):
        fixture = SOURCE.parents[1] / 'tests/fixtures/retrieval_cases.json'
        data = json.loads(fixture.read_text(encoding='utf-8'))
        paths = {}
        body = self.root / 'fixture-body.txt'
        for entry in data['memories']:
            body.write_text(entry['body'],encoding='utf-8')
            paths[entry['id']], _, _ = self.capture(self.mem.cmd_add,entry.get('type','fact'),
                entry['title'],entry['summary'],str(body),
                paths.get(entry.get('supersedes')),scope=entry.get('scope',''))
        for case in data['queries']:
            with self.subTest(query=case['query']):
                rows = self.mem.local_candidates(case['query'],self.mem.documents(),scope=case.get('scope'))
                expected = case.get('expected')
                if expected:
                    self.assertIn(paths[expected], [row[1] for row in rows[:3]])
                else:
                    self.assertEqual(rows,[])

    def test_large_display_count_does_not_expand_rerank_api_budget(self):
        for number in range(12):
            self.add(f'Distinct evidence {number}.',title=f'Quota {number}')
        self.index()
        with mock.patch.object(self.mem,'embed',return_value=[[1.,0.]]), \
             mock.patch.object(self.mem,'rerank',side_effect=lambda q,docs,n:
                 [{'index':i,'relevance_score':.95} for i in range(n)]) as ranking:
            self.capture(self.mem.cmd_search,'semantic query',k=20,mode='hybrid',rerank_limit=4,max_chars=4000)
        self.assertEqual(len(ranking.call_args.args[1]),4)

    def test_show_excerpt_keeps_scope_and_evidence_context(self):
        path = self.add('EvidenceContextMarker.',scope='production',source='deploy/manifest',verified_at='2026-10-13')
        doc = self.mem.documents()[path]
        start,end = self.mem.hit_lines(doc,'EvidenceContextMarker.')
        _, output, _ = self.capture(self.mem.cmd_show,path,lines=f'{start}-{end}')
        self.assertIn('production',output)
        self.assertIn('deploy/manifest',output)
        self.assertIn('2026-10-13',output)

    def test_metadata_only_source_is_locally_searchable(self):
        path = self.add('Verified setting.',source='deploy/manifest.yaml')
        with mock.patch.object(self.mem,'cached',side_effect=AssertionError('Exact source needs no vectors')):
            _, output, _ = self.capture(self.mem.cmd_search,'deploy/manifest.yaml')
        self.assertIn(path,output)

    def test_force_reindex_clears_api_responses_only_after_success(self):
        self.add()
        self.index()
        self.mem._cache_write('saved','test',[1,2])
        with mock.patch.object(self.mem,'embed',side_effect=RuntimeError('Unavailable')):
            with self.assertRaises(RuntimeError):
                self.capture(self.mem.cmd_reindex,force=True)
        self.assertEqual(self.mem._cache_read('saved'),[1,2])
        with mock.patch.object(self.mem,'embed',side_effect=lambda texts:[[1.,0.]]*len(texts)):
            self.capture(self.mem.cmd_reindex,force=True)
        self.assertIsNone(self.mem._cache_read('saved'))

    def test_inflight_agent_edit_is_visible_instead_of_cached_candidate(self):
        path = self.add('BeforeAgentEditMarker.',title='Agent evidence')
        self.index()
        source = self.root / path
        def edit_while_ranking(query,docs,n):
            source.write_text(source.read_text(encoding='utf-8').replace(
                'BeforeAgentEditMarker','AfterAgentEditMarker'),encoding='utf-8')
            return [{'index':0,'relevance_score':.95}]
        with mock.patch.object(self.mem,'embed',return_value=[[1.,0.]]), \
             mock.patch.object(self.mem,'rerank',side_effect=edit_while_ranking):
            _, output, errors = self.capture(self.mem.cmd_search,'AgentEditMarker',mode='hybrid')
        self.assertIn('AfterAgentEditMarker',output)
        self.assertNotIn('BeforeAgentEditMarker',output)
        self.assertIn('最新 Markdown',errors)

    def test_invalid_provider_indexes_and_metadata_are_rejected(self):
        with mock.patch.object(self.mem,'post',return_value={
                'results':[{'index':.5,'relevance_score':.9}]}):
            with self.assertRaisesRegex(RuntimeError,'无效序号'):
                self.mem.rerank('query',['first','second'],2)
        with self.assertRaises(ValueError):
            self.add(verified_at='20261013')
        with self.assertRaises(ValueError):
            self.add(scope='production\nstatus: outdated')

    def test_partial_index_keeps_unchanged_vectors_and_latest_local_files(self):
        stable = self.add('Meaning available only through vector lookup.',title='Stable')
        changed = self.add('BeforeChangeMarker.',title='Changed')
        self.index()
        source = self.root / changed
        source.write_text(source.read_text(encoding='utf-8').replace('BeforeChangeMarker','AfterChangeMarker'),encoding='utf-8')
        added = self.add('AfterChangeMarker new shared evidence.',title='New agent')
        # Corrupt stale vectors must be excluded before decoding or dimension selection.
        with contextlib.closing(self.mem.db_conn()) as conn:
            conn.execute('UPDATE chunks SET vector=? WHERE path=?',(b'bad',changed))
            conn.commit()
        with mock.patch.object(self.mem,'embed',return_value=[[1.,0.]]) as embedding, \
             mock.patch.object(self.mem,'rerank',side_effect=AssertionError('Dirty content must not be reranked')):
            result, output, errors = self.capture(self.mem.cmd_search,'AfterChangeMarker',mode='hybrid',max_chars=4000)
        self.assertEqual(result['kind'],'partial')
        self.assertEqual(result['index']['current'],1)
        self.assertEqual(embedding.call_count,1)
        for path in (stable,changed,added):
            self.assertIn(path,output)
        self.assertIn('AfterChangeMarker',output)
        self.assertNotIn('BeforeChangeMarker',output)
        self.assertIn('部分同步',errors)

    def test_removed_and_incompatible_files_never_supply_vectors(self):
        stable = self.add('Retained semantic evidence.',title='Stable')
        removed = self.add('DeletedMarker.',title='Removed')
        incompatible = self.add('NewLocalMarker.',title='Different model')
        self.index()
        (self.root / removed).unlink()
        with contextlib.closing(self.mem.db_conn()) as conn:
            conn.execute('UPDATE files SET profile=? WHERE path=?',('old-model',incompatible))
            conn.execute('UPDATE chunks SET vector=? WHERE path IN (?,?)',(b'bad',removed,incompatible))
            conn.commit()
        with mock.patch.object(self.mem,'embed',return_value=[[1.,0.]]):
            result = self.mem.search_result('NewLocalMarker',mode='hybrid',no_rerank=True)
        self.assertEqual(result['kind'],'partial')
        self.assertEqual({r[1] for r in result['rows']},{stable,incompatible})
        self.assertEqual(result['index']['removed'],1)
        with mock.patch.object(self.mem,'EMBED_MODEL','replacement-model'), \
             mock.patch.object(self.mem,'embed',side_effect=AssertionError('No compatible vectors needs no API')):
            result = self.mem.search_result('NewLocalMarker',mode='hybrid')
        self.assertEqual(result['kind'],'fallback')
        self.assertEqual({r[1] for r in result['rows']},{incompatible})

    def test_partial_index_respects_corrections_and_scope_without_author_partition(self):
        self.add('Other scope.',title='Other',scope='development')
        old = self.add('ScopeMarker production legacy.',title='Old',scope='production')
        self.index()
        new = self.add('ScopeMarker verified current production.',title='Current',kind='correction',supersedes=old,scope='production')
        with mock.patch.object(self.mem,'embed',side_effect=AssertionError('Only outdated scoped vectors must not call API')):
            result = self.mem.search_result('ScopeMarker',mode='hybrid',scope='production')
        self.assertEqual([r[1] for r in result['rows']],[new])
        with mock.patch.object(self.mem,'embed',return_value=[[1.,0.]]):
            result = self.mem.search_result('ScopeMarker',mode='hybrid')
        self.assertIn(new,[r[1] for r in result['rows']])
        self.assertNotIn(old,[r[1] for r in result['rows']])
        self.assertEqual(result['kind'],'partial')

    def test_inflight_change_keeps_other_valid_vectors_and_discards_rerank(self):
        stable = self.add('Unchanged semantic evidence.',title='Stable')
        changed = self.add('BeforeNetworkMarker.',title='Changed')
        self.index()
        def edit_while_ranking(query,inputs,n):
            source = self.root / changed
            source.write_text(source.read_text(encoding='utf-8').replace('BeforeNetworkMarker','AfterNetworkMarker'),encoding='utf-8')
            return [{'index':0,'relevance_score':.99}]
        with mock.patch.object(self.mem,'embed',return_value=[[1.,0.]]) as embedding, \
             mock.patch.object(self.mem,'rerank',side_effect=edit_while_ranking) as ranking:
            result, output, _ = self.capture(self.mem.cmd_search,'NetworkMarker',mode='hybrid',max_chars=4000)
        self.assertEqual(result['kind'],'partial')
        self.assertEqual(embedding.call_count,1)
        self.assertEqual(ranking.call_count,1)
        self.assertIn(stable,output)
        self.assertIn('AfterNetworkMarker',output)
        self.assertNotIn('BeforeNetworkMarker',output)
        self.assertFalse(result['abstain'])

    def test_cli_upgrade_preserves_220_vector_profile_and_api_cache_keys(self):
        self.add()
        legacy_profile = json.dumps(['2.2.0',self.mem.BASE_URL,self.mem.EMBED_MODEL,self.mem.LIMIT],separators=(',',':'))
        self.assertEqual(self.mem.profile(),legacy_profile)
        raw = json.dumps(['2.2.0',self.mem.BASE_URL,'query',[self.mem.EMBED_MODEL,'semantic upgrade',2]],
                         ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')
        key = hashlib.sha256(raw).hexdigest()
        self.index()
        self.mem._cache_write(key,'query',[1.,0.])
        with mock.patch.object(self.mem,'VERSION','future-cli-version'), \
             mock.patch.object(self.mem,'embed',side_effect=AssertionError('Code-only upgrade must reuse vectors and query cache')):
            self.capture(self.mem.cmd_reindex)
            self.assertEqual(self.mem.query_vector('semantic upgrade',dimension=2),[1.,0.])

    def test_annotation_preview_and_apply_preserve_body_unknown_fields_dates_and_crlf(self):
        path = self.add('Exact body.\n\n```yaml\nsummary: keep inside body\n```')
        target = self.root / path
        original = target.read_text(encoding='utf-8').replace('status: active','status: active\ncustom: |\n  Preserve unknown multiline YAML.\nupdated: 2020-01-02')
        raw = b'\xef\xbb\xbf'+original.replace('\n','\r\n').encode('utf-8')
        target.write_bytes(raw)
        old_doc = self.mem.documents()[path]
        preview, _, _ = self.capture(self.mem.cmd_annotate,path,summary='A concise verified description',scope='production',source='Original deployment record')
        self.assertEqual(target.read_bytes(),raw)
        self.assertFalse(preview['applied'])
        applied, _, _ = self.capture(self.mem.cmd_annotate,path,summary='A concise verified description',scope='production',source='Original deployment record',
                                      expected_hash=preview['expected_hash'],apply=True)
        after = target.read_bytes()
        self.assertTrue(applied['applied'])
        self.assertTrue(after.startswith(b'\xef\xbb\xbf---\r\n'))
        self.assertIn(b'custom: |\r\n  Preserve unknown multiline YAML.\r\n',after)
        self.assertEqual(after.split(b'\r\n---\r\n',1)[1],raw.split(b'\r\n---\r\n',1)[1])
        doc = self.mem.documents()[path]
        self.assertEqual(doc['date'],old_doc['date'])
        self.assertEqual(doc['updated'],'2020-01-02')
        self.assertEqual(doc['verified_at'],'')
        self.assertEqual(self.mem.body_hash(doc),self.mem.body_hash(old_doc))

    def test_annotation_refuses_stale_hash_ambiguous_fields_and_outside_paths(self):
        path = self.add()
        preview, _, _ = self.capture(self.mem.cmd_annotate,path,scope='production')
        target = self.root / path
        target.write_text(target.read_text(encoding='utf-8')+'\nAnother agent wrote this.\n',encoding='utf-8')
        before = target.read_bytes()
        with self.assertRaisesRegex(ValueError,'文件已变化'):
            self.capture(self.mem.cmd_annotate,path,scope='production',apply=True,expected_hash=preview['expected_hash'])
        self.assertEqual(target.read_bytes(),before)
        for raw in (b'---\nsummary: first\nsummary: second\n---\nBody',
                    b'---\nsummary: |\n  Multiple lines.\n---\nBody'):
            with self.assertRaises(ValueError):
                self.mem.metadata_patch(raw,{'summary':'Updated'})
        with self.assertRaises(ValueError):
            self.capture(self.mem.cmd_annotate,path,scope='production',apply=True)
        with self.assertRaises(ValueError):
            self.capture(self.mem.cmd_annotate,str(self.root/'body.txt'),scope='production')

    def test_source_annotation_reindex_reuses_embeddings_without_refreshing_verification(self):
        path = self.add()
        self.index()
        preview, _, _ = self.capture(self.mem.cmd_annotate,path,source='manifest revision 7')
        self.capture(self.mem.cmd_annotate,path,source='manifest revision 7',apply=True,expected_hash=preview['expected_hash'])
        with mock.patch.object(self.mem,'embed',side_effect=AssertionError('Source is not embedding input')):
            self.capture(self.mem.cmd_reindex,local=True)
        self.assertEqual(self.mem.documents()[path]['verified_at'],'')
        self.assertTrue(self.mem.fresh(self.mem.documents(),self.mem.cached()[0]))

    def test_hybrid_reuses_local_candidates_and_scope_only_chunks_relevant_files(self):
        target = self.add('ScopedMarker.',title='Scope target',scope='production')
        self.add('Unrelated content.',title='Other',scope='development')
        self.index()
        original = self.mem.local_candidates
        split = self.mem.split_chunks
        with mock.patch.object(self.mem,'local_candidates',wraps=original) as lexical, \
             mock.patch.object(self.mem,'split_chunks',wraps=split) as chunking, \
             mock.patch.object(self.mem,'embed',return_value=[[1.,0.]]):
            result = self.mem.search_result('ScopedMarker',mode='hybrid',scope='production',no_rerank=True)
        self.assertEqual(lexical.call_count,1)
        self.assertEqual(chunking.call_count,1)
        self.assertEqual([r[1] for r in result['rows']],[target])

    def evaluation_fixture(self, paths_queries):
        docs = self.mem.documents()
        cases = []
        for i,(path,query) in enumerate(paths_queries):
            cases.append({'id':str(i),'query':query,'expected':[path] if path else [],'no_answer':path is None,
                          'reason':'Manually checked original fixture evidence.',
                          'evidence':{path:{'body_sha256':self.mem.body_hash(docs[path]),'contains':['# '+docs[path]['title']]}} if path else {}})
        destination = self.root/'real-cases.json'
        destination.write_text(json.dumps({'schema_version':1,'cases':cases}),encoding='utf-8')
        return destination

    def test_corpus_evaluation_tracks_hits_negative_candidates_and_actual_budget(self):
        path = self.add('Deployment uses Python 3.11.',title='Deployment')
        cases = self.evaluation_fixture([(path,'Python 3.11'),(None,'Deployment GPU inventory'),(None,'UnrelatedWeatherMarker')])
        report = self.root/'report.json'
        result, _, _ = self.capture(self.mem.cmd_eval,cases,report=str(report))
        self.assertEqual(result['metrics']['hit_at_k'],1)
        self.assertEqual(result['metrics']['mrr'],1)
        self.assertEqual(result['metrics']['no_answer_candidate_rate'],.5)
        self.assertEqual(result['pipelines'],{'local':3})
        self.assertEqual(json.loads(report.read_text(encoding='utf-8'))['metrics'],result['metrics'])
        result, _, _ = self.capture(self.mem.cmd_eval,cases,max_chars=64)
        self.assertEqual(result['metrics']['hit_at_k'],0)
        self.assertEqual(result['metrics']['no_answer_candidate_rate'],.5)
        self.assertGreater(result['metrics']['budget_hidden_cases'],0)

    def test_corpus_evaluation_uses_same_rerank_abstention_as_search(self):
        path = self.add('Deployment uses Python.',title='Deployment')
        self.index()
        cases = self.evaluation_fixture([(path,'Deployment runtime'),(None,'Deployment GPU inventory')])
        with mock.patch.object(self.mem,'embed',return_value=[[1.,0.]]), \
             mock.patch.object(self.mem,'rerank',return_value=[{'index':0,'relevance_score':.2}]):
            result, _, _ = self.capture(self.mem.cmd_eval,cases,mode='hybrid')
        self.assertEqual(result['metrics']['hit_at_k'],0)
        self.assertEqual(result['metrics']['no_answer_candidate_rate'],0)
        self.assertEqual(result['pipelines'],{'hybrid':2})
        self.assertTrue(all(r['abstained'] for r in result['results']))

    def test_evaluation_validates_all_ground_truth_before_api_or_report(self):
        path = self.add('Deployment runtime.',title='Deployment')
        self.index()
        cases = self.evaluation_fixture([(path,'Deployment'),(path,'runtime')])
        data = json.loads(cases.read_text(encoding='utf-8'))
        data['cases'][1]['evidence'][path]['body_sha256'] = '0'*64
        cases.write_text(json.dumps(data),encoding='utf-8')
        with mock.patch.object(self.mem,'search_result',side_effect=AssertionError('Invalid later label must fail before earlier query')):
            with self.assertRaisesRegex(ValueError,'正文已变化'):
                self.capture(self.mem.cmd_eval,cases,mode='hybrid',report=str(self.root/'report.json'))
        self.assertFalse((self.root/'report.json').exists())
        cases = self.evaluation_fixture([(path,'Deployment')])
        with self.assertRaisesRegex(ValueError,'memory/'):
            self.capture(self.mem.cmd_eval,cases,report=str(self.root/'memory/report.json'))

    def test_evaluation_rejects_inflight_corpus_change(self):
        path = self.add('InitialMarker.',title='Deployment')
        cases = self.evaluation_fixture([(path,'InitialMarker')])
        original = self.mem.search_result
        def change_then_search(*args,**kwargs):
            source = self.root/path
            source.write_text(source.read_text(encoding='utf-8')+'\nAnother agent added evidence.\n',encoding='utf-8')
            return original(*args,**kwargs)
        with mock.patch.object(self.mem,'search_result',side_effect=change_then_search):
            with self.assertRaisesRegex(ValueError,'记忆已变化'):
                self.capture(self.mem.cmd_eval,cases,report=str(self.root/'report.json'))
        self.assertFalse((self.root/'report.json').exists())

    def test_public_project_queries_validate_real_corpus_and_history(self):
        corpus = SOURCE.parents[1]/'memory'
        if not corpus.is_dir():
            self.skipTest('Repository corpus is not distributed with the global toolkit')
        shutil.copytree(corpus,self.root/'memory',dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('.index.sqlite*','.write.lock'))
        cases = SOURCE.parents[1]/'tests/fixtures/project_queries.json'
        result, _, _ = self.capture(self.mem.cmd_eval,cases)
        self.assertEqual(result['metrics']['cases'],26)
        self.assertEqual(result['metrics']['hit_at_k'],1)
        self.assertEqual(result['metrics']['forbidden_cases'],0)
        self.assertEqual(result['metrics']['reranked_cases'],0)

    def test_http_embedding_and_rerank_contract_with_cache(self):
        counts = {'embeddings':0,'rerank':0}
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args):
                pass
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.headers['Authorization'] != 'Bearer offline-test-key':
                    self.send_error(403)
                    return
                if self.path == '/v1/embeddings':
                    counts['embeddings'] += 1
                    result = {'data':[{'index':i,'embedding':[1.,0.]}
                                     for i in range(len(payload['input']))]}
                elif self.path == '/v1/rerank':
                    counts['rerank'] += 1
                    result = {'results':[{'index':i,'relevance_score':.95}
                                         for i in range(payload['top_n'])]}
                else:
                    self.send_error(404)
                    return
                content = json.dumps(result).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(content)))
                self.end_headers()
                self.wfile.write(content)
        server = http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            path = self.add('HTTPContractMarker.',title='HTTP evidence')
            with mock.patch.object(self.mem,'post',self.real_post), \
                 mock.patch.object(self.mem,'api_key',return_value='offline-test-key'), \
                 mock.patch.object(self.mem,'BASE_URL',f'http://127.0.0.1:{server.server_port}/v1'):
                self.capture(self.mem.cmd_reindex)
                self.capture(self.mem.cmd_search,'semantic query',mode='hybrid')
                _, output, _ = self.capture(self.mem.cmd_search,'semantic query',mode='hybrid')
            self.assertIn(path,output)
            self.assertEqual(counts,{'embeddings':2,'rerank':1})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
