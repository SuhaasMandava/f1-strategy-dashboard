"""Plotly figure builders.

Each function takes an already-shaped, already-validated DataFrame (see
``utils.data_loader``) and returns a ``plotly.graph_objects.Figure``. Keeping all
presentation here means the data layer stays free of styling concerns and the
Streamlit layer just calls ``st.plotly_chart``.

Builders assume tidy input but still tolerate empty frames by returning an empty,
titled figure, so the UI never crashes on a session with missing data.
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

from utils.data_loader import COMPOUND_COLORS

# Shared look-and-feel — a dark, minimal template that matches the site theme.
# Card-colored plot surface (#141414), light-gray text, no bright chrome. The
# tire-compound colors live in data_loader.COMPOUND_COLORS and are intentionally
# NOT themed here — they must stay accurate to real F1 compounds.
_FONT_STACK = "Inter, system-ui, -apple-system, Segoe UI, Roboto, sans-serif"
_TEXT = "#d4d4d4"       # primary chart text
_MUTED = "#a3a3a3"      # axis titles
_GRID = "rgba(255,255,255,0.06)"   # very low-opacity gridlines
_LINE = "rgba(255,255,255,0.15)"   # axis / zero lines

_LAYOUT = dict(
    template="plotly_dark",
    paper_bgcolor="#141414",
    plot_bgcolor="#141414",
    font=dict(color=_TEXT, family=_FONT_STACK),
    margin=dict(l=60, r=30, t=60, b=50),
    hovermode="closest",
    hoverlabel=dict(
        bgcolor="#1a1a1a", bordercolor="#262626", font=dict(color="#f5f5f5")
    ),
    legend=dict(
        orientation="h",
        yanchor="bottom",
        y=1.02,
        xanchor="left",
        x=0,
        bgcolor="rgba(0,0,0,0)",
        font=dict(color=_TEXT),
    ),
)


def _style_axes(fig: go.Figure) -> None:
    """Apply the shared dark gridline/axis styling to a figure's x and y axes.

    Kept separate from ``_LAYOUT`` so builders can call their own ``update_xaxes``
    (titles, custom ticks) first and have this merge on top without clobbering.
    """
    common = dict(
        gridcolor=_GRID,
        zerolinecolor=_LINE,
        linecolor=_LINE,
        tickcolor=_LINE,
        color=_TEXT,
        title_font=dict(color=_MUTED),
    )
    fig.update_xaxes(**common)
    fig.update_yaxes(**common)


def _format_seconds(seconds: float) -> str:
    """Render a lap time in seconds as ``m:ss.mmm`` (e.g. 92.456 -> ``1:32.456``)."""
    if pd.isna(seconds):
        return "—"
    minutes, secs = divmod(float(seconds), 60)
    return f"{int(minutes)}:{secs:06.3f}"


def _empty_figure(message: str) -> go.Figure:
    """A blank, titled figure used as a graceful placeholder for empty data."""
    fig = go.Figure()
    fig.update_layout(
        **_LAYOUT,
        title=message,
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
    )
    return fig


def lap_time_chart(df: pd.DataFrame) -> go.Figure:
    """Line chart of lap time vs lap number, one colored line per driver.

    Expects columns ``Driver``, ``LapNumber``, ``LapTimeSeconds``.
    """
    if df.empty:
        return _empty_figure("No lap-time data for the selected drivers.")

    fig = go.Figure()
    for driver, laps in df.groupby("Driver"):
        fig.add_trace(
            go.Scatter(
                x=laps["LapNumber"],
                y=laps["LapTimeSeconds"],
                mode="lines+markers",
                name=driver,
                marker=dict(size=5),
                customdata=[_format_seconds(s) for s in laps["LapTimeSeconds"]],
                hovertemplate=(
                    f"<b>{driver}</b><br>Lap %{{x}}<br>%{{customdata}}<extra></extra>"
                ),
            )
        )

    fig.update_layout(**_LAYOUT, title="Lap Times")
    fig.update_xaxes(title_text="Lap")
    # Format the time axis ticks as m:ss rather than raw seconds.
    _apply_time_axis(fig, df["LapTimeSeconds"])
    _style_axes(fig)
    return fig


def _apply_time_axis(fig: go.Figure, seconds: pd.Series) -> None:
    """Label a seconds-valued y-axis with human lap-time ticks (m:ss.mmm)."""
    lo, hi = float(seconds.min()), float(seconds.max())
    span = max(hi - lo, 0.001)
    step = max(round(span / 6, 1), 0.1)
    ticks = []
    value = lo
    while value <= hi + step:
        ticks.append(round(value, 1))
        value += step
    fig.update_yaxes(
        title_text="Lap time",
        tickmode="array",
        tickvals=ticks,
        ticktext=[_format_seconds(t) for t in ticks],
    )


def tire_strategy_chart(df: pd.DataFrame) -> go.Figure:
    """Horizontal Gantt-style chart of stints per driver, colored by compound.

    Expects columns ``Driver``, ``Stint``, ``Compound``, ``StartLap``, ``EndLap``,
    ``Laps``. Each bar spans the lap range of one stint; color encodes the tire
    compound (soft=red, medium=yellow, hard=light gray, inter=green, wet=blue).
    """
    if df.empty:
        return _empty_figure("No tire-strategy data for this session.")

    fig = go.Figure()
    seen_compounds: set[str] = set()

    for _, stint in df.iterrows():
        compound = stint["Compound"]
        color = COMPOUND_COLORS.get(compound, COMPOUND_COLORS["UNKNOWN"])
        # The bar starts at StartLap and is `Laps` long. `base` offsets it so the
        # segment visually occupies laps StartLap..EndLap.
        fig.add_trace(
            go.Bar(
                y=[stint["Driver"]],
                x=[stint["Laps"]],
                base=[stint["StartLap"] - 1],
                orientation="h",
                marker=dict(color=color, line=dict(color="#111", width=1)),
                name=compound.title(),
                legendgroup=compound,
                showlegend=compound not in seen_compounds,
                hovertemplate=(
                    f"<b>{stint['Driver']}</b><br>{compound.title()}<br>"
                    f"Laps {int(stint['StartLap'])}–{int(stint['EndLap'])} "
                    f"({int(stint['Laps'])} laps)<extra></extra>"
                ),
            )
        )
        seen_compounds.add(compound)

    fig.update_layout(**_LAYOUT, barmode="stack", title="Tire Strategy")
    fig.update_xaxes(title_text="Lap")
    fig.update_yaxes(title_text="", autorange="reversed")  # first driver on top
    _style_axes(fig)
    return fig


def pit_stop_chart(df: pd.DataFrame) -> go.Figure:
    """Bar chart of pit-stop durations, grouped by driver.

    Expects columns ``Driver``, ``StopNumber``, ``Lap``, ``PitDurationSeconds``.
    """
    if df.empty:
        return _empty_figure("No pit stops detected in this session.")

    fig = px.bar(
        df,
        x="Driver",
        y="PitDurationSeconds",
        color="Driver",
        custom_data=["StopNumber", "Lap"],
    )
    fig.update_traces(
        hovertemplate=(
            "<b>%{x}</b><br>Stop %{customdata[0]} (lap %{customdata[1]})<br>"
            "%{y:.2f} s<extra></extra>"
        )
    )
    fig.update_layout(**_LAYOUT, title="Pit-Stop Durations", showlegend=False)
    fig.update_xaxes(title_text="Driver")
    fig.update_yaxes(title_text="Pit-lane time (s)")
    _style_axes(fig)
    return fig


def delta_chart(df: pd.DataFrame, driver_a: str, driver_b: str) -> go.Figure:
    """Bar chart of the per-lap lap-time delta (A − B) with a zero reference line.

    Bars below zero are laps ``driver_a`` gained; bars above are laps lost. See
    ``utils.data_loader.lap_time_delta`` for the strategy rationale.

    Expects columns ``LapNumber``, ``DeltaSeconds``, ``CumulativeDelta``.
    """
    if df.empty:
        return _empty_figure("No overlapping timed laps to compare.")

    # Green where A gained (delta < 0), red where A lost (delta > 0).
    colors = ["#43B02A" if d < 0 else "#DA291C" for d in df["DeltaSeconds"]]

    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=df["LapNumber"],
            y=df["DeltaSeconds"],
            marker=dict(color=colors),
            customdata=df["CumulativeDelta"],
            hovertemplate=(
                "Lap %{x}<br>Lap delta: %{y:+.3f}s<br>"
                "Cumulative: %{customdata:+.3f}s<extra></extra>"
            ),
        )
    )
    # Zero reference line: the break-even point between gaining and losing time.
    fig.add_hline(y=0, line_width=2, line_color="#AAAAAA")

    fig.update_layout(
        **_LAYOUT,
        title=f"Lap-time delta: {driver_a} − {driver_b}",
        showlegend=False,
    )
    fig.update_xaxes(title_text="Lap")
    fig.update_yaxes(title_text=f"Δ seconds  (negative = {driver_a} faster)")
    _style_axes(fig)
    # Re-assert the zero reference line above the gridline styling.
    fig.update_yaxes(zerolinecolor="#AAAAAA", zerolinewidth=1)
    return fig
