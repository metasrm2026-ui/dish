"""Thin Apify REST client with a budget guard and partial-result salvage."""
from __future__ import annotations

import time

import requests

TERMINAL = {"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"}


class BudgetExceeded(RuntimeError):
    pass


class Apify:
    def __init__(self, token: str, base_url: str = "https://api.apify.com/v2",
                 guard_pct: float = 0.85, poll_seconds: float = 5, monthly_budget_usd: float = 0.0):
        self.base = base_url.rstrip("/")
        self.guard_pct = guard_pct
        self.monthly_budget = monthly_budget_usd
        self.last_cost = 0.0
        self.on_cost = None                    # callback(actor, usd) to record spend
        self.poll = poll_seconds
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"

    def _get(self, path: str, **params):
        r = self.s.get(self.base + path, params=params, timeout=60)
        r.raise_for_status()
        return r.json()

    def usage(self) -> tuple[float, float]:
        """(current monthly usage USD, monthly ceiling USD) from the account's own limits."""
        data = self._get("/users/me/limits")["data"]
        used = float(data.get("current", {}).get("monthlyUsageUsd", 0.0))
        cap = float(data.get("limits", {}).get("maxMonthlyUsageUsd", 0.0))
        self.cycle = data.get("monthlyUsageCycle", {})
        return used, cap

    def ceiling(self, cap: float) -> float:
        """Effective monthly ceiling: the lower of our budget and the guard share of the plan."""
        guard = cap * self.guard_pct if cap > 0 else float("inf")
        return min(guard, self.monthly_budget) if self.monthly_budget else guard

    def check_budget(self) -> None:
        """Refuse to start past the ceiling. Once Apify cuts the account off, every later run
        fails silently, so stopping early is the only safe behaviour."""
        used, cap = self.usage()
        if used >= self.ceiling(cap):
            raise BudgetExceeded(f"Apify usage ${used:.2f} reached the ${self.ceiling(cap):.2f} ceiling")

    def run(self, actor: str, run_input: dict, timeout: int) -> tuple[list[dict], str]:
        """Start an actor, wait for it, and return (items, status). On FAILED / TIMED-OUT the run
        is still billed, so read its dataset and return whatever it produced."""
        self.check_budget()
        actor_id = actor.replace("/", "~")
        r = self.s.post(f"{self.base}/acts/{actor_id}/runs", params={"timeout": timeout},
                        json=run_input, timeout=60)
        r.raise_for_status()
        run = r.json()["data"]
        deadline = time.time() + timeout + 120
        while run["status"] not in TERMINAL and time.time() < deadline:
            time.sleep(self.poll)
            run = self._get(f"/actor-runs/{run['id']}")["data"]
        items = self.dataset(run["defaultDatasetId"])
        try:  # final cost (pay-per-event charges settle at the end of the run)
            self.last_cost = float(self._get(f"/actor-runs/{run['id']}")["data"].get("usageTotalUsd") or 0)
        except Exception:
            self.last_cost = 0.0
        if self.on_cost:
            self.on_cost(actor, self.last_cost)
        return items, run["status"]

    def dataset(self, dataset_id: str) -> list[dict]:
        items, offset, limit = [], 0, 1000
        while True:
            r = self.s.get(f"{self.base}/datasets/{dataset_id}/items",
                           params={"offset": offset, "limit": limit, "clean": "true"}, timeout=120)
            r.raise_for_status()
            batch = r.json()
            items.extend(batch)
            if len(batch) < limit:
                return items
            offset += limit
