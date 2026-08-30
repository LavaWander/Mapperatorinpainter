from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from inpainting.preview import DanserPreviewer, find_danser_executable


class FakeDanserProcess:
    next_pid = 20_000

    def __init__(self, command, **options):
        self.command = command
        self.options = options
        self.pid = self.next_pid
        FakeDanserProcess.next_pid += 1
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


class DanserPreviewerTests(unittest.TestCase):
    def test_stages_only_active_difficulty_and_launches_exact_padded_interval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            executable = root / "danser" / "danser-cli.exe"
            executable.parent.mkdir()
            executable.write_bytes(b"fake-danser")
            working = root / "working"
            (working / "maps").mkdir(parents=True)
            (working / "assets").mkdir()
            (working / "unknown").mkdir()
            active_path = working / "Expert.osu"
            active_path.write_bytes(b"osu file format v14\nVersion:Expert\n")
            (working / "maps" / "Easy.osu").write_bytes(b"osu file format v14\nVersion:Easy\n")
            (working / "assets" / "音声.wav").write_bytes(b"RIFF-test-audio")
            (working / "unknown" / "readme.txt").write_text("preserve me", encoding="utf-8")
            processes = []

            def process_factory(command, **options):
                process = FakeDanserProcess(command, **options)
                processes.append(process)
                return process

            previewer = DanserPreviewer(
                executable=executable,
                beatmapset_root=working,
                temp_root=root / "previews",
                process_factory=process_factory,
            )
            preview_root = previewer.preview_root
            try:
                launched = previewer.preview(active_path, 20_450, 31_200)
                self.assertEqual((20_450, 31_200), (launched.start_time, launched.end_time))
                self.assertEqual(processes[0].pid, launched.process_id)

                staged_osu = list(previewer.mapset_root.rglob("*.osu"))
                self.assertEqual(["Expert.osu"], [path.name for path in staged_osu])
                self.assertTrue((previewer.mapset_root / "assets" / "音声.wav").is_file())
                self.assertTrue((previewer.mapset_root / "unknown" / "readme.txt").is_file())

                command = processes[0].command
                self.assertIn("-start=20.45", command)
                self.assertIn("-end=31.2", command)
                self.assertIn("-quickstart", command)
                self.assertNotIn("-nodbcheck", command)
                md5_argument = next(item for item in command if item.startswith("-md5="))
                self.assertEqual(32, len(md5_argument.removeprefix("-md5=")))
                patch_argument = next(item for item in command if item.startswith("-sPatch="))
                settings_patch = json.loads(patch_argument.removeprefix("-sPatch="))
                self.assertEqual(str(previewer.songs_root), settings_patch["General"]["OsuSongsDir"])
                self.assertFalse(settings_patch["Graphics"]["Fullscreen"])
                self.assertFalse(settings_patch["Playfield"]["SeizureWarning"]["Enabled"])
                self.assertTrue((executable.parent / "settings" / "mapperatorinpainter.json").is_file())

                original_command = list(command)
                active_path.write_bytes(active_path.read_bytes().replace(b"Version:Expert", b"Version:Changed"))
                previewer.preview(active_path, 22_000, 33_000)
                self.assertTrue(processes[0].terminated)
                old_md5 = next(item for item in original_command if item.startswith("-md5="))
                new_md5 = next(item for item in processes[1].command if item.startswith("-md5="))
                self.assertNotEqual(old_md5, new_md5)
            finally:
                previewer.close()
            self.assertFalse(preview_root.exists())

    def test_default_discovery_uses_dedicated_project_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            executable = root / ".tools" / "danser-0.11.0" / "danser-cli.exe"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"fake-danser")
            self.assertEqual(executable.resolve(), find_danser_executable(root))


if __name__ == "__main__":
    unittest.main()
