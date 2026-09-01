from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from inpainting.session import BeatmapsetSession
from inpainting.ui import (
    compose_inpainting_config,
    format_timestamp_ms,
    parse_timestamp_ms,
    session_payload,
)
from osuT5.osuT5.event import ContextType
from tests.test_inpainting_session import normal_entries, write_archive


class TimestampTests(unittest.TestCase):
    def test_supported_timestamp_forms_are_normalized(self) -> None:
        cases = {
            "01:23": (83_000, "01:23.000"),
            "01:23.45": (83_450, "01:23.450"),
            "01:23.450": (83_450, "01:23.450"),
            "83": (83_000, "01:23.000"),
            "83.45": (83_450, "01:23.450"),
        }
        for value, (expected, normalized) in cases.items():
            with self.subTest(value=value):
                self.assertEqual(expected, parse_timestamp_ms(value))
                self.assertEqual(normalized, format_timestamp_ms(expected))

    def test_invalid_timestamp_forms_are_rejected(self) -> None:
        for value in ("", "1:60", "-1", "1.2345", "one minute"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_timestamp_ms(value)


class InpaintUiRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        source = root / "source.osz"
        write_archive(source, normal_entries())
        self.session = BeatmapsetSession.open(source, temp_root=root / "sessions")

    def tearDown(self) -> None:
        self.session.cleanup()
        self.temporary_directory.cleanup()

    def test_payload_distinguishes_inherited_map_values(self) -> None:
        payload = session_payload("session-id", self.session)
        self.assertEqual("session-id", payload["session_id"])
        self.assertEqual("Expert", payload["active_difficulty"]["version"])
        self.assertEqual("音声.wav", payload["assets"]["audio"])
        self.assertEqual(1.4, payload["metadata"]["slider_multiplier"])
        self.assertFalse(payload["dirty"])

    def test_controls_are_applied_to_existing_partial_generation_config(self) -> None:
        config = compose_inpainting_config(
            config_dir=Path(__file__).parents[1] / "configs" / "inference",
            model_name="v32",
            session=self.session,
            values={
                "start_time": "00:02",
                "end_time": "00:04.000",
                "difficulty": "6.2",
                "mapper_id": "123",
                "year": "2024",
                "seed": "38271942",
                "timing_context": "true",
                "hitsounds": "inherit",
                "temperature": "0.75",
                "cfg_scale": "1.5",
                "top_p": "0.85",
                "lookback": "0.6",
                "lookahead": "0.3",
            },
            descriptors=["stream, intense"],
            negative_descriptors=["simple"],
        )

        self.assertEqual(str(self.session.active_difficulty.path), config.beatmap_path)
        self.assertTrue(config.add_to_beatmap)
        self.assertTrue(config.overwrite_reference_beatmap)
        self.assertEqual((2_000, 4_000), (config.start_time, config.end_time))
        self.assertEqual(6.2, config.difficulty)
        self.assertEqual(38271942, config.seed)
        self.assertEqual(["stream", "intense"], config.descriptors)
        self.assertEqual([ContextType.TIMING], config.in_context)
        self.assertIsNone(config.hitsounded)
        self.assertEqual(0.6, config.lookback)

    def test_zero_valued_advanced_controls_are_not_replaced_by_defaults(self) -> None:
        config = compose_inpainting_config(
            config_dir=Path(__file__).parents[1] / "configs" / "inference",
            model_name="v32",
            session=self.session,
            values={
                "start_time": "2",
                "end_time": "4",
                "temperature": "0",
                "cfg_scale": "0",
                "lookback": "0",
                "lookahead": "0",
            },
        )
        self.assertEqual(0, config.temperature)
        self.assertEqual(0, config.cfg_scale)
        self.assertEqual(0, config.lookback)
        self.assertEqual(0, config.lookahead)

    def test_blank_conditioning_leaves_mapper_and_year_unconditioned(self) -> None:
        config = compose_inpainting_config(
            config_dir=Path(__file__).parents[1] / "configs" / "inference",
            model_name="v32",
            session=self.session,
            values={
                "start_time": "2",
                "end_time": "4",
                "difficulty": "",
                "mapper_id": "",
                "year": "",
            },
        )

        # Difficulty remains unset here so compile_args can calculate it from
        # the reference map. Mapper style and descriptors stay unconditioned.
        self.assertIsNone(config.difficulty)
        self.assertIsNone(config.mapper_id)
        self.assertIsNone(config.descriptors)
        self.assertIsNone(config.year)

    def test_interval_cannot_exceed_selected_map(self) -> None:
        with self.assertRaisesRegex(ValueError, "exceeds the selected map length"):
            compose_inpainting_config(
                config_dir=Path(__file__).parents[1] / "configs" / "inference",
                model_name="v32",
                session=self.session,
                values={"start_time": "00:02", "end_time": "99:00"},
            )


if __name__ == "__main__":
    unittest.main()
