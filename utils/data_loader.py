"""FastF1 data-loading helpers.

Every function that touches the network is wrapped so that failures surface as a
clean :class:`DataLoadError` with a user-friendly message — never a raw traceback.
The Streamlit layer (``app.py``) catches that exception and renders it with
``st.error``, so library internals and file paths never leak into the UI.

Caching strategy (three layers, so repeated interactions don't hammer upstream):

1. ``fastf1.Cache`` writes raw timing data to the gitignored ``cache/`` folder.
2. ``@st.cache_data`` memoizes lightweight, serializable lookups (the schedule).
3. ``@st.cache_resource`` memoizes the heavy, non-serializable Session object so
   switching tabs never re-fetches or re-parses a session.
"""

from __future__ import annotations

import os
from pathlib import Path

import fastf1
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

# Load any local .env (used only if a future integration needs secrets). Safe to
# call when no .env exists — python-dotenv simply does nothing.
load_dotenv()

# Directory FastF1 uses for its on-disk cache. Kept alongside the app and
# gitignored so it never gets committed.
CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"

# Human-readable session labels (shown in the sidebar) mapped to the short
# identifiers FastF1's get_session() expects.
SESSION_TYPES: dict[str, str] = {
    "Race": "R",
    "Qualifying": "Q",
    "Sprint": "S",
    "Practice 1": "FP1",
    "Practice 2": "FP2",
    "Practice 3": "FP3",
}

# Reasonable range of seasons FastF1 has timing data for. 2018 is the practical
# floor for lap-level data; we cap at the current year.
FIRST_SUPPORTED_YEAR = 2018


class DataLoadError(Exception):
    """Raised when session/schedule loading fails, carrying a UI-safe message."""


def enable_cache() -> None:
    """Point FastF1 at the local ``cache/`` directory, creating it if needed.

    Idempotent: safe to call on every Streamlit rerun.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fastf1.Cache.enable_cache(str(CACHE_DIR))


def available_years() -> list[int]:
    """Seasons offered in the sidebar, newest first."""
    current = pd.Timestamp.now().year
    return list(range(current, FIRST_SUPPORTED_YEAR - 1, -1))


@st.cache_data(show_spinner=False)
def load_event_schedule(year: int) -> pd.DataFrame:
    """Return the season's event schedule (championship rounds only).

    Cached with ``st.cache_data`` because the result is a small, serializable
    DataFrame that rarely changes for a completed season.

    Testing events (round 0) are dropped so the race picker only shows grands prix.
    """
    try:
        schedule = fastf1.get_event_schedule(year, include_testing=False)
    except Exception as exc:  # noqa: BLE001 — normalize any upstream failure.
        raise DataLoadError(
            f"Couldn't load the {year} race calendar. The data source may be "
            "unavailable — please try again in a moment."
        ) from exc

    if schedule is None or schedule.empty:
        raise DataLoadError(f"No race calendar is available for {year} yet.")

    return schedule


def event_names(year: int) -> list[str]:
    """Ordered list of event names for the given season (for the race dropdown)."""
    schedule = load_event_schedule(year)
    return schedule["EventName"].tolist()


@st.cache_resource(show_spinner=False)
def load_session(year: int, event_name: str, session_label: str):
    """Load and fully parse a session's lap data, memoized across reruns.

    Uses ``st.cache_resource`` because a FastF1 ``Session`` isn't serializable and
    is expensive to build. The cache key is ``(year, event_name, session_label)``,
    so tab switches and widget changes reuse the already-loaded session.

    We deliberately load only what the dashboard needs (``laps=True``) and skip
    telemetry/weather/messages to keep loads fast and light on the upstream source.

    Raises:
        DataLoadError: with a UI-safe message if the session can't be loaded.
    """
    identifier = SESSION_TYPES.get(session_label)
    if identifier is None:
        raise DataLoadError(f"Unknown session type: {session_label}.")

    try:
        session = fastf1.get_session(year, event_name, identifier)
        session.load(laps=True, telemetry=False, weather=False, messages=False)
    except Exception as exc:  # noqa: BLE001 — normalize any upstream failure.
        raise DataLoadError(
            f"Couldn't load {session_label} data for {event_name} {year}. "
            "This session may not exist or the data source may be temporarily "
            "unavailable. Try another session or try again shortly."
        ) from exc

    if session.laps is None or session.laps.empty:
        raise DataLoadError(
            f"No lap data is available for {session_label} at {event_name} {year}."
        )

    return session


# ---------------------------------------------------------------------------
# Data shaping / strategy logic
#
# These helpers turn a raw FastF1 Laps table into the tidy frames the chart
# builders consume. Each validates its input shape before working on it, so a
# malformed or empty table produces an empty result rather than an exception.
# ---------------------------------------------------------------------------

# Compound label -> hex color, following the official F1 tire colors.
COMPOUND_COLORS: dict[str, str] = {
    "SOFT": "#DA291C",          # red
    "MEDIUM": "#FFD12E",        # yellow
    "HARD": "#EBEBEB",          # white / light gray
    "INTERMEDIATE": "#43B02A",  # green
    "WET": "#0067AD",           # blue
    "UNKNOWN": "#8C8C8C",       # gray fallback
}


def _has_columns(df: pd.DataFrame | None, columns: set[str]) -> bool:
    """True only if ``df`` is a non-empty DataFrame containing every column."""
    return (
        isinstance(df, pd.DataFrame)
        and not df.empty
        and columns.issubset(df.columns)
    )


def driver_list(session) -> list[str]:
    """Driver abbreviations that actually set laps in this session, sorted."""
    laps = session.laps
    if not _has_columns(laps, {"Driver"}):
        return []
    return sorted(laps["Driver"].dropna().unique().tolist())


def lap_times(session, drivers: list[str]) -> pd.DataFrame:
    """Tidy per-lap times for the chosen drivers.

    Returns columns ``Driver``, ``LapNumber``, ``LapTimeSeconds``. Laps without a
    recorded time (out-/in-laps, deleted laps) are dropped so the line chart has
    no misleading gaps-to-zero.
    """
    laps = session.laps
    if not drivers or not _has_columns(laps, {"Driver", "LapNumber", "LapTime"}):
        return pd.DataFrame(columns=["Driver", "LapNumber", "LapTimeSeconds"])

    subset = laps[laps["Driver"].isin(drivers)].copy()
    subset["LapTimeSeconds"] = subset["LapTime"].dt.total_seconds()
    subset = subset.dropna(subset=["LapTimeSeconds", "LapNumber"])
    return (
        subset[["Driver", "LapNumber", "LapTimeSeconds"]]
        .sort_values(["Driver", "LapNumber"])
        .reset_index(drop=True)
    )


def stint_summary(session) -> pd.DataFrame:
    """Collapse laps into one row per (driver, stint) for the tire-strategy Gantt.

    A *stint* is the run of consecutive laps on one set of tires between pit
    stops. FastF1 labels every lap with a ``Stint`` number and its ``Compound``,
    so we group by both and take the lap range.

    Returns columns ``Driver``, ``Stint``, ``Compound``, ``StartLap``, ``EndLap``,
    ``Laps`` (the inclusive lap count). Rows are ordered by driver then stint.
    """
    laps = session.laps
    needed = {"Driver", "Stint", "Compound", "LapNumber"}
    if not _has_columns(laps, needed):
        return pd.DataFrame(
            columns=["Driver", "Stint", "Compound", "StartLap", "EndLap", "Laps"]
        )

    valid = laps.dropna(subset=["Stint", "LapNumber"])
    grouped = (
        valid.groupby(["Driver", "Stint"], as_index=False)
        .agg(
            Compound=("Compound", "first"),
            StartLap=("LapNumber", "min"),
            EndLap=("LapNumber", "max"),
        )
    )
    grouped["Compound"] = (
        grouped["Compound"].fillna("UNKNOWN").str.upper()
    )
    # Inclusive lap count (a one-lap stint spans laps N..N, i.e. 1 lap).
    grouped["Laps"] = grouped["EndLap"] - grouped["StartLap"] + 1
    return grouped.sort_values(["Driver", "Stint"]).reset_index(drop=True)


def pit_stops(session) -> pd.DataFrame:
    """Derive pit-stop durations from consecutive in-/out-lap timestamps.

    FastF1 records two session-time stamps per lap:

    * ``PitInTime``  — when the car crossed into the pit lane (set on the in-lap).
    * ``PitOutTime`` — when the car rejoined the track (set on the *next* lap).

    So a stop straddles two consecutive laps: the in-lap N carries ``PitInTime``
    and the out-lap N+1 carries ``PitOutTime``. The elapsed pit-lane time is
    therefore ``PitOutTime(N+1) - PitInTime(N)``. (This is total pit-lane time,
    including the pit-lane drive, not just the stationary service time — but it's
    the honest quantity you can derive from these two fields.)

    Returns columns ``Driver``, ``StopNumber``, ``Lap`` (the in-lap),
    ``PitDurationSeconds``. Returns an empty frame when the session has no stops
    (e.g. qualifying), which callers handle gracefully.
    """
    empty = pd.DataFrame(
        columns=["Driver", "StopNumber", "Lap", "PitDurationSeconds"]
    )
    laps = session.laps
    needed = {"Driver", "LapNumber", "PitInTime", "PitOutTime"}
    if not _has_columns(laps, needed):
        return empty

    records: list[dict] = []
    for driver, driver_laps in laps.groupby("Driver"):
        driver_laps = driver_laps.sort_values("LapNumber")
        # Fast lookup of each lap's out-time by lap number.
        out_by_lap = driver_laps.set_index("LapNumber")["PitOutTime"]

        stop_number = 0
        in_laps = driver_laps[driver_laps["PitInTime"].notna()]
        for _, lap in in_laps.iterrows():
            in_lap_number = lap["LapNumber"]
            out_time = out_by_lap.get(in_lap_number + 1)
            if pd.isna(out_time):
                continue  # No matching out-lap (e.g. retirement in the pits).

            duration = (out_time - lap["PitInTime"]).total_seconds()
            if duration <= 0:
                continue  # Guard against clock quirks / bad rows.

            stop_number += 1
            records.append(
                {
                    "Driver": driver,
                    "StopNumber": stop_number,
                    "Lap": int(in_lap_number),
                    "PitDurationSeconds": round(duration, 2),
                }
            )

    if not records:
        return empty
    return (
        pd.DataFrame(records)
        .sort_values(["Driver", "StopNumber"])
        .reset_index(drop=True)
    )


def lap_time_delta(session, driver_a: str, driver_b: str) -> pd.DataFrame:
    """Per-lap lap-time delta between two drivers, for undercut/overcut analysis.

    The delta on lap *n* is::

        delta(n) = lapTime_A(n) - lapTime_B(n)      (seconds)

    Sign convention: a **negative** delta means driver A was *faster* than driver
    B on that lap; a **positive** delta means A was slower.

    Why this reveals an undercut/overcut
    -------------------------------------
    An *undercut* is pitting a lap or two before a rival: you fit fresh tires and
    immediately bank faster lap times while the rival is still on worn rubber. In
    this chart that shows up as A's delta swinging sharply negative on the out-lap
    after A pits (fresh-tire advantage) — those negative bars are the time gained
    that can leapfrog the rival once they stop.

    An *overcut* is the mirror image: you stay out longer on aging tires, betting
    that clean air and the rival's cold out-laps let you bank time — visible as A
    holding a negative (or shrinking positive) delta during the laps *after B*
    pits but before A does.

    The chart pairs these per-lap bars with a zero reference line: bars below zero
    are laps A gained, bars above are laps A lost. We only compare laps *both*
    drivers actually set a time on (an inner join on ``LapNumber``), so pit laps
    and missing laps don't produce phantom deltas.

    Returns columns ``LapNumber``, ``DeltaSeconds`` (A − B), and
    ``CumulativeDelta`` (running total, i.e. net time A has gained/lost so far).
    """
    empty = pd.DataFrame(columns=["LapNumber", "DeltaSeconds", "CumulativeDelta"])
    if not driver_a or not driver_b or driver_a == driver_b:
        return empty

    a = lap_times(session, [driver_a]).rename(
        columns={"LapTimeSeconds": "A"}
    )[["LapNumber", "A"]]
    b = lap_times(session, [driver_b]).rename(
        columns={"LapTimeSeconds": "B"}
    )[["LapNumber", "B"]]
    if a.empty or b.empty:
        return empty

    merged = a.merge(b, on="LapNumber", how="inner").sort_values("LapNumber")
    if merged.empty:
        return empty

    merged["DeltaSeconds"] = (merged["A"] - merged["B"]).round(3)
    merged["CumulativeDelta"] = merged["DeltaSeconds"].cumsum().round(3)
    return merged[["LapNumber", "DeltaSeconds", "CumulativeDelta"]].reset_index(
        drop=True
    )
