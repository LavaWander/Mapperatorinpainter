from __future__ import annotations

import hashlib
import stat
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path

from slider import Beatmap

from config import InferenceConfig
from inpainting.session import (
    AssetResolutionError,
    BeatmapsetOpenError,
    BeatmapsetSession,
    ExportError,
    UnsafeArchiveError,
)
from inpainting.workflow import (
    GenerationTransactionError,
    GenerationValidationError,
    build_inpainting_config,
    regenerate_interval,
)
from osuT5.osuT5.inference.postprocessor import Postprocessor


FIXTURES = Path(__file__).parent / "fixtures"
REFERENCE_OSU = (FIXTURES / "partial_reference.osu").read_text(encoding="utf-8")
GENERATED_OSU = (FIXTURES / "partial_generated.osu").read_text(encoding="utf-8")


def make_osu(
    *,
    version: str,
    audio_filename: str,
    background: str | None = None,
    mode: int = 0,
) -> bytes:
    content = REFERENCE_OSU.replace("AudioFilename: audio.wav", f"AudioFilename: {audio_filename}")
    content = content.replace("Version:Reference", f"Version:{version}")
    content = content.replace("Mode: 0", f"Mode: {mode}")
    if background is not None:
        content = content.replace("[Events]\n\n", f'[Events]\n0,0,"{background}",0,0\n\n')
    return content.encode("utf-8-sig")


def write_archive(path: Path, entries: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)


def archive_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normal_entries() -> dict[str, bytes]:
    return {
        "Expert.osu": make_osu(
            version="Expert",
            audio_filename="assets/音声.wav",
            background="images/bg.jpg",
        ),
        "maps/Easy.osu": make_osu(
            version="Easy",
            audio_filename="../assets/音声.wav",
            background="../images/bg.jpg",
        ),
        "assets/音声.wav": b"RIFF-test-audio",
        "images/bg.jpg": b"jpeg-test-background",
        "samples/soft-hit.wav": b"custom-sample",
        "video/story.mp4": b"video-asset",
        "unknown/readme.txt": "preserve me — unicode".encode("utf-8"),
    }


class BeatmapsetSessionTests(unittest.TestCase):
    def test_open_discovers_difficulties_and_resolves_nested_unicode_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source.osz"
            write_archive(source, normal_entries())

            session = BeatmapsetSession.open(source, temp_root=root / "sessions")
            working_directory = session.working_directory
            try:
                self.assertEqual(
                    ["Expert.osu", "maps/Easy.osu"],
                    [difficulty.relative_path for difficulty in session.difficulties],
                )
                easy = session.select_difficulty("maps\\Easy.osu")
                self.assertEqual("Easy", easy.version)
                self.assertEqual(0, easy.mode)
                self.assertEqual("Mapperatorinator-Extended", easy.mapper)
                self.assertEqual(5.0, easy.hp_drain_rate)
                self.assertGreater(easy.length_ms or 0, 4_000)
                self.assertEqual("音声.wav", session.resolve_audio().name)
                self.assertEqual("bg.jpg", session.resolve_background().name)
                self.assertFalse(session.dirty)
                self.assertTrue(session.source_is_unchanged())
            finally:
                session.cleanup()

            self.assertFalse(working_directory.exists())
            self.assertTrue(source.exists())

    def test_missing_and_out_of_session_audio_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            missing_source = root / "missing.osz"
            write_archive(missing_source, {
                "maps/Test.osu": make_osu(version="Missing", audio_filename="missing.wav"),
            })
            with BeatmapsetSession.open(missing_source, temp_root=root / "sessions") as session:
                with self.assertRaisesRegex(AssetResolutionError, "audio file is missing"):
                    session.resolve_audio()

            traversal_source = root / "traversal.osz"
            write_archive(traversal_source, {
                "maps/Test.osu": make_osu(version="Traversal", audio_filename="../../outside.wav"),
            })
            with BeatmapsetSession.open(traversal_source, temp_root=root / "sessions") as session:
                with self.assertRaisesRegex(AssetResolutionError, "outside the beatmapset"):
                    session.resolve_audio()

    def test_unsupported_difficulty_is_discovered_but_cannot_be_selected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "unsupported.osz"
            write_archive(source, {
                "Mania.osu": make_osu(version="Mania", audio_filename="audio.wav", mode=3),
                "audio.wav": b"RIFF-test-audio",
            })

            with BeatmapsetSession.open(source, temp_root=root / "sessions", supported_modes={0}) as session:
                self.assertFalse(session.difficulties[0].supported)
                with self.assertRaisesRegex(BeatmapsetOpenError, "not supported"):
                    session.select_difficulty("Mania.osu")

    def test_export_preserves_every_file_and_reopens_without_session_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source.osz"
            exported = root / "modified.osz"
            entries = normal_entries()
            write_archive(source, entries)
            original_hash = archive_sha256(source)

            with BeatmapsetSession.open(source, temp_root=root / "sessions") as session:
                session.select_difficulty("Expert.osu")
                modified = session.active_difficulty.path.read_text(encoding="utf-8-sig")
                session.active_difficulty.path.write_text(
                    modified.replace("Version:Expert", "Version:Expert Modified"),
                    encoding="utf-8-sig",
                )
                session.mark_dirty()
                self.assertEqual(exported, session.export(exported))
                self.assertTrue(session.source_is_unchanged())

                with self.assertRaisesRegex(ExportError, "already exists"):
                    session.export(exported)
                with self.assertRaisesRegex(ExportError, "source archive"):
                    session.export(source, overwrite=True)

            self.assertEqual(original_hash, archive_sha256(source))
            with zipfile.ZipFile(exported, "r") as archive:
                exported_names = set(archive.namelist())
                self.assertEqual(set(entries), exported_names)
                self.assertFalse(any(name.startswith("session-") for name in exported_names))
                for name, content in entries.items():
                    if name != "Expert.osu":
                        self.assertEqual(content, archive.read(name), name)
                self.assertIn(b"Version:Expert Modified", archive.read("Expert.osu"))

            with BeatmapsetSession.open(exported, temp_root=root / "reopen") as reopened:
                self.assertEqual(2, len(reopened.difficulties))
                self.assertEqual("Expert Modified", reopened.select_difficulty("Expert.osu").version)
                self.assertTrue(reopened.resolve_audio().is_file())


class ArchiveSafetyTests(unittest.TestCase):
    def test_non_zip_and_archives_without_difficulties_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            invalid = root / "invalid.osz"
            invalid.write_bytes(b"not a zip")
            with self.assertRaisesRegex(BeatmapsetOpenError, "not a readable zip"):
                BeatmapsetSession.open(invalid, temp_root=root / "sessions")

            empty = root / "empty.osz"
            write_archive(empty, {"asset.txt": b"asset"})
            with self.assertRaisesRegex(BeatmapsetOpenError, "no .osu"):
                BeatmapsetSession.open(empty, temp_root=root / "sessions")

    def test_zip_slip_drive_paths_backslashes_duplicates_and_symlinks_are_rejected(self) -> None:
        unsafe_names = [
            "../escape.osu",
            "/absolute.osu",
            "C:/drive.osu",
            "nested\\..\\escape.osu",
            "nested/file.osu:stream",
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for index, unsafe_name in enumerate(unsafe_names):
                with self.subTest(unsafe_name=unsafe_name):
                    source = root / f"unsafe-{index}.osz"
                    write_archive(source, {unsafe_name: REFERENCE_OSU.encode("utf-8")})
                    with self.assertRaises(UnsafeArchiveError):
                        BeatmapsetSession.open(source, temp_root=root / "sessions")

            duplicate = root / "duplicate.osz"
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with zipfile.ZipFile(duplicate, "w") as archive:
                    archive.writestr("Map.osu", REFERENCE_OSU)
                    archive.writestr("Map.osu", REFERENCE_OSU)
            with self.assertRaisesRegex(UnsafeArchiveError, "duplicate"):
                BeatmapsetSession.open(duplicate, temp_root=root / "sessions")

            symlink = root / "symlink.osz"
            link_info = zipfile.ZipInfo("linked.osu")
            link_info.create_system = 3
            link_info.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(symlink, "w") as archive:
                archive.writestr(link_info, "target.osu")
            with self.assertRaisesRegex(UnsafeArchiveError, "symbolic link"):
                BeatmapsetSession.open(symlink, temp_root=root / "sessions")


class InpaintingWorkflowTests(unittest.TestCase):
    def create_session(self, root: Path) -> BeatmapsetSession:
        root.mkdir(parents=True, exist_ok=True)
        source = root / "source.osz"
        write_archive(source, {
            "Reference.osu": make_osu(version="Reference", audio_filename="audio.wav"),
            "audio.wav": b"RIFF-test-audio",
            "keep.txt": b"keep",
        })
        return BeatmapsetSession.open(source, temp_root=root / "sessions")

    def test_build_config_and_successful_regeneration_use_working_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with self.create_session(root) as session:
                base_config = InferenceConfig(seed=12345)
                config = build_inpainting_config(base_config, session, start_time=2_000, end_time=4_000)

                self.assertIsNone(base_config.beatmap_path)
                self.assertEqual(str(session.active_difficulty.path), config.beatmap_path)
                self.assertEqual(str(session.resolve_audio()), config.audio_path)
                self.assertEqual(str(session.working_directory), config.output_path)
                self.assertEqual((2_000, 4_000), (config.start_time, config.end_time))
                self.assertTrue(config.add_to_beatmap)
                self.assertTrue(config.overwrite_reference_beatmap)
                self.assertFalse(config.export_osz)

                def successful_runner(inference_config: InferenceConfig) -> str:
                    postprocessor = Postprocessor(inference_config)
                    merged = postprocessor.add_to_beatmap(
                        GENERATED_OSU,
                        inference_config.beatmap_path,
                    )
                    Path(inference_config.beatmap_path).write_text(merged, encoding="utf-8-sig")
                    return "generated"

                self.assertEqual("generated", regenerate_interval(session, config, successful_runner))
                self.assertTrue(session.dirty)
                beatmap = Beatmap.from_path(session.active_difficulty.path)
                times = [round(hit_object.time.total_seconds() * 1_000) for hit_object in beatmap.hit_objects(stacking=False)]
                self.assertIn(2_500, times)
                self.assertTrue(session.source_is_unchanged())

    def test_failed_or_invalid_generation_restores_previous_valid_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for invalid_without_exception in (False, True):
                with self.subTest(invalid_without_exception=invalid_without_exception):
                    with self.create_session(root / str(invalid_without_exception)) as session:
                        config = build_inpainting_config(
                            InferenceConfig(seed=12345),
                            session,
                            start_time=2_000,
                            end_time=4_000,
                        )
                        before = session.active_difficulty.path.read_bytes()

                        def failing_runner(inference_config: InferenceConfig):
                            Path(inference_config.beatmap_path).write_text("corrupt", encoding="utf-8")
                            if not invalid_without_exception:
                                raise RuntimeError("simulated inference failure")

                        expected_error = GenerationValidationError if invalid_without_exception else GenerationTransactionError
                        with self.assertRaises(expected_error):
                            regenerate_interval(session, config, failing_runner)

                        self.assertEqual(before, session.active_difficulty.path.read_bytes())
                        self.assertIsInstance(Beatmap.from_path(session.active_difficulty.path), Beatmap)
                        self.assertFalse(session.dirty)

    def test_interval_validation_rejects_empty_reversed_and_negative_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with self.create_session(root) as session:
                for start_time, end_time in [(-1, 1), (2_000, 2_000), (3_000, 2_000)]:
                    with self.subTest(start_time=start_time, end_time=end_time):
                        with self.assertRaises(ValueError):
                            build_inpainting_config(
                                InferenceConfig(),
                                session,
                                start_time=start_time,
                                end_time=end_time,
                            )


if __name__ == "__main__":
    unittest.main()
