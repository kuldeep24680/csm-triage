"""Turning a churn probability into a weekly call list.

Kept separate from the model on purpose: the model answers "will they
leave?", the policy answers "who do I call this week?". Renewal timing and
revenue live here, not inside the model, so they are not double-counted.
"""
from __future__ import annotations

import math

from ..features.build import urgency



def priority(p: float, arr: float | None, days_to_renewal: int | None) -> float:
    """Expected ARR at risk, weighted by how actionable the renewal is now.

    ARR enters as sqrt so a $500k account doesn't permanently bury every
    $20k account in a CSM's book; the ordering by risk still dominates.
    """
    return float(p * math.sqrt(max(arr or 0.0, 1.0)) * urgency(days_to_renewal))


def assign_bands(book: list[dict], capacity: int, watch_min_p: float) -> None:
    """Mutates each row in a CSM's book with band + rank.

    unknown  - not enough signal to score honestly (no usage, no recent contact)
    call     - this week's list: top `capacity` by priority. No probability
               floor: on validation a floor cut churned-ARR caught early from
               93% to 64% (big accounts carry lower p but most of the revenue).
    watch    - elevated risk (p >= 2x base rate) that didn't make this week's list
    healthy  - everything else
    """
    scored = sorted((r for r in book if not r["insufficient_signal"]), key=lambda r: -r["priority"])
    for i, r in enumerate(scored, 1):
        r["rank_in_book"] = i
        if i <= capacity:
            r["band"] = "call"
        elif r["p_churn"] >= watch_min_p:
            r["band"] = "watch"
        else:
            r["band"] = "healthy"
    for r in book:
        if r["insufficient_signal"]:
            r["band"] = "unknown"
            r["rank_in_book"] = None
