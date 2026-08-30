from __future__ import annotations

import unittest
import tempfile
from datetime import timedelta
from pathlib import Path

from slider import Beatmap, Circle, Slider, Spinner

from config import InferenceConfig
from osuT5.osuT5.inference.postprocessor import Postprocessor


FIXTURES = Path(__file__).parent / "fixtures"
START_TIME = 2_000
END_TIME = 4_000


def milliseconds(value: timedelta) -> int:
    return round(value.total_seconds() * 1_000)


class PartialBeatmapMergeTests(unittest.TestCase):
    def setUp(self) -> None:
        args = InferenceConfig(start_time=START_TIME, end_time=END_TIME)
        self.postprocessor = Postprocessor(args)
        self.reference_path = FIXTURES / "partial_reference.osu"
        self.reference_text = self.reference_path.read_text(encoding="utf-8")
        self.generated_text = (FIXTURES / "partial_generated.osu").read_text(encoding="utf-8")

    def merge(self) -> Beatmap:
        merged = self.postprocessor.add_to_beatmap(
            self.generated_text,
            str(self.reference_path),
        )
        return Beatmap.parse(merged)

    def test_merge_is_non_mutating_and_returns_a_parseable_beatmap(self) -> None:
        merged = self.postprocessor.add_to_beatmap(
            self.generated_text,
            str(self.reference_path),
        )

        self.assertEqual(self.reference_text, self.reference_path.read_text(encoding="utf-8"))
        self.assertIsInstance(Beatmap.parse(merged), Beatmap)

    def test_interval_membership_is_inclusive_and_uses_object_start_time(self) -> None:
        objects = self.merge().hit_objects(stacking=False)
        by_time = {milliseconds(hit_object.time): hit_object for hit_object in objects}

        self.assertEqual([900, 1200, 1500, 2000, 2500, 3000, 4000, 4100], sorted(by_time))

        # Compound objects beginning before the interval survive even when their
        # end extends into it.
        self.assertIsInstance(by_time[1200], Spinner)
        self.assertEqual(2500, milliseconds(by_time[1200].end_time))
        self.assertIsInstance(by_time[1500], Slider)
        self.assertGreater(milliseconds(by_time[1500].end_time), START_TIME)

        # Objects beginning inside the interval come from the generated map.
        self.assertIsInstance(by_time[2000], Circle)
        self.assertEqual((320, 64), tuple(by_time[2000].position))
        self.assertIsInstance(by_time[3000], Slider)
        self.assertGreater(milliseconds(by_time[3000].end_time), END_TIME)

        # Both boundaries are replaced, while the first object after the end is
        # retained from the reference. Generated objects outside the interval
        # are not imported.
        self.assertEqual((416, 192), tuple(by_time[4000].position))
        self.assertEqual((288, 288), tuple(by_time[4100].position))
        self.assertNotIn(4500, by_time)

    def test_timing_points_inside_interval_are_replaced_and_sorted(self) -> None:
        timing_points = self.merge().timing_points
        offsets = [milliseconds(timing_point.offset) for timing_point in timing_points]

        self.assertEqual(offsets, sorted(offsets))
        self.assertIn(1000, offsets)
        self.assertIn(2000, offsets)  # start-state reconciliation point
        self.assertIn(2200, offsets)
        self.assertIn(3200, offsets)
        self.assertIn(3900, offsets)
        self.assertIn(4100, offsets)
        self.assertNotIn(2500, offsets)
        self.assertNotIn(3000, offsets)
        self.assertNotIn(3500, offsets)

    def test_reference_slider_velocity_is_restored_at_end_boundary(self) -> None:
        # Make the reference SV at the start (0.5x) deliberately differ from
        # the reference SV at the end (1.0x), proving restoration is based on
        # the last reference state before end_time rather than start_time.
        reference_text = self.reference_text.replace(
            "1000,-100,4,2,1,60,0,0",
            "1000,-200,4,2,1,60,0,0",
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            reference_path = Path(temporary_directory) / "reference.osu"
            reference_path.write_text(reference_text, encoding="utf-8")
            merged = Beatmap.parse(self.postprocessor.add_to_beatmap(self.generated_text, str(reference_path)))

        boundary_points = [
            timing_point
            for timing_point in merged.timing_points
            if milliseconds(timing_point.offset) == END_TIME and timing_point.parent is not None
        ]

        # The reference is at 1.0x SV at 4s (its last greenline before the
        # boundary is -100 at 3.5s), not the 0.5x from the interval start.
        # The generated map changes to 0.8x at 3.9s, which must not leak into
        # the untouched portion of the map.
        self.assertEqual(1, len(boundary_points))
        self.assertAlmostEqual(-100, boundary_points[0].ms_per_beat)

    def test_timing_points_after_an_early_interval_keep_all_reference_effects(self) -> None:
        self.postprocessor.start_time = 0
        self.postprocessor.end_time = 1_000
        reference = Beatmap.from_path(self.reference_path)

        def signature(timing_point):
            return (
                milliseconds(timing_point.offset),
                timing_point.ms_per_beat,
                timing_point.meter,
                timing_point.sample_type,
                timing_point.sample_set,
                timing_point.volume,
                timing_point.parent is None,
                timing_point.kiai_mode,
            )

        expected = [signature(tp) for tp in reference.timing_points if milliseconds(tp.offset) > 1_000]
        merged = Beatmap.parse(self.postprocessor.add_to_beatmap(self.generated_text, str(self.reference_path)))
        actual = [signature(tp) for tp in merged.timing_points if milliseconds(tp.offset) > 1_000]

        self.assertEqual(expected, actual)


if __name__ == "__main__":
    unittest.main()
