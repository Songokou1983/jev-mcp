"""Action gate: compute effective_confidence (for Noul) and map to act/confirm/escalate.

Pure logic — no I/O, easy to unit test.
"""
from __future__ import annotations


def effective_confidence(noul: float) -> float:
    """Map a Noul probability (0-1, >0.5 = yes) to effective_confidence (0-1).

    Noul has no API-returned confidence. We use distance from uncertain midpoint (0.5):
    effective = |noul - 0.5| * 2. This makes Noul much more conservative than Choice
    near the decision boundary — by design (uncertain → escalate).

    Args:
        noul: Jev's noul field, probability that the answer is yes (0-1).

    Returns:
        Effective confidence 0-1. 0 = max uncertain (noul=0.5); 1 = max certain
        (noul=0.0 or 1.0).
    """
    return abs(noul - 0.5) * 2


def compute_action(
    effective_confidence_value: float,
    threshold_act: float,
    threshold_confirm: float,
) -> str:
    """Map effective_confidence to action bucket.

    Args:
        effective_confidence_value: 0-1 certainty score. For Choice/Score this is the
            Jev-returned confidence directly. For Noul, pass `effective_confidence(noul)`.
        threshold_act: >= this → 'act' (auto-execute).
        threshold_confirm: >= this (but < threshold_act) → 'confirm'.
            Below → 'escalate'.

    Returns:
        One of 'act', 'confirm', 'escalate'.
    """
    if effective_confidence_value >= threshold_act:
        return "act"
    if effective_confidence_value >= threshold_confirm:
        return "confirm"
    return "escalate"