"""Runs inside a fresh network namespace; forwards only to a mounted Unix socket."""
import argparse
import http.client
import http.server
import os
import socket
import subprocess
import threading


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--socket', required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()

    class Connection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(75)
            self.sock.connect(args.socket)

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            length = int(self.headers.get('content-length', '-1'))
            if not 0 < length <= 90_000 or self.path.split('?')[0] != '/v1/messages':
                self.send_error(403, 'unsupported path or size')
                return
            conn = Connection('localhost')
            try:
                conn.request('POST', self.path, body=self.rfile.read(length), headers={
                    key: value for key, value in self.headers.items()
                    if key.lower() in {'x-api-key', 'authorization', 'anthropic-beta', 'content-type'}
                })
                response = conn.getresponse()
                body = response.read(16 * 1024 * 1024 + 1)
                self.send_response(response.status)
                self.send_header('Content-Type', response.getheader('Content-Type', 'application/json'))
                self.send_header('Content-Length', str(len(body)))
                request_id = response.getheader('request-id')
                if request_id:
                    self.send_header('request-id', request_id)
                self.end_headers()
                self.wfile.write(body)
            finally:
                conn.close()

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = dict(os.environ)
    env['ANTHROPIC_BASE_URL'] = f'http://127.0.0.1:{server.server_port}'
    if 'OPENCODE_CONFIG_CONTENT' in env:
        env['OPENCODE_CONFIG_CONTENT'] = env['OPENCODE_CONFIG_CONTENT'].replace(
            '__GATEWAY__', env['ANTHROPIC_BASE_URL'])
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    result = subprocess.run(command, env=env, stdin=subprocess.DEVNULL)
    server.shutdown()
    server.server_close()
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
