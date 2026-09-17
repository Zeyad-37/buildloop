import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

import support  # noqa: F401

from buildloop import ask, claude_cli, config, db
from buildloop.server import BuildLoopServer


class ServerTestCase(unittest.TestCase):
    """A real server on an ephemeral port, with Claude replaced by a fake."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        self.cfg = config.parse({"project": [{"name": "p", "github_repo": "o/r"}]}, home)
        db.connect(self.cfg.db_path).close()
        self.calls = []
        self.server = BuildLoopServer(self.cfg, 0, answerer=self.fake_answer, token="secret-token", quiet=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def fake_answer(self, conn, project, question, history):
        question, history = ask.validate(question, history)
        self.calls.append((project, question, history))
        if question == "boom":
            raise claude_cli.ClaudeUnavailable("claude timed out after 240s")
        return f"answer to: {question}"

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        base = {"Host": f"127.0.0.1:{self.server.port}"}
        base.update(headers or {})
        data = json.dumps(body).encode() if isinstance(body, dict) else body
        conn.request(method, path, body=data, headers=base)
        res = conn.getresponse()
        payload = res.read()
        conn.close()
        return res.status, res.getheader("Content-Type") or "", payload

    def ask(self, body=None, **header_overrides):
        headers = {"Content-Type": "application/json", "X-Buildloop-Token": "secret-token"}
        headers.update(header_overrides)
        headers = {k: v for k, v in headers.items() if v is not None}
        status, _, payload = self.request("POST", "/api/ask",
                                          body or {"project": "p", "question": "why?"}, headers)
        return status, json.loads(payload or b"{}")


class TestAsk(ServerTestCase):
    def test_a_well_formed_question_is_answered(self):
        status, body = self.ask()
        self.assertEqual(status, 200)
        self.assertEqual(body["answer"], "answer to: why?")

    def test_history_is_passed_through(self):
        turn = {"question": "a", "answer": "b"}
        self.ask({"project": "p", "question": "and then?", "history": [turn]})
        self.assertEqual(self.calls[-1][2], [turn])

    def test_claude_failure_is_a_503_with_a_readable_message(self):
        status, body = self.ask({"project": "p", "question": "boom"})
        self.assertEqual(status, 503)
        self.assertIn("timed out", body["error"])

    def test_empty_question_is_a_400(self):
        status, body = self.ask({"project": "p", "question": "  "})
        self.assertEqual(status, 400)
        self.assertIn("question", body["error"].lower())

    def test_unknown_project_is_a_404(self):
        status, _ = self.ask({"project": "someone-else", "question": "x"})
        self.assertEqual(status, 404)


class TestRequestTrust(ServerTestCase):
    """Every model call costs money and time, so only this server's pages may ask."""

    def test_missing_token_is_rejected(self):
        status, _ = self.ask(**{"X-Buildloop-Token": None})
        self.assertEqual(status, 403)
        self.assertEqual(self.calls, [])

    def test_wrong_token_is_rejected(self):
        status, _ = self.ask(**{"X-Buildloop-Token": "guess"})
        self.assertEqual(status, 403)
        self.assertEqual(self.calls, [])

    def test_non_json_content_type_is_rejected(self):
        # text/plain is what a cross-origin page can send without a preflight.
        status, _ = self.ask(**{"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        self.assertEqual(self.calls, [])

    def test_foreign_host_header_is_rejected(self):
        # DNS rebinding: evil.example re-resolved to 127.0.0.1.
        status, _ = self.ask(Host=f"evil.example:{self.server.port}")
        self.assertEqual(status, 403)
        self.assertEqual(self.calls, [])

    def test_foreign_origin_is_rejected(self):
        status, _ = self.ask(Origin="https://evil.example")
        self.assertEqual(status, 403)
        self.assertEqual(self.calls, [])

    def test_same_origin_is_accepted(self):
        status, _ = self.ask(Origin=f"http://127.0.0.1:{self.server.port}")
        self.assertEqual(status, 200)

    def test_oversized_body_is_rejected_before_parsing(self):
        huge = {"project": "p", "question": "x" * 40_000}
        status, _ = self.ask(huge)
        self.assertEqual(status, 413)
        self.assertEqual(self.calls, [])

    def test_no_cors_headers_are_ever_sent(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        conn.request("POST", "/api/ask", body=b"{}", headers={
            "Host": f"127.0.0.1:{self.server.port}", "Content-Type": "application/json",
            "X-Buildloop-Token": "secret-token",
        })
        res = conn.getresponse()
        res.read()
        conn.close()
        self.assertIsNone(res.getheader("Access-Control-Allow-Origin"))


class TestPages(ServerTestCase):
    def test_single_project_root_redirects_to_it(self):
        status, _, _ = self.request("GET", "/")
        self.assertEqual(status, 302)

    def test_served_page_carries_the_token(self):
        status, ctype, body = self.request("GET", "/p/p")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        if claude_cli.available():
            self.assertIn(b"secret-token", body)

    def test_get_with_foreign_host_is_rejected(self):
        # Otherwise a rebinding page could read the token straight out of the HTML.
        status, _, _ = self.request("GET", "/p/p", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)

    def test_unknown_project_page_is_a_404(self):
        status, _, _ = self.request("GET", "/p/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
