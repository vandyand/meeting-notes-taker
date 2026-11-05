import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import process_latest  # noqa: E402
import windows_capture_ui  # noqa: E402
import windows_ocr  # noqa: E402


class OcrCheckpointTests(unittest.TestCase):
    def make_session(self, results):
        temporary = tempfile.TemporaryDirectory()
        session = Path(temporary.name)
        (session / "visual").mkdir()
        (session / "screenshots").mkdir()
        paths = [str((session / "screenshots" / f"image-{index}.png").resolve()) for index in range(len(results))]
        normalized = [{**result, "path": path} for result, path in zip(results, paths)]
        (session / "visual" / "ocr.json").write_text(
            json.dumps({"selected_image_count": len(normalized), "results": normalized}), encoding="utf-8"
        )
        (session / "visual" / "ocr.md").write_text("ocr\n", encoding="utf-8")
        (session / "visual" / "timeline.md").write_text("timeline\n", encoding="utf-8")
        (session / "screenshots" / "manifest.json").write_text(
            json.dumps({"frames": [{"path": path, "selected": True} for path in paths]}), encoding="utf-8"
        )
        self.addCleanup(temporary.cleanup)
        return session

    def test_partial_ocr_is_not_a_completed_checkpoint(self):
        session = self.make_session([{"ok": True}, {"ok": False, "error": "HTTP 500"}])
        self.assertIsNone(process_latest.completed_ocr_summary(session))

    def test_all_successful_ocr_is_a_completed_checkpoint(self):
        session = self.make_session([{"ok": True}, {"ok": True}])
        self.assertEqual(
            process_latest.completed_ocr_summary(session),
            {"selected": 2, "succeeded": 2, "failed": 0},
        )

    def test_selected_image_without_result_remains_pending(self):
        session = self.make_session([{"ok": True, "status": "complete"}])
        manifest_path = session / "screenshots" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["frames"].append({
            "path": str((session / "screenshots" / "missing-result.png").resolve()),
            "selected": True,
        })
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual(
            windows_capture_ui.CaptureApp.ocr_counts(None, session),
            {"total": 2, "complete": 1, "failed": 0, "pending": 1},
        )

    def test_persisted_process_failure_survives_app_restart(self):
        session = self.make_session([])
        (session / "transcripts").mkdir(exist_ok=True)
        (session / "transcripts" / "process_state.json").write_text(
            json.dumps({"status": "failed", "error": "worker stopped"}), encoding="utf-8"
        )
        self.assertTrue(windows_capture_ui.CaptureApp.processing_failed(None, session))

    def test_review_mousewheel_scrolls_in_both_directions(self):
        class Container:
            callback = None

            def bind(self, event, callback):
                self.event = event
                self.callback = callback

        class Canvas:
            calls = []

            def yview_scroll(self, amount, unit):
                self.calls.append((amount, unit))

        container = Container()
        canvas = Canvas()
        windows_capture_ui.bind_vertical_mousewheel(container, canvas)
        self.assertEqual(container.event, "<MouseWheel>")
        self.assertEqual(container.callback(SimpleNamespace(delta=-120)), "break")
        self.assertEqual(container.callback(SimpleNamespace(delta=120)), "break")
        self.assertEqual(canvas.calls, [(1, "units"), (-1, "units")])

    def test_blank_exception_has_a_visible_error_name(self):
        self.assertEqual(windows_ocr.error_message(TimeoutError()), "TimeoutError")

    def test_structured_ocr_worker_error_is_preserved(self):
        detail = process_latest.ocr_worker_error(
            {"ok": False, "error": "Only 1200 MiB GPU memory is free"},
            ['{"ok": false, "error": "Only 1200 MiB GPU memory is free"}\n'],
            1,
        )
        self.assertEqual(detail, "Only 1200 MiB GPU memory is free")

    def test_preflight_releases_ollama_models_and_blocks_low_vram(self):
        with (
            mock.patch.object(windows_ocr, "ollama_running_models", return_value=["other-model:latest"]),
            mock.patch.object(windows_ocr, "unload_model", return_value=True) as unload,
            mock.patch.object(windows_ocr, "gpu_memory_mb", return_value=(1200, 11264)),
        ):
            with self.assertRaisesRegex(RuntimeError, "1200 MiB"):
                windows_ocr.prepare_ollama_resources("http://127.0.0.1:11434", "glm-ocr:latest")
        unload.assert_called_once_with("http://127.0.0.1:11434", "other-model:latest")

    def test_healthy_ollama_is_reused_without_starting_another_server(self):
        with (
            mock.patch.object(windows_ocr, "ollama_models", return_value=["glm-ocr:latest"]),
            mock.patch.object(windows_ocr, "start_ollama_server", side_effect=AssertionError("must not start")),
        ):
            state = windows_ocr.ensure_ollama_ready("http://127.0.0.1:11434", timeout=1)
        self.assertEqual(state, {"ollama_started": False, "ollama_pid": None})

    def test_unavailable_local_ollama_is_started_and_waited_for(self):
        unavailable = urllib.error.URLError(ConnectionRefusedError(10061, "connection refused"))
        with (
            mock.patch.object(
                windows_ocr,
                "ollama_models",
                side_effect=[unavailable, unavailable, ["glm-ocr:latest"]],
            ),
            mock.patch.object(
                windows_ocr,
                "start_ollama_server",
                return_value=SimpleNamespace(pid=4321),
            ),
            mock.patch.object(windows_ocr.time, "sleep"),
        ):
            state = windows_ocr.ensure_ollama_ready("http://127.0.0.1:11434", timeout=5)
        self.assertEqual(state, {"ollama_started": True, "ollama_pid": 4321})

    def test_remote_ollama_failure_does_not_start_a_local_server(self):
        unavailable = urllib.error.URLError(ConnectionRefusedError(10061, "connection refused"))
        with (
            mock.patch.object(windows_ocr, "ollama_models", side_effect=unavailable),
            mock.patch.object(windows_ocr, "start_ollama_server", side_effect=AssertionError("must not start")),
        ):
            with self.assertRaisesRegex(RuntimeError, "remote OCR service.*cannot be started locally"):
                windows_ocr.ensure_ollama_ready("http://ocr-box:11434", timeout=1)

    def test_start_ollama_server_uses_the_installed_cli_and_writes_a_log(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        local_app_data = Path(temporary.name)
        executable = local_app_data / "Programs" / "Ollama" / "ollama.exe"
        executable.parent.mkdir(parents=True)
        executable.touch()
        log_path = local_app_data / "meeting" / "ollama_server.log"
        expected_process = SimpleNamespace(pid=9876)
        with (
            mock.patch.dict(windows_ocr.os.environ, {"LOCALAPPDATA": str(local_app_data)}),
            mock.patch.object(windows_ocr.subprocess, "Popen", return_value=expected_process) as popen,
        ):
            process = windows_ocr.start_ollama_server(log_path)
        self.assertIs(process, expected_process)
        self.assertTrue(log_path.exists())
        self.assertEqual(popen.call_args.args[0], [str(executable), "serve"])

    def test_ollama_start_failure_is_reported_as_a_service_error(self):
        unavailable = urllib.error.URLError(ConnectionRefusedError(10061, "connection refused"))
        with (
            mock.patch.object(windows_ocr, "ollama_models", side_effect=unavailable),
            mock.patch.object(windows_ocr, "start_ollama_server", side_effect=OSError("launch denied")),
        ):
            with self.assertRaisesRegex(RuntimeError, "Could not start the local Ollama OCR service.*launch denied"):
                windows_ocr.ensure_ollama_ready("http://127.0.0.1:11434", timeout=1)

    def test_overloaded_ollama_is_waited_for_without_starting_a_duplicate_server(self):
        overloaded = urllib.error.HTTPError("http://127.0.0.1:11434/api/tags", 503, "busy", {}, None)
        with (
            mock.patch.object(windows_ocr, "ollama_models", side_effect=[overloaded, ["glm-ocr:latest"]]),
            mock.patch.object(windows_ocr, "start_ollama_server", return_value=SimpleNamespace(pid=9999)),
            mock.patch.object(windows_ocr.time, "sleep"),
        ):
            state = windows_ocr.ensure_ollama_ready("http://127.0.0.1:11434", timeout=5)
        self.assertEqual(state, {"ollama_started": False, "ollama_pid": None})

    def test_requested_ocr_model_must_be_installed(self):
        with mock.patch.object(windows_ocr, "ollama_models", return_value=["llama3:8b"]):
            with self.assertRaisesRegex(RuntimeError, "OCR model 'glm-ocr' is not installed"):
                windows_ocr.choose_model("http://127.0.0.1:11434", "glm-ocr", 3)

    def test_transient_ollama_errors_include_connection_refusal_and_overload(self):
        refused = urllib.error.URLError(ConnectionRefusedError(10061, "connection refused"))
        overloaded = urllib.error.HTTPError("http://127.0.0.1:11434/api/generate", 503, "busy", {}, None)
        bad_request = urllib.error.HTTPError("http://127.0.0.1:11434/api/generate", 400, "bad", {}, None)
        self.assertTrue(windows_ocr.is_transient_ollama_error(refused))
        self.assertTrue(windows_ocr.is_transient_ollama_error(overloaded))
        self.assertFalse(windows_ocr.is_transient_ollama_error(bad_request))

    def test_transient_image_failure_recovers_ollama_and_retries_only_that_image(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        session = Path(temporary.name)
        screenshots = session / "screenshots"
        screenshots.mkdir()
        image = screenshots / "screen-20260909-120000-0000.png"
        image.touch()
        runtime = {"readiness_checks": 0, "recovered": False}

        def ensure_ready(*args, **kwargs):
            runtime["readiness_checks"] += 1
            if runtime["readiness_checks"] > 1:
                runtime["recovered"] = True
            return {"ollama_started": runtime["recovered"], "ollama_pid": 2468 if runtime["recovered"] else None}

        def generate(*args, **kwargs):
            if not runtime["recovered"]:
                raise urllib.error.URLError(ConnectionRefusedError(10061, "connection refused"))
            return "Recovered OCR text"

        argv = [
            "windows_ocr.py",
            str(session),
            "--model",
            "glm-ocr",
            "--max-attempts",
            "2",
            "--retry-delay",
            "0",
        ]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(windows_ocr, "ensure_ollama_ready", side_effect=ensure_ready),
            mock.patch.object(windows_ocr, "ollama_models", return_value=["glm-ocr:latest"]),
            mock.patch.object(
                windows_ocr,
                "prepare_ollama_resources",
                return_value={"model": "glm-ocr:latest", "released_models": [], "gpu_free_mb": 8000, "gpu_total_mb": 11264},
            ),
            mock.patch.object(windows_ocr, "ollama_generate", side_effect=generate),
            mock.patch.object(windows_ocr, "unload_model", return_value=True),
            mock.patch.object(windows_ocr, "wait_for_ollama", return_value=False),
            mock.patch.object(windows_ocr.time, "sleep"),
        ):
            returncode = windows_ocr.main()

        result = json.loads((session / "visual" / "ocr.json").read_text(encoding="utf-8"))["results"][0]
        self.assertEqual(returncode, 0)
        self.assertEqual(runtime["readiness_checks"], 2)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["text"], "Recovered OCR text")

    def test_ocr_worker_restarts_once_after_pre_item_failure(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        session = Path(temporary.name)
        (session / "screenshots").mkdir()
        (session / "screenshots" / "screen.png").touch()

        class Worker:
            def __init__(self, lines, returncode):
                self.stdout = iter(lines)
                self.returncode = returncode

            def wait(self):
                return self.returncode

        workers = [
            Worker(['{"ok": false, "error": "temporary startup failure"}\n'], 1),
            Worker(['{"ok": true, "successful_image_count": 1, "failed_image_count": 0}\n'], 0),
        ]
        args = SimpleNamespace(skip_ocr=False, force_ocr=False, ocr_model="auto")
        with (
            mock.patch.object(process_latest.subprocess, "Popen", side_effect=workers) as popen,
            mock.patch.object(process_latest.time, "sleep"),
        ):
            messages = process_latest.run_ocr(session, args)
        self.assertEqual(popen.call_count, 2)
        self.assertTrue((session / "transcripts" / "ocr_worker.log").exists())
        self.assertIn("visual", messages[0].casefold())

    def test_screenshot_paths_are_unlimited_by_default(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        session = Path(temporary.name)
        screenshots = session / "screenshots"
        screenshots.mkdir()
        for index in range(205):
            (screenshots / f"screen-{index:03}.png").touch()
        self.assertEqual(len(windows_ocr.screenshot_paths(session)), 205)
        self.assertEqual(len(windows_ocr.screenshot_paths(session, 200)), 200)

    def test_manual_choice_matches_relative_and_absolute_paths(self):
        temporary = tempfile.TemporaryDirectory(dir=REPO_ROOT)
        self.addCleanup(temporary.cleanup)
        image = Path(temporary.name) / "screen.png"
        image.touch()
        relative = image.relative_to(REPO_ROOT)
        frames = [{
            "path": str(relative),
            "selected": True,
            "automatic_selected": True,
        }]
        selected = windows_ocr.apply_manual_overrides(
            [relative], frames, {windows_ocr.path_key(image): False}
        )
        self.assertEqual(selected, [])
        self.assertFalse(frames[0]["selected"])


if __name__ == "__main__":
    unittest.main()
