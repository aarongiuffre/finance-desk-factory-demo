"""paper_calib_trades.py safety: live-endpoint / live-key refusal and calib- tagging.
Offline: a fake Api routes _req; the real post/delete guards are exercised."""
import json
import re

import pytest

import paper_calib_trades as P


# ------------------------------------------------------------------ live refusal
@pytest.mark.parametrize("url", ["https://api.alpaca.markets", "https://paper-api.alpaca.markets.evil.com",
                                 "http://paper-api.alpaca.markets", "https://broker-api.alpaca.markets", ""])
def test_refuses_non_paper_base(url):
    with pytest.raises(P.Refused) as e:
        P.assert_paper(url, env={})
    assert e.value.code == 3


def test_accepts_paper_base():
    assert P.assert_paper(P.PAPER_BASE, env={})


@pytest.mark.parametrize("var", ["APCA_API_BASE_URL", "ALPACA_BASE_URL", "ALPACA_API_BASE_URL"])
def test_env_override_to_live_refused(var):
    with pytest.raises(P.Refused):
        P.assert_paper(None, env={var: "https://api.alpaca.markets"})
    assert P.assert_paper(None, env={var: P.PAPER_BASE + "/"})


@pytest.mark.parametrize("kid", ["AKXXXXXXXXXXXXXXXXXX", "pkxxxx", "", None.__class__.__name__])
def test_live_or_unknown_key_refused(kid):
    with pytest.raises(P.Refused) as e:
        P.assert_paper_key(kid, env={})
    assert e.value.code == 3


def test_live_key_in_env_refused():
    with pytest.raises(P.Refused):
        P.assert_paper_key("PKXXXX", env={"APCA_API_KEY_ID": "AKLIVE"})
    assert P.assert_paper_key("PKXXXX", env={})


def test_read_keys_refuses_live_key_file(tmp_path):
    f = tmp_path / "k"
    f.write_text("AKLIVEKEY\nsecret\n")
    with pytest.raises(P.Refused):
        P.read_keys(str(f))
    f.write_text("PKPAPERKEY\nsecret\n")
    assert P.read_keys(str(f))[0] == "PKPAPERKEY"


def test_live_account_refused():
    with pytest.raises(P.Refused):
        P.assert_paper_account({"account_number": "123456789"})
    assert P.assert_paper_account({"account_number": "PA3XYZ"})


def test_api_constructor_refuses_live():
    with pytest.raises(P.Refused):
        P.Api(("PKX", "s"), base="https://api.alpaca.markets")
    with pytest.raises(P.Refused):
        P.Api(("AKX", "s"))


def test_cli_exits_3_on_live_env_before_any_network(monkeypatch, capsys):
    monkeypatch.setenv("APCA_API_BASE_URL", "https://api.alpaca.markets")
    monkeypatch.setattr("sys.argv", ["paper_calib_trades.py", "run", "--dry-run"])
    assert P.main() == 3
    assert "REFUSED" in capsys.readouterr().err


def test_cli_refuses_limits_beyond_approval(monkeypatch):
    monkeypatch.setattr("sys.argv", ["x", "run", "--dry-run", "--notional-hi", "5000"])
    assert P.main() == 2


# ------------------------------------------------------------------ tagging
def test_client_order_id_format():
    c = P.coid("20261019", 7, "buy")
    assert re.fullmatch(r"calib-20261019-007-b-[0-9a-f]{6}", c) and len(c) <= 48
    assert P.is_calib(c) and not P.is_calib("abc") and not P.is_calib({"client_order_id": None})


def test_build_order_tagged_and_marketable():
    q = {"bid": 100.0, "ask": 100.10}
    m = P.build_order("XYZ", "buy", 5, "market", "20261019", 1)
    assert m["type"] == "market" and m["client_order_id"].startswith("calib-") and m["qty"] == "5"
    lb = P.build_order("XYZ", "buy", 5, "mlimit", "20261019", 2, q)
    ls = P.build_order("XYZ", "sell", 5, "mlimit", "20261019", 2, q)
    assert float(lb["limit_price"]) >= 100.10 * 1.001 - 1e-9 and float(ls["limit_price"]) <= 100.0 * 0.999 + 1e-9
    with pytest.raises(ValueError):
        P.build_order("XYZ", "buy", 5, "opg", "20261019", 3, q)


class FakeApi(P.Api):
    def __init__(self, positions=None, open_orders=None):
        self.calls, self.k, self.base, self.data = [], ("PKX", "s"), P.PAPER_BASE, P.DATA_BASE
        self.pos, self.opens, self.orders, self.n = positions or {}, open_orders or [], {}, 0

    def _req(self, url, method="GET", payload=None):
        self.calls.append((method, url, payload))
        if "/quotes/latest" in url:
            return {"quote": {"bp": 99.9, "ap": 100.1, "t": "2026-10-19T14:00:00Z"}}
        if method == "POST":
            self.n += 1
            oid = f"o{self.n}"
            self.orders[oid] = {"id": oid, "status": "filled", "filled_qty": payload["qty"],
                                "filled_avg_price": "100.05", "client_order_id": payload["client_order_id"]}
            return {"id": oid}
        if method == "DELETE":
            return {}
        if "/v2/orders/" in url:
            return self.orders[url.rsplit("/", 1)[1]]
        if url.endswith("/v2/positions"):
            return [{"symbol": s, "qty": q} for s, q in self.pos.items()]
        if "/v2/orders?status=open" in url:
            return self.opens
        raise AssertionError(url)


def test_post_refuses_untagged_order():
    api = FakeApi()
    with pytest.raises(P.Refused):
        api.post("/v2/orders", {"symbol": "X", "qty": "1", "side": "buy", "type": "market"})
    with pytest.raises(P.Refused):
        api.post("/v2/orders", {"symbol": "X", "client_order_id": "driver-1"})
    assert not any(c[0] == "POST" for c in api.calls)


def test_cancel_refuses_non_calib_order():
    api = FakeApi()
    with pytest.raises(P.Refused):
        api.delete_order("o1", "paper-driver-xyz")


def test_flatten_touches_only_calib(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "LOGS", str(tmp_path))
    day = "20261019"
    path = P.ledger_path(day)
    P.log(path, {"event": "fill", "sym": "ZZZ", "side": "buy", "qty": 4, "client_order_id": "calib-x", "order_id": "a"})
    api = FakeApi(positions={"ACGL": "50", "ZZZ": "4"},
                  open_orders=[{"id": "d1", "client_order_id": "", "symbol": "AES"},
                               {"id": "c1", "client_order_id": "calib-20261019-001-s-aaaaaa", "symbol": "ZZZ"}])
    left = P.flatten(api, path, day, "test")
    assert left == {}
    deletes = [c for c in api.calls if c[0] == "DELETE"]
    posts = [c[2] for c in api.calls if c[0] == "POST"]
    assert len(deletes) == 1 and deletes[0][1].endswith("/v2/orders/c1")          # driver order untouched
    assert posts == [p for p in posts if p["client_order_id"].startswith("calib-")]
    assert [(p["symbol"], p["side"], p["qty"]) for p in posts] == [("ZZZ", "sell", "4")]   # never ACGL


def test_execute_side_records_fill(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "LOGS", str(tmp_path))
    path = P.ledger_path("20261019")
    p = {"trip": 1, "sym": "ZZZ", "tier": "top", "bucket": "large", "adv20": 1e9}
    r = P.execute_side(FakeApi(), path, "20261019", p, "buy", 3, "market")
    assert r["client_order_id"].startswith("calib-20261019-001-b-")
    assert r["mid"] == pytest.approx(100.0) and r["spread_bps"] == pytest.approx(20.0)
    assert r["slippage_bps"] == pytest.approx(5.0)
    assert P.calib_net(P.read_ledger(path)) == {"ZZZ": 3}


def test_summary_recomputes():
    rows = [{"event": "fill", "bucket": "large", "tier": "top", "type": "market", "slippage_bps": 4.0, "spread_bps": 6.0},
            {"event": "fill", "bucket": "large", "tier": "low", "type": "mlimit", "slippage_bps": 8.0, "spread_bps": 10.0},
            {"event": "fill", "bucket": "flatten", "tier": "flatten", "type": "market", "slippage_bps": 99.0, "spread_bps": 1.0}]
    s = P.summarize_rows(rows)
    g = s["groups"]["bucket=large"]
    assert s["n_fills"] == 2 and g["mean_slippage_bps"] == 6.0 and g["mean_half_spread_bps"] == 4.0
    assert g["model_side_bps_ex_fee"] == 15.0 and g["model_side_bps_total"] == 30.0
    assert g["realized_minus_model_ex_fee"] == -9.0
    assert json.loads(json.dumps(s)) == s
