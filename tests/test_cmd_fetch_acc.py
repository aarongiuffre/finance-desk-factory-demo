"""cmd_fetch accession re-dashing guard (the 10-2-6 format).

The old code dropped accn[12:14] (10-2-4-... slicing bug, present before AND
after the 2026-10-09 rewrite): 000032019326000020 became
0000320193-26-00020 (17 chars) instead of 0000320193-26-000020 — a wrong
directory name and a 404'd index URL.
"""

import edgar_pull as ep


def test_accession_dashing_10_2_6():
    accn = "000032019326000020"
    assert (f"{accn[:10]}-{accn[10:12]}-{accn[12:]}"
            == "0000320193-26-000020")


def test_cmd_fetch_builds_correct_acc(monkeypatch, tmp_path):
    captured = {}

    def fake_resolve_cik(ticker, ua):
        return "0000320193"

    def fake_fetch(url, ua, retries=8):
        captured["url"] = url
        return b'{"directory":{"item":[{"name":"main.htm","last-modified":"2026-03-28 00:00:00"}]}}'

    monkeypatch.setattr(ep, "resolve_cik", fake_resolve_cik)
    monkeypatch.setattr(ep, "fetch", fake_fetch)

    class A:  # argparse namespace stand-in
        ticker = "AAPL"
        accession = "0000320193-26-000020"
        form = "10-Q"
        date = "2026-03-28"

    class FakePull:
        def __call__(self, acc, form, date, primary, cikn, outbase, ua):
            captured["acc"] = acc
            return "done"

    monkeypatch.setattr(ep, "pull_one", FakePull())
    rc = ep.cmd_fetch(A())
    assert rc == 0
    assert captured["acc"] == "0000320193-26-000020"
    assert ("/Archives/edgar/data/320193/000032019326000020/index.json"
            in captured["url"])
