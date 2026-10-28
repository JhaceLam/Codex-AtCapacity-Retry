"""Exercise the actual retry handler and framing without contacting a model."""
import json
import unittest
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

from wrapper import Retrier, FrameTap, encode_frame


def completed(number, status='failed', info='serverOverloaded'):
    return {'method': 'turn/completed', 'params': {'threadId': 'thread-a', 'turn': {
        'id': str(number), 'status': status, 'error': {'codexErrorInfo': info}}}}


class Tests(unittest.TestCase):
    def test_ten_thousand_consecutive_failures_retry_without_delay_or_limit(self):
        sent, logs, visible = [], [], []
        r = Retrier(sent.append, lambda **k: logs.append(k))
        for i in range(10000):
            r.server(completed(i), lambda: visible.append(i))
            response = {'id': sent[-1]['id'], 'result': {'turn': {'id': str(i + 1)}}}
            r.server(response, lambda: self.fail('Internal response leaked'))
        self.assertEqual(len(sent), 10000)
        self.assertEqual(len(visible), 10000)
        self.assertEqual(logs[-1]['attempt'], 10000)
        self.assertTrue(all(x['artificial_delay_ms'] == 0 for x in logs))
        self.assertEqual(sent[-1]['params'], {'threadId': 'thread-a', 'input': [],
                                           'turnTrigger': 'capacity_retry_automatic'})
        self.assertEqual(r.requests, {})

    def test_stop_success_and_other_errors_do_not_retry(self):
        sent = []
        r = Retrier(sent.append)
        for error in ['usageLimitExceeded', 'unauthorized', 'internalServerError']:
            r.server(completed(error, info=error), lambda: None)
        r.server(completed('success', status='completed'), lambda: None)
        self.assertEqual(sent, [])
        r.client({'method': 'turn/interrupt', 'params': {'threadId': 'thread-a'}})
        r.server(completed('stopped'), lambda: None)
        self.assertEqual(sent, [])
        r.client({'method': 'turn/start', 'params': {'threadId': 'thread-a', 'input': []}})
        r.server(completed('resumed'), lambda: None)
        self.assertEqual(len(sent), 1)
        r.server(completed('resumed'), lambda: None)
        self.assertEqual(len(sent), 1)

    def test_websocket_mask_lengths_chunking_and_internal_reply_filter(self):
        for masked in [False, True]:
            for size in [4, 200, 70000]:
                payload = json.dumps({'value': 'x' * size}).encode()
                frames, decoded = [], []
                t = FrameTap(lambda p, r: (decoded.append(p), frames.append(r)), frames.append)
                header = b'HTTP/1.1 101 Switching Protocols\r\n\r\n'
                frame = encode_frame(payload, masked)
                wire = header + frame
                for i in range(0, len(wire), 17):
                    t.feed(wire[i:i + 17])
                self.assertEqual(decoded, [payload])
                self.assertEqual(b''.join(frames), wire)

    def test_websocket_fragmentation_with_ping(self):
        raw, decoded = [], []
        tap = FrameTap(lambda p, r: (decoded.append(p), raw.append(r)), raw.append)
        tap.feed(b'GET / HTTP/1.1\r\n\r\n')
        tap.feed(b'\x01\x04{"x"' + b'\x89\x00' + b'\x80\x03:1}')
        self.assertEqual(decoded, [b'{"x":1}'])
        self.assertIn(b'\x89\x00', raw)

    def test_stop_during_retry_start_targets_the_new_turn(self):
        sent = []
        r = Retrier(sent.append)
        r.server(completed('old'), lambda: None)
        retry_id = sent[0]['id']
        stop = {'id': 123, 'method': 'turn/interrupt',
                'params': {'threadId': 'thread-a', 'turnId': 'old'}}
        self.assertEqual(r.client(stop), 'defer')
        r.server({'id': retry_id, 'result': {'turn': {'id': 'new'}}}, lambda: None)
        self.assertEqual(sent[-1], {'id': 123, 'method': 'turn/interrupt',
                                   'params': {'threadId': 'thread-a', 'turnId': 'new'}})
        r.server(completed('new'), lambda: None)
        self.assertEqual(len(sent), 2)
        stop2 = {'id': 124, 'method': 'turn/interrupt',
                 'params': {'threadId': 'thread-a', 'turnId': 'old'}}
        r.retry_threads.add('thread-a');r.active['thread-a'] = 'newest'
        self.assertEqual(r.client(stop2), 'rewrite')
        self.assertEqual(stop2['params']['turnId'], 'newest')

    def test_real_subprocess_stdio_and_websocket_transports(self):
        for proxy in [False, True]:
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                shutil.copy2(Path(__file__).with_name('wrapper.py'), root / 'wrapper.py')
                fake = root / 'fake.py'
                fake.write_text('''#!/usr/bin/env python3
import json,sys
from wrapper import encode_frame
proxy = sys.argv[-1] == 'proxy'
def read():
 if not proxy:return json.loads(sys.stdin.buffer.readline())
 a,b=sys.stdin.buffer.read(2);n=b&127
 if n==126:n=int.from_bytes(sys.stdin.buffer.read(2),'big')
 if n==127:n=int.from_bytes(sys.stdin.buffer.read(8),'big')
 mask=sys.stdin.buffer.read(4) if b&128 else None
 p=sys.stdin.buffer.read(n)
 if mask:p=bytes(x^mask[i%4] for i,x in enumerate(p))
 return json.loads(p)
def send(m):
 p=json.dumps(m).encode();sys.stdout.buffer.write(encode_frame(p,False) if proxy else p+b'\\n');sys.stdout.buffer.flush()
if proxy:
 while sys.stdin.buffer.readline()!=b'\\r\\n':pass
 sys.stdout.buffer.write(b'HTTP/1.1 101 Switching Protocols\\r\\n\\r\\n');sys.stdout.buffer.flush()
r=read();send({'id':r['id'],'result':{}})
for i in range(100):
 send({'method':'turn/completed','params':{'threadId':'thread-a','turn':{'id':str(i),'status':'failed','error':{'codexErrorInfo':'serverOverloaded'}}}})
 r=read();assert r['method']=='turn/start' and r['params']['input']==[]
 send({'id':r['id'],'result':{'turn':{'id':str(i+1)}}})
send({'method':'turn/completed','params':{'threadId':'thread-a','turn':{'id':'100','status':'completed','error':None}}})
''')
                fake.chmod(0o700)
                (root / 'config.json').write_text(json.dumps({'real_bin': str(fake),
                    'log_path': str(root / 'events.jsonl')}))
                p = subprocess.Popen([sys.executable, str(root / 'wrapper.py'), 'app-server'] +
                                     (['proxy'] if proxy else []), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                request = json.dumps({'id': 1, 'method': 'initialize', 'params': {}}).encode()
                data = (b'GET / HTTP/1.1\r\n\r\n' + encode_frame(request, True)
                        if proxy else request + b'\n')
                p.stdin.write(data)
                p.stdin.flush()
                with ThreadPoolExecutor(max_workers=2) as readers:
                    output_future = readers.submit(p.stdout.read)
                    error_future = readers.submit(p.stderr.read)
                    try:
                        p.wait(timeout=8)
                        output, err = output_future.result(), error_future.result()
                    finally:
                        if p.poll() is None:
                            p.kill()
                            p.wait()
                        p.stdin.close();p.stdout.close();p.stderr.close()
                self.assertEqual(p.returncode, 0, err)
                if proxy:
                    messages = []
                    tap = FrameTap(lambda body, raw: messages.append(json.loads(body)), lambda raw: None)
                    tap.feed(output)
                else:
                    messages = [json.loads(line) for line in output.splitlines()]
                self.assertEqual(len(messages), 102)
                self.assertEqual(messages[0]['id'], 1)
                self.assertEqual(messages[-1]['params']['turn']['status'], 'completed')
                events = [json.loads(x) for x in (root / 'events.jsonl').read_text().splitlines()]
                retries = [x for x in events if x['event'] == 'retry_dispatched']
                self.assertEqual(len(retries), 100)
                self.assertEqual(retries[-1]['attempt'], 100)


if __name__ == '__main__':
    unittest.main()
