# 🏁 F1 Strategy Dashboard

An interactive **Streamlit** dashboard for exploring Formula 1 race strategy from
real timing data, powered by the [FastF1](https://docs.fastf1.dev/) library.

Pick a season, race, and session, then dig into the strategy across four views:

| Tab | What it shows |
| --- | --- |
| **Lap Times** | Lap time vs lap number for any drivers you select — spot pace, degradation, and traffic. |
| **Tire Strategy** | A Gantt-style chart of each driver's stints, colored by tire compound, with lap ranges. |
| **Pit Stops** | Pit-lane time per stop per driver (derived from in-/out-lap timestamps), with a data table. |
| **Undercut / Overcut** | Head-to-head lap-time delta between two drivers, with a zero reference line, to see whether a pit call gained or lost time. |

## How the interesting bit works — undercut/overcut

The delta for lap *n* is `lapTime_A(n) − lapTime_B(n)`:

- **Negative** delta → driver A was faster on that lap.
- **Positive** delta → driver A was slower.

An **undercut** — pitting a lap or two earlier than a rival — shows up as a sharp
negative swing on A's out-lap: fresh tires immediately bank time while the rival is
still on worn rubber. An **overcut** is the mirror image: staying out longer and
using clean air plus the rival's cold out-laps to gain instead. The chart only
compares laps *both* drivers actually set a time on, so pit laps don't create
phantom deltas. See the docstring in
[`utils/data_loader.py`](utils/data_loader.py) (`lap_time_delta`) for the full
rationale.

## Project structure

```
f1-strategy-dashboard/
├── app.py                 # Streamlit entry point (sidebar + 4 tabs)
├── utils/
│   ├── data_loader.py     # FastF1 loading + strategy data shaping (cached)
│   └── charts.py          # Plotly figure builders
├── requirements.txt
├── .env.example           # Template for future secrets (none needed today)
├── .gitignore
└── README.md
```

## Run it locally

Requires **Python 3.10+**.

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Launch
streamlit run app.py
```

The app opens at <http://localhost:8501>. The first load of any session downloads
timing data and can take a while; it's then written to the local `cache/` folder
(gitignored), so subsequent loads are fast.

## Caching

Three cooperating layers keep the app responsive and gentle on the upstream data
source (both a courtesy and a rate-limit safeguard):

1. **FastF1 disk cache** (`cache/`) — raw timing data, persisted between runs.
2. **`st.cache_data`** — memoizes the lightweight season schedule lookups.
3. **`st.cache_resource`** — memoizes the heavy parsed `Session` object, so
   switching tabs or changing widgets never re-fetches data.

## Deployment

This is a **live Python process**, not a static site, so it can't go on Vercel /
Netlify / GitHub Pages. Deploy it on a platform that runs a long-lived server:

- **[Streamlit Community Cloud](https://streamlit.io/cloud)** — point it at this
  repo and `app.py`; zero config for a Streamlit app.
- **[Railway](https://railway.app/)** — run `streamlit run app.py --server.port $PORT`.

Make sure the host allows disk writes so FastF1's `cache/` works (both platforms
above do).

## Secrets & configuration

FastF1 needs **no API key**, so this project ships with no secrets. If you later
add an integration that does (e.g. a weather overlay):

- Read every key from an **environment variable** via `python-dotenv` — never
  hardcode it.
- Add the variable name to [`.env.example`](.env.example); keep real values in a
  local `.env`, which is **gitignored and must never be committed**.
- In production, set those keys as **platform secrets/environment variables**
  (Streamlit Cloud's secrets manager, Railway env vars) — not in the repo.

## Notes on reliability

- Every FastF1 call is wrapped in try/except; failures render as a clean
  `st.error`, never a raw traceback or internal path.
- Loaded data is validated (non-empty, expected columns) before anything is
  plotted, so malformed or partial sessions degrade gracefully instead of
  crashing.
- Sessions with no pit stops (e.g. qualifying) are handled explicitly.

## Disclaimer

- **Privacy:** this dashboard collects no personal data. It sets no accounts,
  forms, cookies, or analytics of its own. (If you deploy it, your hosting
  provider — e.g. Streamlit Community Cloud or Railway — may set its own cookies
  and telemetry, which are governed by that provider's policies, not this app's.)
- **Data & warranty:** all timing data comes from
  [FastF1](https://docs.fastf1.dev/), which sources the official F1 live timing
  and Ergast data. It is shown as-is, with no warranty of accuracy — treat it as
  informational, not authoritative.
- **Affiliation:** this is an unofficial, non-commercial project, not affiliated
  with or endorsed by Formula 1. F1, FORMULA 1, and related marks are trademarks
  of Formula One Licensing BV.
