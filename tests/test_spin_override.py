"""SPIN_EPS_BASIS override: post-spin stubs must rank on continuing-ops economics.

CTVA completed the separation of its seed business (Vylor Inc.) on 2026-10-01.
The as-filed XBRL chain keeps reporting the CONSOLIDATED predecessor until every
TTM quarter is post-spin (~Q2 2027), while the as-traded price is the crop-
protection stub only — pe = px/consolidated-eps read 7.7x (phantom) vs ~48x on
the 8-K pro forma continuing-ops basis ($0.24 diluted, FY2025).  candidate_value
must return the override basis from d_from on, keep the pre-spin path untouched,
and keep the 1..500 pe guard.

No network; facts_of is monkeypatched so the test never depends on live facts
files.  The override VALUES are pinned against the 8-K numbers (audit anchor).
"""

import datetime
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import walkforward as wf  # noqa: E402


@pytest.fixture
def no_facts(monkeypatch):
    monkeypatch.setattr(wf, "facts_of", lambda t: {"ok": True})


def test_override_applies_from_spin_date(no_facts):
    d = datetime.date(2026, 10, 31).isoformat()
    v = wf.candidate_value("CTVA", d, px=11.64)
    assert v is not None
    pe, eps, ni = v[0], v[1], v[2]
    assert eps == pytest.approx(0.24)
    assert ni == pytest.approx(161e6)
    assert pe == pytest.approx(11.64 / 0.24, rel=1e-6)     # ~48.5x, NOT 7.7x
    assert v[3] == "2026-10-01"                             # eps_end = spin date marker


def test_override_active_on_the_spin_date_itself(no_facts):
    v = wf.candidate_value("CTVA", "2026-10-01", px=12.57)
    assert v is not None and v[0] == pytest.approx(12.57 / 0.24, rel=1e-6)


def test_pre_spin_path_untouched(no_facts, monkeypatch):
    """Before d_from the override must NOT fire — the consolidated basis is
    correct for pre-spin prices (price covered both businesses too)."""
    monkeypatch.setattr(wf, "facts_asof", lambda f, d: {"units": {"USD": {}, "USD/shares": {}}})
    monkeypatch.setattr(wf, "ttm_chain", lambda *a, **k: (None, None, None, []))
    monkeypatch.setattr(wf, "ttm_via_engine", lambda *a, **k: (None, None, None))
    v = wf.candidate_value("CTVA", "2026-09-30", px=11.64)
    assert v is None                                        # tag-less stub facts -> normal None path


def test_pe_guard_still_applies(no_facts):
    """px/eps outside 1..500 refuses (no phantom multiples through the override)."""
    assert wf.candidate_value("CTVA", "2026-10-31", px=0.05) is None    # pe=0.2
    assert wf.candidate_value("CTVA", "2026-10-31", px=500.0) is None   # pe=2083
    assert wf.candidate_value("CTVA", "2026-10-31", px=100.0) is not None  # pe=417


def test_override_values_match_the_8k():
    """Audit anchor: the pinned numbers are the pro forma FY2025 figures from
    Corteva's 8-K (accn 0001755672-26-000028, New Corteva pro forma combined
    statement of operations): diluted EPS $0.24, NI attributable $161M."""
    ov = wf.SPIN_EPS_BASIS["CTVA"]
    assert ov["eps_cont"] == 0.24
    assert ov["ni_cont"] == pytest.approx(161e6)
    assert ov["d_from"] == "2026-10-01"
    assert "0001755672-26-000028" in ov["source"]
