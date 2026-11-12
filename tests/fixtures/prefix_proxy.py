#!/usr/bin/env python3
import http.server
import socketserver
import sys
import urllib.error
import urllib.parse
import urllib.request

TARGET_PORT = 18090
STATS_KEY = "key-stats-sec"


class PrefixProxyHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Quiet logging
        pass

    def do_GET(self):
        self._proxy("GET")

    def do_POST(self):
        self._proxy("POST")

    def do_DELETE(self):
        self._proxy("DELETE")

    def _proxy(self, method):
        # Map /prefix/* -> :TARGET_PORT/*
        path = self.path
        if path.startswith("/prefix"):
            target_path = path[len("/prefix"):]
            if not target_path.startswith("/"):
                target_path = "/" + target_path
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found: expected /prefix prefix")
            return

        target_url = f"http://127.0.0.1:{TARGET_PORT}{target_path}"

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else None

        req = urllib.request.Request(target_url, data=body, method=method)

        # Copy incoming headers
        for k, v in self.headers.items():
            if k.lower() not in ("host", "content-length", "x-api-key"):
                req.add_header(k, v)

        # Inject stats API key and Cloudflare header to simulate external tunnel traffic
        req.add_header("X-API-Key", STATS_KEY)
        req.add_header("cf-ray", "test-proxy-ray-1234")

        try:
            with urllib.request.urlopen(req) as resp:
                self.send_response(resp.status)
                for hk, hv in resp.getheaders():
                    if hk.lower() not in ("transfer-encoding", "content-length"):
                        self.send_header(hk, hv)
                resp_body = resp.read()
                self.send_header("Content-Length", str(len(resp_body)))
                self.end_headers()
                self.wfile.write(resp_body)
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            for hk, hv in e.headers.items():
                if hk.lower() not in ("transfer-encoding", "content-length"):
                    self.send_header(hk, hv)
            err_body = e.read()
            self.send_header("Content-Length", str(len(err_body)))
            self.end_headers()
            self.wfile.write(err_body)
        except Exception as exc:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(f"Proxy error: {exc}".encode())


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18091
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", port), PrefixProxyHandler) as httpd:
        httpd.serve_forever()
