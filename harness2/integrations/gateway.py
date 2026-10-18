"""A serial, fail-closed Anthropic transport for the synthetic hybrid pilot.

Pricing snapshot: https://platform.claude.com/docs/en/about-claude/pricing,
2026-10-18. Local prices are estimates, not an invoice reconciliation.
"""
from __future__ import annotations

import hashlib
import fcntl
import hmac
import http.server
import json
import math
import os
import secrets
import socket
import socketserver
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

MODEL = 'claude-haiku-5-5'
MAX_BODY = 90_000
MAX_OUTPUT = 4096
MAX_RESPONSE = 16 * 1024 * 1024


class Refused(ValueError):
    pass


def number(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise Refused('invalid numeric value')
    if not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise Refused('invalid numeric value')
    return float(value)


class Ledger:
    def __init__(self, path: Path, *, total_cap=5.0, session_cap=0.25):
        self.path = Path(path)
        self.total_cap = number(total_cap, positive=True)
        self.session_cap = number(session_cap, positive=True)
        self.lock = threading.RLock()
        self.pending = {}
        self.done = {}
        self.requests = set()
        self.sessions = {}
        self.spent = 0.0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(str(self.path) + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.close()
            raise Refused('experiment admission already owned') from exc
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                self._apply(json.loads(line))
        if self.pending or self.spent > self.total_cap or any(v > self.session_cap for v in self.sessions.values()):
            raise Refused('ledger needs reconciliation or exceeds selected limits')

    def _apply(self, row):
        rid = row['reservation']
        if row['kind'] == 'reserve':
            if self.pending or rid in self.done or row['request'] in self.requests:
                raise Refused('repeated or concurrent reservation')
            number(row['reserved_usd'], positive=True)
            self.pending[rid] = row
            self.requests.add(row['request'])
        elif row['kind'] == 'settle':
            held = self.pending.pop(rid)
            usd = number(row['receipt']['usd'])
            if usd > held['reserved_usd'] + 1e-12:
                raise Refused('settlement exceeds reservation')
            self.spent += usd
            session = held['session']
            self.sessions[session] = self.sessions.get(session, 0.0) + usd
            self.done[rid] = row['receipt']
        else:
            raise Refused('unknown receipt record')

    def _append(self, row):
        row = {**row, 'at': time.time()}
        data = (json.dumps(row, sort_keys=True, allow_nan=False) + '\n').encode()
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            written = os.write(fd, data)
            if written != len(data):
                raise OSError('short ledger write')
            os.fsync(fd)
        finally:
            os.close(fd)
        self._apply(row)

    def reserve(self, session, request, upper):
        upper = number(upper, positive=True)
        with self.lock:
            if self.pending or request in self.requests:
                raise Refused('prior call unresolved or request identity reused')
            if self.spent + upper > self.total_cap + 1e-12:
                raise Refused('experiment cap reached before transmission')
            if self.sessions.get(session, 0) + upper > self.session_cap + 1e-12:
                raise Refused('session cap reached before transmission')
            rid = secrets.token_hex(16)
            self._append({'kind': 'reserve', 'reservation': rid, 'session': session,
                          'request': request, 'reserved_usd': upper})
            return rid

    def settle(self, reservation, receipt):
        usd = number(receipt.get('usd'))
        if not isinstance(receipt.get('request_id'), str) or not receipt['request_id']:
            raise Refused('provider receipt missing identity')
        with self.lock:
            if reservation in self.done:
                if self.done[reservation] != receipt:
                    raise Refused('receipt identity drift')
                return
            if reservation not in self.pending or usd > self.pending[reservation]['reserved_usd'] + 1e-12:
                raise Refused('missing reservation or receipt exceeds held cost')
            if any(old['request_id'] == receipt['request_id'] for old in self.done.values()):
                raise Refused('provider receipt already accounted under another reservation')
            self._append({'kind': 'settle', 'reservation': reservation, 'receipt': receipt})

    def close(self):
        if getattr(self, '_fd', None) is not None:
            os.close(self._fd)
            self._fd = None

    def __del__(self):
        self.close()


def usage_cost(usage):
    for key in ('input_tokens', 'output_tokens'):
        if type(usage.get(key)) is not int or usage[key] < 0:
            raise Refused('usage missing or malformed')
    for key in ('cache_creation_input_tokens', 'cache_read_input_tokens'):
        if type(usage.get(key, 0)) is not int or usage.get(key, 0) < 0:
            raise Refused('cache usage malformed')
    inputs = usage['input_tokens']
    reads = usage.get('cache_read_input_tokens', 0)
    writes = usage.get('cache_creation_input_tokens', 0)
    long = inputs + reads + writes > 100_000
    one_hour = (usage.get('cache_creation') or {}).get('ephemeral_1h_input_tokens', 0)
    if type(one_hour) is not int or not 0 <= one_hour <= writes:
        raise Refused('invalid cache duration usage')
    ir, outr, rr, wr, hr = (0.5, 2.5, 0.05, 0.625, 1.0) if long else (0.1, 0.5, 0.01, 0.125, 0.2)
    return (inputs * ir + usage['output_tokens'] * outr + reads * rr +
            (writes - one_hour) * wr + one_hour * hr) / 1_000_000


def validate_request(body):
    allowed = {'model', 'max_tokens', 'messages', 'system', 'tools', 'tool_choice', 'stream',
               'temperature', 'top_p', 'top_k', 'stop_sequences', 'metadata', 'cache_control'}
    if not isinstance(body, dict) or set(body) - allowed or body.get('model') != MODEL:
        raise Refused('unsupported request shape or model')
    output = body.get('max_tokens')
    if type(output) is not int or not 1 <= output <= MAX_OUTPUT:
        raise Refused('output cap exceeds admitted request')
    if not isinstance(body.get('messages'), list) or not body['messages']:
        raise Refused('no messages')
    raw = json.dumps(body, ensure_ascii=True, allow_nan=False).encode()
    if len(raw) > MAX_BODY:
        raise Refused('request size guard before transmission')

    def content(value, *, assistant=False):
        if isinstance(value, str):
            return
        if not isinstance(value, list):
            raise Refused('unsupported content')
        for block in value:
            kinds = {'text', 'tool_use', 'tool_result'} | ({'thinking', 'redacted_thinking'} if assistant else set())
            if not isinstance(block, dict) or block.get('type') not in kinds:
                raise Refused('non-text or server-side feature excluded')
            if block['type'] == 'thinking' and any(not isinstance(block.get(k), str) for k in ('thinking', 'signature')):
                raise Refused('malformed signed thinking replay')
            if block['type'] == 'redacted_thinking' and not isinstance(block.get('data'), str):
                raise Refused('malformed redacted thinking replay')
            if block['type'] == 'tool_result':
                content(block.get('content', ''))

    for message in body['messages']:
        if not isinstance(message, dict) or message.get('role') not in {'user', 'assistant'}:
            raise Refused('invalid message')
        content(message.get('content'), assistant=message['role'] == 'assistant')
    if 'system' in body:
        content(body['system'])
    for tool in body.get('tools', []):
        if tool.get('type', 'custom') != 'custom' or not isinstance(tool.get('input_schema'), dict):
            raise Refused('server-side tools excluded')
    # Reserve above the most expensive Haiku input/cache tier, plus fixed overhead.
    # Full raw-byte cap bounds even poor tokenization; actual receipts select the tier.
    return ((len(raw) + 8192) * 1.0 + output * 2.5) / 1_000_000


def response_usage(raw, *, streaming):
    if not streaming:
        payload = json.loads(raw)
        if not payload.get('stop_reason'):
            raise Refused('response lacks terminal reason')
        usage_cost(payload.get('usage', {}))
        return payload
    message = {}
    usage = {}
    stopped = False
    reason = None
    for line in raw.splitlines():
        if not line.startswith(b'data: '):
            continue
        event = json.loads(line[6:])
        if event.get('type') == 'error':
            raise Refused('provider stream error; reservation retained')
        if event.get('type') == 'message_start':
            if message:
                raise Refused('duplicate stream start')
            message = event['message']
            usage.update(message.get('usage') or {})
        if event.get('type') == 'message_delta':
            usage.update(event.get('usage') or {})
            reason = (event.get('delta') or {}).get('stop_reason') or reason
        if event.get('type') == 'message_stop':
            stopped = True
    if not message or not stopped or not reason:
        raise Refused('incomplete provider stream; reservation retained')
    usage_cost(usage)
    return {**message, 'usage': usage, 'stop_reason': reason}


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    address_family = socket.AF_UNIX
    daemon_threads = True


class Gateway:
    def __init__(self, root: Path, *, key=None, total_cap=5.0, session_cap=0.25, transport=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if (self.root / 'STOPPED.json').exists():
            raise Refused('experiment halt persists; inspect prior evidence')
        self.ledger = Ledger(self.root / 'receipts.jsonl', total_cap=total_cap, session_cap=session_cap)
        self.key = key
        self.transport = transport or self._forward
        self.tokens = {}
        self.tokens_lock = threading.RLock()
        self.closing = False
        self.serial = threading.Lock()
        self.socket_path = self.root / 'api.sock'
        if self.socket_path.exists():
            raise Refused('prior socket exists; choose a new experiment root')
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                token = self.headers.get('x-api-key') or self.headers.get('authorization', '').removeprefix('Bearer ')
                session = owner.session_for(token)
                if session is None or self.path.split('?')[0] != '/v1/messages':
                    self.send_error(403, 'not admitted')
                    return
                try:
                    length = int(self.headers.get('content-length', '-1'))
                    if not 0 < length <= MAX_BODY:
                        raise Refused('request bytes exceed cap')
                    body = json.loads(self.rfile.read(length))
                    upper = validate_request(body)
                    with owner.serial:
                        if owner.closing or owner.session_for(token) != session:
                            raise Refused('attempt credential revoked before admission')
                        if getattr(owner, 'halted', False):
                            raise Refused('experiment halted after failed verification')
                        rid = owner.ledger.reserve(session, secrets.token_hex(16), upper)
                        status, headers, raw = owner.transport(body, self.headers)
                        if status != 200:
                            raise Refused('provider rejected request; reservation retained')
                        receipt = response_usage(raw, streaming=bool(body.get('stream')))
                        if receipt.get('model') != MODEL:
                            raise Refused('actual provider model differs from admission')
                        owner.ledger.settle(rid, {
                            'request_id': headers.get('request-id') or receipt.get('id'),
                            'message_id': receipt.get('id'), 'model': receipt['model'],
                            'usage': receipt['usage'], 'usd': usage_cost(receipt['usage']),
                            'stop_reason': receipt['stop_reason'],
                            'request_sha256': hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest(),
                            'response_sha256': hashlib.sha256(raw).hexdigest(),
                        })
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream' if body.get('stream') else 'application/json')
                    self.send_header('Content-Length', str(len(raw)))
                    if headers.get('request-id'):
                        self.send_header('request-id', headers['request-id'])
                    self.end_headers()
                    self.wfile.write(raw)
                except Exception as exc:
                    with (owner.root / 'refusals.jsonl').open('a') as log:
                        log.write(json.dumps({'session': session,
                            'reason': str(exc) if isinstance(exc, Refused) else type(exc).__name__,
                            'pending_reservations': len(owner.ledger.pending)}) + '\n')
                    # Do not print exception text: provider exceptions can contain auth/request data.
                    self.send_error(503, 'admission or provider evidence refused; inspect receipt state')

        self.server = _Server(str(self.socket_path), Handler)
        os.chmod(self.socket_path, 0o600)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def token(self, session):
        with self.tokens_lock:
            if self.closing or getattr(self, 'halted', False):
                raise Refused('experiment admission closed')
            token = secrets.token_urlsafe(32)
            self.tokens[token] = session
            return token

    def session_for(self, token):
        with self.tokens_lock:
            return self.tokens.get(token)

    def revoke(self, token):
        with self.tokens_lock:
            self.tokens.pop(token, None)
        # The credential is invalid immediately; wait for already-admitted transport.
        with self.serial:
            pass

    def halt(self, reason):
        with self.serial:
            self.halted = True
            path = self.root / 'STOPPED.json'
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600) if not path.exists() else None
            if fd is not None:
                try:
                    os.write(fd, json.dumps({'reason': reason, 'at': time.time()}).encode())
                    os.fsync(fd)
                finally:
                    os.close(fd)
                directory = os.open(self.root, os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)

    def _forward(self, body, headers):
        if not self.key:
            raise Refused('no provider credential')
        safe_headers = {'x-api-key': self.key, 'anthropic-version': '2023-06-01', 'Content-Type': 'application/json'}
        if headers.get('anthropic-beta'):
            safe_headers['anthropic-beta'] = headers['anthropic-beta']
        request = urllib.request.Request('https://api.anthropic.com/v1/messages',
                                         data=json.dumps(body).encode(), headers=safe_headers)
        # No environment proxies, redirects to alternate providers, or automatic retries.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=60) as response:
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise Refused('provider response exceeds evidence limit')
            return response.status, {k.lower(): v for k, v in response.headers.items()}, raw

    def close(self):
        with self.tokens_lock:
            self.closing = True
            self.tokens.clear()
        # Drain any transport before revocation/ledger ownership is released.
        with self.serial:
            pass
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.socket_path.unlink(missing_ok=True)
        self.ledger.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
