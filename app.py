"""F1 Strategy Dashboard — Streamlit entry point.

An interactive dashboard for exploring Formula 1 race strategy from FastF1 timing
data: lap-time pace, tire-stint strategy, pit-stop performance, and head-to-head
undercut/overcut analysis.

Run locally with::

    streamlit run app.py

Layout philosophy
-----------------
The page is built from custom HTML/CSS blocks (rendered via ``st.markdown`` with
``unsafe_allow_html``) so it reads like a designed page rather than a stack of
default Streamlit widgets. Native widgets are used *only* for real interactive
inputs — the sidebar selectors, the driver multiselect, and the nav buttons.
Everything structural (header, stat cards, section eyebrows, content cards) is
hand-written markup.

View switching replaces ``st.tabs`` entirely: the top-right nav is a row of keyed
buttons that set ``st.session_state["view"]`` and rerun, styled via their
``st-key-*`` classes to look like plain text links with an underline on the active
one. Using buttons (not anchor links) keeps the loaded session in ``session_state``
alive across view changes — no reload, no re-fetch.

Reliability notes:
* All data loading lives in ``utils.data_loader`` and is wrapped in try/except so
  the UI only ever sees clean ``st.error`` messages — never a raw traceback.
* The parsed FastF1 Session is cached with ``st.cache_resource``, so switching
  views never re-fetches.
"""

from __future__ import annotations

import streamlit as st

from utils import charts
from utils.data_loader import (
    SESSION_TYPES,
    DataLoadError,
    available_years,
    delta_summary,
    driver_list,
    enable_cache,
    event_names,
    lap_time_delta,
    lap_time_summary,
    lap_times,
    load_session,
    pit_stop_summary,
    pit_stops,
    stint_summary,
    tire_strategy_summary,
)

st.set_page_config(
    page_title="F1 Strategy Dashboard",
    page_icon=None,
    layout="wide",
)

# Point FastF1 at the gitignored on-disk cache before any data is requested.
enable_cache()

# The four views, in nav order: (slug, nav label). The slug is the session_state
# value and the eyebrow name; it maps to a render function in VIEW_RENDERERS.
VIEWS: list[tuple[str, str]] = [
    ("lap-times", "Lap Times"),
    ("tire-strategy", "Tire Strategy"),
    ("pit-stops", "Pit Stops"),
    ("undercut-overcut", "Undercut-Overcut"),
]


# ---------------------------------------------------------------------------
# Theme / styling
#
# Palette: near-black page (#0a0a0a), card surfaces (#141414), hairline borders
# (#262626), light text with muted #8a8a8a secondary. The ONLY red anywhere is
# the "F1" characters in the wordmark and the soft tire compound in the charts —
# plus the deliberate red error-state styling. No other reds, no extra gradients.
# ---------------------------------------------------------------------------
#
# NOTE: this MUST be a single <style> block. Streamlit renders st.markdown text
# through a Markdown parser first; a leading <link> tag (or anything before
# <style>) starts an HTML block that Markdown ends at the first blank line —
# which lands inside the CSS and dumps the rest as literal text on the page. A
# lone <style> block is passed through verbatim until </style>, so the fonts are
# loaded via @import inside it rather than with <link> tags.
_HEAD = """
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500;600&family=Instrument+Serif:ital@1&display=swap');

  /* ---- Hide Streamlit default chrome ---- */
  #MainMenu { visibility: hidden; }
  footer { visibility: hidden; }
  header[data-testid="stHeader"] { display: none; }
  [data-testid="stToolbar"] { display: none; }
  [data-testid="stDecoration"] { display: none; }

  /* ---- Base typography / page ---- */
  html, body, .stApp, [class*="css"] {
    font-family: 'Inter', system-ui, -apple-system, 'Segoe UI', sans-serif;
  }
  .stApp { background: #0a0a0a; }
  .block-container { padding-top: 2.25rem; padding-bottom: 4rem; max-width: 1180px; }
  .stApp p, .stApp li { color: #8a8a8a; line-height: 1.7; }
  .stApp a { color: #d4d4d4; }

  /* ---- Header wordmark (dot/ring + text + underscore cursor) ---- */
  .app-header { display: flex; align-items: center; gap: 0.6rem; }
  .wm-dot {
    width: 15px; height: 15px; border: 1.5px solid #6b6b6b; border-radius: 50%;
    display: inline-flex; align-items: center; justify-content: center; flex: none;
  }
  .wm-dot::after { content: ""; width: 5px; height: 5px; background: #e5e5e5; border-radius: 50%; }
  .wordmark {
    font-family: 'JetBrains Mono', ui-monospace, Menlo, Consolas, monospace;
    font-size: 1rem; font-weight: 600; letter-spacing: 0.01em; color: #f5f5f5;
  }
  .wordmark .f1 { color: #DA291C; }               /* the one sanctioned red accent */
  .wordmark .cursor { color: #737373; animation: blink 1.15s steps(1) infinite; }
  @keyframes blink { 50% { opacity: 0; } }

  /* ---- Intro line with the single serif-italic accent moment ---- */
  .intro { font-size: 0.98rem; color: #8a8a8a; margin: 1.4rem 0 0.2rem; max-width: 620px; }
  .intro .accent {
    font-family: 'Instrument Serif', Georgia, serif; font-style: italic;
    font-size: 1.28rem; color: #ededed; letter-spacing: 0.01em;
  }
  .top-divider { border-bottom: 1px solid #1c1c1c; margin: 1.4rem 0 2rem; }

  /* ---- Nav: keyed buttons restyled as plain text links ---- */
  div[class*="st-key-nav"] button {
    background: transparent !important; border: none !important; box-shadow: none !important;
    color: #8a8a8a !important; padding: 0.15rem 0 !important; min-height: 0 !important;
    border-radius: 0 !important; white-space: nowrap;
    font-family: 'JetBrains Mono', ui-monospace, Menlo, monospace !important;
    font-size: 0.8rem !important; font-weight: 500 !important; letter-spacing: 0.02em;
    transition: color 120ms ease;
  }
  div[class*="st-key-nav"] button:hover { color: #f5f5f5 !important; }
  div[class*="st-key-nav"] button:focus { box-shadow: none !important; }
  /* Active view: full-light text + a thin underline (no pill, no fill). */
  div[class*="st-key-navon"] button {
    color: #f5f5f5 !important; border-bottom: 2px solid #e5e5e5 !important;
  }

  /* ---- Stat cards ---- */
  .stat-row { display: flex; gap: 1rem; margin: 0 0 3rem; }
  .stat-card {
    flex: 1 1 0; border: 1px solid #262626; border-radius: 12px;
    background: linear-gradient(180deg, #161616 0%, #121212 100%);
    padding: 1.5rem 1.5rem;
  }
  .stat-value {
    font-family: 'JetBrains Mono', ui-monospace, Menlo, monospace;
    font-size: 2rem; font-weight: 600; color: #f5f5f5; line-height: 1.1;
  }
  .stat-label {
    font-size: 0.72rem; color: #737373; margin-top: 0.55rem;
    text-transform: uppercase; letter-spacing: 0.1em;
  }

  /* ---- Section eyebrow ("_ /name _") + heading ---- */
  .eyebrow {
    font-family: 'JetBrains Mono', ui-monospace, Menlo, monospace;
    text-transform: lowercase; letter-spacing: 0.16em; font-size: 0.74rem;
    color: #737373; margin: 0 0 0.55rem;
  }
  .section-title { font-size: 1.5rem; font-weight: 600; color: #f5f5f5; margin: 0 0 0.35rem; }
  .section-sub { font-size: 0.92rem; color: #8a8a8a; margin: 0 0 1.15rem; }

  /* ---- Content cards (st.container(border=True)) ---- */
  [data-testid="stVerticalBlockBorderWrapper"] {
    background: #141414; border: 1px solid #262626 !important;
    border-radius: 12px; padding: 1.6rem 1.6rem; margin-bottom: 3.5rem;
  }
  .card-title {
    font-size: 0.9rem; font-weight: 600; color: #ededed; letter-spacing: 0.01em;
    margin: 0 0 1rem;
  }

  /* ---- Per-chart "what it shows" + dynamic summary ---- */
  .chart-what { font-size: 0.9rem; color: #8a8a8a; line-height: 1.65; margin: 1.2rem 0 1rem; }
  .chart-summary {
    border: 1px solid #1f1f1f; background: #101010; border-radius: 10px;
    padding: 0.9rem 1.15rem;
  }
  .chart-summary-head {
    font-family: 'JetBrains Mono', ui-monospace, Menlo, monospace;
    text-transform: uppercase; letter-spacing: 0.13em; font-size: 0.66rem;
    color: #737373; margin: 0 0 0.55rem;
  }
  .chart-summary ul { margin: 0; padding-left: 1.15rem; }
  .chart-summary li { color: #c4c4c4; font-size: 0.9rem; line-height: 1.7; margin-bottom: 0.15rem; }

  /* ---- Buttons (sidebar load etc.): plain bordered chips, no fill ---- */
  .stButton > button {
    border: 1px solid #262626; border-radius: 8px; background: #141414;
    color: #f5f5f5; font-weight: 500;
  }
  .stButton > button:hover { border-color: #404040; background: #1c1c1c; color: #ffffff; }
  .stButton > button:disabled { color: #4d4d4d; border-color: #1c1c1c; }

  /* ---- Sidebar: strip default chrome, add a right border ---- */
  [data-testid="stSidebar"] { background: #0a0a0a; border-right: 1px solid #262626; }
  [data-testid="stSidebar"] > div { background: #0a0a0a; }
  .sidebar-title {
    font-family: 'JetBrains Mono', ui-monospace, Menlo, monospace;
    font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.14em;
    color: #8a8a8a; margin: 0.25rem 0 1.25rem;
  }
  /* Small gray uppercase field labels above every selector. */
  [data-testid="stSidebar"] label, .stMultiSelect label, .stSelectbox label {
    font-family: 'JetBrains Mono', ui-monospace, Menlo, monospace;
    font-size: 0.68rem !important; text-transform: uppercase; letter-spacing: 0.1em;
    color: #737373 !important; font-weight: 500;
  }

  /* ---- Alerts: info/success neutralized to gray; errors keep a red tint ---- */
  [data-testid="stAlert"] {
    background: #141414; border: 1px solid #262626; border-radius: 10px; color: #d4d4d4;
  }
  [data-testid="stAlert"] > div { background: transparent !important; border: none !important; }
  [data-testid="stAlert"] svg { color: #a3a3a3; fill: #a3a3a3; }
  /* Error states stay visually distinct — a red-tinted border/text/icon. Red here
     is a status signal, not a brand accent. `[data-testid*="Error"]` matches
     Streamlit's per-kind content testid; if absent it falls back to gray above. */
  [data-testid="stAlert"]:has([data-testid*="Error"]) {
    background: rgba(218, 41, 28, 0.08); border-color: rgba(218, 41, 28, 0.55); color: #f3b0ab;
  }
  [data-testid="stAlert"]:has([data-testid*="Error"]) svg { color: #DA291C; fill: #DA291C; }

  /* ---- Dataframe: match the card border ---- */
  [data-testid="stDataFrame"] { border: 1px solid #262626; border-radius: 10px; }
</style>
"""


def inject_head() -> None:
    """Load fonts + the site CSS and hide Streamlit's default chrome (once/rerun)."""
    st.markdown(_HEAD, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Header, stats, and section helpers (all custom markup)
# ---------------------------------------------------------------------------
def render_topbar(active_view: str) -> None:
    """Top bar: dot+wordmark on the left, the text-link view nav on the right.

    The nav is a row of keyed buttons; the active one carries a ``navon`` key so
    its ``st-key-*`` class gets the underline styling. Clicking sets the view and
    reruns (session_state is preserved — no reload).
    """
    left, right = st.columns([1.5, 2.5], vertical_alignment="center")
    with left:
        st.markdown(
            '<div class="app-header"><span class="wm-dot"></span>'
            '<span class="wordmark"><span class="f1">F1</span> Strategy Dashboard'
            '<span class="cursor">_</span></span></div>',
            unsafe_allow_html=True,
        )
    with right:
        nav_cols = st.columns(len(VIEWS), gap="medium")
        for col, (slug, label) in zip(nav_cols, VIEWS):
            with col:
                key = f"navon-{slug}" if slug == active_view else f"nav-{slug}"
                if st.button(label, key=key):
                    st.session_state["view"] = slug
                    st.rerun()


def render_intro() -> None:
    """One-line intro carrying the single serif-italic accent phrase."""
    st.markdown(
        '<p class="intro">Formula 1 race strategy, '
        '<span class="accent">read at a glance</span> — pace, tires, and pit calls '
        "from live FastF1 timing data.</p>"
        '<div class="top-divider"></div>',
        unsafe_allow_html=True,
    )


def _session_stats(session) -> list[tuple[str, str]]:
    """Build the four stat-card (value, label) pairs.

    Dynamic once a session is loaded (drivers, laps, pit stops, season); sensible
    dashes before then.
    """
    if session is None:
        return [
            ("—", "Drivers"),
            ("—", "Laps"),
            ("—", "Pit stops"),
            ("—", "Season"),
        ]

    drivers = len(driver_list(session))
    try:
        laps = int(session.laps["LapNumber"].max())
    except Exception:  # noqa: BLE001 — a malformed lap table just shows 0.
        laps = 0
    stops = len(pit_stops(session))
    year = str(st.session_state.get("load_year", "—"))
    return [
        (str(drivers), "Drivers"),
        (str(laps), "Laps"),
        (str(stops), "Pit stops"),
        (year, "Season"),
    ]


def render_stat_row(session) -> None:
    """Render the four equal-width stat cards as a single custom HTML block."""
    cards = "".join(
        f'<div class="stat-card"><div class="stat-value">{value}</div>'
        f'<div class="stat-label">{label}</div></div>'
        for value, label in _session_stats(session)
    )
    st.markdown(f'<div class="stat-row">{cards}</div>', unsafe_allow_html=True)


def section_header(slug: str, title: str, subtitle: str = "") -> None:
    """Eyebrow label ("_ /slug _") above a section title, with optional subtitle."""
    sub = f'<div class="section-sub">{subtitle}</div>' if subtitle else ""
    st.markdown(
        f'<div class="eyebrow">_ /{slug} _</div>'
        f'<div class="section-title">{title}</div>{sub}',
        unsafe_allow_html=True,
    )


def card_title(text: str) -> None:
    """Small bold card header (used inside content cards, not st.header)."""
    st.markdown(f'<div class="card-title">{text}</div>', unsafe_allow_html=True)


# Plain-terms "what this chart shows" copy, paired with a dynamic summary that's
# computed from the data currently plotted (see utils.data_loader.*_summary).
_CHART_WHAT = {
    "lap-times": (
        "Each line is one driver's lap time on every lap — lower is faster. Watch "
        "for spikes on pit laps, the drop onto fresh tires afterwards, and the "
        "gradual climb as tires wear."
    ),
    "tire-strategy": (
        "Each row is a driver and each colored bar is a stint on one set of tires "
        "between pit stops. Bar length is how many laps that set lasted; the color "
        "is the compound."
    ),
    "pit-stops": (
        "Each bar is a single pit stop's total time in the pit lane — shorter is "
        "better. The table below lists every stop and the lap it happened on."
    ),
    "undercut-overcut": (
        "Each bar is how much faster or slower Driver A was than Driver B on that "
        "lap. Green (below the line) means A gained time; red (above) means A lost "
        "it — the swing right after a stop is where an undercut pays off."
    ),
}


def chart_notes(view: str, facts: list[str]) -> None:
    """Render the plain-terms explanation and a dynamic summary under a chart."""
    st.markdown(
        f'<div class="chart-what">{_CHART_WHAT[view]}</div>', unsafe_allow_html=True
    )
    if facts:
        items = "".join(f"<li>{fact}</li>" for fact in facts)
        st.markdown(
            '<div class="chart-summary"><div class="chart-summary-head">'
            f"Summary of this chart</div><ul>{items}</ul></div>",
            unsafe_allow_html=True,
        )


# ---------------------------------------------------------------------------
# Sidebar + session loading (functional logic unchanged)
# ---------------------------------------------------------------------------
def render_sidebar() -> None:
    """Sidebar controls; on button press, load the chosen session into state."""
    st.sidebar.markdown(
        '<div class="sidebar-title">Session</div>', unsafe_allow_html=True
    )

    year = st.sidebar.selectbox("Year", available_years())

    # The race list depends on the season; loading it can fail, so guard it.
    try:
        races = event_names(year)
    except DataLoadError as err:
        st.sidebar.error(str(err))
        races = []

    race = st.sidebar.selectbox("Race", races) if races else None
    session_label = st.sidebar.selectbox("Session type", list(SESSION_TYPES.keys()))

    if st.sidebar.button("Load session", disabled=not race, use_container_width=True):
        _load_into_state(year, race, session_label)

    loaded = st.session_state.get("loaded_meta")
    if loaded:
        st.sidebar.success(f"Loaded: {loaded}")


def _load_into_state(year: int, race: str, session_label: str) -> None:
    """Load a session, storing it (or a clean error) in ``st.session_state``."""
    with st.spinner(f"Loading {session_label} — {race} {year}…"):
        try:
            session = load_session(year, race, session_label)
        except DataLoadError as err:
            for key in ("session", "loaded_meta", "load_year"):
                st.session_state.pop(key, None)
            st.session_state["load_error"] = str(err)
            return

    st.session_state["session"] = session
    st.session_state["loaded_meta"] = f"{race} {year} — {session_label}"
    st.session_state["load_year"] = year
    st.session_state.pop("load_error", None)


# ---------------------------------------------------------------------------
# Views (data logic unchanged — only wrapped in the new card/eyebrow structure)
# ---------------------------------------------------------------------------
# Preferred default drivers for the pickers, in priority order (Hamilton,
# Verstappen). Falls back to whoever's available when they aren't in a session.
_PREFERRED_DEFAULT_DRIVERS = ["HAM", "VER"]


def _default_drivers(drivers: list[str], count: int = 2) -> list[str]:
    """Pick ``count`` default drivers, preferring HAM and VER when present.

    Any preferred driver missing from this session (e.g. a season they didn't
    race) is simply skipped, and the selection is padded with the first available
    drivers so the pickers always have a sensible, distinct default.
    """
    chosen = [d for d in _PREFERRED_DEFAULT_DRIVERS if d in drivers]
    for driver in drivers:
        if len(chosen) >= count:
            break
        if driver not in chosen:
            chosen.append(driver)
    return chosen[:count]


def render_lap_times(session) -> None:
    """Lap-time line chart with a driver multiselect."""
    section_header(
        "lap-times",
        "Lap Times",
        "Lap time versus lap number for the drivers you select.",
    )
    drivers = driver_list(session)
    if not drivers:
        st.info("No drivers with lap data in this session.")
        return

    with st.container(border=True):
        card_title("Lap-time trace")
        default = _default_drivers(drivers, 2)
        chosen = st.multiselect(
            "Drivers", drivers, default=default, key="laptime_drivers"
        )
        if not chosen:
            st.info("Select at least one driver to plot lap times.")
            return
        st.plotly_chart(
            charts.lap_time_chart(lap_times(session, chosen)),
            use_container_width=True,
        )
        chart_notes("lap-times", lap_time_summary(session, chosen))


def render_tire_strategy(session) -> None:
    """Gantt-style tire-stint chart, colored by compound."""
    section_header(
        "tire-strategy",
        "Tire Strategy",
        "One bar per stint, colored by compound, across the whole race.",
    )
    df = stint_summary(session)
    if df.empty:
        st.info("No tire-stint data available for this session.")
        return

    with st.container(border=True):
        card_title("Stint timeline")
        st.plotly_chart(charts.tire_strategy_chart(df), use_container_width=True)
        chart_notes("tire-strategy", tire_strategy_summary(session))


def render_pit_stops(session) -> None:
    """Pit-stop duration bar chart plus a data table."""
    section_header(
        "pit-stops",
        "Pit Stops",
        "Pit-lane time per stop, derived from in-/out-lap timestamps.",
    )
    df = pit_stops(session)
    if df.empty:
        st.info("No pit stops were detected in this session (e.g. qualifying).")
        return

    with st.container(border=True):
        card_title("Pit-stop durations")
        st.plotly_chart(charts.pit_stop_chart(df), use_container_width=True)
        st.dataframe(
            df.rename(
                columns={
                    "StopNumber": "Stop #",
                    "Lap": "In-lap",
                    "PitDurationSeconds": "Pit-lane time (s)",
                }
            ),
            use_container_width=True,
            hide_index=True,
        )
        chart_notes("pit-stops", pit_stop_summary(session))
        st.caption(
            "Pit-lane time is derived as PitOutTime(next lap) − PitInTime(in-lap): "
            "the total time spent in the pit lane, not just stationary service time."
        )


def render_undercut_overcut(session) -> None:
    """Two-driver lap-time delta for undercut/overcut analysis."""
    section_header(
        "undercut-overcut",
        "Undercut / Overcut",
        "Lap-by-lap time delta between two drivers, around a zero reference line.",
    )
    drivers = driver_list(session)
    if len(drivers) < 2:
        st.info("Need at least two drivers with lap data to compare.")
        return

    with st.container(border=True):
        card_title("Lap-time delta")
        defaults = _default_drivers(drivers, 2)
        col_a, col_b = st.columns(2)
        driver_a = col_a.selectbox(
            "Driver A", drivers, index=drivers.index(defaults[0]), key="delta_a"
        )
        driver_b = col_b.selectbox(
            "Driver B", drivers, index=drivers.index(defaults[1]), key="delta_b"
        )

        if driver_a == driver_b:
            st.info("Pick two different drivers to compare.")
            return

        df = lap_time_delta(session, driver_a, driver_b)
        if df.empty:
            st.info("These two drivers share no timed laps to compare.")
            return

        st.plotly_chart(
            charts.delta_chart(df, driver_a, driver_b), use_container_width=True
        )
        chart_notes("undercut-overcut", delta_summary(session, driver_a, driver_b))


VIEW_RENDERERS = {
    "lap-times": render_lap_times,
    "tire-strategy": render_tire_strategy,
    "pit-stops": render_pit_stops,
    "undercut-overcut": render_undercut_overcut,
}


def render_prompt_card() -> None:
    """Placeholder shown before any session is loaded."""
    section_header(
        "get-started",
        "Load a session",
        "No timing data yet — pick a race in the sidebar to begin.",
    )
    with st.container(border=True):
        card_title("Getting started")
        st.markdown(
            "Choose a **year**, **race**, and **session type** in the sidebar, then "
            "press **Load session**. The first load of a session downloads timing "
            "data (and caches it locally), so it can take a moment; after that it's "
            "instant.",
        )


def render_footer() -> None:
    """Concise disclaimer shown on every view (no data collected, unaffiliated)."""
    st.markdown('<div class="top-divider"></div>', unsafe_allow_html=True)
    st.caption(
        "This dashboard collects no personal data — no accounts, forms, cookies, "
        "or analytics are set by the app itself. Timing data comes from "
        "[FastF1](https://docs.fastf1.dev/) and is shown as-is with no warranty. "
        "This is an unofficial project, not affiliated with or endorsed by Formula "
        "1; F1, FORMULA 1, and related marks are trademarks of Formula One "
        "Licensing BV."
    )


def main() -> None:
    inject_head()
    render_sidebar()

    view = st.session_state.setdefault("view", "lap-times")
    session = st.session_state.get("session")

    render_topbar(view)
    render_intro()
    render_stat_row(session)

    # Surface any load error from the last button press (red-tinted alert).
    if "load_error" in st.session_state:
        st.error(st.session_state["load_error"])

    if session is None:
        render_prompt_card()
    else:
        VIEW_RENDERERS[view](session)

    render_footer()


if __name__ == "__main__":
    main()
