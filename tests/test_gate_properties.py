"""Gate invariants under random answers, unfreezes and clock moves.

Whatever the source answers, a request is never admitted:

- beyond the daily cap of its UTC day;
- while the host is frozen (403, 451, or 429 by default);
- before a Retry-After the source asked for (one longer than any pause of
  the policy is a freeze until then, which a person may lift);
- sooner than the minimum interval after the previous request ended;
- sooner than the growing pause after a failure.
"""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from politefetch.clock import FakeClock
from politefetch.errors import Refused
from politefetch.gate import Gate
from politefetch.policy import HostPolicy
from politefetch.store import MemoryStore

T = datetime(2026, 10, 16, 20, 0, tzinfo=UTC)
URL = "https://example.org/x"
HOST = "example.org"
POLICY = HostPolicy(
    daily_cap=4,
    min_interval="5m",
    backoff_base="1m",
    backoff_factor=2,
    backoff_max="1h",
)

answers = st.one_of(
    st.tuples(st.sampled_from([200, 301, 304, 403, 451, 500, 503]), st.none()),
    st.tuples(st.just(429), st.sampled_from([None, "60", "7200"])),
    st.tuples(st.just(503), st.sampled_from(["30", "5400"])),
    st.tuples(st.sampled_from(["timeout", "exception", "unrecorded"]), st.none()),
)
actions = st.lists(
    st.one_of(
        st.tuples(st.just("try"), answers),
        st.tuples(st.just("wait"), st.integers(min_value=1, max_value=7200)),
        st.tuples(st.just("unfreeze"), st.none()),
    ),
    max_size=40,
)


@dataclass
class Model:
    frozen: bool = False
    frozen_until: datetime | None = None
    not_before: datetime = T
    failures: int = 0
    per_day: dict[date, int] = field(default_factory=dict)


@settings(max_examples=150, deadline=None)
@given(actions)
def test_the_gate_never_admits_too_early(script: list[tuple[str, object]]) -> None:
    clock = FakeClock(T)
    gate = Gate(MemoryStore(), {HOST: POLICY}, clock=clock)
    model = Model()
    for kind, arg in script:
        if kind == "wait":
            assert isinstance(arg, int)
            clock.advance(timedelta(seconds=arg))
        elif kind == "unfreeze":
            gate.unfreeze(HOST, by="test")
            model.frozen = False
            model.frozen_until = None
        else:
            assert isinstance(arg, tuple)
            answer, retry_after = arg
            now = clock.now()
            try:
                context = gate.attempt(URL)
                attempt = context.__enter__()
            except Refused:
                continue
            # Admitted: check every rule the source relies on.
            assert not model.frozen
            assert model.frozen_until is None or now >= model.frozen_until
            assert now >= model.not_before
            day = now.date()
            model.per_day[day] = model.per_day.get(day, 0) + 1
            assert model.per_day[day] <= POLICY.daily_cap
            clock.advance(timedelta(seconds=1))
            end = clock.now()
            error: BaseException | None = None
            if isinstance(answer, int):
                attempt.response(answer, retry_after=retry_after)
            elif answer == "timeout":
                attempt.failure("connect_timeout")
            elif answer == "exception":
                error = OSError("reset")
            try:
                if error is None:
                    context.__exit__(None, None, None)
                else:
                    context.__exit__(type(error), error, None)
            except OSError:
                pass
            ok = answer in (200, 304)
            if ok:
                model.failures = 0
                model.not_before = end + POLICY.min_interval
            else:
                model.failures += 1
                pause = max(POLICY.min_interval, POLICY.backoff(model.failures))
                model.not_before = max(model.not_before, end + pause)
            if isinstance(retry_after, str):
                asked = end + timedelta(seconds=int(retry_after))
                longest = end + max(POLICY.min_interval, POLICY.backoff_max)
                if answer in (403, 451, 429) or asked <= longest:
                    model.not_before = max(model.not_before, asked)
                else:
                    model.frozen_until = asked
            if answer in (403, 451, 429):
                model.frozen = True
