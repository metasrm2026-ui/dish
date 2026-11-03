"""Krea API client with a hard monthly image/spend cap (the plan is never topped up)."""
from __future__ import annotations

import time
from datetime import datetime, timezone

import requests


class KreaBudgetExceeded(RuntimeError):
    pass


def month_start(now: float | None = None) -> float:
    d = datetime.fromtimestamp(now or time.time(), timezone.utc)
    return d.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


class Krea:
    def __init__(self, token: str, cfg: dict, store=None):
        self.kc = cfg["krea"]
        self.base = self.kc["base_url"].rstrip("/")
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"
        self.store = store

    def usage_this_month(self) -> tuple[int, float]:
        if not self.store:
            return 0, 0.0
        row = self.store.db.execute("SELECT COUNT(*), COALESCE(SUM(usd), 0) FROM spend WHERE actor LIKE 'krea:%' AND at >= ?",
                                    (month_start(),)).fetchone()
        return int(row[0]), float(row[1])

    def check_budget(self, model: str) -> None:
        n, usd = self.usage_this_month()
        price = self.kc["prices_usd"].get(model, 0.05)
        if n + 1 > self.kc["monthly_max_images"] or usd + price > self.kc["monthly_max_usd"]:
            raise KreaBudgetExceeded(f"Krea month-to-date {n} images / ${usd:.2f} (caps "
                                     f"{self.kc['monthly_max_images']} / ${self.kc['monthly_max_usd']:.2f})")

    def generate(self, model: str, payload: dict, timeout: int = 180) -> str:
        """Run one generation and return the result image URL. Records spend on success."""
        self.check_budget(model)
        r = self.s.post(f"{self.base}/generate/image/{model}", json=payload, timeout=60)
        if r.status_code == 402:
            raise KreaBudgetExceeded("Krea reports insufficient credits (402)")
        r.raise_for_status()
        job_id = r.json()["job_id"]
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(3)
            j = self.s.get(f"{self.base}/jobs/{job_id}", timeout=30).json()
            if j.get("status") == "completed":
                if self.store:
                    self.store.record_spend(f"krea:{model}", self.kc["prices_usd"].get(model, 0.05))
                return (j.get("result") or {}).get("urls", [None])[0]
            if j.get("status") in ("failed", "cancelled"):
                raise RuntimeError(f"Krea job {job_id} {j.get('status')}: {j.get('error')}")
        raise TimeoutError(f"Krea job {job_id} did not finish in {timeout}s")
