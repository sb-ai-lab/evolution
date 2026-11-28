"""Neutral health server; the test controls exits through HTTP, never saved PIDs."""
import argparse
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

parser = argparse.ArgumentParser()
parser.add_argument('--port', type=int, required=True)
parser.add_argument('--delay', type=float, default=0)
args = parser.parse_args()
stopped = threading.Event()
signal.signal(signal.SIGTERM, lambda *_: stopped.set())
signal.signal(signal.SIGINT, lambda *_: stopped.set())
# Prove the target's env and secrets reach the probe without publishing their values.
print(json.dumps({'target_env': 'TARGET_MODE' in os.environ,
                  'target_secret': os.environ.get('TARGET_TOKEN') == 'private-neutral-token',
                  'execution_secret': 'EXECUTION_TOKEN' in os.environ,
                  'cwd': str(Path.cwd()), 'executable': sys.executable}), flush=True)
if stopped.wait(args.delay):
    sys.exit(0)

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(204 if self.path in {'/neutral-health', '/exit'} else 503)
        self.end_headers()
        if self.path == '/exit':
            self.wfile.flush()
            os._exit(7)
    def log_message(self, *args):
        pass

class Server(HTTPServer):
    allow_reuse_address = True

server = Server(('127.0.0.1', args.port), Handler)
server.timeout = .05
while not stopped.is_set():
    server.handle_request()
server.server_close()
