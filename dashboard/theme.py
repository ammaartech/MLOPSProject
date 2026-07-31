"""
The dashboard's visual system, in one place.

`.streamlit/config.toml` carries everything Streamlit can theme natively —
colours, the type scale, radii, the chart palette. This module carries the
rest: the CSS for what config.toml cannot reach, and the small set of
components that keep every tab looking like the same product.

Rules this file exists to enforce
--------------------------------
* **No emoji, anywhere.** Not in headings, tabs, buttons or captions.
* **Three heading levels, no more.** `page()` for the tab title, then
  `section()`, then `sub()`. Calling `st.header` directly reintroduces a
  fourth size, so don't.
* **Status is never colour alone.** `status_tag()` pairs the colour with
  its word, which is what makes it readable in greyscale, on a projector,
  and to a colour-blind reader.
* **Numbers align.** Everything numeric gets tabular figures; a column of
  costs that jitters horizontally is unreadable at a glance.

Series colours come from the validated categorical palette and are handed
out by SLOT, in fixed order — never cycled, never reassigned when a filter
changes the series count. `series()` is the only correct way to get them.
"""

import streamlit as st

# ----------------------------------------------------------------------
# Tokens. These mirror .streamlit/config.toml; they are duplicated here
# only because inline HTML (the bullet bars, the tags) cannot read it.
# ----------------------------------------------------------------------
INK          = "#111418"
INK_SECOND   = "#4a5058"
INK_MUTED    = "#6b7280"
SURFACE      = "#ffffff"
SURFACE_ALT  = "#f4f5f7"
BORDER       = "#e3e6ea"
GRID         = "#eceef1"
ACCENT       = "#1c5cab"

# Status palette — fixed, never themed, never reused as a series colour.
GOOD     = "#0ca30c"
WARNING  = "#fab219"
SERIOUS  = "#ec835a"
CRITICAL = "#d03b3b"

# Categorical slots, in the validated order. Index 0 is slot 1.
SERIES = [
    "#2a78d6",  # blue
    "#eb6834",  # orange
    "#1baf7a",  # aqua
    "#eda100",  # yellow
    "#e87ba4",  # magenta
    "#008300",  # green
    "#4a3aa7",  # violet
    "#e34948",  # red
]


# Two colours that are deliberately NOT categorical slots.
#
# BAND is the uncertainty envelope around a forecast. It belongs to the
# same entity as the forecast line, so giving it its own categorical hue
# would claim a second series exists where there is one — it is a lighter
# step of slot 2, which reads as "the same thing, less certain".
#
# REFERENCE is the allocation line: an annotation the policy drew, not
# something measured. Muted ink keeps it from competing with the data it
# is drawn against.
BAND      = "#f5bfa6"
REFERENCE = "#6b7280"


def series(n):
    """The first `n` categorical colours, in slot order.

    Pass the result straight to `st.line_chart(color=...)`. Assigning by
    slot rather than letting Streamlit cycle is what keeps a series the
    same colour across tabs — and stops a filtered-out series from
    repainting the ones that remain.
    """
    if n > len(SERIES):
        raise ValueError(
            f"{n} series exceeds the {len(SERIES)}-slot palette. A ninth "
            f"colour is never generated — fold the tail into 'other' or "
            f"split the chart."
        )
    return SERIES[:n]


CSS = f"""
<style>
/* ---------------------------------------------------------------- shell */
.block-container {{
    padding-top: 2.2rem;
    padding-bottom: 3rem;
    max-width: 1500px;
}}

/* Streamlit ships an h1-sized gap above the first element; the page title
   component below supplies its own spacing, so reclaim it. */
.block-container > div:first-child {{ margin-top: 0; }}

/* ------------------------------------------------------------ page title */
.rm-title {{
    font-size: 1.5rem;
    font-weight: 600;
    letter-spacing: -0.01em;
    color: {INK};
    margin: 0 0 0.25rem 0;
}}
.rm-subtitle {{
    font-size: 0.875rem;
    line-height: 1.5;
    color: {INK_MUTED};
    margin: 0 0 1.25rem 0;
    max-width: 78ch;
}}
.rm-rule {{
    border: 0;
    border-top: 1px solid {BORDER};
    margin: 1.75rem 0 1.25rem 0;
}}

/* --------------------------------------------------------------- headings */
h2, h3, h4 {{ letter-spacing: -0.005em; }}
h2 {{ margin-top: 0.5rem !important; padding-top: 0 !important; }}
h3 {{ margin-top: 0.25rem !important; padding-top: 0 !important; }}

/* Section label: small, muted, spaced — reads as structure, not content. */
.rm-section {{
    font-size: 0.6875rem;
    font-weight: 600;
    letter-spacing: 0.07em;
    text-transform: uppercase;
    color: {INK_MUTED};
    margin: 1.5rem 0 0.5rem 0;
    padding-bottom: 0.4rem;
    border-bottom: 1px solid {BORDER};
}}
.rm-sub {{
    font-size: 0.9375rem;
    font-weight: 600;
    color: {INK};
    margin: 1.1rem 0 0.35rem 0;
}}
.rm-note {{
    font-size: 0.8125rem;
    line-height: 1.55;
    color: {INK_MUTED};
    margin: 0 0 0.75rem 0;
    max-width: 78ch;
}}

/* ------------------------------------------------------------------ tabs */
.stTabs [data-baseweb="tab-list"] {{
    gap: 0;
    border-bottom: 1px solid {BORDER};
    margin-bottom: 1.5rem;
    overflow-x: auto;
    scrollbar-width: thin;
}}
.stTabs [data-baseweb="tab"] {{
    height: 40px;
    padding: 0 0.95rem;
    font-size: 0.875rem;
    font-weight: 500;
    color: {INK_MUTED};
    background: transparent;
    border-radius: 0;
    white-space: nowrap;
}}
.stTabs [data-baseweb="tab"]:hover {{ color: {INK}; }}
.stTabs [aria-selected="true"] {{ color: {ACCENT} !important; font-weight: 600; }}
.stTabs [data-baseweb="tab-highlight"] {{ background-color: {ACCENT}; height: 2px; }}
.stTabs [data-baseweb="tab-border"] {{ display: none; }}

/* --------------------------------------------------------------- metrics */
[data-testid="stMetric"] {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 0.25rem;
    padding: 0.85rem 1rem;
}}
[data-testid="stMetricLabel"] p {{
    font-size: 0.6875rem !important;
    font-weight: 600;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    color: {INK_MUTED};
}}
[data-testid="stMetricValue"] {{
    font-variant-numeric: tabular-nums;
    color: {INK};
    line-height: 1.2;
}}
[data-testid="stMetricDelta"] {{
    font-size: 0.75rem;
    font-variant-numeric: tabular-nums;
}}

/* -------------------------------------------------------------- captions */
[data-testid="stCaptionContainer"] p {{
    font-size: 0.8125rem;
    line-height: 1.55;
    color: {INK_MUTED};
}}

/* ------------------------------------------------------------- dataframes */
[data-testid="stDataFrame"] {{ font-variant-numeric: tabular-nums; }}
[data-testid="stTable"] {{ font-size: 0.8125rem; }}

/* ---------------------------------------------------------------- sidebar */
[data-testid="stSidebar"] {{ border-right: 1px solid {BORDER}; }}
[data-testid="stSidebar"] .block-container {{ padding-top: 1.5rem; }}
.rm-side-label {{
    font-size: 0.6875rem;
    font-weight: 600;
    letter-spacing: 0.07em;
    text-transform: uppercase;
    color: {INK_MUTED};
    margin: 1.25rem 0 0.4rem 0;
}}
.rm-side-kv {{
    display: flex;
    justify-content: space-between;
    gap: 0.75rem;
    font-size: 0.75rem;
    line-height: 1.7;
    color: {INK_SECOND};
}}
.rm-side-kv span:last-child {{
    font-variant-numeric: tabular-nums;
    color: {INK};
    text-align: right;
    word-break: break-all;
}}

/* ----------------------------------------------------------------- alerts */
[data-testid="stAlert"] {{
    border-radius: 0.25rem;
    font-size: 0.8125rem;
    padding: 0.7rem 0.9rem;
}}
[data-testid="stAlert"] p {{ font-size: 0.8125rem; line-height: 1.55; }}

/* -------------------------------------------------------------- expanders */
[data-testid="stExpander"] summary p {{ font-size: 0.875rem; font-weight: 500; }}

/* --------------------------------------------------------------- controls */
.stButton button {{ font-size: 0.8125rem; font-weight: 500; }}
label p {{ font-size: 0.8125rem !important; font-weight: 500; }}

/* ------------------------------------------------------------ status tags */
.rm-tag {{
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    font-size: 0.75rem;
    font-weight: 600;
    letter-spacing: 0.02em;
    color: {INK};
    white-space: nowrap;
}}
.rm-tag i {{
    width: 8px; height: 8px;
    border-radius: 2px;
    flex: none;
    display: inline-block;
}}

/* --------------------------------------------------------- bullet gauges */
/* A bullet chart: measure bar, comparative marker, threshold tick. It
   replaced a set of 3D cylinders — those encoded one number in a shape
   whose area, gradient and elliptical cap all varied, so nothing on
   screen was actually comparable between resources. */
.rm-bullet-wrap {{
    border: 1px solid {BORDER};
    border-radius: 0.25rem;
    padding: 1.1rem 1.25rem 0.85rem 1.25rem;
    background: {SURFACE};
}}
.rm-bullet + .rm-bullet {{ margin-top: 1.35rem; }}
.rm-bullet-head {{
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 1rem;
    margin-bottom: 0.45rem;
}}
.rm-bullet-name {{ font-size: 0.875rem; font-weight: 600; color: {INK}; }}
.rm-bullet-cap {{
    font-size: 0.75rem;
    color: {INK_MUTED};
    font-variant-numeric: tabular-nums;
}}
.rm-bullet-track {{
    position: relative;
    height: 22px;
    background: {SURFACE_ALT};
    border-radius: 2px;
    overflow: hidden;
}}
.rm-bullet-fill {{
    position: absolute;
    left: 0; top: 0; bottom: 0;
    border-radius: 2px 0 0 2px;
}}
/* The threshold is a rule across the track, not a colour change — the
   bar keeps one colour so its length stays the only length signal. */
.rm-bullet-threshold {{
    position: absolute;
    top: -2px; bottom: -2px;
    width: 2px;
    background: {CRITICAL};
}}
.rm-bullet-forecast {{
    position: absolute;
    top: 2px; bottom: 2px;
    width: 2px;
    background: {INK};
}}
.rm-bullet-scale {{
    position: relative;
    height: 1.1rem;
    margin-top: 0.2rem;
    font-size: 0.6875rem;
    color: {INK_MUTED};
    font-variant-numeric: tabular-nums;
}}
.rm-bullet-scale span {{ position: absolute; transform: translateX(-50%); }}
.rm-bullet-scale span:first-child {{ transform: none; }}
.rm-bullet-scale span:last-child {{ transform: translateX(-100%); }}
.rm-bullet-facts {{
    display: flex;
    flex-wrap: wrap;
    gap: 1.5rem;
    margin-top: 0.5rem;
    font-size: 0.75rem;
    color: {INK_SECOND};
}}
.rm-bullet-facts b {{
    font-weight: 600;
    color: {INK};
    font-variant-numeric: tabular-nums;
}}

/* ------------------------------------------------- capacity cylinders */
/* Allocation units, drawn as vertical vessels that fill from the bottom.
   The earlier cylinders were withdrawn for a reason worth restating,
   because it constrains everything below: they were 3D, gradient-filled,
   and each was sized against its OWN capacity, so a full CPU cylinder and
   a full disk cylinder looked identical while meaning nothing alike.

   The fix is that geometry is FIXED. Every cylinder is the same width and
   the same height, and the only thing that varies is the height of the
   fill, always on the shared 0-100% scale. No gradient, no perspective,
   no varying cap — none of which carry data. Native units appear as text
   beneath, where a number belongs. */
.rm-cyl-wrap {{
    display: flex;
    flex-wrap: wrap;
    gap: 1.75rem;
    align-items: flex-start;
    border: 1px solid {BORDER};
    border-radius: 0.25rem;
    padding: 1.35rem 1.5rem 1.1rem 1.5rem;
    background: {SURFACE};
}}
.rm-cyl {{ width: 104px; text-align: center; }}
.rm-cyl-track {{
    position: relative;
    width: 62px;
    height: 186px;
    margin: 0 auto;
    background: {SURFACE_ALT};
    border: 1px solid {BORDER};
    /* An elliptical radius on the ends reads as a vessel without adding a
       drawn 3D cap. It is identical on every cylinder, so it encodes
       nothing and cannot mislead. */
    border-radius: 31px / 16px;
    overflow: hidden;
}}
.rm-cyl-fill {{
    position: absolute;
    left: 0; right: 0; bottom: 0;
    background: {SERIES[0]};
}}
/* The threshold is drawn across the full width and slightly proud of the
   track, so it reads as a level line rather than part of the fill. */
.rm-cyl-threshold {{
    position: absolute;
    left: 0; right: 0;
    height: 2px;
    background: {CRITICAL};
}}
.rm-cyl-forecast {{
    position: absolute;
    left: 0; right: 0;
    height: 2px;
    background: {INK};
}}
/* The recommended addition is a proposal, not a measurement. Dashed and
   unfilled is the whole point: nothing in it has been observed. */
.rm-cyl-add .rm-cyl-track {{
    background: transparent;
    border: 1.5px dashed {ACCENT};
}}
.rm-cyl-add .rm-cyl-fill {{ background: {ACCENT}; opacity: 0.16; }}
.rm-cyl-name {{
    font-size: 0.8125rem;
    font-weight: 600;
    color: {INK};
    margin-top: 0.6rem;
}}
.rm-cyl-add .rm-cyl-name {{ color: {ACCENT}; }}
.rm-cyl-meta {{
    font-size: 0.6875rem;
    line-height: 1.55;
    color: {INK_MUTED};
    font-variant-numeric: tabular-nums;
    margin-top: 0.15rem;
}}
.rm-cyl-meta b {{ font-weight: 600; color: {INK}; }}
.rm-cyl-scale {{
    position: relative;
    width: 2.6rem;
    height: 186px;
    font-size: 0.6875rem;
    color: {INK_MUTED};
    font-variant-numeric: tabular-nums;
}}
.rm-cyl-scale span {{
    position: absolute;
    right: 0;
    transform: translateY(50%);
}}

/* ------------------------------------------------------------ key/value */
.rm-kv {{
    display: grid;
    grid-template-columns: minmax(9rem, auto) 1fr;
    gap: 0.3rem 1.25rem;
    font-size: 0.8125rem;
    line-height: 1.6;
    margin: 0.35rem 0 0.75rem 0;
}}
.rm-kv dt {{ color: {INK_MUTED}; }}
.rm-kv dd {{
    margin: 0;
    color: {INK};
    font-variant-numeric: tabular-nums;
    word-break: break-word;
}}

/* --------------------------------------------------------------- legend */
.rm-legend {{
    display: flex;
    flex-wrap: wrap;
    gap: 1.25rem;
    font-size: 0.75rem;
    color: {INK_SECOND};
    margin-top: 0.6rem;
}}
.rm-legend span {{ display: inline-flex; align-items: center; gap: 0.4rem; }}
.rm-legend i {{ width: 12px; height: 3px; border-radius: 1px; display: inline-block; }}
</style>
"""


def apply():
    """Inject the stylesheet. Call once, immediately after set_page_config."""
    st.markdown(CSS, unsafe_allow_html=True)


# ----------------------------------------------------------------------
# The three heading levels. Nothing else.
# ----------------------------------------------------------------------
def page(title, subtitle=None):
    """Level 1 — the tab's own title. Exactly one per tab."""
    st.markdown(f'<div class="rm-title">{title}</div>', unsafe_allow_html=True)
    if subtitle:
        st.markdown(f'<div class="rm-subtitle">{subtitle}</div>',
                    unsafe_allow_html=True)


def section(label):
    """Level 2 — a rule-and-label divider between blocks of a tab."""
    st.markdown(f'<div class="rm-section">{label}</div>', unsafe_allow_html=True)


def sub(label, note=None):
    """Level 3 — a titled chart or table inside a section."""
    st.markdown(f'<div class="rm-sub">{label}</div>', unsafe_allow_html=True)
    if note:
        st.markdown(f'<div class="rm-note">{note}</div>', unsafe_allow_html=True)


def note(text):
    """Explanatory prose. Narrower measure than the page, muted."""
    st.markdown(f'<div class="rm-note">{text}</div>', unsafe_allow_html=True)


def rule():
    st.markdown('<hr class="rm-rule">', unsafe_allow_html=True)


# ----------------------------------------------------------------------
# Components
# ----------------------------------------------------------------------
_STATUS_COLOURS = {
    "PASS": GOOD, "OK": GOOD, "GOOD": GOOD, "MET": GOOD, "COMPLIANT": GOOD,
    "WARN": WARNING, "WARNING": WARNING,
    "SERIOUS": SERIOUS, "REJECTED": SERIOUS,
    "FAIL": CRITICAL, "FAILED": CRITICAL, "ERROR": CRITICAL, "BREACH": CRITICAL,
    "INFO": INK_MUTED,
}


def status_tag(text, level=None):
    """A colour chip and its word, together.

    The word is not optional. Colour alone fails for a colour-blind
    reader, a greyscale print and a washed-out projector — all three of
    which this dashboard has to survive.
    """
    key = (level or text).strip().upper()
    colour = _STATUS_COLOURS.get(key, INK_MUTED)
    return f'<span class="rm-tag"><i style="background:{colour}"></i>{text}</span>'


def kv(pairs):
    """A definition list. Replaces the bulleted `**key**: value` blocks —
    same content, but the values line up in a column."""
    body = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in pairs)
    st.markdown(f'<dl class="rm-kv">{body}</dl>', unsafe_allow_html=True)


def legend(items):
    """items: [(colour, label), ...]"""
    body = "".join(
        f'<span><i style="background:{c}"></i>{label}</span>'
        for c, label in items
    )
    st.markdown(f'<div class="rm-legend">{body}</div>', unsafe_allow_html=True)


def bullet(rows):
    """A bullet chart per resource, all on one 0-100% scale.

    rows: [{name, capacity_label, current, forecast, threshold, facts}]
    where the three numbers are percentages and `facts` is [(label, value)].

    One shared scale is the point. The cylinders this replaced sized each
    resource against its own capacity, so a full CPU cylinder and a full
    disk cylinder looked identical while meaning completely different
    things.
    """
    def pct(v):
        return max(0.0, min(100.0, float(v)))

    # Emitted as one unbroken line per bullet, like every other helper here.
    # A pretty-printed block does not survive: st.markdown still runs the
    # string through a Markdown parser before the HTML reaches the page, a
    # blank line closes the raw-HTML block, and the next line — indented
    # four spaces or more — becomes an indented CODE block. The symptom is
    # the first bullet rendering and every one after it printing its own
    # source. Keep this free of newlines and leading whitespace.
    blocks = []
    for r in rows:
        facts = "".join(
            f"<span>{label} <b>{value}</b></span>" for label, value in r["facts"]
        )
        scale = "".join(
            f'<span style="left:{tick}%">{tick}%</span>'
            for tick in (0, 25, 50, 75, 100)
        )
        blocks.append(
            '<div class="rm-bullet">'
            '<div class="rm-bullet-head">'
            f'<span class="rm-bullet-name">{r["name"]}</span>'
            f'<span class="rm-bullet-cap">{r["capacity_label"]}</span>'
            '</div>'
            '<div class="rm-bullet-track">'
            f'<div class="rm-bullet-fill" style="width:{pct(r["current"])}%;'
            f'background:{SERIES[0]}"></div>'
            f'<div class="rm-bullet-threshold" style="left:{pct(r["threshold"])}%"></div>'
            f'<div class="rm-bullet-forecast" style="left:{pct(r["forecast"])}%"></div>'
            '</div>'
            f'<div class="rm-bullet-scale">{scale}</div>'
            f'<div class="rm-bullet-facts">{facts}</div>'
            '</div>'
        )

    st.markdown(
        f'<div class="rm-bullet-wrap">{"".join(blocks)}</div>',
        unsafe_allow_html=True,
    )


def cylinders(units):
    """Allocation units as vertical cylinders on one shared 0-100% scale.

    units: [{name, fill, threshold, forecast, lines, addition}] where
    `fill`, `threshold` and `forecast` are percentages, `lines` is a list
    of caption strings, and `addition` marks a proposed unit rather than a
    measured one. `forecast` may be None, which omits the marker.

    Everything except the fill height is identical between cylinders, so
    two fills at the same height mean the same fraction of their own
    resource — which is the only comparison a shared scale can support.
    Native units belong in `lines`, as text.

    Emitted as one unbroken string. `st.markdown` runs this through a
    Markdown parser before the HTML reaches the page: a blank line closes
    the raw-HTML block and any line indented four spaces becomes a code
    block, so the first cylinder would render and the rest would print
    their own source. Keep this free of newlines and leading whitespace.
    """
    def pct(value):
        return max(0.0, min(100.0, float(value)))

    scale = "".join(
        f'<span style="bottom:{tick}%">{tick}%</span>'
        for tick in (0, 25, 50, 75, 100)
    )
    blocks = [f'<div class="rm-cyl-scale">{scale}</div>']

    for unit in units:
        marker = ""
        if unit.get("forecast") is not None:
            marker = (f'<div class="rm-cyl-forecast" '
                      f'style="bottom:{pct(unit["forecast"])}%"></div>')
        threshold = ""
        if unit.get("threshold") is not None:
            threshold = (f'<div class="rm-cyl-threshold" '
                         f'style="bottom:{pct(unit["threshold"])}%"></div>')

        captions = "".join(f"<div>{line}</div>" for line in unit.get("lines", []))
        blocks.append(
            f'<div class="rm-cyl{" rm-cyl-add" if unit.get("addition") else ""}">'
            '<div class="rm-cyl-track">'
            f'<div class="rm-cyl-fill" style="height:{pct(unit["fill"])}%"></div>'
            f'{threshold}{marker}'
            '</div>'
            f'<div class="rm-cyl-name">{unit["name"]}</div>'
            f'<div class="rm-cyl-meta">{captions}</div>'
            '</div>'
        )

    st.markdown(f'<div class="rm-cyl-wrap">{"".join(blocks)}</div>',
                unsafe_allow_html=True)


# Row tints, one step off the surface. Light enough that black body text
# still clears contrast on top of them.
TINT_GOOD = "#f0f8f0"
TINT_WARN = "#fdf6e5"
TINT_BAD  = "#fbeeee"


def status_frame(frame, column="status"):
    """Tint a table's rows by a status column, in light-surface tints.

    The tint is redundant with the column it reads, deliberately: the word
    carries the meaning and the colour only speeds up the scan.
    """
    tints = {
        "PASS": TINT_GOOD, "OK": TINT_GOOD,
        "WARN": TINT_WARN, "WARNING": TINT_WARN,
        "FAIL": TINT_BAD, "ERROR": TINT_BAD,
    }

    def paint(row):
        shade = tints.get(str(row.get(column, "")).strip().upper(), "")
        return [f"background-color: {shade}" if shade else ""] * len(row)

    return frame.style.apply(paint, axis=1)
