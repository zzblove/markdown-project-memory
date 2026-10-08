"""Isolated, offline regression tests for the Markdown memory CLI."""
import concurrent.futures
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
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
            kind='fact', supersedes=None):
        body_file = self.root / 'body.txt'
        body_file.write_text(body, encoding='utf-8')
        path, _, _ = self.capture(self.mem.cmd_add, kind, title, 'Verified environment',
                                  str(body_file), supersedes)
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
                   '--title', 'Same title', '--summary', 'Concurrent write', '--body-file', str(body)]
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


if __name__ == '__main__':
    unittest.main()
