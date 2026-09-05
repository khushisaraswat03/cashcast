"""The agent: it decides what to look at and explains. It never does arithmetic.

A seam -- `StubModel` returns scripted tool calls with no network, `GroqModel` talks
to a real one -- so the loop is testable in milliseconds and the provider is a
one-file change.

A loop: ask, let the model call tools, feed results back, let it call more. The
chaining is what makes this an agent rather than a wrapper over five lookups.

A guardrail: every number in the answer is checked against the numbers the tools
produced, and anything else is rejected. "Never do arithmetic" in a prompt is a
request; this makes it enforcement.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence

MAX_STEPS = 6

SYSTEM_PROMPT = """\
You are a cash-flow assistant for a small online merchant. You have tools that return
a 14-day bank balance forecast and how accurate that forecast has been.

Rules, in order of importance:

1. NEVER calculate anything. Do not add, subtract, compare, average or estimate. If
   an answer needs arithmetic, there is a tool for it -- use the tool. Every number
   you write must have come back from a tool call, character for character. Quote
   figures exactly as the tool returned them; do not round them, and do not convert
   them into lakhs or crores.
2. Use the tools before answering. Do not answer from memory or from what seems
   likely.
3. Chain them. If the first result raises an obvious follow-up -- the merchant
   cannot afford something, or a day looks unusual -- look into it before answering.
4. Refuse clearly when you cannot answer, and say why, and say what you can answer
   instead. Refuse if: the question is beyond 14 days; it asks you to predict a
   chargeback or a dispute (unpredictable in principle, not merely unpredicted); it
   is about profit, tax or anything that is not a cash balance; it is about a
   different business; or it asks for advice rather than a fact about the forecast.
5. Be brief. Two or three sentences. A finance person wants the number and the
   reason, not an essay.

Amounts are Indian rupees.
"""


# --------------------------------------------------------------------------
# The seam
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Reply:
    """One model turn: either tool calls, or the final text. Never usefully both."""

    text: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class Model(Protocol):
    def respond(
        self, messages: list[dict[str, Any]], schemas: list[dict[str, Any]]
    ) -> Reply: ...


@dataclass
class StubModel:
    """A scripted model. No network, no key, deterministic.

    Exists so the tool layer, the loop and the guardrail have tests that run in
    milliseconds and never flake -- and so that when a real model is plugged in, any
    new failure is definitely the model rather than the plumbing.
    """

    script: list[Reply] = field(default_factory=list)
    seen: list[list[dict[str, Any]]] = field(default_factory=list)

    def respond(self, messages, schemas) -> Reply:
        self.seen.append(list(messages))
        if not self.script:
            return Reply(text="(stub exhausted)")
        return self.script.pop(0)


@dataclass
class GroqModel:
    """Groq's OpenAI-compatible chat completions API.

    Deliberately thin. Everything interesting -- the tools, the loop, the guardrail
    -- sits above this, so swapping providers is this class and nothing else.
    """

    model: str
    api_key: str
    temperature: float = 0.0  # a forecast assistant should not be creative
    #: The free tier caps tokens per minute, and a fourteen-question evaluation
    #: goes straight through it. Waiting is the correct response to being asked to
    #: wait; failing the run would report a model as broken when it is only busy.
    max_retries: int = 6
    _client: Any = None

    def __post_init__(self) -> None:
        from groq import Groq

        self._client = Groq(api_key=self.api_key)

    def _create(self, messages, schemas):
        import time

        from groq import RateLimitError

        delay = 2.0
        for attempt in range(self.max_retries):
            try:
                return self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=[{"type": "function", "function": s} for s in schemas],
                    temperature=self.temperature,
                )
            except RateLimitError:
                if attempt == self.max_retries - 1:
                    raise
                time.sleep(delay)
                delay *= 2
        raise RuntimeError("unreachable")

    def respond(self, messages, schemas) -> Reply:
        resp = self._create(messages, schemas)
        choice = resp.choices[0].message
        calls = tuple(
            ToolCall(name=c.function.name, arguments=json.loads(c.function.arguments))
            for c in (choice.tool_calls or [])
        )
        return Reply(text=choice.content, tool_calls=calls)


# --------------------------------------------------------------------------
# The guardrail
# --------------------------------------------------------------------------

#: Numbers in the answer. Handles 1,23,456.78 and 123456.78 and 14 and 80%.
_NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?")

#: Small integers a sentence needs to be readable -- day counts, "14 days", "80%",
#: an ordinal. Requiring these to appear in tool output would reject fluent English
#: for no benefit, since none of them can misstate a balance.
_ALLOWED_SMALL = 100

#: How far a quoted figure may sit from the tool value it claims to be. Both
#: rejections on first contact with a real model were rounding, not invention --
#: "short by 46,620" for a margin of -46,619.55. 0.5% is far tighter than any
#: arithmetic error could hide in: a subtracted balance sits tens of percent from
#: every tool value, not half of one.
TOLERANCE_RUPEES = 1.0
TOLERANCE_SHARE = 0.005


def _numbers_in(text: str) -> set[float]:
    out = set()
    for raw in _NUMBER.findall(text):
        try:
            out.add(float(raw.replace(",", "")))
        except ValueError:
            continue
    return out


def _numbers_from(value: Any) -> set[float]:
    """Every number anywhere in a tool result, however nested."""
    out: set[float] = set()
    if isinstance(value, bool):
        return out
    if isinstance(value, (int, float)):
        out.add(float(value))
    elif isinstance(value, dict):
        for v in value.values():
            out |= _numbers_from(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            out |= _numbers_from(v)
    elif isinstance(value, str):
        out |= _numbers_in(value)
    return out


@dataclass(frozen=True)
class GuardrailResult:
    ok: bool
    invented: tuple[float, ...] = ()

    @property
    def message(self) -> str:
        listed = ", ".join(f"{n:,.2f}" for n in self.invented)
        return (
            "Answer rejected: it contained figures no tool produced "
            f"({listed}). Every number has to come from the forecast, so an "
            "invented one is withheld rather than shown."
        )


def check_numbers(answer: str, tool_outputs: Sequence[Any]) -> GuardrailResult:
    """Every number in the answer must have come from a tool.

    Three legitimate restatements are tolerated: small integers ("the next 14 days"
    is English, not a claim about money), a share written as a percentage (0.08 as
    "8%"), and a sign flip (a margin of -46,619.55 as "short by 46,620"). None of
    these lets a model produce a magnitude no tool returned.
    """
    allowed: set[float] = set()
    for out in tool_outputs:
        for n in _numbers_from(out):
            for v in (n, abs(n)):
                allowed.add(round(v, 2))
                allowed.add(round(v * 100, 2))  # a share written as a percentage

    def traceable(n: float) -> bool:
        return any(abs(n - a) <= max(TOLERANCE_RUPEES, TOLERANCE_SHARE * abs(a))
                   for a in allowed)

    invented = sorted(
        n for n in _numbers_in(answer)
        if abs(n) > _ALLOWED_SMALL and not traceable(n)
    )
    return GuardrailResult(ok=not invented, invented=tuple(invented))


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Answer:
    text: str
    tool_calls: tuple[ToolCall, ...]
    tool_outputs: tuple[Any, ...]
    guardrail: GuardrailResult

    @property
    def rejected(self) -> bool:
        return not self.guardrail.ok

    @property
    def shown(self) -> str:
        """What the merchant sees. A rejected answer is replaced, not annotated."""
        return self.guardrail.message if self.rejected else self.text


def ask(
    question: str,
    model: Model,
    tools: dict[str, Callable[..., dict]],
    schemas: list[dict[str, Any]],
    *,
    max_steps: int = MAX_STEPS,
) -> Answer:
    """Run one question to an answer, letting the model chain tools as it goes."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    called: list[ToolCall] = []
    outputs: list[Any] = []

    for _ in range(max_steps):
        reply = model.respond(messages, schemas)
        if not reply.wants_tools:
            text = (reply.text or "").strip()
            return Answer(
                text=text,
                tool_calls=tuple(called),
                tool_outputs=tuple(outputs),
                guardrail=check_numbers(text, outputs),
            )

        messages.append(
            {
                "role": "assistant",
                "content": reply.text or "",
                "tool_calls": [
                    {
                        "id": f"call_{i}",
                        "type": "function",
                        "function": {
                            "name": c.name,
                            "arguments": json.dumps(c.arguments),
                        },
                    }
                    for i, c in enumerate(reply.tool_calls)
                ],
            }
        )
        for i, call in enumerate(reply.tool_calls):
            called.append(call)
            try:
                result = tools[call.name](**call.arguments)
            except KeyError:
                result = {"error": f"no such tool: {call.name}"}
            except (TypeError, ValueError) as exc:
                # Handed back rather than raised, so the model can correct itself --
                # asking for day 30 of a 14-day forecast should produce a refusal,
                # not a crash.
                result = {"error": str(exc)}
            outputs.append(result)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": f"call_{i}",
                    "name": call.name,
                    "content": json.dumps(result, default=str),
                }
            )

    text = "I could not settle this within a reasonable number of steps."
    return Answer(
        text=text,
        tool_calls=tuple(called),
        tool_outputs=tuple(outputs),
        guardrail=GuardrailResult(ok=True),
    )


# --------------------------------------------------------------------------
# Wiring it to the real thing
# --------------------------------------------------------------------------


def accuracy_summary(store, horizon: int = 14) -> dict[str, Any]:
    """What the agent is allowed to say about its own accuracy.

    Computed from the same backtest the accuracy report prints, rather than written
    down here, so the agent cannot quote a figure the report disagrees with.
    """
    from .backtest import run

    bt = run(store, horizon, estimated=True, intervals=True)
    rows = {r.horizon: r for r in bt.by_horizon()}
    cal = bt.calibration()
    return {
        "forecasts_measured": len(bt.predictions),
        "vantage_points": len(bt.windows),
        "typical_error_rupees_1_day": round(rows[1].mae / 100, 2),
        "typical_error_rupees_3_days": round(rows[3].mae / 100, 2),
        "typical_error_rupees_7_days": round(rows[7].mae / 100, 2),
        "typical_error_rupees_14_days": round(rows[horizon].mae / 100, 2),
        "band_confidence_claimed": bt.confidence,
        "band_actually_contained_truth": round(
            sum(c.covered for c in cal) / sum(c.n for c in cal), 3
        ),
        "cash_shortfall_called_correctly": round(bt.breach_accuracy(), 3),
        "tightest_day_named_correctly": round(bt.trough_accuracy(), 3),
    }


def load_key() -> str:
    """GROQ_API_KEY, from the environment or a gitignored .env."""
    import os

    from dotenv import load_dotenv

    load_dotenv()
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key:
        raise SystemExit(
            "GROQ_API_KEY is not set. Put it in a .env file at the repo root:\n"
            "    GROQ_API_KEY=gsk_...\n"
            "Create that file in an editor rather than the terminal -- a shell "
            "writes every command to a history file in plain text."
        )
    return key


def list_models(api_key: str) -> list[str]:
    """Ask the API what it has, rather than guessing from memory.

    Groq's catalogue changes, and models differ on whether they support tool
    calling -- which is the only capability that matters here.
    """
    from groq import Groq

    return sorted(m.id for m in Groq(api_key=api_key).models.list().data)


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    from .estimate import Estimator
    from .forecast import _intervals_up_to, forecast
    from .tools import SCHEMAS, bind
    from .world import EventStore, world_as_of

    p = argparse.ArgumentParser(description="Ask the forecast a question.")
    p.add_argument("question", nargs="*", help="the question to ask")
    p.add_argument("--data", default="data")
    p.add_argument("--day", type=int, default=57, help="vantage day to stand on")
    p.add_argument("--model", default=None, help="Groq model id")
    p.add_argument("--list-models", action="store_true",
                   help="print the models this key can reach, and exit")
    p.add_argument("--stub", action="store_true",
                   help="no API call; prints the tools that would be available")
    p.add_argument("--verbose", action="store_true",
                   help="show which tools were called, and with what")
    args = p.parse_args(argv)

    if args.list_models:
        for name in list_models(load_key()):
            print(name)
        return 0

    store = EventStore.load(args.data)
    as_of = store.date_for_day(args.day)
    world = world_as_of(store, as_of)
    f = forecast(
        world, 14,
        estimator=Estimator.fit(world, horizon_days=14),
        bands=_intervals_up_to(store, as_of, 14).band_fn(),
    )
    tools = bind(f, accuracy_summary(store))

    if args.stub or not args.question:
        print(f"Standing on {as_of}. Tools available:")
        for s in SCHEMAS:
            print(f"  {s['name']:<20} {s['description'].split('.')[0]}.")
        if not args.question:
            print('\nAsk something:  python -m src.agent "when is it tightest?"')
        return 0

    if not args.model:
        raise SystemExit(
            "Pass --model. Run --list-models first and pick one that documents "
            "tool-calling support; the catalogue changes and models vary on it."
        )

    answer = ask(" ".join(args.question), GroqModel(args.model, load_key()),
                 tools, SCHEMAS)

    if args.verbose:
        for call in answer.tool_calls:
            print(f"  -> {call.name}({', '.join(f'{k}={v}' for k, v in call.arguments.items())})")
        print()
    print(answer.shown)
    if answer.rejected:
        print(f"\n(raw answer withheld: {answer.text})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
