"""cashcast — a browser view of the forecaster, its accuracy, and its agent.

A presentation layer. Everything shown is computed by `src/`; this file adds no
arithmetic of its own, which is the rule the agent works under too.

The heavy work -- 854 backtested forecasts and the replayed error history behind
the uncertainty bands -- is cached, because Streamlit reruns the whole script on
every widget change.
"""

from __future__ import annotations

import os
import statistics

import altair as alt
import streamlit as st

from src.agent import GroqModel, accuracy_summary, ask
from src.backtest import RECENT_AVERAGE, run
from src.estimate import Estimator
from src.exceptions import REMEDY, Cause
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
      .block-container {padding-top: 2.6rem; max-width: 1240px;}

      html, body, [class*="css"] {
        font-feature-settings: "ss01", "cv05";
        -webkit-font-smoothing: antialiased;
      }
      .stDataFrame, code, .tnum {font-variant-numeric: tabular-nums;}

      .wordmark {
        font-size: 4.4rem; font-weight: 660; letter-spacing: -0.055em;
        line-height: 0.95; margin: 0; color: #F2F4F7;
      }
      .wordmark span {color: #B98C33;}
      .oneliner {
        color: #F2F4F7; font-size: 1.3rem; font-weight: 420; line-height: 1.4;
        letter-spacing: -0.015em; max-width: 34ch; margin: 1.1rem 0 0 0;
      }
      .oneliner b {color: #B98C33; font-weight: 560;}
      .lede {
        color: #8B929C; font-size: 0.95rem; line-height: 1.65;
        max-width: 58ch; margin: 0;
      }
      .lede + .lede {margin-top: 0.85rem;}
      .rule {border: none; border-top: 1px solid #1E222B; margin: 2.4rem 0 1.8rem 0;}

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

      h3 {letter-spacing: -0.025em; font-weight: 620 !important;}

      .flagcard {
        border-left: 2px solid #E5484D; padding: 0.15rem 0 0.15rem 0.85rem;
        margin-bottom: 0.9rem;
      }
      .flagcard.calm {border-left-color: #B98C33;}
      .flagcard b {color: #F2F4F7;}
      .flagcard .why {color: #8B929C; font-size: 0.82rem; line-height: 1.45;}

      [data-baseweb="tab-list"] {gap: 2rem; border-bottom: 1px solid #1E222B;}
      [data-baseweb="tab"] {
        padding: 0.7rem 0 !important; font-size: 0.94rem; font-weight: 520;
      }
      [data-baseweb="tab-highlight"] {background-color: #B98C33 !important;}
      [data-baseweb="tab-border"] {display: none;}
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
median_balance = statistics.median(b.closing for b in store.balances)
cal = bt.calibration()
calibration_hit = sum(c.covered for c in cal) / sum(c.n for c in cal)


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
        ).encode(x=x, y=alt.value(0), text=alt.value("tightest day")))
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
# Landing
# --------------------------------------------------------------------------

hero_left, hero_right = st.columns([5, 4], gap="large")

with hero_left:
    st.markdown(
        "<p class='wordmark'>cash<span>cast</span></p>"
        "<p class='oneliner'>Fourteen days of your bank balance — and how much "
        "of it is <b>actually a guess</b>.</p>",
        unsafe_allow_html=True,
    )

with hero_right:
    st.markdown("<div style='height:1.1rem'></div>", unsafe_allow_html=True)
    st.markdown(
        "<p class='lede'>When a customer pays, the money does not arrive. It sits "
        "with the payment gateway for a working day or two, lands minus fees, and "
        "refunds are taken out of a <i>later</i> payout than the sale they undo.</p>"
        "<p class='lede'>So a shop that sold well this week still cannot say "
        "whether payroll clears on Friday. This answers that — and says plainly "
        "how much of the answer is certain and how much is estimated.</p>",
        unsafe_allow_html=True,
    )

st.markdown("<div style='height:1.8rem'></div>", unsafe_allow_html=True)

k1, k2, k3, k4 = st.columns(4, gap="medium")
k1.markdown(kpi("Error tomorrow", "₹0", "exact, every time it was tested", True),
            unsafe_allow_html=True)
k2.markdown(kpi("Error at 14 days", f"{rows[14].mae / median_balance:.1%}",
                f"about {fmt_inr(rows[14].mae)} on a typical balance"),
            unsafe_allow_html=True)
k3.markdown(kpi("Shortfall calls", f"{bt.breach_accuracy():.0%}",
                "correctly called, days before they happen"),
            unsafe_allow_html=True)
k4.markdown(kpi("Honest about itself", f"{calibration_hit:.0%}",
                "claimed 80% confidence, delivered this"),
            unsafe_allow_html=True)

st.markdown(
    f"<p class='kpi-foot' style='margin-top:1rem'>Measured over "
    f"<b style='color:#F2F4F7'>{len(bt.predictions)} forecasts</b> — "
    f"{len(bt.windows)} different days to stand on, {bt.horizon} days ahead from "
    f"each — every one scored against what actually happened.</p>"
    f"<hr class='rule'>",
    unsafe_allow_html=True,
)


# --------------------------------------------------------------------------
# Tabs
# --------------------------------------------------------------------------

tab_forecast, tab_ask, tab_accuracy, tab_limits = st.tabs(
    ["Forecast", "Ask it", "Accuracy", "Limits"]
)


with tab_forecast:
    st.markdown("<div style='height:0.8rem'></div>", unsafe_allow_html=True)
    st.caption("Pick a day to stand on. The forecaster sees only what the merchant "
               "could have known that morning.")

    day = st.slider("Standing on day", 46, 106, 57, label_visibility="collapsed")
    f = forecast_for(day)
    recs = chart_rows(f)

    chart_col, side_col = st.columns([3, 2], gap="large")

    with chart_col:
        st.altair_chart(balance_chart(f, recs), width="stretch")
        st.markdown(
            f"<p class='note'>The shaded band is where the balance lands 80% of the "
            f"time. Dashed line is the <b>floor</b> — {fmt_inr(f.floor)}, the "
            f"largest single bill due in this window.</p>",
            unsafe_allow_html=True,
        )

        with st.expander("Why the band gets wider"):
            st.write(
                "Because the forecast genuinely gets worse with distance. The band "
                "is built from this forecaster's own past errors at each horizon, "
                "so it is a measured statement rather than a decoration. A band "
                "that stayed narrow across a fortnight would be the dishonest one."
            )
            st.write(
                "The floor is derived rather than chosen. *“Why ₹1,00,000?”* has no "
                "good answer; *“because that is what you owe on the 12th”* does."
            )

        st.markdown("<div style='height:1.4rem'></div>", unsafe_allow_html=True)
        st.altair_chart(certainty_chart(recs), width="stretch")
        st.markdown(
            "<p class='note'>How much of each day is <b>already committed</b> — "
            "money captured and merely in transit, or a bill already raised. "
            "Everything else is an estimate.</p>",
            unsafe_allow_html=True,
        )

        with st.expander("Why most days are empty"):
            st.write(
                f"Past the first few days there is very little the merchant "
                f"already holds, so almost everything that far out is estimated. "
                f"Averaged over all {len(bt.windows)} vantage points the committed "
                f"share falls from 100% tomorrow to "
                f"{rows[14].mean_certain_share:.0%} at fourteen days."
            )
            st.write(
                "The error is allowed to rise as that share falls. That "
                "relationship is what stops the accuracy claim being unearned."
            )

    with side_col:
        st.markdown(
            f"<div class='kpi-label'>Balance today</div>"
            f"<div class='kpi-value tnum'>{fmt_inr(f.opening)}</div>"
            f"<div style='height:1.6rem'></div>",
            unsafe_allow_html=True,
        )
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


with tab_ask:
    st.markdown("<div style='height:0.8rem'></div>", unsafe_allow_html=True)
    st.caption("The model decides what to look at and writes the sentence. "
               "It never does the arithmetic.")

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
                        st.write(
                            "No tools called. For a question it should refuse, "
                            "that is the right behaviour — there is nothing to "
                            "look up."
                        )
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
            st.markdown("<div style='height:1.2rem'></div>",
                        unsafe_allow_html=True)
            st.markdown(
                "<div class='kpi-label'>What it refuses</div>"
                "<p class='note'>Anything beyond 14 days. Predicting a chargeback "
                "— unpredictable in principle, not merely unpredicted. Profit and "
                "tax, which are accrual questions when this forecasts cash.</p>"
                "<p class='note' style='margin-top:0.7rem'><b>Refusing well is the "
                "point</b>, not a fallback. The last two examples in the list are "
                "there to be refused.</p>",
                unsafe_allow_html=True,
            )


with tab_accuracy:
    st.markdown("<div style='height:0.8rem'></div>", unsafe_allow_html=True)
    st.caption("Nothing here is claimed. Every figure was measured by forecasting "
               "the past and checking against what happened.")

    acc_left, acc_right = st.columns(2, gap="large")

    with acc_left:
        st.markdown("**Error by horizon**")
        st.markdown(
            "<p class='note'>Never averaged into one number. Tomorrow is nearly "
            "free; a fortnight is mostly guesswork.</p>",
            unsafe_allow_html=True,
        )
        st.dataframe(
            [{
                "days ahead": r.horizon,
                "typical error": fmt_inr(r.mae),
                "of balance": f"{r.mae / median_balance:.1%}",
                "a rule doing no work": fmt_inr(r.baseline_mae[RECENT_AVERAGE]),
                "committed": f"{r.mean_certain_share:.0%}",
            } for r in bt.by_horizon()],
            width="stretch", hide_index=True,
        )
        st.markdown(
            "<p class='note'><b>A rule doing no work</b> is the average of the "
            "last 14 balances — picked in advance as the strongest of five "
            "trivial rules, not chosen afterwards for being easy to beat.</p>",
            unsafe_allow_html=True,
        )

    with acc_right:
        st.markdown("**Are the uncertainty bands honest?**")
        st.markdown(
            "<p class='note'>Saying “80% confident” is a checkable claim: the "
            "truth should land inside the band about 80 times in 100.</p>",
            unsafe_allow_html=True,
        )
        st.dataframe(
            [{
                "days ahead": c.horizon,
                "landed inside": f"{c.hit_rate:.0%}",
                "band width": fmt_inr(c.mean_width),
                "verdict": c.verdict,
            } for c in cal],
            width="stretch", hide_index=True,
        )
        st.markdown(
            "<p class='note'>The overconfident rows are reported rather than "
            "tuned away. Bands come only from vantage points <i>before</i> the "
            "one being forecast, so widening them after seeing the answer would "
            "be the cheat this whole measurement exists to prevent.</p>",
            unsafe_allow_html=True,
        )

    st.markdown("<div style='height:1.2rem'></div>", unsafe_allow_html=True)
    with st.expander("Naming the tightest day — where a trivial rule ties it"):
        st.write(
            f"It names the single tightest day out of fourteen correctly "
            f"{bt.trough_accuracy():.0%} of the time. Guessing at random would be "
            f"7%. But a trivial *“it is the day of the biggest bill”* rule also "
            f"gets {bt.baselines().trough_lazy:.0%} — a tie, reported as one."
        )
        st.write(
            "The tightest day usually **is** the day of the biggest bill, so both "
            "rules find it. The difference is that only the forecast knows the "
            "**balance** on that day, which is what decides whether the bill can "
            "actually be paid. The lazy rule names a date and nothing else."
        )


with tab_limits:
    st.markdown("<div style='height:0.8rem'></div>", unsafe_allow_html=True)
    st.caption(f"{len(exceptions.exceptions)} of {exceptions.total_predictions} "
               f"forecasts ({exceptions.share:.0%}) were wrong by more than "
               f"{fmt_inr(exceptions.threshold)} — past the point where the error "
               f"stops being ordinary noise.")

    st.markdown(
        "<p class='note'>Listing bad days would be an apology. Attributing them to "
        "causes, and saying which have a fix, is a system that knows its own "
        "limits.</p>",
        unsafe_allow_html=True,
    )
    st.markdown("<div style='height:1rem'></div>", unsafe_allow_html=True)

    for cause, items in exceptions.by_cause().items():
        fixable, remedy = REMEDY[cause]
        errs = sorted(e.error for e in items)
        with st.expander(
            f"{'FIXABLE' if fixable else 'NOT FIXABLE'} — {cause.value} · "
            f"{len(items)} forecasts · median error {fmt_inr(errs[len(errs) // 2])}"
        ):
            st.write(remedy)
            worst = max(items, key=lambda e: e.error)
            st.caption(
                f"Worst: {worst.prediction.target} — off by {fmt_inr(worst.error)} "
                f"at {worst.prediction.horizon} days out. {worst.detail}."
            )

    for cause in Cause:
        if cause not in exceptions.by_cause():
            with st.expander(f"NOT FIXABLE — {cause.value} · 0 forecasts"):
                st.write(
                    "Checked and found to explain none of the misses in this "
                    "dataset. A cause that was looked for and not found is "
                    "information."
                )
                st.caption(REMEDY[cause][1])

    st.markdown("<div style='height:1.2rem'></div>", unsafe_allow_html=True)
    with st.expander("What this deliberately does not handle"):
        st.markdown(
            "- **Sales-prediction skill cannot be measured here.** The sales "
            "pattern was chosen when the generator was written, so any model that "
            "“discovers” it is discovering a choice. What *can* be measured is "
            "failure behaviour: whether the system notices it is wrong, "
            "quantifies it, and attributes the cause.\n"
            "- **Chargebacks are unpredictable in principle**, not merely "
            "unpredicted — too rare for any realistic dataset to estimate a rate "
            "from.\n"
            "- **One merchant, one bank account, one currency, one gateway, one "
            "promotion.**\n"
            "- **Bank holidays are a placeholder list**, not the real RBI "
            "calendar.\n"
            "- **Fee assumptions are unverified** — 2% plus 18% GST on the fee. "
            "The structure is what is modelled, not the rates."
        )


st.markdown("<hr class='rule'>", unsafe_allow_html=True)
st.markdown(
    "<p class='kpi-foot'>Synthetic data — 120 days of a fictional D2C fashion "
    "merchant, as the brief specifies. "
    "<a href='https://github.com/khushisaraswat03/cashcast' "
    "style='color:#B98C33'>Source</a></p>",
    unsafe_allow_html=True,
)
