from __future__ import annotations

import unittest
from pathlib import Path

from inpainting.preview_window import PreviewWindowController, hitobject_density, preview_map_data


FIXTURE = Path(__file__).parent / "fixtures" / "partial_reference.osu"


class PreviewWindowControllerTests(unittest.TestCase):
    def test_configuration_and_copy_buttons_keep_a_valid_selection(self) -> None:
        controller = PreviewWindowController()
        initial = controller.configure(
            session_id="session-one",
            selection_start=10_000,
            selection_end=20_000,
            padding_before=3_000,
            padding_after=3_000,
            length_ms=60_000,
        )
        self.assertEqual(7_000, initial.cursor)

        moved_start = controller.copy_boundary("start", 30_000, length_ms=60_000)
        self.assertEqual((30_000, 40_000), (moved_start.selection_start, moved_start.selection_end))

        moved_end = controller.copy_boundary("end", 5_000, length_ms=60_000)
        self.assertEqual((0, 5_000), (moved_end.selection_start, moved_end.selection_end))

    def test_new_session_moves_cursor_to_padded_selection_start(self) -> None:
        controller = PreviewWindowController()
        controller.configure(
            session_id="first",
            selection_start=15_000,
            selection_end=25_000,
            padding_before=2_000,
            padding_after=2_000,
            length_ms=60_000,
        )
        changed = controller.configure(
            session_id="second",
            selection_start=4_000,
            selection_end=9_000,
            padding_before=3_000,
            padding_after=3_000,
            length_ms=20_000,
        )
        self.assertEqual(1_000, changed.cursor)
        self.assertIsNone(controller.clear_session("second").session_id)

    def test_parser_backed_density_and_scene_include_renderable_objects(self) -> None:
        density, count = hitobject_density(FIXTURE, 5_000, bins=10)
        parsed = preview_map_data(FIXTURE, 5_000, bins=10)

        self.assertEqual(9, count)
        self.assertEqual(10, len(density))
        self.assertEqual(count, parsed["object_count"])
        self.assertEqual(512, parsed["scene"]["width"])
        self.assertGreater(parsed["scene"]["circle_radius"], 0)
        self.assertEqual(
            ["circle", "spinner", "slider"],
            [item["type"] for item in parsed["scene"]["objects"][:3]],
        )
        slider = parsed["scene"]["objects"][2]
        self.assertGreaterEqual(len(slider["path"]), 17)
        self.assertGreater(slider["end_time"], slider["time"])


if __name__ == "__main__":
    unittest.main()
