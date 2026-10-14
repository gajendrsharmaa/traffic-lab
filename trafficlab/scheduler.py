"""When visits arrive and which page they pick.

Real traffic does not arrive at a perfectly steady tick, so the default
arrival process is a Poisson process: inter-arrival times are drawn from an
exponential distribution with mean 1/rate. "constant" gives a fixed 1/rate
gap, which is useful for controlled comparisons.
"""

from __future__ import annotations

import random

from .discovery import PageTarget


class ArrivalProcess:
    def __init__(self, rate: float, distribution: str, rng: random.Random) -> None:
        if rate <= 0:
            raise ValueError("rate must be > 0")
        self.rate = rate
        self.distribution = distribution
        self.rng = rng

    def next_delay(self) -> float:
        if self.distribution == "constant":
            return 1.0 / self.rate
        return self.rng.expovariate(self.rate)   # exponential inter-arrival


def weighted_page(targets: list[PageTarget], rng: random.Random) -> PageTarget:
    if not targets:
        raise ValueError("no pages to choose from")
    weights = [t.weight for t in targets]
    return rng.choices(targets, weights=weights, k=1)[0]
