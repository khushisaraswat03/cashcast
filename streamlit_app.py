"""cashcast — a browser view of the forecaster, its accuracy, and its agent.

A presentation layer. Everything shown is computed by `src/`; this file adds no
arithmetic of its own, which is the rule the agent works under too.

The heavy work -- 854 backtested forecasts and the replayed error history behind
the uncertainty bands -- is cached, because Streamlit reruns the whole script on
every widget change.
"""

from __future__ import annotations

import os

import altair as alt
import streamlit as st

from src.agent import GroqModel, accuracy_summary, ask
from src.backtest import run
from src.estimate import Estimator
from src.exceptions import build as build_exceptions
from src.forecast import Flag, _intervals_up_to, forecast
from src.generate import main as generate
from src.money import fmt_inr
from src.tools import SCHEMAS, bind
from src.world import EventStore, world_as_of

DATA = "data"
DEFAULT_MODEL = "openai/gpt-oss-120b"

# Validated against the #121418 chart surface: gold and the teal ramp each pass
# the lightness, chroma, contrast and colour-vision checks. Red is a status only
# and never carries meaning without a label beside it.
GOLD = "#B98C33"
RED = "#E5484D"
TEAL = ["#17705B", "#1F8C72", "#2AA98B", "#55C7A8", "#7FD9C0"]
SURFACE = "#121418"
# Vega-Lite takes padding as a number or an object, but Streamlit's chart component
# writes padding.bottom into the spec -- which throws on a number, and takes the
# whole chart down with it. Always an object.
PADDING = {"top": 14, "bottom": 10, "left": 10, "right": 14}
INK = "#F2F4F7"
MUTED = "#8B929C"
GRID = "#232730"

st.set_page_config(page_title="cashcast", page_icon="◆", layout="wide")

st.markdown(
    """
    <style>
      #MainMenu, footer, header {visibility: hidden;}
      .block-container {padding-top: 1.4rem; max-width: 1180px;}

      html, body, [class*="css"] {
        font-feature-settings: "ss01", "cv05";
        -webkit-font-smoothing: antialiased;
      }
      .stDataFrame, code, .tnum {font-variant-numeric: tabular-nums;}

      /* Streamlit styles every <p> in its markdown container and wins on
         specificity, so brand type is set on divs, scoped to that container,
         and marked important. Any one of the three alone loses. */
      [data-testid="stMarkdownContainer"] .brand {
        font-size: 2.25rem !important; font-weight: 700 !important;
        letter-spacing: -0.045em !important; color: #F2F4F7 !important;
        margin: 0 0 0.2rem 0 !important; line-height: 1 !important;
      }
      [data-testid="stMarkdownContainer"] .brand span {color: #B98C33 !important;}

      .hero {
        text-align: center; padding: 3.6rem 1rem 2.4rem; border-radius: 20px;
        background: radial-gradient(120% 150% at 50% 0%,
                    rgba(185,140,51,0.15) 0%, rgba(10,11,13,0) 60%);
      }
      .badge {
        display: inline-block; padding: 0.4rem 0.95rem; border-radius: 999px;
        border: 1px solid #2A2F3A; background: #14181F;
        color: #A8AEB8; font-size: 0.78rem; letter-spacing: 0.01em;
        margin-bottom: 1.6rem;
      }
      .badge b {color: #B98C33; font-weight: 600;}

      [data-testid="stMarkdownContainer"] .headline {
        font-size: clamp(2.3rem, 4.6vw, 3.9rem) !important;
        font-weight: 690 !important; letter-spacing: -0.045em !important;
        line-height: 1.06 !important; color: #F5F6F8 !important;
        margin: 0 auto !important; max-width: 20ch;
      }
      [data-testid="stMarkdownContainer"] .headline .dim {color: #6B7280 !important;}
      [data-testid="stMarkdownContainer"] .subhead {
        color: #9AA1AB !important; font-size: 1.02rem !important;
        line-height: 1.6 !important; font-weight: 400 !important;
        max-width: 60ch; margin: 1.3rem auto 0 auto !important;
      }

      .kpi {
        border: 1px solid #1E222B; border-radius: 10px; padding: 1rem 1.1rem;
        background: linear-gradient(180deg, #131720 0%, #101319 100%);
        height: 100%;
      }
      .kpi-label {
        color: #8B929C; font-size: 0.72rem; text-transform: uppercase;
        letter-spacing: 0.09em; font-weight: 600; margin-bottom: 0.45rem;
      }
      .kpi-value {
        color: #F2F4F7; font-size: 1.85rem; font-weight: 640;
        letter-spacing: -0.035em; line-height: 1; font-variant-numeric: tabular-nums;
      }
      .kpi-value.gold {color: #B98C33;}
      .kpi-foot {
        color: #6E7681; font-size: 0.76rem; margin-top: 0.5rem; line-height: 1.45;
        font-variant-numeric: tabular-nums;
      }

      .note {color: #6E7681; font-size: 0.82rem; line-height: 1.55; margin: 0;}
      .note b {color: #A8AEB8;}

      .flagcard {
        border-left: 2px solid #E5484D; padding: 0.15rem 0 0.15rem 0.85rem;
        margin-bottom: 0.9rem;
      }
      .flagcard.calm {border-left-color: #B98C33;}
      .flagcard b {color: #F2F4F7;}
      .flagcard .why {color: #8B929C; font-size: 0.82rem; line-height: 1.45;}

      /* Navigation is st.segmented_control, not st.tabs: it renders as real
         buttons without help, so it stays visible even if these rules miss. */
      [data-testid="stSegmentedControl"] {
        display: flex; justify-content: center; margin: 0.2rem 0 1.7rem 0;
      }
      [data-testid="stSegmentedControl"] button {
        padding: 0.7rem 2.2rem !important;
        font-size: 1.05rem !important; font-weight: 620 !important;
        letter-spacing: -0.012em;
      }

      /* "cast" slides out from behind "cash". Rendered once per session -- a
         brand mark that re-animates on every slider drag reads as a glitch. */
      .brand .cast {display: inline-block;}
      .brand.intro .cast {
        animation: castOut 720ms cubic-bezier(.16, 1, .3, 1) 120ms both;
      }
      @keyframes castOut {
        0%   {transform: translateX(-1.05em) scaleX(0.55); opacity: 0;}
        55%  {opacity: 1;}
        100% {transform: translateX(0) scaleX(1); opacity: 1;}
      }
    </style>
    """,
    unsafe_allow_html=True,
)


# --------------------------------------------------------------------------
# Loading, cached
# --------------------------------------------------------------------------


@st.cache_resource(show_spinner="Generating the dataset and running 854 backtests…")
def load():
    """Dataset, backtest and exception list. Computed once per session.

    The dataset is generated if absent rather than shipped, so a fresh deployment
    builds it on first visit. It is seeded, so every deployment gets the identical
    120 days and these numbers always match the repository's.
    """
    if not os.path.exists(os.path.join(DATA, "balance.csv")):
        generate(["--out", DATA, "--quiet"])
    store = EventStore.load(DATA)
    bt = run(store, estimated=True, intervals=True)
    return store, bt, build_exceptions(bt, store), accuracy_summary(store)


@st.cache_resource(show_spinner="Building the forecast…")
def forecast_for(day: int):
    """One vantage day's forecast, with uncertainty bands.

    A band is a statement about how wrong this forecaster has been *before*, so
    producing one means replaying every earlier vantage point. Cached per day.
    """
    store = EventStore.load(DATA)
    as_of = store.date_for_day(day)
    world = world_as_of(store, as_of)
    return forecast(
        world, 14,
        estimator=Estimator.fit(world, horizon_days=14),
        bands=_intervals_up_to(store, as_of, 14).band_fn(),
    )


def groq_key() -> str | None:
    """Environment first, then Streamlit secrets, then a local .env."""
    if os.environ.get("GROQ_API_KEY"):
        return os.environ["GROQ_API_KEY"]
    try:
        return st.secrets["GROQ_API_KEY"]
    except Exception:
        pass
    try:
        from dotenv import load_dotenv

        load_dotenv()
        return os.environ.get("GROQ_API_KEY")
    except Exception:
        return None


def kpi(label: str, value: str, foot: str, gold: bool = False) -> str:
    cls = "kpi-value gold" if gold else "kpi-value"
    return (f"<div class='kpi'><div class='kpi-label'>{label}</div>"
            f"<div class='{cls}'>{value}</div>"
            f"<div class='kpi-foot'>{foot}</div></div>")


store, bt, exceptions, accuracy = load()
rows = {r.horizon: r for r in bt.by_horizon()}


# --------------------------------------------------------------------------
# Charts
# --------------------------------------------------------------------------


def chart_rows(f) -> list[dict]:
    """One record per forecast day, plus day 0 anchored on today's real balance.

    Amounts become rupees here because a chart axis is presentation. Every exact
    figure in a tooltip is still the formatted integer from src.money.
    """
    out = [{
        "horizon": 0,
        "date": "today",
        "balance": f.opening / 100,
        "low": None, "high": None, "range": "known",
        "money": fmt_inr(f.opening),
        "certain": 1.0, "certain_pct": "100%", "full": 1.0,
        "mark": "", "trough": False,
    }]
    for p in f.days:
        flags = []
        if Flag.TROUGH in p.flags:
            flags.append("tightest day")
        if Flag.BREACH in p.flags:
            flags.append("falls below what is owed")
        if Flag.AT_RISK in p.flags:
            flags.append("at risk")
        out.append({
            "horizon": p.horizon,
            "date": p.date.isoformat(),
            "balance": p.closing / 100,
            "low": p.band_low / 100 if p.band_low is not None else None,
            "high": p.band_high / 100 if p.band_high is not None else None,
            "range": (f"{fmt_inr(p.band_low)} – {fmt_inr(p.band_high)}"
                      if p.band_low is not None else "—"),
            "money": fmt_inr(p.closing),
            "certain": p.certain_share,
            "certain_pct": f"{p.certain_share:.0%}",
            "full": 1.0,
            "mark": " · ".join(flags),
            "trough": Flag.TROUGH in p.flags,
        })
    return out


def balance_chart(f, recs: list[dict]) -> alt.LayerChart:
    """Projected balance, its 80% band, the floor, and the tightest day.

    One series, so no legend -- the title names it. The band widening to the right
    is the whole argument: near-certain tomorrow, openly uncertain in a fortnight.
    """
    data = alt.Data(values=recs)
    x = alt.X("horizon:Q",
              title="days ahead",
              scale=alt.Scale(domain=[0, 14], nice=False),
              axis=alt.Axis(values=list(range(0, 15, 2)), grid=False,
                            domainColor=GRID, tickColor=GRID, labelColor=MUTED,
                            titleColor=MUTED, labelFontSize=11, titleFontSize=11,
                            titlePadding=12))
    y = alt.Y("balance:Q",
              title="closing balance",
              scale=alt.Scale(zero=False, nice=True),
              axis=alt.Axis(format="~s", grid=True, gridColor=GRID, gridDash=[2, 4],
                            domain=False, ticks=False, labelColor=MUTED,
                            titleColor=MUTED, labelFontSize=11, titleFontSize=11,
                            labelPadding=8))
    tip = [
        alt.Tooltip("date:N", title="date"),
        alt.Tooltip("money:N", title="balance"),
        alt.Tooltip("range:N", title="80% range"),
        alt.Tooltip("certain_pct:N", title="already committed"),
        alt.Tooltip("mark:N", title="flags"),
    ]

    # No curve interpolation. A smoothed line passes through balances the forecast
    # never produced, which in a solvency chart is a quiet lie -- the reader would
    # be reading money off the gaps between days.
    base = alt.Chart(data)
    band = base.mark_area(color="#D6AC55", opacity=0.20).encode(
        x=x, y=alt.Y("low:Q", title="closing balance",
                     scale=alt.Scale(zero=False, nice=True)),
        y2="high:Q")
    line = base.mark_line(color=GOLD, strokeWidth=2).encode(x=x, y=y)

    floor = (alt.Chart(alt.Data(values=[{"floor": f.floor / 100}]))
             .mark_rule(color=MUTED, strokeDash=[5, 5], strokeWidth=1)
             .encode(y="floor:Q"))
    floor_text = (alt.Chart(alt.Data(values=[{"floor": f.floor / 100,
                                              "t": f"floor {fmt_inr(f.floor)}"}]))
                  .mark_text(align="left", dx=4, dy=-8, color=MUTED, fontSize=10.5)
                  .encode(x=alt.value(4), y="floor:Q", text="t:N"))

    troughs = [r for r in recs if r["trough"]]
    layers = [band, floor, floor_text]
    if troughs:
        td = alt.Data(values=troughs)
        # The label hangs from the top of the plot on a dropline rather than
        # floating beside the dot, so it cannot collide with the line or the
        # y-axis. It flips side near the right edge for the same reason.
        right = troughs[0]["horizon"] > 10
        layers.append(alt.Chart(td).mark_rule(
            color=RED, strokeWidth=1, strokeDash=[3, 3], opacity=0.65).encode(x=x))
        layers.append(line)
        layers.append(alt.Chart(td).mark_point(
            size=150, color=RED, filled=True, stroke=SURFACE, strokeWidth=2,
        ).encode(x=x, y=y, tooltip=tip))
        layers.append(alt.Chart(td).mark_text(
            align="right" if right else "left", dx=-7 if right else 7, dy=1,
            baseline="top", color=RED, fontSize=11, fontWeight=600,
        ).encode(x=x, y=alt.value(0), text=alt.value("Tightest day")))
    else:
        layers.append(line)

    hover = alt.selection_point(fields=["horizon"], nearest=True,
                                on="pointermove", empty=False, clear="pointerout")
    layers.append(base.mark_rule(color=MUTED, strokeWidth=1, opacity=0.45)
                  .encode(x=x).transform_filter(hover))
    layers.append(base.mark_point(size=220, opacity=0)
                  .encode(x=x, y=y, tooltip=tip).add_params(hover))
    layers.append(base.mark_point(size=55, color=GOLD, filled=True,
                                  stroke=SURFACE, strokeWidth=2)
                  .encode(x=x, y=y).transform_filter(hover))

    return (alt.layer(*layers)
            .properties(height=330, background=SURFACE, padding=PADDING)
            .configure_view(strokeWidth=0))


def certainty_chart(recs: list[dict]) -> alt.LayerChart:
    """Share of each day already committed rather than estimated.

    Magnitude over time, so one hue, stronger where more is committed. Each day
    gets a full-height track behind it: on most days nothing is committed, and an
    empty track says that, where a bare axis would just look broken.
    """
    data = alt.Data(values=[r for r in recs if r["horizon"] > 0])

    # Both layers carry an identical axis spec so Vega-Lite merges them into one.
    # Setting axis=None on the second layer does not hide that layer's axis -- it
    # removes the shared one, and the chart comes out with no axes at all.
    x = alt.X("horizon:O", title="days ahead",
              scale=alt.Scale(paddingInner=0.28),
              axis=alt.Axis(grid=False, domainColor=GRID, ticks=False,
                            labelColor=MUTED, titleColor=MUTED, labelFontSize=11,
                            titleFontSize=11, titlePadding=10, labelAngle=0))

    def y_for(field: str) -> alt.Y:
        return alt.Y(f"{field}:Q", title="already committed",
                     scale=alt.Scale(domain=[0, 1]),
                     axis=alt.Axis(format="%", grid=True, gridColor=GRID,
                                   gridDash=[2, 4], domain=False, ticks=False,
                                   values=[0, 0.5, 1], labelColor=MUTED,
                                   titleColor=MUTED, labelFontSize=11,
                                   titleFontSize=11, labelPadding=8))

    track = (alt.Chart(data)
             .mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3,
                       color="#191D26")
             .encode(x=x, y=y_for("full")))
    bars = (alt.Chart(data)
            .mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3)
            .encode(
                x=x,
                y=y_for("certain"),
                color=alt.Color("certain:Q",
                                scale=alt.Scale(range=TEAL, domain=[0, 1]),
                                legend=None),
                tooltip=[alt.Tooltip("date:N", title="date"),
                         alt.Tooltip("certain_pct:N", title="already committed"),
                         alt.Tooltip("money:N", title="balance")]))
    return (alt.layer(track, bars)
            .properties(height=150, background=SURFACE, padding=PADDING)
            .configure_view(strokeWidth=0))


# --------------------------------------------------------------------------
# Header and hero
# --------------------------------------------------------------------------

intro = "" if st.session_state.get("brand_shown") else " intro"
st.session_state["brand_shown"] = True
st.markdown(f"<div class='brand{intro}'>cash<span class='cast'>cast</span></div>",
            unsafe_allow_html=True)

st.markdown(
    f"<div class='hero'>"
    f"<div class='badge'>Settlement timing, fees and refunds "
    f"&nbsp;·&nbsp; <b>Not just sales</b></div>"
    f"<div class='headline'><span class='dim'>Know what&rsquo;s in the bank</span>"
    f"<br>before it&rsquo;s there.</div>"
    f"<div class='subhead'>cashcast projects a merchant's cash position fourteen "
    f"days ahead — and says how much of that is already certain and how much is "
    f"still an estimate.</div>"
    f"</div>",
    unsafe_allow_html=True,
)


# --------------------------------------------------------------------------
# Navigation
# --------------------------------------------------------------------------

view = st.segmented_control(
    "View", ["Forecast", "Ask it"], default="Forecast",
    label_visibility="collapsed",
) or "Forecast"

# The slider exists only on the Forecast view, and Streamlit drops the state of
# any widget it did not render on a given run. So the chosen day lives in a
# plain session key rather than a widget key -- otherwise switching to the agent
# and back silently resets it. The agent needs the same forecast either way.
st.session_state.setdefault("vantage", 57)

if view == "Forecast":
    st.caption("Choose a day to stand on. The forecast uses only what the merchant "
               "could have known that morning.")
    st.session_state["vantage"] = st.slider(
        "Standing on day", 46, 106, value=st.session_state["vantage"],
        label_visibility="collapsed")

f = forecast_for(st.session_state["vantage"])
recs = chart_rows(f)

if view == "Forecast":

    # What a merchant opens the app to find out, not how the model scored.
    trough = next((p for p in f.days if Flag.TROUGH in p.flags), None)
    short_days = sum(1 for p in f.days if p.closing < f.floor)

    st.markdown("<div style='height:0.6rem'></div>", unsafe_allow_html=True)
    k1, k2, k3, k4 = st.columns(4, gap="medium")
    k1.markdown(kpi("Balance today", fmt_inr(f.opening),
                    f.as_of.strftime("%d %b %Y")), unsafe_allow_html=True)
    k2.markdown(kpi("Tightest day",
                    fmt_inr(trough.closing) if trough else "—",
                    (f"{trough.date.strftime('%d %b')} · {trough.horizon} days away"
                     if trough else "none in this window"), True),
                unsafe_allow_html=True)
    k3.markdown(kpi("Due in this window", fmt_inr(f.floor),
                    "the largest single bill you owe"), unsafe_allow_html=True)
    k4.markdown(kpi("Days below that", f"{short_days} of {len(f.days)}",
                    "when the balance will not cover it"), unsafe_allow_html=True)

    st.markdown("<div style='height:1.6rem'></div>", unsafe_allow_html=True)

    chart_col, side_col = st.columns([3, 2], gap="large")

    with chart_col:
        st.altair_chart(balance_chart(f, recs), width="stretch")
        st.markdown(
            f"<p class='note'>The shaded band is where the balance lands 80% of "
            f"the time. The dashed line is the <b>floor</b> — {fmt_inr(f.floor)}, "
            f"the largest single bill due in this window.</p>",
            unsafe_allow_html=True,
        )

        with st.expander("How the band is built"):
            st.write(
                "From this forecaster's own past errors at each distance. It "
                "widens toward day 14 because accuracy genuinely falls with "
                "distance."
            )
            st.write(
                "The floor is taken from the merchant's own commitments in the "
                "window rather than set to a round number."
            )

        st.markdown("<div style='height:1.4rem'></div>", unsafe_allow_html=True)
        st.altair_chart(certainty_chart(recs), width="stretch")
        st.markdown(
            "<p class='note'>How much of each day is <b>already committed</b> — "
            "money captured and in transit, or a bill already raised. The rest is "
            "estimated.</p>",
            unsafe_allow_html=True,
        )

        with st.expander("Why most days are empty"):
            st.write(
                f"Past the first few days there is very little the merchant "
                f"already holds. Averaged over all {len(bt.windows)} starting "
                f"days, the committed share falls from 100% tomorrow to "
                f"{rows[14].mean_certain_share:.0%} at fourteen days."
            )

    with side_col:
        st.markdown("<div class='kpi-label'>Days worth attention</div>",
                    unsafe_allow_html=True)
        for p in f.flagged():
            names = " + ".join(sorted(x.value.replace("_", " ") for x in p.flags))
            calm = "" if Flag.BREACH in p.flags else " calm"
            st.markdown(
                f"<div class='flagcard{calm}'>"
                f"<b>+{p.horizon} · {p.date}</b> — <span class='tnum'>"
                f"{fmt_inr(p.closing)}</span> · {names}"
                f"<div class='why'>{p.reason()}</div></div>",
                unsafe_allow_html=True,
            )
        if not f.flagged():
            st.markdown("<p class='note'>Nothing flagged in this window.</p>",
                        unsafe_allow_html=True)

        with st.expander("Day by day, in figures"):
            st.dataframe(
                [{
                    "day": f"+{r['horizon']}",
                    "date": r["date"],
                    "balance": r["money"],
                    "80% range": r["range"],
                    "committed": r["certain_pct"],
                    "": r["mark"],
                } for r in recs if r["horizon"] > 0],
                width="stretch", hide_index=True,
            )


else:
    st.caption("Ask about the next fourteen days. The model chooses which tools to "
               "call and writes the sentence; it never does the arithmetic.")

    key = groq_key()
    if not key:
        st.warning(
            "No `GROQ_API_KEY` configured, so the agent is unavailable here. "
            "Everything else on the page still works."
        )
    else:
        ask_left, ask_right = st.columns([3, 2], gap="large")

        with ask_left:
            examples = [
                "When is my tightest day over the next two weeks?",
                "Can I pay a supplier 2,00,000 rupees in 9 days?",
                "Why is my tightest day so low?",
                "How much of the day 14 forecast is guesswork?",
                "How accurate has this forecast been in the past?",
                "What will my balance be in three months?",
                "What was my profit last month?",
            ]
            picked = st.selectbox("Try one, or write your own", [""] + examples)
            question = st.text_input("Question", value=picked,
                                     label_visibility="collapsed",
                                     placeholder="Ask about the next 14 days…")

            if st.button("Ask", type="primary", disabled=not question.strip()):
                with st.spinner("Thinking…"):
                    answer = ask(question, GroqModel(DEFAULT_MODEL, key),
                                 bind(f, accuracy), SCHEMAS)

                if answer.rejected:
                    st.error(answer.shown)
                    with st.expander("What it tried to say"):
                        st.write(answer.text)
                else:
                    st.success(answer.shown)

                with st.expander(
                        f"How it got there — {len(answer.tool_calls)} tool call(s)"):
                    if not answer.tool_calls:
                        st.write("No tools were called.")
                    for call, out in zip(answer.tool_calls, answer.tool_outputs):
                        args = ", ".join(f"{k}={v}"
                                         for k, v in call.arguments.items())
                        st.markdown(f"**`{call.name}({args})`**")
                        st.json(out, expanded=False)

        with ask_right:
            st.markdown(
                "<div class='kpi-label'>The guardrail</div>"
                "<p class='note'>Every number in the answer is checked against "
                "what the tools returned. An answer containing a figure no tool "
                "produced is <b>rejected rather than shown</b>.</p>",
                unsafe_allow_html=True,
            )
            st.markdown("<div style='height:1.3rem'></div>",
                        unsafe_allow_html=True)
            st.markdown(
                "<div class='kpi-label'>Out of scope</div>"
                "<p class='note'>Anything beyond fourteen days. Predicting a "
                "chargeback. Profit and tax, which are accrual questions where "
                "this forecasts cash.</p>"
                "<p class='note' style='margin-top:0.7rem'>The last two examples "
                "in the list are there to be refused.</p>",
                unsafe_allow_html=True,
            )


st.markdown("<div style='height:2.4rem'></div>", unsafe_allow_html=True)
st.markdown(
    "<p class='kpi-foot' style='text-align:center'>Synthetic data — 120 days of a "
    "fictional D2C fashion merchant. "
    "<a href='https://github.com/khushisaraswat03/cashcast' "
    "style='color:#B98C33'>Source on GitHub</a></p>",
    unsafe_allow_html=True,
)
