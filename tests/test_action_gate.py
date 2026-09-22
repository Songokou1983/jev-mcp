"""Action gate: confidence (or effective_confidence for Noul) → action string.

TDD — these tests define the behavior before implementation.
"""
import pytest

from jev_mcp.action_gate import compute_action, effective_confidence


class TestEffectiveConfidence:
    """Noul has no API confidence; server derives it from |noul - 0.5| * 2."""

    def test_high_yes(self):
        # noul=0.95 → effective 0.9 (high certainty yes)
        assert effective_confidence(noul=0.95) == pytest.approx(0.9)

    def test_high_no(self):
        # noul=0.05 → effective 0.9 (high certainty no)
        assert effective_confidence(noul=0.05) == pytest.approx(0.9)

    def test_uncertain(self):
        # noul=0.5 → effective 0 (max uncertain)
        assert effective_confidence(noul=0.5) == pytest.approx(0.0)

    def test_weak_yes_collapses_fast(self):
        # noul=0.6 → effective 0.2 (already very uncertain — asymmetric by design)
        assert effective_confidence(noul=0.6) == pytest.approx(0.2)

    def test_weak_no_same_as_weak_yes(self):
        # Symmetric around 0.5
        assert effective_confidence(noul=0.4) == effective_confidence(noul=0.6)


class TestComputeAction:
    """Map effective_confidence → act / confirm / escalate via configurable thresholds."""

    def test_act_above_threshold(self):
        assert compute_action(0.9, threshold_act=0.7, threshold_confirm=0.4) == "act"

    def test_confirm_in_middle_band(self):
        assert compute_action(0.5, threshold_act=0.7, threshold_confirm=0.4) == "confirm"

    def test_escalate_below_confirm(self):
        assert compute_action(0.2, threshold_act=0.7, threshold_confirm=0.4) == "escalate"

    def test_at_act_threshold_is_act(self):
        # boundary: == threshold_act counts as act (>=)
        assert compute_action(0.7, threshold_act=0.7, threshold_confirm=0.4) == "act"

    def test_at_confirm_threshold_is_confirm(self):
        assert compute_action(0.4, threshold_act=0.7, threshold_confirm=0.4) == "confirm"

    def test_below_confirm_is_escalate(self):
        assert compute_action(0.3999, threshold_act=0.7, threshold_confirm=0.4) == "escalate"

    def test_noul_high_yes_is_act(self):
        # noul=0.95 → effective 0.9 → act
        eff = effective_confidence(noul=0.95)
        assert compute_action(eff, threshold_act=0.7, threshold_confirm=0.4) == "act"

    def test_noul_weak_yes_is_escalate(self):
        # noul=0.6 → effective 0.2 → escalate (asymmetric, by design)
        eff = effective_confidence(noul=0.6)
        assert compute_action(eff, threshold_act=0.7, threshold_confirm=0.4) == "escalate"