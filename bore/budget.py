"""Budget pacer: keep total Apify spend under the monthly ceiling.

Two layers:
  * hard stop (Apify.check_budget): no run starts once cycle usage reaches the ceiling
  * pacing (this module): the remaining budget is spread evenly over the days left in the
    billing cycle; optional runs (Reddit, web, refresh, escalation) are skipped when today's
    spend plus the run's expected cost would exceed today's share. X always runs until the
    hard stop, because that is where scoops break.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .textutil import parse_ts


@dataclass
class Pacer:
    ceiling: float          # effective monthly ceiling, USD
    used: float             # usage so far this billing cycle, USD
    cycle_end: float        # unix seconds
    spent_today: float      # our own ledger, since UTC midnight
    now: float

    @property
    def days_left(self) -> float:
        return max((self.cycle_end - self.now) / 86400, 1.0)

    @property
    def daily_allowance(self) -> float:
        return max(self.ceiling - self.used, 0.0) / self.days_left

    def allows(self, expected_cost: float) -> bool:
        return self.spent_today + expected_cost <= self.daily_allowance

    def summary(self) -> str:
        return (f"budget: ${self.used:.2f} of ${self.ceiling:.2f} used this cycle; "
                f"today ${self.spent_today:.3f} of ${self.daily_allowance:.3f} allowance "
                f"({self.days_left:.1f} days left)")


def utc_midnight(now: float) -> float:
    d = datetime.fromtimestamp(now, timezone.utc)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def make_pacer(apify, store, now: float | None = None) -> Pacer:
    now = now or time.time()
    used, cap = apify.usage()
    end = parse_ts((getattr(apify, "cycle", {}) or {}).get("endAt")) or now + 30 * 86400
    return Pacer(ceiling=apify.ceiling(cap), used=used, cycle_end=end,
                 spent_today=store.apify_spend_since(utc_midnight(now)), now=now)


def expected_cost(store, actor: str, default: float) -> float:
    """Expected cost of the next run of an actor: its last measured run, else the default."""
    last = store.last_cost(actor)
    return last if last is not None else default
