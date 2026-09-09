import json
import unittest

import support  # noqa: F401

from buildloop import charts
from buildloop.charts import Series
from buildloop.humanize import ms


class TestTooltipMetadata(unittest.TestCase):
    """The tooltip is only as good as the metadata the page ships with it."""

    def test_line_chart_emits_a_hover_target_per_category(self):
        c = charts.line_chart(
            "c1", "t", "s", ["w1", "w2", "w3"],
            [Series("a", [("w1", 1000), ("w3", 3000)])], ms,
        )
        self.assertEqual(c.html.count('class="hit"'), 3)
        self.assertEqual(c.meta["type"], "line")
        # Gaps stay gaps: a missing week must be null, not zero.
        self.assertEqual(c.meta["series"][0]["values"], ["1s", None, "3s"])

    def test_line_chart_marks_a_dot_per_real_point(self):
        c = charts.line_chart(
            "c1", "t", "s", ["w1", "w2"], [Series("a", [("w1", 1000)])], ms,
        )
        self.assertEqual(c.html.count('class="dot"'), 1)

    def test_stacked_chart_carries_totals(self):
        c = charts.stacked_bar_chart(
            "c2", "t", "s", ["w1"],
            [Series("q", [("w1", 1000)]), Series("e", [("w1", 2000)])], ms,
        )
        self.assertEqual(c.meta["totals"], ["3s"])

    def test_hbar_carries_labels_and_a_heading(self):
        c = charts.hbar_chart("c3", "t", "s", [("job a", 4)], str, note="failures")
        self.assertEqual(c.meta["note"], "failures")
        self.assertEqual(c.meta["rows"], [{"label": "job a", "value": "4"}])

    def test_empty_chart_has_no_metadata_to_hover(self):
        c = charts.empty("c4", "t", "s", "no data yet")
        self.assertIsNone(c.meta)
        self.assertIn("no data yet", c.html)

    def test_metadata_is_json_serialisable(self):
        c = charts.line_chart("c1", "t", "s", ["w1"], [Series("a", [("w1", 1)])], ms)
        json.dumps(c.meta)  # must not raise

    def test_labels_are_escaped(self):
        c = charts.hbar_chart("c5", "t", "s", [("<script>x</script>", 1)], str)
        self.assertNotIn("<script>x", c.html)


if __name__ == "__main__":
    unittest.main()
