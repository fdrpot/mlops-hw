"""The client must distinguish admission rejection from a broken HTTP stream."""
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from loadgen import perform, schedule, summarize


class AccountingTests(unittest.TestCase):
    def test_real_http_completed_rejected_and_truncated_attempts(self):
        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.0'
            def log_message(self,*args):pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path.startswith('/reject'):
                    self.send_response(429);self.end_headers()
                    self.wfile.write(b'{"error":"queue_full","pod":"p1","model_version":"A","backend_mode":"simulated"}')
                    return
                self.send_response(200);self.end_headers()
                frame={'text':'one','pod':'p1','model_version':'A','backend_mode':'simulated','done':False}
                self.wfile.write((json.dumps(frame)+'\n').encode());self.wfile.flush()
                if self.path.startswith('/abort'):
                    self.close_connection=True
                    return
                self.wfile.write(b'{"done":true,"chunks":1}\n');self.wfile.flush()
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            item=schedule(1,1)[0]
            url=f'http://127.0.0.1:{server.server_port}'
            rows=[perform(url+'/'+case,item,time.monotonic(),time.time(),case) for case in ('ok','reject','abort')]
            self.assertEqual([r['outcome'] for r in rows],['completed','rejected','aborted'])
            counts=summarize(rows)
            self.assertEqual(counts['planned'],3)
            self.assertEqual(counts['sent'],3)
            self.assertEqual(counts['completed'],1)
            self.assertEqual(counts['rejected'],1)
            self.assertEqual(counts['aborted'],1)
            self.assertEqual(counts['ttfc_seconds']['n'],1)
            self.assertTrue(all(r['attempt']==1 for r in rows))
            self.assertIsNone(rows[1]['ttfc_seconds'])
            self.assertEqual(rows[2]['chunks'],1)
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_deterministic_open_loop_schedule_and_hot_client_share(self):
        uniform=schedule(120,4,'uniform')
        hot=schedule(120,4,'hot80')
        self.assertEqual(len(hot),480)
        self.assertEqual(sum(row['client']==0 for row in hot),384)
        self.assertEqual(uniform,schedule(120,4,'uniform'))
        self.assertEqual([r['offset_seconds'] for r in uniform],[r['offset_seconds'] for r in hot])
        self.assertEqual([r['prompt'] for r in uniform],[r['prompt'] for r in hot])
        self.assertEqual(len({r['prompt'][:64] for r in hot}),8)
        self.assertGreaterEqual(max(r['offset_seconds'] for r in hot),119)


if __name__=='__main__':unittest.main()
