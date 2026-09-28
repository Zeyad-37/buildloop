"""Value formatters, shared by the charts and the narrative.

One definition of "what does 1457000 look like to a person", used by the axis
labels, the tooltips and the JSON summary handed to Claude. Formatting the
summary matters as much as formatting the page: a model given raw milliseconds
writes "1,457,000 ms" into the analysis, which is technically correct and
useless to read.
"""

from __future__ import annotations


def ms(value: float | None) -> str:
    """Duration, at the coarsest unit that still carries information."""
    if value is None:
        return "—"
    seconds = value / 1000
    if seconds < 90:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    return f"{minutes:.0f}m" if minutes < 90 else f"{minutes / 60:.1f}h"


def pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f}%"


def count(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f}"


def mins(value: float | None) -> str:
    """A count of CI minutes. Kept in minutes rather than hours because that
    is the unit GitHub's quota and invoices are stated in."""
    return "—" if value is None else f"{value:,.0f} min"


def mins_axis(value: float) -> str:
    """Axis ticks for CI minutes: "7.5k min" where the exact form won't fit."""
    return f"{value / 1000:g}k min" if value >= 1000 else f"{value:.0f} min"
