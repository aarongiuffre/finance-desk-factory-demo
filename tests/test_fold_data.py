"""fold_data: fold-period copies + reader routing (Verifier finding 4: ungated readers).

* cut_price re-bases split adjustment so the copy holds no post-cutoff split or
  dividend information, while every in-period return and raw price is unchanged
* facts filed after the cutoff, later members, later ledger/signal rows are dropped
* research main scripts resolve to folds/dev; only OPERATIONAL scripts get live
* static guard: research readers contain no literal live-data path
"""
import json
import os
import re
import sys
from pathlib import Path

import pytest

import finpaths
import fold_data as FD

REPO = Path(__file__).resolve().parent.parent
RESEARCH = ["walkforward.py", "backtest.py", "event_study.py", "verdict_backtest.py", "lang_shift_outcomes.py",
            "edge_harness.py", "edge_events.py", "edge_calibrate.py", "fsds_inventory.py"]
LIVE_PATH = re.compile(r'os\.path\.join\(ROOT,\s*"(raw|facts)"|os\.path\.join\(ROOT,\s*"universe",\s*"sp500'
                       r'|/data/finance/(raw|facts|logs/confluence)|os\.path\.join\(ROOT,\s*"logs",\s*"confluence')


def test_cut_price_yahoo_rebases_future_split_and_dividends():
    # 4:1 split on 2020-08-31 (after the cutoff): Yahoo pre-split bars are already /4 in the live file
    bars = [{"d": "2019-12-30", "o": 70.0, "h": 71, "l": 69, "c": 70.5, "a": 68.0, "v": 4000},
            {"d": "2019-12-31", "o": 71.0, "h": 72, "l": 70, "c": 72.0, "a": 69.5, "v": 4400},
            {"d": "2020-09-01", "o": 130.0, "h": 131, "l": 129, "c": 131.0, "a": 130.0, "v": 1000}]
    doc = {"ticker": "X", "source": "yahoo_chart_v8", "fetched": "2026-10-01", "bars": bars}
    spl = {"ticker": "X", "splits": {"2014-06-09": 7.0, "2020-08-31": 4.0}}
    nd, ns = FD.cut_price(doc, spl, "2019-12-31")
    assert [b["d"] for b in nd["bars"]] == ["2019-12-30", "2019-12-31"] and "fetched" not in nd
    assert ns["splits"] == {"2014-06-09": 7.0}
    b0, b1 = nd["bars"]
    assert b1["c"] == pytest.approx(288.0) and b1["o"] == pytest.approx(284.0) and b1["v"] == pytest.approx(1100)
    assert b1["a"] == pytest.approx(b1["c"])                                   # no post-cutoff dividend info
    assert b1["a"] / b0["a"] == pytest.approx(69.5 / 68.0)                     # in-period total return kept
    assert b1["c"] * 1.0 == pytest.approx(72.0 * 4.0)                          # raw price = c * F(all) preserved


def test_cut_price_eodhd_and_v8_lists():
    doc = {"source": "eodhd_eod", "splits": [{"date": "2021-01-04", "factor": 2.0}],
           "bars": [{"d": "2019-06-03", "o": 100.0, "c": 50.0, "a": 48.0, "v": 10},
                    {"d": "2021-02-01", "o": 60.0, "c": 60.0, "a": 60.0, "v": 10}]}
    nd, _ = FD.cut_price(doc, None, "2019-12-31")
    assert nd["splits"] == [] and nd["bars"][0]["c"] == 100.0 and nd["bars"][0]["o"] == 100.0
    v8 = {"timestamps": [1577800800 + 21 * 3600, 1609459200 + 86400 * 4], "cclose": [1, 2], "copen": [1, 2],
          "chigh": [1, 2], "clow": [1, 2], "volume": [5, 6]}
    nv, _ = FD.cut_price(v8, None, "2019-12-31")
    assert nv["cclose"] == [1] and FD.cut_price({"bars": [{"d": "2021-01-01", "c": 1, "a": 1}]}, None,
                                                "2019-12-31") == (None, None)


def test_cut_facts_membership_ledger(tmp_path):
    f = {"ticker": "X", "fetched": "x", "units": {"USD": {"Rev": [{"filed": "2019-02-01", "val": 1},
                                                                  {"filed": "2020-02-01", "val": 2}]}}}
    nd, n = FD.cut_facts(f, "2019-12-31")
    assert n == 1 and nd["units"]["USD"]["Rev"] == [{"filed": "2019-02-01", "val": 1}]
    m = tmp_path / "m.csv"
    m.write_text("ticker,start_date,end_date\nA,2010-01-01,\nB,2015-01-01,2021-03-01\nC,2021-01-01,\n")
    rows = FD.cut_membership(str(m), "2019-12-31")
    assert [(r["ticker"], r["end_date"]) for r in rows] == [("A", ""), ("B", "")]       # C dropped, B's exit hidden
    assert FD._jsonl_prefix_date('{"date":"2019-12-31","rows":[]}', "date") == "2019-12-31"
    assert FD._quarter_end("2019q4.zip") == "2019-12-31" and FD._quarter_end("2020q1_form345.zip") == "2020-03-31"


def test_build_and_verify_copy_in_temp_root(tmp_path):
    root = tmp_path / "r"
    (root / "raw" / "price").mkdir(parents=True)
    (root / "raw" / "dera_fsds").mkdir(parents=True)
    (root / "logs").mkdir()
    finpaths.set_test_root(str(root))
    json.dump({"source": "yahoo_chart_v8", "bars": [{"d": "2019-12-31", "o": 1, "c": 1, "a": 1, "v": 1},
                                                    {"d": "2024-05-01", "o": 2, "c": 2, "a": 2, "v": 1}]},
              open(root / "raw" / "price" / "SPY.json", "w"))
    (root / "raw" / "dera_fsds" / "2019q4.zip").write_bytes(b"z")
    (root / "raw" / "dera_fsds" / "2020q1.zip").write_bytes(b"z")
    (root / "logs" / "confluence-history.jsonl").write_text('{"date":"2019-12-31"}\n{"date":"2020-01-02", BAD\n')
    m = FD.build("dev", apply=False)
    assert not (root / "folds").exists() and m["counts"]["raw/price"] == 1             # dry run writes nothing
    FD.build("dev", apply=True)
    bad, mx = FD.verify_copy("dev")
    assert bad == [] and mx["raw/price"] == "2019-12-31"
    assert sorted(os.listdir(root / "folds" / "dev" / "raw" / "dera_fsds")) == ["2019q4.zip"]
    p = root / "folds" / "dev" / "raw" / "price" / "SPY.json"
    d = json.load(open(p))
    d["bars"].append({"d": "2024-05-01", "o": 2, "c": 2, "a": 2, "v": 1})
    json.dump(d, open(p, "w"))
    bad, _ = FD.verify_copy("dev")
    assert bad and "changed since build" in bad[0]


def test_reader_root_routing(tmp_path, monkeypatch):
    root = tmp_path / "r"
    (root / "raw" / "price").mkdir(parents=True)
    finpaths.set_test_root(str(root))
    monkeypatch.setattr(sys, "argv", ["walkforward.py"])
    with pytest.raises(FD.FoldDataError, match="missing"):
        FD.reader_root()                                      # research never falls back to live data
    FD.build("dev", apply=True)
    dev = os.path.realpath(str(root / "folds" / "dev"))
    assert FD.reader_root() == dev and FD.reader_cutoff() == "2019-12-31"
    # O1 (round 2): naming the process like a live-desk script no longer selects live data
    for op in ("paper_driver.py", "paper_bridge.py", "confluence.py", "nasdaq_scan.py", "quotes.py"):
        monkeypatch.setattr(sys, "argv", [f"/data/finance/scripts/{op}"])
        assert FD.reader_root() == dev and not FD.is_operational()
    # live only through the explicit opt-in the live-desk mains make
    monkeypatch.setattr(FD, "_LIVE", None)
    FD.use_live("/data/finance/scripts/paper_driver.py")
    assert FD.reader_root() == os.path.realpath(str(root)) and FD.reader_cutoff() == "9999-12-31"
    assert not (set(RESEARCH) & FD.OPERATIONAL)


def test_walkforward_and_ta_read_the_dev_copy(tmp_path, monkeypatch):
    root = tmp_path / "r"
    (root / "raw" / "price").mkdir(parents=True)
    (root / "facts").mkdir()
    finpaths.set_test_root(str(root))
    json.dump({"source": "yahoo_chart_v8", "bars": [{"d": "2019-12-31", "o": 1, "c": 1, "a": 1, "v": 1},
                                                    {"d": "2025-01-02", "o": 2, "c": 2, "a": 2, "v": 1}]},
              open(root / "raw" / "price" / "ZZZ.json", "w"))
    json.dump({"ticker": "ZZZ", "units": {"USD": {"Rev": [{"filed": "2019-02-01", "val": 1, "end": "2018-12-31"},
                                                          {"filed": "2025-02-01", "val": 2, "end": "2024-12-31"}]}}},
              open(root / "facts" / "ZZZ.json", "w"))
    FD.build("dev", apply=True)
    import ta
    import walkforward as wf
    monkeypatch.setattr(sys, "argv", ["walkforward.py"])
    wf._FACTS.clear()
    assert [b["d"] for b in ta.load_bars("ZZZ")] == ["2019-12-31"]
    assert [r["filed"] for r in wf.facts_of("ZZZ")["units"]["USD"]["Rev"]] == ["2019-02-01"]
    monkeypatch.setattr(sys, "argv", ["paper_driver.py"])              # argv0 alone: still the dev copy
    assert [b["d"] for b in ta.load_bars("ZZZ")] == ["2019-12-31"]
    monkeypatch.setattr(FD, "_LIVE", None)
    FD.use_live("paper_driver.py")                                       # explicit live-desk opt-in
    wf._FACTS.clear()
    assert [b["d"] for b in ta.load_bars("ZZZ")][-1] == "2025-01-02"
    wf._FACTS.clear()


@pytest.mark.parametrize("name", RESEARCH)
def test_research_readers_have_no_literal_live_data_path(name):
    src = (REPO / "scripts" / name).read_text()
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    code = re.sub(r'"""[\s\S]*?"""', "", code)
    assert not LIVE_PATH.search(code), f"{name} reads a live data path directly: {LIVE_PATH.search(code).group(0)}"
