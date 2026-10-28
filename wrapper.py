#!/usr/bin/env python3
"""Transparent Codex CLI transport wrapper. Final capacity failures retry immediately.

Only app-server stdio and app-server proxy transports are intercepted. Other
commands exec the original CLI. There is deliberately no retry delay or limit.
No credentials, prompts, or responses are written to the operational log.
"""
import json
import io
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import threading
import time
import uuid


def encode_frame(payload, masked):
    n = len(payload)
    flag = 128 if masked else 0
    head = bytes([129, flag | n]) if n < 126 else (
        bytes([129, flag | 126]) + struct.pack('!H', n) if n < 65536
        else bytes([129, flag | 127]) + struct.pack('!Q', n))
    if not masked:
        return head + payload
    mask = os.urandom(4)
    return head + mask + bytes(x ^ mask[i % 4] for i, x in enumerate(payload))


class FrameTap:
    """Parse an HTTP-upgraded WebSocket stream without changing its messages."""
    def __init__(self, callback, emit):
        self.callback, self.emit = callback, emit
        self.buffer = bytearray()
        self.upgraded = False
        self.parts, self.raw_parts = [], []

    def feed(self, data):
        self.buffer.extend(data)
        if not self.upgraded:
            pos = self.buffer.find(b'\r\n\r\n')
            if pos < 0:
                return
            pos += 4
            self.emit(bytes(self.buffer[:pos]))
            del self.buffer[:pos]
            self.upgraded = True
        while len(self.buffer) >= 2:
            a, b = self.buffer[:2]
            size, pos = b & 127, 2
            if size in (126, 127):
                count = 2 if size == 126 else 8
                if len(self.buffer) < pos + count:
                    return
                size = int.from_bytes(self.buffer[pos:pos + count], 'big')
                pos += count
            mask = None
            if b & 128:
                if len(self.buffer) < pos + 4:
                    return
                mask = bytes(self.buffer[pos:pos + 4])
                pos += 4
            if len(self.buffer) < pos + size:
                return
            raw = bytes(self.buffer[:pos + size])
            payload = bytes(self.buffer[pos:pos + size])
            del self.buffer[:pos + size]
            if mask:
                payload = bytes(x ^ mask[i % 4] for i, x in enumerate(payload))
            opcode = a & 15
            if opcode >= 8:
                self.emit(raw)
                continue
            self.parts.append(payload)
            self.raw_parts.append(raw)
            if a & 128:
                payload, raw = b''.join(self.parts), b''.join(self.raw_parts)
                self.parts.clear()
                self.raw_parts.clear()
                self.callback(payload, raw)


class Retrier:
    def __init__(self, send, log=None):
        self.send = send
        self.log = log or (lambda **kw: None)
        self.lock = threading.RLock()
        self.paused = set()
        self.last_completed = {}
        self.counts = {}
        self.requests = {}
        self.request_context = {}
        self.generations = {}
        self.retry_threads = set()
        self.active = {}
        self.deferred_interrupts = {}
        self.stopping = False

    def client(self, message):
        with self.lock:
            params = message.get('params') or {}
            tid = params.get('threadId')
            if not tid:
                return 'raw'
            method = message.get('method')
            if method in ('turn/interrupt', 'thread/unsubscribe', 'thread/archive'):
                self.paused.add(tid)
                self.counts.pop(tid, None)
                self.log(event='user_stop', thread_id=tid)
                if method == 'turn/interrupt' and tid in self.retry_threads:
                    if tid in self.active:
                        message['params'] = {**params, 'turnId': self.active[tid]}
                        return 'rewrite'
                    if tid in self.requests.values():
                        self.deferred_interrupts.setdefault(tid, []).append(message)
                        return 'defer'
            elif method == 'turn/start':
                self.generations[tid] = self.generations.get(tid, 0) + 1
                self.paused.discard(tid)
                self.counts.pop(tid, None)
                self.retry_threads.discard(tid)
                self.active.pop(tid, None)
            return 'raw'

    def started(self, tid, turn_id):
        if not tid or not turn_id or self.last_completed.get(tid) == turn_id:
            return
        self.active[tid] = turn_id
        if tid not in self.retry_threads:
            return
        for request in self.deferred_interrupts.pop(tid, []):
            request['params'] = {**request['params'], 'turnId': turn_id}
            self.send(request)

    def dispatch(self, tid, failed_turn_id):
        """One request at a time, with no timer, backoff, or attempt ceiling."""
        if self.stopping or tid in self.paused:
            return
        self.counts[tid] = self.counts.get(tid, 0) + 1
        rid = '__capacity_retry_' + uuid.uuid4().hex
        self.requests[rid] = tid
        self.request_context[rid] = (failed_turn_id, self.generations.get(tid, 0))
        self.retry_threads.add(tid)
        request = {'id': rid, 'method': 'turn/start', 'params': {
            'threadId': tid, 'input': [], 'turnTrigger': 'capacity_retry_automatic'}}
        # Omitted model/permissions/mode inherit the existing thread settings.
        before = time.monotonic_ns()
        self.send(request)
        self.log(event='retry_dispatched', thread_id=tid, failed_turn_id=failed_turn_id,
                 attempt=self.counts[tid], artificial_delay_ms=0,
                 write_us=(time.monotonic_ns() - before) // 1000)

    @staticmethod
    def rejection_reason(error):
        # Record only a fixed label: RPC error text can contain private context.
        message = str(error.get('message', '')).lower()
        if error.get('code') == -32603 and 'emptyinput' in message:
            return 'turn_transition'
        data = error.get('data')
        if (isinstance(data, dict) and data.get('codexErrorInfo') == 'serverOverloaded') or (
                message.strip() == 'selected model is at capacity. please try a different model.'):
            return 'capacity'
        return 'other'

    def server(self, message, forward):
        """Forward client-visible events before immediately dispatching the retry."""
        with self.lock:
            request_id = message.get('id')
            if request_id in self.requests:
                tid = self.requests.pop(request_id)
                failed_turn_id, generation = self.request_context.pop(request_id)
                current = generation == self.generations.get(tid, 0) and tid in self.retry_threads
                if 'error' in message:
                    error = message.get('error') or {}
                    reason = self.rejection_reason(error)
                    self.log(event='retry_request_rejected', thread_id=tid,
                             rpc_code=error.get('code'), reason=reason)
                    if current and tid not in self.paused and not self.stopping:
                        if self.active.get(tid) and self.active[tid] != failed_turn_id:
                            self.log(event='retry_already_running', thread_id=tid,
                                     turn_id=self.active[tid])
                            return
                        if reason in ('turn_transition', 'capacity'):
                            self.dispatch(tid, failed_turn_id)
                            return
                        self.retry_threads.discard(tid)
                    for request in self.deferred_interrupts.pop(tid, []):
                        # The retry was rejected, so there is no new turn to stop.
                        self.send({**request, 'params': {**request['params'],
                                   'turnId': self.last_completed.get(tid, request['params']['turnId'])}})
                else:
                    turn_id = ((message.get('result') or {}).get('turn') or {}).get('id')
                    if current:
                        self.started(tid, turn_id)
                    self.log(event='retry_accepted', thread_id=tid, turn_id=turn_id)
                return
            forward()
            if message.get('method') == 'turn/started':
                p = message.get('params') or {}
                self.started(p.get('threadId'), (p.get('turn') or {}).get('id'))
            if self.stopping or message.get('method') != 'turn/completed':
                return
            params = message.get('params') or {}
            tid, turn = params.get('threadId'), params.get('turn') or {}
            turn_id = turn.get('id')
            if not tid or not turn_id or self.last_completed.get(tid) == turn_id:
                return
            self.last_completed[tid] = turn_id
            if self.active.get(tid) == turn_id:
                self.active.pop(tid, None)
            error = turn.get('error') or {}
            capacity = error.get('codexErrorInfo') == 'serverOverloaded' or (
                error.get('message', '').strip() ==
                'Selected model is at capacity. Please try a different model.')
            if turn.get('status') != 'failed' or not capacity or tid in self.paused:
                self.counts.pop(tid, None)
                self.retry_threads.discard(tid)
                return
            if self.active.get(tid):
                self.retry_threads.add(tid)
                self.log(event='retry_already_running', thread_id=tid, turn_id=self.active[tid])
            elif tid not in self.requests.values():
                self.dispatch(tid, turn_id)


def main():
    config_path = Path(__file__).resolve().with_name('config.json')
    config = json.loads(config_path.read_text())
    candidates = [config['real_bin']] + config.get('real_candidates', [])
    real = next((p for p in candidates if Path(p).is_file() and os.access(p, os.X_OK)), None)
    if real is None:
        raise RuntimeError('Codex CLI moved; run codex-capacity-patch apply --all to repair the patch')
    args = sys.argv[1:]
    if 'app-server' not in args:
        os.execv(real, [real] + args)
    index = args.index('app-server')
    rest = args[index + 1:]
    proxy = rest == ['proxy']
    is_server = not rest or rest[0].startswith('-')
    stdio = is_server and not any(
        (v == '--listen' and i + 1 < len(rest) and rest[i + 1] != 'stdio://') or
        (v.startswith('--listen=') and v != '--listen=stdio://') for i, v in enumerate(rest))
    if not proxy and not stdio:
        os.execv(real, [real] + args)

    log_path = Path(config['log_path']).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_lock = threading.Lock()

    def log(**fields):
        fields.update(at=time.time(), pid=os.getpid(), transport='websocket' if proxy else 'stdio',
                      patch_version=config.get('patch_version'))
        with log_lock:
            if log_path.exists() and log_path.stat().st_size > 4 * 1024 * 1024:
                os.replace(log_path, log_path.with_suffix('.previous.jsonl'))
            with log_path.open('a') as stream:
                stream.write(json.dumps(fields, separators=(',', ':')) + '\n')

    child = subprocess.Popen([real] + args, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=None, bufsize=0)
    child_write_lock, stdout_lock = threading.Lock(), threading.Lock()

    def write(stream, data, lock):
        with lock:
            view = memoryview(data)
            while view:
                n = os.write(stream.fileno(), view)
                view = view[n:]

    def to_child(data):
        write(child.stdin, data, child_write_lock)

    def to_app(data):
        write(sys.stdout.buffer, data, stdout_lock)

    def send(request):
        payload = json.dumps(request, separators=(',', ':')).encode()
        to_child(encode_frame(payload, True) if proxy else payload + b'\n')

    retrier = Retrier(send, log)

    def from_client(payload, raw):
        try:
            message = json.loads(payload)
        except (ValueError, UnicodeError):
            to_child(raw)
            return
        with retrier.lock:
            mode = retrier.client(message)
            if mode == 'rewrite':
                send(message)
            elif mode != 'defer':
                to_child(raw)

    def from_server(payload, raw):
        try:
            message = json.loads(payload)
        except (ValueError, UnicodeError):
            to_app(raw)
            return
        retrier.server(message, lambda: to_app(raw))

    def pump_input():
        try:
            if proxy:
                tap = FrameTap(from_client, to_child)
                while data := os.read(sys.stdin.fileno(), 65536):
                    tap.feed(data)
            else:
                pending = bytearray()
                while data := os.read(sys.stdin.fileno(), 65536):
                    pending.extend(data)
                    while (end := pending.find(b'\n')) >= 0:
                        line = bytes(pending[:end + 1])
                        del pending[:end + 1]
                        from_client(line, line)
                if pending:
                    from_client(bytes(pending), bytes(pending))
        except (BrokenPipeError, OSError):
            pass
        finally:
            retrier.stopping = True
            try:
                child.stdin.close()
            except OSError:
                pass

    def shutdown(signum, frame):
        retrier.stopping = True
        if child.poll() is None:
            child.send_signal(signum)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    log(event='connected', artificial_delay_ms=0, retry_limit=None)
    threading.Thread(target=pump_input, daemon=True).start()
    try:
        if proxy:
            tap = FrameTap(from_server, to_app)
            while data := os.read(child.stdout.fileno(), 65536):
                tap.feed(data)
        else:
            for line in io.BufferedReader(child.stdout):
                from_server(line, line)
    except (BrokenPipeError, OSError):
        retrier.stopping = True
        if child.poll() is None:
            child.terminate()
    return child.wait()


if __name__ == '__main__':
    raise SystemExit(main())
