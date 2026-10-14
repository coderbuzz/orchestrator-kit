import http.server
import threading

import pytest

from orchestrator_kit.verify import _http_ok


@pytest.mark.parametrize("code,ok", [(200, True), (403, True), (429, True), (404, False), (500, False)])
def test_http_ok_status_handling(code, ok):
    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers["User-Agent"])
            self.send_response(code)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    r = _http_ok("url", f"http://127.0.0.1:{srv.server_port}/", {}, None, set())
    assert (r is None) == ok and "Python-urllib" not in seen[0]
    if not ok:
        assert str(code) in r, "the status code is in the note"
