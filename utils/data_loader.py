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


def _session_start_utc(year: int, event_name: str, session_label: str):
    """Scheduled UTC start of a session, or ``None`` if it can't be determined.

    The event schedule lists up to five sessions per weekend as
    ``Session1..Session5`` (names) with matching ``Session1DateUtc..Session5DateUtc``
    timestamps. We find the column whose name matches ``session_label`` and return
    its start time.
    """
    try:
        schedule = load_event_schedule(year)
        rows = schedule.loc[schedule["EventName"] == event_name]
        if rows.empty:
            return None
        row = rows.iloc[0]
        for i in range(1, 6):
            if str(row.get(f"Session{i}")) == session_label:
                start = row.get(f"Session{i}DateUtc")
                return start if pd.notna(start) else None
    except Exception:  # noqa: BLE001 — schedule shape issues just mean "unknown".
        return None
    return None


def _session_in_future(year: int, event_name: str, session_label: str) -> bool:
    """True if the session is scheduled but hasn't started yet.

    Returns False when the start time is unknown, so an uncertain case still falls
    through to a normal load attempt rather than being wrongly blocked.
    """
    start = _session_start_utc(year, event_name, session_label)
    if start is None:
        return False
    start = pd.Timestamp(start)
    if start.tzinfo is None:
        start = start.tz_localize("UTC")
    return start > pd.Timestamp.now(tz="UTC")


@st.cache_resource(show_spinner=False)
def load_session(year: int, event_name: str, session_label: str):
    """Load and fully parse a session's lap data, memoized across reruns.

    Uses ``st.cache_resource`` because a FastF1 ``Session`` isn't serializable and
    is expensive to build. The cache key is ``(year, event_name, session_label)``,
    so tab switches and widget changes reuse the already-loaded session.

    We deliberately load only what the dashboard needs (``laps=True``) and skip
    telemetry/weather/messages to keep loads fast and light on the upstream source.

    Raises:
        DataLoadError: with a UI-safe message if the session can't be loaded —
        including a clear "hasn't happened yet" message for future sessions.
    """
    identifier = SESSION_TYPES.get(session_label)
    if identifier is None:
        raise DataLoadError(f"Unknown session type: {session_label}.")

    # Fail fast (and clearly) for sessions that haven't run yet — no point hitting
    # the network for data that can't exist.
    if _session_in_future(year, event_name, session_label):
        raise DataLoadError(
            f"{session_label} for {event_name} {year} hasn't happened yet — "
            "there's no timing data to show. Check back after the session runs."
        )

    try:
        session = fastf1.get_session(year, event_name, identifier)
        session.load(laps=True, telemetry=False, weather=False, messages=False)
        # NOTE: `session.laps` is validated *inside* the try. FastF1 raises
        # DataNotLoadedError on this attribute when the load didn't populate laps
        # (e.g. a future/unavailable session), so it must be guarded here too.
        laps = session.laps
        has_laps = laps is not None and not laps.empty
    except DataLoadError:
        raise
    except Exception as exc:  # noqa: BLE001 — normalize any upstream failure.
        # A late-breaking future check catches sessions whose date we couldn't
        # read earlier but which still fail because they haven't run.
        if _session_in_future(year, event_name, session_label):
            raise DataLoadError(
                f"{session_label} for {event_name} {year} hasn't happened yet — "
                "there's no timing data to show. Check back after the session runs."
            ) from exc
        raise DataLoadError(
            f"Couldn't load {session_label} data for {event_name} {year}. "
            "This session may not exist or the data source may be temporarily "
            "unavailable. Try another session or try again shortly."
        ) from exc

    if not has_laps:
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


# ---------------------------------------------------------------------------
# Plain-language summaries
#
# Each summary function reads the *same* shaped data the matching chart plots,
# so its output tracks the current selection (e.g. only the chosen drivers). It
# returns a list of short human-readable sentences the UI renders as bullets, or
# an empty list when there's nothing to summarise.
# ---------------------------------------------------------------------------
def _fmt_time(seconds: float) -> str:
    """Render a lap time in seconds as ``m:ss.mmm`` (e.g. 92.456 -> ``1:32.456``)."""
    if pd.isna(seconds):
        return "—"
    minutes, secs = divmod(float(seconds), 60)
    return f"{int(minutes)}:{secs:06.3f}"


def lap_time_summary(session, drivers: list[str]) -> list[str]:
    """Summarise the lap-time chart for the currently selected drivers."""
    df = lap_times(session, drivers)
    if df.empty:
        return []

    fastest = df.loc[df["LapTimeSeconds"].idxmin()]
    per_driver = (
        df.groupby("Driver")["LapTimeSeconds"]
        .agg(["mean", "min", "count"])
        .sort_values("mean")
    )

    facts = [
        f"Comparing {len(per_driver)} "
        f"{'driver' if len(per_driver) == 1 else 'drivers'} over "
        f"{int(df['LapNumber'].nunique())} laps.",
        f"Fastest lap: {fastest['Driver']} {_fmt_time(fastest['LapTimeSeconds'])} "
        f"on lap {int(fastest['LapNumber'])}.",
    ]
    best_avg = per_driver.index[0]
    facts.append(
        f"Best average pace: {best_avg} "
        f"({_fmt_time(per_driver.loc[best_avg, 'mean'])} per lap)."
    )
    bests = ", ".join(
        f"{driver} {_fmt_time(row['min'])}"
        for driver, row in per_driver.sort_values("min").iterrows()
    )
    facts.append(f"Best lap each: {bests}.")
    return facts


def tire_strategy_summary(session) -> list[str]:
    """Summarise the tire-strategy chart (stints, compounds, stops)."""
    df = stint_summary(session)
    if df.empty:
        return []

    n_drivers = df["Driver"].nunique()
    stops_per_driver = df.groupby("Driver")["Stint"].nunique() - 1
    common_stops = int(stops_per_driver.clip(lower=0).mode().iloc[0])
    compounds = ", ".join(
        c.title() for c in sorted(df["Compound"].unique())
    )
    longest = df.loc[df["Laps"].idxmax()]

    return [
        f"{n_drivers} drivers; most ran a {common_stops}-stop race.",
        f"Compounds used: {compounds}.",
        f"Longest stint: {longest['Driver']}, {int(longest['Laps'])} laps on "
        f"{longest['Compound'].title()}.",
    ]


def pit_stop_summary(session) -> list[str]:
    """Summarise the pit-stop chart (count, fastest, average)."""
    df = pit_stops(session)
    if df.empty:
        return []

    fastest = df.loc[df["PitDurationSeconds"].idxmin()]
    return [
        f"{len(df)} stops across {df['Driver'].nunique()} drivers.",
        f"Fastest: {fastest['Driver']} {fastest['PitDurationSeconds']:.1f}s "
        f"on lap {int(fastest['Lap'])}.",
        f"Average pit-lane time: {df['PitDurationSeconds'].mean():.1f}s.",
    ]


def delta_summary(session, driver_a: str, driver_b: str) -> list[str]:
    """Summarise the head-to-head delta between two drivers."""
    df = lap_time_delta(session, driver_a, driver_b)
    if df.empty:
        return []

    a_faster = int((df["DeltaSeconds"] < 0).sum())
    b_faster = int((df["DeltaSeconds"] > 0).sum())
    net = float(df["CumulativeDelta"].iloc[-1])

    facts = [
        f"Compared over {len(df)} shared laps.",
        f"{driver_a} quicker on {a_faster} laps, {driver_b} on {b_faster}.",
    ]
    if net < 0:
        facts.append(
            f"Net: {driver_a} gained {abs(net):.1f}s in total lap time — the "
            f"faster car across these laps."
        )
    elif net > 0:
        facts.append(
            f"Net: {driver_b} gained {abs(net):.1f}s in total lap time — the "
            f"faster car across these laps."
        )
    else:
        facts.append("Net: dead even on cumulative lap time.")
    return facts


# ---------------------------------------------------------------------------
# Race outcome / results
#
# ``session.results`` carries the classification (grid + finishing position,
# status, points). We derive the headline story from it — winner, biggest
# movers, DNFs — plus the fastest lap from the lap data. Everything is guarded
# so non-race sessions (or partial data) degrade to whatever is available.
# ---------------------------------------------------------------------------
def _is_classified(status) -> bool:
    """True if a finishing ``Status`` counts as classified (finished/+N laps)."""
    s = str(status)
    return s == "Finished" or s.startswith("+")


def _fastest_lap(session) -> dict | None:
    """The single fastest lap of the session: driver, formatted time, lap number."""
    laps = session.laps
    if not _has_columns(laps, {"Driver", "LapTime", "LapNumber"}):
        return None
    valid = laps.dropna(subset=["LapTime"])
    if valid.empty:
        return None
    row = valid.loc[valid["LapTime"].idxmin()]
    return {
        "driver": row["Driver"],
        "time": _fmt_time(row["LapTime"].total_seconds()),
        "lap": int(row["LapNumber"]),
    }


def race_results(session) -> pd.DataFrame:
    """Tidy classification table: one row per driver, ordered by finishing place.

    Columns: ``Driver``, ``Team``, ``Grid``, ``Finish``, ``Gained`` (grid − finish,
    so positive = places gained), ``Status``, ``Points``. Returns an empty frame
    if the session exposes no results.
    """
    empty = pd.DataFrame(
        columns=["Driver", "Team", "Grid", "Finish", "Gained", "Status", "Points"]
    )
    try:
        res = session.results
    except Exception:  # noqa: BLE001 — results may not be loaded for some sessions.
        return empty
    if not isinstance(res, pd.DataFrame) or res.empty:
        return empty

    def col(name):
        return res[name] if name in res.columns else pd.Series(index=res.index, dtype="object")

    out = pd.DataFrame(
        {
            "Driver": col("Abbreviation"),
            "Team": col("TeamName"),
            "Grid": pd.to_numeric(col("GridPosition"), errors="coerce"),
            "Finish": pd.to_numeric(col("Position"), errors="coerce"),
            "Status": col("Status"),
            "Points": pd.to_numeric(col("Points"), errors="coerce"),
        }
    )
    out["Gained"] = out["Grid"] - out["Finish"]
    return out.sort_values("Finish", na_position="last").reset_index(drop=True)


def race_highlights(session) -> dict:
    """Headline outcomes for the Race Summary view.

    Returns a dict with ``winner``, ``fastest_lap``, ``most_gained``,
    ``most_lost`` (each a ``{driver, detail}`` dict or ``None``), a ``podium`` and
    ``dnf`` driver list, and a ``story`` — a few plain-language sentences
    summarising the race. Movers are computed among *classified finishers* so a
    retirement doesn't masquerade as the biggest loser; DNFs get their own line.
    """
    out: dict = {
        "winner": None,
        "fastest_lap": None,
        "most_gained": None,
        "most_lost": None,
        "podium": [],
        "dnf": [],
        "story": [],
    }

    fl = _fastest_lap(session)
    if fl:
        out["fastest_lap"] = {
            "driver": fl["driver"],
            "detail": f"{fl['time']} on lap {fl['lap']}",
        }

    df = race_results(session)
    if df.empty:
        if out["fastest_lap"]:
            out["story"].append(
                f"Fastest lap: {fl['driver']} ({out['fastest_lap']['detail']})."
            )
        return out

    # A grid column with real values means this is a race/sprint (grid → finish);
    # without it (qualifying/practice) we soften the wording accordingly.
    race_like = bool(df["Grid"].notna().any())

    # Winner
    winner_row = df[df["Finish"] == 1]
    winner = winner_row.iloc[0] if not winner_row.empty else None
    if winner is not None:
        bits = []
        if pd.notna(winner["Grid"]):
            bits.append("from pole" if winner["Grid"] == 1 else f"from P{int(winner['Grid'])}")
        if pd.notna(winner["Points"]):
            bits.append(f"{int(winner['Points'])} pts")
        out["winner"] = {"driver": winner["Driver"], "detail": " • ".join(bits)}

    # Podium
    out["podium"] = (
        df[df["Finish"].isin([1, 2, 3])].sort_values("Finish")["Driver"].tolist()
    )

    # Biggest mover / faller among classified finishers with a known grid slot.
    classified = df[df["Status"].apply(_is_classified) & df["Gained"].notna()]
    gainer = loser = None
    if not classified.empty:
        gainer = classified.loc[classified["Gained"].idxmax()]
        loser = classified.loc[classified["Gained"].idxmin()]
        if gainer["Gained"] > 0:
            out["most_gained"] = {
                "driver": gainer["Driver"],
                "detail": f"+{int(gainer['Gained'])} · P{int(gainer['Grid'])}→P{int(gainer['Finish'])}",
            }
        if loser["Gained"] < 0:
            out["most_lost"] = {
                "driver": loser["Driver"],
                "detail": f"{int(loser['Gained'])} · P{int(loser['Grid'])}→P{int(loser['Finish'])}",
            }

    # DNFs
    out["dnf"] = df[~df["Status"].apply(_is_classified)]["Driver"].tolist()

    # Plain-language story
    story: list[str] = []
    if out["winner"]:
        line = f"{winner['Driver']} won the race" if race_like else f"{winner['Driver']} finished on top"
        if race_like and pd.notna(winner["Grid"]):
            line += " from pole" if winner["Grid"] == 1 else f" from P{int(winner['Grid'])}"
        if out["fastest_lap"] and out["fastest_lap"]["driver"] == winner["Driver"]:
            line += ", and set the fastest lap"
        story.append(line + ".")
    if out["podium"]:
        label = "Podium" if race_like else "Top three"
        story.append(f"{label}: " + ", ".join(out["podium"]) + ".")
    if gainer is not None and out["most_gained"]:
        n = int(gainer["Gained"])
        story.append(
            f"Biggest climber: {gainer['Driver']}, up {n} "
            f"{'place' if n == 1 else 'places'} "
            f"(P{int(gainer['Grid'])} to P{int(gainer['Finish'])})."
        )
    if loser is not None and out["most_lost"]:
        n = abs(int(loser["Gained"]))
        story.append(
            f"Toughest run: {loser['Driver']}, down {n} "
            f"{'place' if n == 1 else 'places'} "
            f"(P{int(loser['Grid'])} to P{int(loser['Finish'])})."
        )
    if out["fastest_lap"] and (
        not out["winner"] or out["fastest_lap"]["driver"] != out["winner"]["driver"]
    ):
        story.append(
            f"Fastest lap: {out['fastest_lap']['driver']} "
            f"({out['fastest_lap']['detail']})."
        )
    if out["dnf"]:
        n = len(out["dnf"])
        if n <= 5:
            story.append(f"{n} did not finish: {', '.join(out['dnf'])}.")
        else:
            story.append(f"{n} cars failed to finish.")
    out["story"] = story
    return out
