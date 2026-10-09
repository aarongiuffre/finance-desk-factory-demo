"""Tag-alias resolution guards: scripts/edgar_facts.resolve_tag and the
edgar_screen.metric_entries one-tag-per-metric selection.

Guards three real bugs:
- alias fan-out must see sibling tags (revenue_incl_tax rows were invisible
  to metric_entries before the revenue_* family was recognized);
- freshness preference must pick the migrated tag (NVDA-style Revenues
  migration would otherwise pin the series to a dead tag);
- the MPC case: ProfitLoss and NetIncomeLoss coexisting with equal freshness
  and length must resolve to NetIncomeLoss, never a blended pool.

edgar_facts imports edgar_pull (constants only at import; the module-level
code defines functions — no I/O until called), verified 2026-10-09.
"""

import edgar_facts as ef
import edgar_screen as es


def _q(end, val, form="10-Q", start=None, filed="2026-01-01"):
    return {"start": start or end, "end": end, "val": val, "form": form,
            "filed": filed}


# ---------------------------------------------------------------- resolve_tag
def test_resolve_tag_incl_tax_only_tag_present():
    facts = {"units": {"USD": {
        "RevenueFromContractWithCustomerIncludingAssessedTax": [
            _q("2026-03-31", 5.0)],
    }}}
    unit, tag, rows = ef.resolve_tag("revenue_incl_tax", facts)
    assert (unit, tag) == ("USD", "RevenueFromContractWithCustomerIncludingAssessedTax")
    assert rows and rows[0]["val"] == 5.0


def test_resolve_tag_prefers_freshest_migration():
    # NVDA-style migration: the canonical tag's series is stale, Revenues is
    # current — resolution must follow the DATA, not the alias order.
    facts = {"units": {"USD": {
        "RevenueFromContractWithCustomerExcludingAssessedTax": [
            _q("2018-12-29", 30.0)],
        "Revenues": [_q("2026-01-25", 39.3)],
    }}}
    _u, tag, rows = ef.resolve_tag("revenue", facts)
    assert tag == "Revenues"
    assert rows[0]["end"] == "2026-01-25"


def test_rename_revenue_ytd_gone():
    assert "revenue_ytd" not in ef.ALIASES
    assert ef.ALIASES["revenue_incl_tax"] == (
        "RevenueFromContractWithCustomerIncludingAssessedTax")


# ------------------------------------------------- edgar_screen family merge
def test_metric_entries_sees_incl_tax_rows_via_family():
    # ONLY the IncludingAssessedTax tag exists; the revenue family fan-out
    # (k == name or k.startswith(name + "_")) must surface its rows.
    facts = {"units": {"USD": {
        "RevenueFromContractWithCustomerIncludingAssessedTax": [
            _q("2026-03-31", 5.0, start="2026-01-01"),
            _q("2025-12-31", 4.0, start="2025-10-01"),
        ],
    }}}
    rows = es.metric_entries(facts, "revenue")
    assert rows, "family fan-out failed: revenue_incl_tax rows invisible"
    assert all(r["_tag"] == "RevenueFromContractWithCustomerIncludingAssessedTax"
               for r in rows)
    assert {r["val"] for r in rows} == {5.0, 4.0}


def test_mpc_profitloss_vs_netincome_no_blend():
    # Both tags present, identical freshness (same ends) and length.  The
    # pre-v5 dedupe-across-tags logic blended the series; v5 must pick ONE.
    ni_ends = ["2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30"]
    facts = {"units": {"USD": {
        "NetIncomeLoss": [_q(e, 100.0 + i, start=e) for i, e in enumerate(ni_ends)],
        "ProfitLoss": [_q(e, 110.0 + i, start=e) for i, e in enumerate(ni_ends)],
    }}}
    rows = es.metric_entries(facts, "net_income")
    assert rows, "no rows returned"
    assert len(rows) == 4
    # One tag, and it is the attributable-to-parent one.
    assert {r["_tag"] for r in rows} == {"NetIncomeLoss"}
    # NOT a blend: every value comes from the NI series (110.x would mean
    # ProfitLoss rows leaked in).
    assert {r["val"] for r in rows} == {100.0, 101.0, 102.0, 103.0}
    assert not any(r["val"] >= 110.0 for r in rows)
