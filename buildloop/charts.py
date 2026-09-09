"""Hand-rolled inline SVG charts with hover tooltips.

No CDN, no chart library, no external requests — the generated dashboard works
offline and will still open in five years (RFC T-055 §4.5). Colours come from
CSS custom properties so the page follows the reader's light/dark preference
without duplicating a palette here.

Each chart returns its markup plus a small metadata record. The page collects
those into one JSON blob that a ~60-line script turns into tooltips, so the
values behind every mark are readable without exporting the data. Values are
formatted server-side: the Python formatters are the single source of truth,
and the browser only positions strings.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field

W, H = 900, 260
PAD_L, PAD_R, PAD_T, PAD_B = 62, 16, 14, 46

SERIES_COLORS = 6  # matches --s0..--s5 in the stylesheet


@dataclass
class Series:
    label: str
    points: list[tuple[str, float | None]] = field(default_factory=list)


@dataclass
class Chart:
    """Rendered markup plus the data its tooltip needs."""

    html: str
    meta: dict | None = None


def esc(text) -> str:
    return html.escape(str(text), quote=True)


def _nice_max(value: float) -> float:
    """Round an axis maximum up to something a human would have picked."""
    if value <= 0:
        return 1.0
    import math

    exp = math.floor(math.log10(value))
    base = 10 ** exp
    for step in (1, 2, 2.5, 5, 10):
        if value <= step * base:
            return step * base
    return 10 * base


def _x_labels(categories: list[str], limit: int = 10) -> list[tuple[int, str]]:
    if not categories:
        return []
    step = max(1, len(categories) // limit)
    return [(i, c) for i, c in enumerate(categories) if i % step == 0 or i == len(categories) - 1]


def _axes(categories, y_max, y_fmt, *, x_slot_center: bool) -> list[str]:
    n = max(len(categories), 1)
    plot_w = W - PAD_L - PAD_R
    plot_h = H - PAD_T - PAD_B
    out = []
    for i in range(5):
        y = PAD_T + plot_h * i / 4
        value = y_max * (1 - i / 4)
        out.append(f'<line class="grid" x1="{PAD_L}" y1="{y:.1f}" x2="{W - PAD_R}" y2="{y:.1f}"/>')
        out.append(
            f'<text class="tick" x="{PAD_L - 8}" y="{y + 4:.1f}" text-anchor="end">{esc(y_fmt(value))}</text>'
        )
    slot = plot_w / n
    for i, label in _x_labels(categories):
        x = PAD_L + (slot * (i + 0.5) if x_slot_center else (plot_w * i / max(n - 1, 1)))
        out.append(
            f'<text class="tick" x="{x:.1f}" y="{H - PAD_B + 18}" text-anchor="middle">{esc(label)}</text>'
        )
    return out


def _legend(labels: list[str]) -> str:
    items = "".join(
        f'<span class="key"><i style="background:var(--s{i % SERIES_COLORS})"></i>{esc(l)}</span>'
        for i, l in enumerate(labels)
    )
    return f'<div class="legend">{items}</div>'


def _frame(cid, title, subtitle, body, legend="", height=H) -> str:
    return (
        f'<figure class="chart" id="{esc(cid)}">'
        f"<figcaption><h3>{esc(title)}</h3><p>{esc(subtitle)}</p></figcaption>"
        f"{legend}"
        f'<svg viewBox="0 0 {W} {height}" role="img" aria-label="{esc(title)}">{body}</svg>'
        f"</figure>"
    )


def empty(cid, title, subtitle, reason) -> Chart:
    body = f'<text class="empty" x="{W // 2}" y="{H // 2}" text-anchor="middle">{esc(reason)}</text>'
    return Chart(_frame(cid, title, subtitle, body))


def _fmt_or_none(value, fmt):
    return None if value is None else fmt(value)


def line_chart(cid, title, subtitle, categories, series: list[Series], y_fmt=str) -> Chart:
    """Multi-series line chart over evenly spaced categories (weeks)."""
    values = [v for s in series for _, v in s.points if v is not None]
    if not values:
        return empty(cid, title, subtitle, "no data yet")
    y_max = _nice_max(max(values))
    plot_w, plot_h = W - PAD_L - PAD_R, H - PAD_T - PAD_B
    index = {c: i for i, c in enumerate(categories)}
    n = max(len(categories) - 1, 1)

    def x_at(i: int) -> float:
        return PAD_L + plot_w * i / n

    parts = _axes(categories, y_max, y_fmt, x_slot_center=False)
    parts.append(f'<line class="cursor" y1="{PAD_T}" y2="{PAD_T + plot_h}" x1="0" x2="0"/>')

    lookup: list[list[str | None]] = []
    for si, s in enumerate(series):
        by_cat = {c: v for c, v in s.points}
        lookup.append([_fmt_or_none(by_cat.get(c), y_fmt) for c in categories])
        pts = [
            (x_at(index[c]), PAD_T + plot_h * (1 - v / y_max))
            for c, v in s.points
            if v is not None and c in index
        ]
        if not pts:
            continue
        color = f"var(--s{si % SERIES_COLORS})"
        path = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(pts))
        parts.append(f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2"/>')
        for c, v in s.points:
            if v is None or c not in index:
                continue
            cx, cy = x_at(index[c]), PAD_T + plot_h * (1 - v / y_max)
            parts.append(
                f'<circle class="dot" data-s="{si}" data-i="{index[c]}" '
                f'cx="{cx:.1f}" cy="{cy:.1f}" r="3.5" fill="{color}"/>'
            )

    # Invisible hover bands, one per category, so the whole column is a target
    # rather than requiring the reader to hit a 3px dot.
    slot = plot_w / max(len(categories), 1)
    for i in range(len(categories)):
        parts.append(
            f'<rect class="hit" data-i="{i}" x="{max(x_at(i) - slot / 2, 0):.1f}" y="{PAD_T}" '
            f'width="{slot:.1f}" height="{plot_h}"/>'
        )

    meta = {
        "type": "line",
        "categories": categories,
        "series": [{"label": s.label, "values": lookup[i]} for i, s in enumerate(series)],
    }
    return Chart(_frame(cid, title, subtitle, "".join(parts), _legend([s.label for s in series])), meta)


def stacked_bar_chart(cid, title, subtitle, categories, series: list[Series], y_fmt=str) -> Chart:
    """Stacked bars — for compositions that must be read as a whole."""
    lookup = [{c: v for c, v in s.points} for s in series]
    totals = [sum(t.get(cat) or 0 for t in lookup) for cat in categories]
    if not any(totals):
        return empty(cid, title, subtitle, "no data yet")
    y_max = _nice_max(max(totals))
    plot_w, plot_h = W - PAD_L - PAD_R, H - PAD_T - PAD_B
    slot = plot_w / max(len(categories), 1)
    bar_w = max(slot * 0.7, 1.0)

    parts = _axes(categories, y_max, y_fmt, x_slot_center=True)
    for ci, cat in enumerate(categories):
        base = 0.0
        x = PAD_L + slot * ci + (slot - bar_w) / 2
        for si, table in enumerate(lookup):
            value = table.get(cat) or 0
            if value <= 0:
                continue
            h = plot_h * value / y_max
            y = PAD_T + plot_h * (1 - (base + value) / y_max)
            parts.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h:.1f}" '
                f'fill="var(--s{si % SERIES_COLORS})"><title>{esc(cat)} — '
                f'{esc(series[si].label)}: {esc(y_fmt(value))}</title></rect>'
            )
            base += value
        parts.append(
            f'<rect class="hit" data-i="{ci}" x="{PAD_L + slot * ci:.1f}" y="{PAD_T}" '
            f'width="{slot:.1f}" height="{plot_h}"/>'
        )

    meta = {
        "type": "stack",
        "categories": categories,
        "series": [
            {"label": s.label, "values": [_fmt_or_none(lookup[i].get(c), y_fmt) for c in categories]}
            for i, s in enumerate(series)
        ],
        "totals": [y_fmt(t) for t in totals],
    }
    return Chart(_frame(cid, title, subtitle, "".join(parts), _legend([s.label for s in series])), meta)


def hbar_chart(cid, title, subtitle, rows: list[tuple[str, float]], value_fmt=str,
               note: str | None = None) -> Chart:
    """Horizontal bars — for ranking a handful of named things."""
    if not rows:
        return empty(cid, title, subtitle, "no data yet")
    height = PAD_T + PAD_B + 26 * len(rows)
    label_w = 300
    bar_max = W - label_w - 90
    top = max(v for _, v in rows) or 1
    parts = []
    for i, (label, value) in enumerate(rows):
        y = PAD_T + 26 * i
        w = bar_max * value / top
        shown = label if len(label) <= 44 else label[:41] + "…"
        parts.append(
            f'<text class="rowlabel" x="{label_w - 10}" y="{y + 14}" text-anchor="end">{esc(shown)}</text>'
        )
        parts.append(
            f'<rect x="{label_w}" y="{y + 3}" width="{max(w, 1):.1f}" height="15" rx="2" fill="var(--s0)"/>'
        )
        parts.append(
            f'<text class="tick" x="{label_w + max(w, 1) + 8:.1f}" y="{y + 15}">{esc(value_fmt(value))}</text>'
        )
        parts.append(f'<rect class="hit" data-i="{i}" x="0" y="{y}" width="{W}" height="21"/>')

    meta = {
        "type": "hbar",
        "rows": [{"label": l, "value": value_fmt(v)} for l, v in rows],
        "note": note,
    }
    return Chart(_frame(cid, title, subtitle, "".join(parts), height=height), meta)
