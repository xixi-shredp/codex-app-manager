import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from test_chatgpt_app_server import CLI, FakeServer


class DeleteServer(FakeServer):
    def __init__(self, root, fail_id=None, **kwargs):
        self.fail_id = fail_id
        super().__init__(root, **kwargs)

    def result(self, method, params):
        if method == 'thread/delete':
            if params['threadId'] == self.fail_id:
                return {'_delete_failure': True}
            if params['threadId'] in self.loaded:
                self.loaded.remove(params['threadId'])
            return {}
        result = super().result(method, params)
        if method == 'thread/read':
            result['thread']['name'] = 'App 标题 ' + params['threadId'] + '\n下一行'
        return result

    def frame(self, data, **kwargs):
        if isinstance(data, dict) and data.get('result') == {'_delete_failure': True}:
            data = {'id': data['id'], 'error': {'code': -32600, 'message': 'fake delete refused'}}
        return super().frame(data, **kwargs)


class DeleteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.server = DeleteServer(self.root, loaded=['a', 'b'])

    def tearDown(self):
        self.server.close()
        self.assertEqual(self.server.errors, [])
        self.tmp.cleanup()

    def write_team(self, name, members):
        path = self.root / name
        path.write_text(json.dumps({'sessions': members}, ensure_ascii=False))
        return path

    def run_cli(self, command, *args, answer=''):
        return subprocess.run([CLI, '--socket', self.server.path, command, *map(str, args)],
                              input=answer, text=True, capture_output=True, timeout=5)

    def deleted(self):
        return [p['threadId'] for method, p in self.server.calls if method == 'thread/delete']

    def test_confirmation_defaults_to_no_and_table_has_titles(self):
        path = self.write_team('team.json', [{'name': 'JSON 成员', 'id': 'a'}])
        before = path.read_bytes()
        result = self.run_cli('delete', path, answer='\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.deleted(), [])
        self.assertEqual(path.read_bytes(), before)
        self.assertIn('Session ID', result.stdout)
        self.assertIn('Session Name', result.stdout)
        self.assertIn('JSON 成员', result.stdout)
        self.assertIn('App 标题 a\\n下一行', result.stdout)
        self.assertIn('(y/N)', result.stderr)

    def test_eof_cancels(self):
        path = self.write_team('team.json', [{'name': 'A', 'id': 'a'}])
        result = self.run_cli('delete', path)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.deleted(), [])
        self.assertEqual(json.loads(path.read_text())['sessions'][0]['id'], 'a')

    def test_yes_word_is_not_explicit_y(self):
        path = self.write_team('team.json', [{'name': 'A', 'id': 'a'}])
        result = self.run_cli('delete', path, answer='yes\n')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.deleted(), [])

    def test_confirmed_batch_clears_only_existing_ids(self):
        path = self.write_team('team.json', [{'name': 'A', 'id': 'a'}, {'name': '未创建', 'id': None}])
        second = self.write_team('other.json', [{'name': 'B', 'id': 'b'}])
        result = self.run_cli('delete', path, second, answer='Y\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.deleted(), ['a', 'b'])
        self.assertTrue(all(x['id'] is None for x in json.loads(path.read_text())['sessions']))
        self.assertIsNone(json.loads(second.read_text())['sessions'][0]['id'])
        self.assertIn('null', result.stdout)
        self.assertFalse(any(m in {'thread/resume', 'turn/start'} for m, _ in self.server.calls))

    def test_duplicate_id_deleted_once_and_all_references_cleared(self):
        path = self.write_team('team.json', [{'name': 'A', 'id': 'a'}, {'name': 'Alias', 'id': 'a'}])
        result = self.run_cli('delete', path, answer='y\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.deleted(), ['a'])
        self.assertTrue(all(x['id'] is None for x in json.loads(path.read_text())['sessions']))

    def test_partial_failure_retains_failed_id(self):
        self.server.fail_id = 'b'
        path = self.write_team('team.json', [{'name': 'A', 'id': 'a'}, {'name': 'B', 'id': 'b'}])
        result = self.run_cli('delete', path, answer='y\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.deleted(), ['a', 'b'])
        self.assertEqual([x['id'] for x in json.loads(path.read_text())['sessions']], [None, 'b'])

    def test_all_null_needs_no_confirmation(self):
        path = self.write_team('team.json', [{'name': 'A', 'id': None}])
        result = self.run_cli('delete', path, answer='y\n')
        self.assertEqual(result.returncode, 0)
        self.assertNotIn('(y/N)', result.stderr)
        self.assertEqual(self.deleted(), [])

    def test_invalid_second_file_prevents_any_deletion(self):
        path = self.write_team('team.json', [{'name': 'A', 'id': 'a'}])
        bad = self.root / 'bad.json'
        bad.write_text('{')
        result = self.run_cli('delete', path, bad, answer='y\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.deleted(), [])
        self.assertEqual(json.loads(path.read_text())['sessions'][0]['id'], 'a')

    def test_set_recreates_after_delete_without_recovering_deleted_id(self):
        path = self.root / 'team.json'
        # Preserve user's schema_version-free format.
        path.write_text(json.dumps({'team_prompt': '团队', 'sessions': [
            {'name': 'A', 'id': 'a', 'cwd': str(self.root), 'base_prompt': '成员'}]}))
        result = self.run_cli('delete', path, answer='y\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_cli('set', path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(path.read_text())['sessions'][0]['id'], 'new-id')
        self.assertEqual(sum(m == 'thread/start' for m, _ in self.server.calls), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
