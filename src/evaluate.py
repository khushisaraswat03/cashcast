"""Scoring the agent against a fixed question set.

Fourteen questions: seven it should answer and seven it should refuse. The list was
fixed before any of them were run, which is the point -- choosing questions after
seeing which ones a model does well on is the same cherry-picking the brief rules
out, whether or not you notice you are doing it.

Each answer is scored on four things that a machine can check:

* **tool** -- did it call a sensible tool, or answer from thin air?
* **figure** -- does the answer contain the number the tool actually returned?
* **guardrail** -- did every figure in it trace back to a tool?
* **refusal** -- for the seven it should decline, did it decline *and say why*?

What a machine cannot check is whether the prose is any good, so that stays a human
job and is not scored here.

The refusals are half the set on purpose. The brief's *"verification capacity, not
generation speed, is the bottleneck"* is a statement about knowing what you cannot
answer. A system that answers everything has not been careful, it has been lucky.

Each refusal is for a *different structural reason*, which matters more than the
count: beyond the horizon, unpredictable in principle, an accrual question the cash
data cannot answer, a business that is not in the data, advice rather than fact, and
a figure the tools never produced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .agent import Answer, Model, ask
from .tools import SCHEMAS


@dataclass(frozen=True)
class Question:
    text: str
    #: Tools any sensible route to an answer would use. Empty when refusing.
    expects_tools: tuple[str, ...] = ()
    #: A tool result key whose value must appear in the answer, if any.
    expects_figure: str | None = None
    should_refuse: bool = False
    why: str = ""


#: The seven it should answer.
ANSWERABLE: tuple[Question, ...] = (
    Question(
        "What will my bank balance be in 7 days?",
        expects_tools=("get_forecast",),
        expects_figure="projected_balance",
        why="the core question the whole system exists to answer",
    ),
    Question(
        "When is my tightest day over the next two weeks?",
        expects_tools=("get_tightest_day",),
        expects_figure="projected_balance",
        why="the product's headline claim: naming the day, not just the number",
    ),
    Question(
        "Why is my tightest day so low?",
        expects_tools=("get_tightest_day", "explain_day"),
        why="only answerable because every day carries its receipts",
    ),
    Question(
        "Can I pay a supplier 2,00,000 rupees in 9 days?",
        expects_tools=("can_i_afford",),
        expects_figure="margin",
        why="a comparison, and therefore arithmetic, so it must be a tool",
    ),
    Question(
        "How much of the day 14 forecast is guesswork rather than money I already have?",
        expects_tools=("get_forecast",),
        why="the certain/estimated split -- what most forecasters cannot tell you",
    ),
    Question(
        "How accurate has this forecast been in the past?",
        expects_tools=("get_accuracy",),
        why="measured rather than claimed, and rare to be able to answer at all",
    ),
    Question(
        "Could I still afford 1,50,000 rupees in 7 days if things go badly?",
        expects_tools=("can_i_afford",),
        why="the pessimistic case, which only exists because Bucket 3 gives a band",
    ),
)

#: The seven it should refuse, each for a different structural reason.
REFUSABLE: tuple[Question, ...] = (
    Question("What will my balance be in three months?", should_refuse=True,
             why="beyond the horizon: nothing in the pipeline reaches that far"),
    Question("Will I get a chargeback next week?", should_refuse=True,
             why="unpredictable in principle, not merely unpredicted"),
    Question("What was my profit last month?", should_refuse=True,
             why="an accrual concept; cash data cannot answer it"),
    Question("How much GST do I owe this quarter?", should_refuse=True,
             why="a tax liability, not a cash movement"),
    Question("How is my other shop in Pune doing?", should_refuse=True,
             why="a business that is not in the data"),
    Question("Should I take a loan to cover the shortfall?", should_refuse=True,
             why="advice, not a fact about the forecast"),
    Question("Roughly what is my average daily profit margin?", should_refuse=True,
             why="both an accrual question and one requiring arithmetic"),
)

QUESTIONS = ANSWERABLE + REFUSABLE

#: Words that mark a decline.
#:
#: **First person, deliberately.** A bare "cannot" is not a refusal -- it depends
#: entirely on the subject. "*You* cannot afford ₹2,00,000 on Thursday" is a correct
#: answer to the question asked; "*I* cannot tell you" is a decline. Matching on
#: "cannot" alone scored a right answer as a refusal, which is the same class of
#: error as the apostrophe: an instrument that misreads its own subject.
#:
#: The impersonal entries below are kept because they are unambiguous about scope
#: rather than about the merchant's balance.
_REFUSAL_MARKERS = (
    "i can't", "i cannot", "i can not", "i'm unable", "i am unable",
    "i don't have", "i do not have", "i'm not able", "i am not able",
    "i can only", "i'm sorry", "i am sorry", "i don't provide", "i do not provide",
    "outside the scope", "outside what", "beyond the", "only covers",
    "not something i",
)


def _normalise(text: str) -> str:
    """Fold the punctuation a model actually writes into what a matcher expects.

    Models produce typographic apostrophes and dashes. The first run of this
    evaluation scored 1 refusal out of 7 when the true figure was 7 out of 7 --
    every answer said "can’t" with U+2019 and the marker list said "can't" with
    an ASCII quote. A measurement instrument that fails on punctuation reports
    a wrong number confidently, which is worse than reporting nothing.
    """
    return (
        text.lower()
        .replace("’", "'")
        .replace("‘", "'")
        .replace("‑", "-")
        .replace("–", "-")
        .replace("—", "-")
        .replace(" ", " ")
    )


def _looks_like_refusal(text: str) -> bool:
    low = _normalise(text)
    return any(m in low for m in _REFUSAL_MARKERS)


def _gives_a_reason(text: str) -> bool:
    """A bare "I can't" is a worse answer than one that says why and offers an
    alternative. Refusing well is where the value is, so it is scored separately."""
    low = _normalise(text)
    return any(m in low for m in ("because", "only", "beyond", "not in", "instead",
                                 "i can tell you", "i can give", "however", "but i"))


def _figures_in(outputs: Sequence[Any], key: str) -> list[float]:
    out = []
    for o in outputs:
        if isinstance(o, dict) and isinstance(o.get(key), (int, float)):
            out.append(float(o[key]))
    return out


_NUM = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def _quotes(text: str, value: float) -> bool:
    """Allows the same restatements the guardrail does: rounding, and a negative
    margin written as a positive shortfall."""
    seen = {float(m.replace(",", "")) for m in _NUM.findall(text)}
    return any(
        abs(s - t) <= max(1.0, 0.005 * abs(t))
        for s in seen
        for t in (value, abs(value))
    )


@dataclass(frozen=True)
class Score:
    question: Question
    answer: Answer
    called_expected_tool: bool
    quoted_expected_figure: bool | None
    refused: bool
    gave_a_reason: bool

    @property
    def passed(self) -> bool:
        if self.question.should_refuse:
            return self.refused and self.gave_a_reason
        # Refusing an answerable question is a failure, and it was scored as a pass
        # until a real run caught it: asked what share of day 14 was guesswork, the
        # model declined because working it out needed arithmetic it is forbidden.
        # It called the right tool and quoted nothing wrong, so every other check
        # was satisfied. The tool was at fault -- it returned only the certain share
        # and left the complement to be derived -- but the scoring should have said
        # so rather than passing it.
        return (
            self.called_expected_tool
            and not self.refused
            and not self.answer.rejected
            and self.quoted_expected_figure is not False
        )


def score_one(q: Question, model: Model, tools: dict[str, Callable[..., dict]]) -> Score:
    a = ask(q.text, model, tools, SCHEMAS)
    names = {c.name for c in a.tool_calls}
    figure = None
    if q.expects_figure:
        values = _figures_in(a.tool_outputs, q.expects_figure)
        figure = any(_quotes(a.text, v) for v in values) if values else False
    return Score(
        question=q,
        answer=a,
        called_expected_tool=(
            True if not q.expects_tools else bool(names & set(q.expects_tools))
        ),
        quoted_expected_figure=figure,
        refused=_looks_like_refusal(a.text),
        gave_a_reason=_gives_a_reason(a.text),
    )


@dataclass
class Report:
    model_name: str
    scores: list[Score] = field(default_factory=list)

    @property
    def answered(self) -> list[Score]:
        return [s for s in self.scores if not s.question.should_refuse]

    @property
    def refusals(self) -> list[Score]:
        return [s for s in self.scores if s.question.should_refuse]

    @property
    def guardrail_catches(self) -> int:
        return sum(1 for s in self.scores if s.answer.rejected)

    def render(self) -> str:
        w = 78
        out = [f"agent evaluation -- {self.model_name}", "-" * w]
        for label, group in (("should answer", self.answered),
                             ("should refuse", self.refusals)):
            out += ["", label, ""]
            for s in group:
                mark = "PASS" if s.passed else "FAIL"
                out.append(f"  [{mark}] {s.question.text}")
                bits = []
                if s.question.expects_tools:
                    bits.append("tool " + ("ok" if s.called_expected_tool else "MISSED"))
                if s.quoted_expected_figure is not None:
                    bits.append("figure " + ("ok" if s.quoted_expected_figure else "MISSED"))
                if s.question.should_refuse:
                    bits.append("refused" if s.refused else "DID NOT REFUSE")
                    bits.append("reason given" if s.gave_a_reason else "NO REASON")
                if s.answer.rejected:
                    bits.append("GUARDRAIL REJECTED")
                out.append(f"         {' · '.join(bits)}")
                out.append(f"         called: {[c.name for c in s.answer.tool_calls]}")

        a_ok = sum(1 for s in self.answered if s.passed)
        r_ok = sum(1 for s in self.refusals if s.passed)
        out += [
            "", "-" * w,
            f"  answered correctly   {a_ok}/{len(self.answered)}",
            f"  refused correctly    {r_ok}/{len(self.refusals)}",
            f"  guardrail rejections {self.guardrail_catches}/{len(self.scores)}"
            "   (arithmetic caught before it reached the merchant)",
        ]
        return "\n".join(out)


def evaluate(model: Model, tools, name: str = "model") -> Report:
    r = Report(model_name=name)
    for q in QUESTIONS:
        r.scores.append(score_one(q, model, tools))
    return r


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    from .agent import GroqModel, accuracy_summary, load_key
    from .estimate import Estimator
    from .forecast import _intervals_up_to, forecast
    from .tools import bind
    from .world import EventStore, world_as_of

    p = argparse.ArgumentParser(description="Score the agent on a fixed question set.")
    p.add_argument("--data", default="data")
    p.add_argument("--day", type=int, default=57)
    p.add_argument("--model", action="append", required=True,
                   help="Groq model id; repeat to compare several")
    p.add_argument("--show-answers", action="store_true")
    args = p.parse_args(argv)

    store = EventStore.load(args.data)
    as_of = store.date_for_day(args.day)
    world = world_as_of(store, as_of)
    f = forecast(world, 14, estimator=Estimator.fit(world, horizon_days=14),
                 bands=_intervals_up_to(store, as_of, 14).band_fn())
    tools = bind(f, accuracy_summary(store))
    key = load_key()

    for name in args.model:
        report = evaluate(GroqModel(name, key), tools, name)
        print(report.render())
        if args.show_answers:
            print()
            for s in report.scores:
                print(f"  Q: {s.question.text}")
                print(f"  A: {s.answer.shown}\n")
        print()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
