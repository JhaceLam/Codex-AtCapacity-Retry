"""Exercise turn completion/retry timing on an isolated, unauthenticated CLI.

The test shim classifies terminal errors as capacity to exercise the handler.
It never connects to the user's daemon, loads user credentials, or runs tools.
"""
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def probe(real, wrapper_source):
    namespace = {'__name__': 'capacity_retry_probe'}
    exec(compile(wrapper_source, 'wrapper.py', 'exec'), namespace)
    with tempfile.TemporaryDirectory(prefix='capacity-retry-real-') as folder:
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length', '0')))
                body = json.dumps({'error': {'type': 'server_error',
                    'code': 'model_capacity_exceeded',
                    'message': 'Selected model is at capacity. Please try a different model.'}}).encode()
                self.send_response(503)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers();self.wfile.write(body)
            def log_message(self, *args):
                pass
        http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        (Path(folder)/'config.toml').write_text('''model = "gpt-6.1-sol"
model_provider = "retry_probe"
[model_providers.retry_probe]
name = "Offline capacity probe"
base_url = "http://127.0.0.1:%s/v1"
env_key = "CODEX_RETRY_PROBE_API_KEY"
wire_api = "responses"
request_max_retries = 0
stream_max_retries = 0
''' % http.server_port)
        env = dict(os.environ, CODEX_HOME=folder)
        env['CODEX_RETRY_PROBE_API_KEY'] = 'offline-test-only'
        for name in ['OPENAI_API_KEY', 'CODEX_API_KEY', 'OPENAI_BASE_URL', 'CODEX_AUTH_JSON']:
            env.pop(name, None)
        with tempfile.TemporaryFile() as err:
            proc = subprocess.Popen([real, 'app-server'], env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err,
                start_new_session=True, bufsize=0)
            pending, messages, records = bytearray(), [], []
            selector = selectors.DefaultSelector()
            selector.register(proc.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + 20

            def send(message):
                proc.stdin.write(json.dumps(message).encode() + b'\n')
                proc.stdin.flush()

            def receive():
                while time.monotonic() < deadline:
                    end = pending.find(b'\n')
                    if end >= 0:
                        raw = bytes(pending[:end]);del pending[:end + 1]
                        return json.loads(raw)
                    if selector.select(0.5):
                        chunk = os.read(proc.stdout.fileno(), 65536)
                        if not chunk:
                            raise RuntimeError('CLI exited before completing test')
                        pending.extend(chunk)
                raise TimeoutError('probe deadline')

            def rpc(rid, method, params):
                send({'id': rid, 'method': method, 'params': params})
                while True:
                    msg = receive()
                    if msg.get('id') == rid:
                        if 'error' in msg:raise RuntimeError(msg['error'])
                        return msg['result']

            try:
                rpc(1, 'initialize', {'clientInfo': {'name': 'capacity_retry_offline_test',
                    'version': '1'}, 'capabilities': {'experimentalApi': True}})
                send({'method': 'initialized'})
                tid = rpc(2, 'thread/start', {'ephemeral': True, 'cwd': folder,
                    'approvalPolicy': 'never', 'sandbox': 'read-only'})['thread']['id']
                handlers = [namespace['Retrier'](send,
                    lambda handler=i, **k: records.append(dict(handler=handler, **k)))
                    for i in range(2)]
                send({'id': 3, 'method': 'turn/start', 'params': {'threadId': tid,
                    'input': [{'type': 'text', 'text': 'Reply OK only. Do not use tools.'}]}})
                completed = 0
                while completed < 5:
                    msg = receive()
                    if msg.get('method') == 'turn/completed':
                        completed += 1
                        turn = msg['params']['turn']
                        messages.append({'actual_terminal_status': turn['status'],
                                         'actual_error': (turn.get('error') or {}).get('codexErrorInfo')})
                        if completed == 5:
                            for handler in handlers:handler.stopping = True
                        else:
                            turn['status'] = 'failed'
                            turn['error'] = {'codexErrorInfo': 'serverOverloaded'}
                    for handler in handlers:handler.server(msg, lambda: None)
                return {'ok': not any(tid in r.paused for r in handlers),
                    'terminal_events': completed,
                    'actual_errors': messages,
                    'events': records, 'pending_retry_requests': sum(len(r.requests) for r in handlers),
                    'stalled_handlers': [i for i,r in enumerate(handlers) if tid in r.paused]}
            finally:
                http.shutdown();http.server_close()
                selector.close()
                proc.stdin.close()
                try:proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL);proc.wait()
                proc.stdout.close()


class NativeBackendTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('CODEX_CAPACITY_TEST_CLI'),
                         'Set CODEX_CAPACITY_TEST_CLI to an original CLI for the offline integration test')
    def test_two_clients_continue_after_real_empty_input_rejection(self):
        result = probe(os.environ['CODEX_CAPACITY_TEST_CLI'],
                       Path(__file__).with_name('wrapper.py').read_text())
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['terminal_events'], 5)
        self.assertEqual(result['pending_retry_requests'], 0)
        self.assertTrue(any(x['event'] == 'retry_request_rejected'
                            and x.get('reason') == 'turn_transition' for x in result['events']), result)
        self.assertTrue(any(x['event'] == 'retry_already_running' for x in result['events']), result)


if __name__ == '__main__':
    unittest.main()
