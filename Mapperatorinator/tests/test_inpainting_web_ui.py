from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from inpainting.preview import PreviewLaunch
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
        self.web_ui._shutdown_inpaint_previewers()
        with self.web_ui.inpaint_sessions_lock:
            sessions = list(self.web_ui.inpaint_sessions.values())
            self.web_ui.inpaint_sessions.clear()
        for session in sessions:
            session.cleanup()
        snapshot = self.web_ui.preview_window_controller.snapshot()
        if snapshot.session_id:
            self.web_ui.preview_window_controller.clear_session(snapshot.session_id)
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
        revision_state = self.web_ui._get_inpaint_session(session_id).revision_payload()
        self.assertEqual(1, revision_state["current_revision"])
        self.assertEqual(32, revision_state["items"][-1]["metadata"]["seed"])
        self.assertEqual(["skillset/streams", "streams/stamina"], revision_state["items"][-1]["metadata"]["descriptors"])

    def test_undo_redo_and_dirty_close_are_enforced_by_endpoints(self) -> None:
        opened = self.post("/inpaint/open", {"path": str(self.source)})
        session_data = opened.get_json()["session"]
        session_id = session_data["session_id"]
        session = self.web_ui._get_inpaint_session(session_id)
        path = session.active_difficulty.path
        original = path.read_bytes()
        modified = original.replace(b"Version:Expert", b"Version:Changed")
        path.write_bytes(modified)
        session.record_revision(metadata={"kind": "generation", "seed": 44})

        protected_close = self.post("/inpaint/close", {"session_id": session_id})
        self.assertEqual(409, protected_close.status_code)
        self.assertTrue(protected_close.get_json()["unsaved_changes"])

        undone = self.post("/inpaint/undo", {"session_id": session_id})
        self.assertEqual(200, undone.status_code)
        self.assertFalse(undone.get_json()["session"]["dirty"])
        self.assertEqual(original, path.read_bytes())

        redone = self.post("/inpaint/redo", {"session_id": session_id})
        self.assertEqual(200, redone.status_code)
        self.assertTrue(redone.get_json()["session"]["dirty"])
        self.assertEqual(modified, path.read_bytes())

        discarded = self.post("/inpaint/close", {"session_id": session_id, "discard": "true"})
        self.assertEqual(200, discarded.status_code)
        self.assertFalse(session.working_directory.exists())

    def test_inpaint_export_uses_automatic_unique_output_directory(self) -> None:
        opened = self.post("/inpaint/open", {"path": str(self.source)})
        session_id = opened.get_json()["session"]["session_id"]
        output_directory = Path(self.temporary_directory.name) / "inpaint_output"

        with patch.object(self.web_ui, "INPAINT_OUTPUT_DIRECTORY", output_directory):
            first = self.post("/inpaint/export", {"session_id": session_id})
            second = self.post("/inpaint/export", {"session_id": session_id})

        self.assertEqual(200, first.status_code)
        self.assertEqual(200, second.status_code)
        first_path = Path(first.get_json()["path"])
        second_path = Path(second.get_json()["path"])
        self.assertEqual(output_directory, first_path.parent)
        self.assertTrue(first_path.name.endswith("__R000__original.osz"))
        self.assertTrue(second_path.stem.endswith("__02"))
        self.assertNotEqual(first_path, second_path)

    def test_open_song_folder_endpoint_uses_working_copy(self) -> None:
        source_folder = Path(self.temporary_directory.name) / "song-folder"
        source_folder.mkdir()
        for relative_name, content in normal_entries().items():
            path = source_folder / relative_name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

        opened = self.post("/inpaint/open", {"path": str(source_folder)})
        self.assertEqual(200, opened.status_code)
        payload = opened.get_json()["session"]
        self.assertEqual(source_folder.name, payload["source_name"])
        self.assertNotEqual(str(source_folder), payload["working_directory"])

    def test_session_mutations_require_csrf(self) -> None:
        response = self.client.post("/inpaint/open", data={"path": str(self.source)})
        self.assertEqual(403, response.status_code)

    def test_m4_controls_are_rendered(self) -> None:
        response = self.client.get("/")
        self.assertEqual(200, response.status_code)
        html = response.get_data(as_text=True)
        for control_id in (
            "inpaint-random-seed",
            "inpaint-undo-button",
            "inpaint-redo-button",
            "inpaint-revision-history",
            "inpaint-close-button",
        ):
            self.assertIn(f'id="{control_id}"', html)

    def test_preview_endpoint_applies_independent_padding(self) -> None:
        opened = self.post("/inpaint/open", {"path": str(self.source)})
        session_data = opened.get_json()["session"]
        session_id = session_data["session_id"]
        session = self.web_ui._get_inpaint_session(session_id)

        class FakePreviewer:
            def __init__(self):
                self.calls = []

            def preview(self, beatmap_path, start_time, end_time):
                self.calls.append((Path(beatmap_path), start_time, end_time))
                return PreviewLaunch(Path(beatmap_path), start_time, end_time, 1234, "Danser 0.11.0")

        previewer = FakePreviewer()
        with patch.object(self.web_ui, "_get_inpaint_previewer", return_value=previewer):
            response = self.post("/inpaint/preview", {
                "session_id": session_id,
                "start_time": "00:02.000",
                "end_time": "00:04.000",
                "padding_before": "3",
                "padding_after": "1.5",
            })

        self.assertEqual(200, response.status_code)
        self.assertEqual((0, min(5_500, session.active_difficulty.length_ms)), previewer.calls[0][1:])
        self.assertEqual(session.active_difficulty.path, previewer.calls[0][0])
        self.assertEqual("Danser 0.11.0", response.get_json()["viewer"])

    def test_m5_controls_are_rendered(self) -> None:
        response = self.client.get("/")
        self.assertEqual(200, response.status_code)
        html = response.get_data(as_text=True)
        for control_id in (
            "inpaint-preview-button",
            "inpaint_preview_padding_before",
            "inpaint_preview_padding_after",
            "inpaint-preview-status",
        ):
            self.assertIn(f'id="{control_id}"', html)

    def test_persistent_preview_state_follows_revision_and_copies_boundaries(self) -> None:
        opened = self.post("/inpaint/open", {"path": str(self.source)})
        session_id = opened.get_json()["session"]["session_id"]
        configured = self.post("/inpaint/preview-window/config", {
            "session_id": session_id,
            "start_time": "00:02.000",
            "end_time": "00:04.000",
            "padding_before": "1",
            "padding_after": "2",
        })
        self.assertEqual(200, configured.status_code)

        first_state = self.post("/inpaint/preview-window/state", {}).get_json()
        self.assertTrue(first_state["has_session"])
        self.assertEqual(0, first_state["map"]["mode"])
        self.assertGreater(first_state["map"]["object_count"], 0)
        self.assertEqual(240, len(first_state["map"]["density"]))
        first_key = first_state["map"]["key"]

        scene_response = self.post("/inpaint/preview-window/data", {"key": first_key})
        self.assertEqual(200, scene_response.status_code)
        scene = scene_response.get_json()["scene"]
        self.assertEqual(first_state["map"]["object_count"], len(scene["objects"]))
        self.assertIn("slider", {item["type"] for item in scene["objects"]})

        audio_response = self.client.get(first_state["map"]["audio_url"])
        self.assertEqual(200, audio_response.status_code)
        self.assertEqual(b"RIFF-test-audio", audio_response.data)
        audio_response.close()
        range_response = self.client.get(
            first_state["map"]["audio_url"],
            headers={"Range": "bytes=0-3"},
        )
        self.assertEqual(206, range_response.status_code)
        self.assertEqual(b"RIFF", range_response.data)
        range_response.close()

        copied = self.post("/inpaint/preview-window/selection", {
            "boundary": "start",
            "cursor": "3000",
        }).get_json()["selection"]
        self.assertEqual(3_000, copied["start_time"])

        session = self.web_ui._get_inpaint_session(session_id)
        path = session.active_difficulty.path
        path.write_bytes(path.read_bytes().replace(b"Version:Expert", b"Version:Changed"))
        session.record_revision(metadata={"kind": "generation", "seed": 6})
        changed_state = self.post("/inpaint/preview-window/state", {}).get_json()
        self.assertNotEqual(first_key, changed_state["map"]["key"])

    def test_persistent_preview_play_uses_cursor_to_map_end(self) -> None:
        opened = self.post("/inpaint/open", {"path": str(self.source)})
        session_id = opened.get_json()["session"]["session_id"]
        self.post("/inpaint/preview-window/config", {
            "session_id": session_id,
            "start_time": "0",
            "end_time": "4",
        })
        session = self.web_ui._get_inpaint_session(session_id)

        class FakePreviewer:
            def __init__(self):
                self.calls = []

            def preview(self, beatmap_path, start_time, end_time):
                self.calls.append((Path(beatmap_path), start_time, end_time))
                return PreviewLaunch(Path(beatmap_path), start_time, end_time, 4321, "Danser 0.11.0")

        previewer = FakePreviewer()
        with patch.object(self.web_ui, "_get_inpaint_previewer", return_value=previewer):
            played = self.post("/inpaint/preview-window/play", {"cursor": "2500"})

        self.assertEqual(200, played.status_code)
        self.assertEqual(2_500, previewer.calls[0][1])
        self.assertEqual(session.active_difficulty.length_ms + 1, previewer.calls[0][2])

    def test_preview_serves_only_parser_resolved_session_hitsounds(self) -> None:
        opened = self.post("/inpaint/open", {"path": str(self.source)})
        session_id = opened.get_json()["session"]["session_id"]
        session = self.web_ui._get_inpaint_session(session_id)
        path = session.active_difficulty.path
        content = path.read_bytes().replace(
            b"64,64,900,1,0,0:0:0:0:",
            b"64,64,900,1,0,0:0:0:70:samples/soft-hit.wav",
        )
        path.write_bytes(content)
        session.record_revision(metadata={"kind": "hitsound-test"})
        self.post("/inpaint/preview-window/config", {
            "session_id": session_id,
            "start_time": "0",
            "end_time": "4",
        })

        state = self.post("/inpaint/preview-window/state", {}).get_json()
        scene = self.post(
            "/inpaint/preview-window/data",
            {"key": state["map"]["key"]},
        ).get_json()["scene"]
        circle_event = next(event for event in scene["hitsounds"] if event["time"] == 900)
        self.assertEqual("circle", circle_event["type"])
        self.assertEqual(70, circle_event["samples"][0]["volume"])
        sample_url = circle_event["samples"][0]["url"]
        sample_response = self.client.get(sample_url)
        self.assertEqual(200, sample_response.status_code)
        self.assertEqual(b"custom-sample", sample_response.data)
        sample_response.close()

    def test_m6_preview_window_controls_are_rendered(self) -> None:
        response = self.client.get("/inpaint/preview-window")
        self.assertEqual(200, response.status_code)
        html = response.get_data(as_text=True)
        for control_id in (
            "playfield",
            "play-pause-button",
            "density-timeline",
            "cursor-time",
            "copy-start-button",
            "copy-end-button",
            "auto-play-updates",
            "hitsounds-enabled",
            "hitsound-volume",
            "hitsound-volume-value",
            "danser-button",
        ):
            self.assertIn(f'id="{control_id}"', html)


if __name__ == "__main__":
    unittest.main()
