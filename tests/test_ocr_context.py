"""Exercise OCR requests against a server with the 4096-token default that failed."""

import base64
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import windows_ocr
from PIL import Image


class ContextTests(unittest.TestCase):
    def run_checkpoint_resume(self, override, expected_context):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def respond(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/api/tags":
                    self.respond(200, {"models": [{"name": "glm-ocr:latest"}]})
                else:
                    self.respond(404, {"error": "unexpected endpoint"})

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(payload)
                if payload.get("options", {}).get("num_ctx", 4096) < 4155:
                    self.respond(400, {"error": "request (4155 tokens) exceeds available context size (4096 tokens)"})
                else:
                    self.respond(200, {"response": "Recovered screenshot text", "done": True})

        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary)
            (session / "screenshots").mkdir()
            (session / "visual").mkdir()
            first = session / "screenshots" / "screen-20260914-110000-0000.png"
            second = session / "screenshots" / "screen-20260914-110100-0001.png"
            Image.new("RGB", (32, 32), "white").save(first)
            Image.new("RGB", (32, 32), "black").save(second)
            os.utime(first, (1000, 1000))
            os.utime(second, (2000, 2000))
            prior = {"path": str(first), "ok": True, "status": "complete", "text": "Saved text", "attempts": 1}
            (session / "visual" / "ocr.json").write_text(json.dumps({"results": [prior]}), encoding="utf-8")
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            output = io.StringIO()
            try:
                with (
                    mock.patch.dict(os.environ),
                    mock.patch.object(sys, "argv", ["windows_ocr.py", str(session), "--host", f"http://127.0.0.1:{server.server_port}", "--model", "glm-ocr"]),
                    mock.patch.object(windows_ocr, "prepare_ollama_resources", return_value={}),
                    mock.patch.object(windows_ocr, "unload_model", return_value=True),
                    contextlib.redirect_stdout(output),
                ):
                    os.environ.pop("MEETING_OCR_CONTEXT_LENGTH", None)
                    if override is not None:
                        os.environ["MEETING_OCR_CONTEXT_LENGTH"] = override
                    status = windows_ocr.main()
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

            self.assertEqual(status, 0, output.getvalue())
            self.assertEqual(len(requests), 1, "Completed images must not be sent again")
            self.assertEqual(requests[0]["options"]["num_ctx"], expected_context)
            self.assertEqual(base64.b64decode(requests[0]["images"][0]), second.read_bytes())
            saved = json.loads((session / "visual" / "ocr.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["requested_context_length"], expected_context)
            self.assertEqual(saved["results"][0], prior, "Do not relabel old checkpoints with the new context")
            self.assertEqual(saved["results"][1]["text"], "Recovered screenshot text")
            self.assertEqual(saved["results"][1]["requested_context_length"], expected_context)
            events = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertTrue(any(e.get("requested_context_length") == expected_context and "progress" in e for e in events))

    def test_default_context_overrides_existing_4k_server_and_preserves_checkpoint(self):
        self.run_checkpoint_resume(None, 16384)

    def test_environment_context_reaches_request_and_saved_metadata(self):
        self.run_checkpoint_resume("32768", 32768)

    def test_invalid_context_fails_before_contacting_ollama(self):
        for value in ["0", "-1", "garbage"]:
            with (
                self.subTest(value=value),
                tempfile.TemporaryDirectory() as temporary,
                mock.patch.dict(os.environ, {"MEETING_OCR_CONTEXT_LENGTH": value}),
                mock.patch.object(sys, "argv", ["windows_ocr.py", temporary]),
                mock.patch.object(windows_ocr, "ensure_ollama_ready", return_value={}) as ready,
                mock.patch.object(windows_ocr, "choose_model", return_value="glm-ocr:latest"),
                mock.patch.object(windows_ocr, "prepare_ollama_resources", return_value={}),
                mock.patch.object(windows_ocr, "unload_model", return_value=True),
                contextlib.redirect_stderr(io.StringIO()),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                (Path(temporary) / "screenshots").mkdir()
                with self.assertRaises(SystemExit) as raised:
                    windows_ocr.main()
                self.assertEqual(raised.exception.code, 2)
                ready.assert_not_called()


if __name__ == "__main__":
    unittest.main()
