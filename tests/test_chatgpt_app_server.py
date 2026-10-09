import base64
import hashlib
import contextlib
import importlib.machinery
import importlib.util
import sys
import json
import os
import signal
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest

CLI = os.environ.get('CODEX_APP_MANAGER_CLI') or os.environ.get('CHATGPT_APP_SERVER_CLI', str(Path(__file__).resolve().parents[1] / 'codex-app-manager'))


class FakeServer:
    def __init__(self, root, loaded=None, busy=False, wake=True, disconnect=False, fragmented=False, disconnect_create=False, settings_mismatch=False):
        self.path = str(Path(root) / 'app.sock')
        self.loaded = list(loaded or [])
        self.busy, self.wake = busy, wake
        self.disconnect, self.fragmented = disconnect, fragmented
        self.disconnect_create = disconnect_create
        self.settings_mismatch = settings_mismatch
        self.calls = []
        self.errors = []
        self.settings = {}
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(self.path)
        self.listener.listen()
        self.listener.settimeout(.1)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        self.listener.close()
        self.thread.join(2)

    def frame(self, data, opcode=1, fin=True):
        if isinstance(data, dict):
            data = json.dumps(data, ensure_ascii=False).encode()
        n = len(data)
        head = bytes([(128 if fin else 0) | opcode])
        if n < 126:
            head += bytes([n])
        elif n < 65536:
            head += bytes([126]) + struct.pack('!H', n)
        else:
            head += bytes([127]) + struct.pack('!Q', n)
        return head + data

    def read_exact(self, conn, n):
        out = b''
        while len(out) < n:
            part = conn.recv(n - len(out))
            if not part:
                raise EOFError()
            out += part
        return out

    def read_frame(self, conn):
        first, second = self.read_exact(conn, 2)
        n = second & 127
        if n == 126:
            n = struct.unpack('!H', self.read_exact(conn, 2))[0]
        elif n == 127:
            n = struct.unpack('!Q', self.read_exact(conn, 8))[0]
        mask = self.read_exact(conn, 4) if second & 128 else None
        data = self.read_exact(conn, n)
        if mask:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        return first & 15, data

    def result(self, method, p):
        tid = p.get('threadId', 'new-id')
        if method == 'initialize':
            return {'userAgent': 'fake-app'}
        if method == 'thread/loaded/list':
            return {'data': self.loaded, 'nextCursor': None}
        if method in ('thread/start', 'thread/resume') and tid not in self.loaded:
            self.loaded.append(tid)
        if method in ('thread/start', 'thread/resume', 'thread/settings/update'):
            self.settings.setdefault(tid, {}).update({k: v for k, v in p.items() if v is not None})
        cfg = self.settings.get(tid, {})
        if method in ('thread/start', 'thread/resume', 'thread/read'):
            obj = {'id': tid, 'cwd': cfg.get('cwd', '/tmp'), 'turns': [],
                   'status': {'type': 'active' if self.busy else 'idle'}}
            out = {'thread': obj, 'model': cfg.get('model', 'gpt-6.1-sol'),
                   'modelProvider': 'openai', 'cwd': obj['cwd'],
                   'approvalPolicy': cfg.get('approvalPolicy', 'on-request'),
                   'sandbox': {'type': cfg.get('sandbox', 'workspaceWrite')},
                   'reasoningEffort': cfg.get('reasoningEffort', 'medium')}
            return out
        if method == 'turn/start':
            return {'turn': {'id': 'turn-1', 'status': 'inProgress', 'items': []}}
        if method == 'config/read':
            return {'config': {}, 'origins': {}, 'layers': []}
        return {}

    def run(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                conn.settimeout(3)
                req = b''
                while b'\r\n\r\n' not in req:
                    req += self.read_exact(conn, 1)
                headers = {}
                for line in req.decode().split('\r\n')[1:]:
                    if ':' in line:
                        k, v = line.split(':', 1)
                        headers[k.lower()] = v.strip()
                key = headers['sec-websocket-key']
                accept = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
                conn.sendall(('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + '\r\n\r\n').encode())
                while not self.stop.is_set():
                    opcode, data = self.read_frame(conn)
                    if opcode == 8:
                        break
                    if opcode == 10:
                        self.calls.append(('pong', data))
                        continue
                    if opcode != 1:
                        continue
                    request = json.loads(data)
                    method = request.get('method')
                    p = request.get('params', {})
                    self.calls.append((method, p))
                    if 'id' not in request:
                        continue
                    if self.disconnect and method == 'turn/start':
                        break
                    if self.disconnect_create and method == 'thread/start':
                        break
                    response = {'id': request['id'], 'result': self.result(method, p)}
                    if self.fragmented and method == 'initialize':
                        raw = json.dumps(response).encode()
                        mid = len(raw) // 2
                        conn.sendall(self.frame(raw[:mid], fin=False) + self.frame(b'check', opcode=9) + self.frame(raw[mid:], opcode=0))
                    else:
                        conn.sendall(self.frame(response))
                    if method == 'thread/settings/update':
                        settings = {k: v for k, v in p.items() if k != 'threadId'}
                        if self.settings_mismatch:
                            settings['model'] = 'wrong-model'
                        conn.sendall(self.frame({'method': 'thread/settings/updated', 'params': {'threadId': p['threadId'], 'threadSettings': settings}}))
                    if method == 'thread/read' and self.busy and self.wake:
                        time.sleep(.15)
                        self.busy = False
                        conn.sendall(self.frame({'method': 'thread/status/changed', 'params': {'threadId': p['threadId'], 'status': {'type': 'idle'}}}))
            except (EOFError, BrokenPipeError, ConnectionResetError, socket.timeout):
                pass
            except Exception as exc:
                self.errors.append(repr(exc))
            finally:
                conn.close()


class CLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            server.close()
            self.assertEqual(server.errors, [])
        self.tmp.cleanup()

    def server(self, **kwargs):
        s = FakeServer(self.root, **kwargs)
        self.servers.append(s)
        return s

    def run_cli(self, s, *args):
        return subprocess.run([CLI, '--socket', s.path, *args], text=True, capture_output=True, timeout=5)

    def team(self, **changes):
        member = {'name': '工程师 α', 'id': None, 'cwd': './work', 'base_prompt': '成员任务'}
        member.update(changes)
        (self.root / 'work').mkdir(exist_ok=True)
        value = {'schema_version': 1, 'team_prompt': '团队任务',
                 'defaults': {'model': 'gpt-6.1-sol', 'reasoning_effort': 'medium', 'approval_policy': None},
                 'sessions': [member]}
        p = self.root / 'team.json'
        p.write_text(json.dumps(value, ensure_ascii=False))
        return p

    def calls(self, s, method):
        return [p for m, p in s.calls if m == method]

    def test_new_unicode_prompt_and_writeback(self):
        s = self.server()
        p = self.team(reasoning_effort='high')
        result = self.run_cli(s, 'set', str(p))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(p.read_text())['sessions'][0]['id'], 'new-id')
        start, = self.calls(s, 'thread/start')
        self.assertEqual(start['cwd'], str(self.root / 'work'))
        self.assertIsNotNone(start.get('approvalPolicy', 'omitted'))
        self.assertEqual(start['model'], 'gpt-6.1-sol')
        turn, = self.calls(s, 'turn/start')
        self.assertEqual(turn['input'][0]['text'], '团队任务\n\n成员任务')
        self.assertEqual(self.calls(s, 'thread/name/set')[0]['name'], '工程师 α')

    def test_existing_resume_config_without_prompt(self):
        s = self.server()
        p = self.team(id='old-id')
        result = self.run_cli(s, 'set', str(p))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(s, 'thread/start'), [])
        self.assertEqual(self.calls(s, 'turn/start'), [])
        self.assertTrue(self.calls(s, 'thread/resume'))
        self.assertEqual(self.calls(s, 'thread/resume')[-1]['threadId'], 'old-id')
        update, = self.calls(s, 'thread/settings/update')
        self.assertEqual(update['threadId'], 'old-id')
        self.assertEqual(update['model'], 'gpt-6.1-sol')
        self.assertEqual(update['cwd'], str(self.root / 'work'))

    def test_prevalidates_all_json_before_mutation(self):
        s = self.server()
        p = self.team()
        bad = self.root / 'bad.json'
        bad.write_text('{')
        result = self.run_cli(s, 'set', str(p), str(bad))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(s, 'thread/start'), [])
        self.assertIsNone(json.loads(p.read_text())['sessions'][0]['id'])

    def test_send_refuses_unloaded(self):
        s = self.server()
        result = self.run_cli(s, 'send', 'outsider', '测试')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(s, 'thread/resume'), [])
        self.assertEqual(self.calls(s, 'turn/start'), [])

    def test_send_busy_waits_for_event(self):
        s = self.server(loaded=['member'], busy=True)
        result = self.run_cli(s, 'send', 'member', '测试 Unicode', '--timeout', '2')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreaterEqual(len(self.calls(s, 'thread/read')), 2)
        self.assertLessEqual(len(self.calls(s, 'thread/read')), 3)
        self.assertEqual(self.calls(s, 'turn/start')[0]['input'][0]['text'], '测试 Unicode')
        self.assertEqual(self.calls(s, 'turn/steer'), [])
        for p in self.calls(s, 'thread/resume'):
            self.assertTrue(p.get('excludeTurns'))
            self.assertNotIn('model', p)

    def test_send_wait_timeout_no_polling(self):
        s = self.server(loaded=['member'], busy=True, wake=False)
        result = self.run_cli(s, 'send', 'member', '测试', '--timeout', '.25')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(s, 'turn/start'), [])
        self.assertLessEqual(len(self.calls(s, 'thread/read')), 2)

    def test_disconnect_does_not_retry_unknown_send(self):
        s = self.server(loaded=['member'], disconnect=True)
        result = self.run_cli(s, 'send', 'member', '测试')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.calls(s, 'turn/start')), 1)

    def test_fragmented_response_with_interleaved_ping(self):
        s = self.server(loaded=['member'], fragmented=True)
        result = self.run_cli(s, 'send', 'member', '测试')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any(m == 'pong' and p == b'check' for m, p in s.calls))

    def test_disconnect_create_unknown_not_repeated(self):
        s = self.server(disconnect_create=True)
        p = self.team()
        result = self.run_cli(s, 'set', str(p))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.calls(s, 'thread/start')), 1)
        self.assertEqual(self.calls(s, 'turn/start'), [])
        self.assertIsNone(json.loads(p.read_text())['sessions'][0]['id'])

    def test_known_created_id_survives_unknown_prompt_result(self):
        s = self.server(disconnect=True)
        p = self.team()
        result = self.run_cli(s, 'set', str(p))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.calls(s, 'thread/start')), 1)
        self.assertEqual(len(self.calls(s, 'turn/start')), 1)
        self.assertEqual(json.loads(p.read_text())['sessions'][0]['id'], 'new-id')

    def test_update_mismatch_never_claims_success_or_sends_prompt(self):
        s = self.server(settings_mismatch=True)
        p = self.team(id='old-id')
        result = self.run_cli(s, 'set', str(p))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(s, 'turn/start'), [])

    def test_creation_journal_reloaded_after_lock_acquired(self):
        loader = importlib.machinery.SourceFileLoader('chatgpt_app_server_test_module', CLI)
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        loader.exec_module(module)
        p = self.team()
        team_file = module.TeamFile(p)
        self.assertEqual(team_file.journal, {})
        journal = {'工程师 α': {'status': 'creating', 'request_id': 'unknown-result'}}
        team_file.journal_path.write_text(json.dumps(journal))
        self.assertNotIn(team_file.journal_path, list(self.root.glob('*.json')))
        with contextlib.ExitStack() as stack:
            team_file.acquire(stack)
            self.assertEqual(team_file.journal, journal)
            self.assertEqual(team_file.journal['工程师 α']['status'], 'creating')

    def test_guardian_reviewer_alias_normalized(self):
        s = self.server(loaded=['old-id'])
        p = self.team(id='old-id', approvals_reviewer='guardian_subagent')
        result = self.run_cli(s, 'set', str(p))
        self.assertEqual(result.returncode, 0, result.stderr)
        update, = self.calls(s, 'thread/settings/update')
        self.assertEqual(update['approvalsReviewer'], 'auto_review')
        self.assertEqual(self.calls(s, 'turn/start'), [])

    def test_ctrl_c_busy_does_not_send(self):
        s = self.server(loaded=['member'], busy=True, wake=False)
        proc = subprocess.Popen([CLI, '--socket', s.path, 'send', 'member', '测试'], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 2
            while not self.calls(s, 'thread/read') and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(self.calls(s, 'thread/read'))
            proc.send_signal(signal.SIGINT)
            stdout, stderr = proc.communicate(timeout=2)
            self.assertNotEqual(proc.returncode, 0, stdout + stderr)
            self.assertEqual(self.calls(s, 'turn/start'), [])
            self.assertEqual(self.calls(s, 'turn/steer'), [])
            self.assertEqual(len(self.calls(s, 'thread/resume')), 1)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


if __name__ == '__main__':
    unittest.main(verbosity=2)
