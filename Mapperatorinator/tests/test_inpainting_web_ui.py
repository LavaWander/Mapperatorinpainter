from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_inpainting_session import normal_entries, write_archive


WEB_UI_PATH = Path(__file__).parents[1] / "web-ui.py"


def load_web_ui():
    spec = importlib.util.spec_from_file_location("mapperatorinator_web_ui_test", WEB_UI_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeProcess:
    next_pid = 10_000

    def __init__(self, *, target, args, daemon):
        self.target = target
        self.args = args
        self.daemon = daemon
        self.exitcode = None
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1
        self.alive = False

    def start(self):
        self.alive = True

    def is_alive(self):
        return self.alive

    def join(self, timeout=None):
        return None


class InpaintWebUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.web_ui = load_web_ui()

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.source = root / "source.osz"
        write_archive(self.source, normal_entries())
        self.client = self.web_ui.app.test_client()
        self.headers = {self.web_ui.CSRF_HEADER_NAME: self.web_ui.LOCAL_UI_CSRF_TOKEN}

    def tearDown(self) -> None:
        with self.web_ui.process_lock:
            records = list(self.web_ui.processes.values())
            self.web_ui.processes.clear()
        for record in records:
            record["queue"].close()
        with self.web_ui.inpaint_sessions_lock:
            sessions = list(self.web_ui.inpaint_sessions.values())
            self.web_ui.inpaint_sessions.clear()
        for session in sessions:
            session.cleanup()
        self.temporary_directory.cleanup()

    def post(self, path: str, data: dict):
        return self.client.post(path, data=data, headers=self.headers)

    def test_open_regenerate_twice_without_reextracting_and_through_shared_server_path(self) -> None:
        opened = self.post("/inpaint/open", {"path": str(self.source)})
        self.assertEqual(200, opened.status_code)
        session_data = opened.get_json()["session"]
        session_id = session_data["session_id"]
        working_directory = session_data["working_directory"]

        request_data = {
            "session_id": session_id,
            "model": "v32",
            "start_time": "00:02",
            "end_time": "00:04",
            "seed": "32",
            "timing_context": "on",
            "hitsounds": "inherit",
            "descriptors": ["skillset/streams", "streams/stamina"],
        }
        ensured_configs = []
        with patch.object(self.web_ui, "_ensure_inference_server", side_effect=ensured_configs.append), \
                patch.object(self.web_ui.mp, "Process", FakeProcess):
            first = self.post("/inpaint/start", request_data)
            self.assertEqual(202, first.status_code)
            first_job = first.get_json()["job_id"]
            first_record = self.web_ui.processes[first_job]
            first_record["success_event"].set()
            first_record["process"].alive = False
            first_record["queue"].put({"_event": "exit", "code": 0})
            self.client.get(f"/stream_output?job_id={first_job}").get_data()

            second = self.post("/inpaint/start", request_data)
            self.assertEqual(202, second.status_code)

        self.assertEqual(working_directory, str(self.web_ui._get_inpaint_session(session_id).working_directory))
        self.assertTrue(self.web_ui._get_inpaint_session(session_id).dirty)
        self.assertEqual(2, len(ensured_configs))
        self.assertTrue(all(config.use_server for config in ensured_configs))
        self.assertTrue(all(config.descriptors == ["skillset/streams", "streams/stamina"] for config in ensured_configs))
        self.assertEqual(32, second.get_json()["seed"])

    def test_session_mutations_require_csrf(self) -> None:
        response = self.client.post("/inpaint/open", data={"path": str(self.source)})
        self.assertEqual(403, response.status_code)


if __name__ == "__main__":
    unittest.main()
