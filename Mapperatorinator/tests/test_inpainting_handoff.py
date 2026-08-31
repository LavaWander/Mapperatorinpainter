from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from inpainting.handoff import GeneratedHandoffError, materialize_generated_workspace
from inpainting.session import BeatmapsetSession
from tests.test_inpainting_session import make_osu


class GeneratedHandoffTests(unittest.TestCase):
    def test_generation_result_becomes_an_adopted_clean_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = Path(tempfile.mkdtemp(prefix="session-generated-", dir=root))
            audio = root / "audio.wav"
            background = root / "bg.jpg"
            result = root / "normal-output.osu"
            audio.write_bytes(b"RIFF-audio")
            background.write_bytes(b"jpeg-background")
            content = make_osu(version="Generated", audio_filename="audio.wav", background="bg.jpg")
            result.write_bytes(content)
            provenance = {
                "model": "v32",
                "seed": 12345,
                "difficulty": 7.2,
                "descriptors": ["skillset/streams"],
            }

            manifest = materialize_generated_workspace(
                workspace,
                osu_content=content.decode("utf-8"),
                result_path=result,
                audio_path=audio,
                background_path=background,
                provenance=provenance,
            )

            self.assertEqual(workspace.resolve(), Path(manifest["workspace"]))
            self.assertEqual(b"RIFF-audio", (workspace / "audio.wav").read_bytes())
            self.assertEqual(b"jpeg-background", (workspace / "bg.jpg").read_bytes())
            session = BeatmapsetSession.adopt_generated_workspace(
                result,
                workspace,
                provenance=manifest["provenance"],
            )
            try:
                self.assertEqual(workspace.resolve(), session.working_directory)
                self.assertEqual("Generated", session.active_difficulty.version)
                self.assertEqual(provenance, session.generation_provenance)
                self.assertFalse(session.dirty)
                self.assertEqual(b"RIFF-audio", session.resolve_audio().read_bytes())
            finally:
                session.cleanup()
            self.assertFalse(workspace.exists())

    def test_unsafe_generated_asset_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = Path(tempfile.mkdtemp(prefix="session-generated-", dir=root))
            audio = root / "audio.wav"
            result = root / "result.osu"
            audio.write_bytes(b"RIFF")
            result.write_bytes(make_osu(version="Unsafe", audio_filename="../audio.wav"))

            with self.assertRaises(GeneratedHandoffError):
                materialize_generated_workspace(
                    workspace,
                    osu_content=result.read_text(encoding="utf-8"),
                    result_path=result,
                    audio_path=audio,
                    background_path=None,
                    provenance={},
                )


if __name__ == "__main__":
    unittest.main()
