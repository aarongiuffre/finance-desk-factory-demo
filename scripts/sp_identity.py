"""sp_identity.py - company-identity check for S&P-exit price files (Verifier finding 2).

A price file filed under membership ticker T must be the SAME COMPANY as the S&P
member T, not a ticker re-use (SE = Spectra Energy 2007-2017, Sea Ltd since 2017).

  member identity : universe/sp_pit_cik_map.json map[T] -> cik (+ name when known)
  file identity   : the EODHD code it came from (doc["eodhd_code"]) -> CIK via local
                    EDGAR raw/edgar/<code>/submissions.json or facts/<code>.json, and
                    its names (EDGAR name + formerNames, EODHD list name)

verdict:
  "cik"        CIKs agree                                    -> accept
  "name"       no CIK on one side, a distinctive name token
               (not Inc/Corp/Group/...) is shared            -> accept
  "mismatch"   CIKs disagree, or both names known and share
               no distinctive token                          -> REJECT
  "unverified" not enough local data to decide               -> accept with warning
"""
import json
import os
import re

STOP = {"inc", "corp", "corporation", "co", "company", "group", "ltd", "plc", "the", "holdings", "holding",
        "trust", "lp", "llc", "sa", "nv", "and", "of", "a", "class", "common", "stock", "new", "international",
        "properties", "us", "na", "de", "ag", "se", "l", "p"}


def _norm_cik(c):
    try:
        return int(str(c).lstrip("0") or 0) or None
    except ValueError:
        return None


def tokens(name):
    return {w for w in re.split(r"[^a-z0-9]+", str(name or "").lower()) if len(w) > 1 and w not in STOP}


def member(root, t):
    p = os.path.join(root, "universe", "sp_pit_cik_map.json")
    if not os.path.exists(p):
        return None, set()
    m = json.load(open(p)).get("map", {}).get(t) or {}
    names = set()
    if m.get("name"):
        names.add(m["name"])
    names |= set((m.get("alt_names") or {}).values())
    cik = _norm_cik(m.get("cik"))
    if cik:
        names |= edgar_names_for_cik(root, t, cik)
    return cik, names


_IDX = {}


def cik_index(root):
    """cik -> {names} over every local EDGAR submissions.json (name + formerNames)."""
    if root in _IDX:
        return _IDX[root]
    idx = {}
    base = os.path.join(root, "raw", "edgar")
    for t in os.listdir(base) if os.path.isdir(base) else []:
        p = os.path.join(base, t, "submissions.json")
        if not os.path.isfile(p):
            continue
        try:
            s = json.load(open(p))
        except ValueError:
            continue
        c = _norm_cik(s.get("cik"))
        if c:
            idx.setdefault(c, set()).update(n for n in [s.get("name")] + [f.get("name") for f in s.get("formerNames", [])] if n)
    _IDX[root] = idx
    return idx


def edgar_names_for_cik(root, t, cik):
    return set(cik_index(root).get(cik, set()))


def code_identity(root, code):
    """(cik, names) for an EODHD code such as 'BKR.US' / 'HCP_old.US'."""
    base = code.split(".")[0]
    base_t = base.split("_old")[0]
    cik, names = None, set()
    for t in dict.fromkeys((base, base_t)):
        p = os.path.join(root, "raw", "edgar", t, "submissions.json")
        if cik is None and os.path.exists(p) and "_old" not in base:   # _old codes are an earlier holder of the ticker
            try:
                s = json.load(open(p))
                cik = _norm_cik(s.get("cik"))
                names |= {s.get("name")} | {f.get("name") for f in s.get("formerNames", [])}
            except ValueError:
                pass
        for fp in (os.path.join(root, "facts", f"{t}.json"), os.path.join(root, "facts", "dead", f"{t}.json")):
            if cik is None and "_old" not in base and os.path.exists(fp):
                try:
                    cik = _norm_cik(json.load(open(fp)).get("cik"))
                except ValueError:
                    pass
    return cik, {n for n in names if n}


def verdict(root, t, doc):
    code = doc.get("eodhd_code") or ""
    mcik, mnames = member(root, t)
    ccik, cnames = code_identity(root, code)
    if doc.get("eodhd_name"):
        cnames = cnames | {doc["eodhd_name"]}
    if mcik and ccik:
        return ("cik" if mcik == ccik else "mismatch"), f"member cik {mcik} vs code {code} cik {ccik}"
    mt = set().union(*(tokens(n) for n in mnames)) if mnames else set()
    ct = set().union(*(tokens(n) for n in cnames)) if cnames else set()
    if mt and ct:
        shared = mt & ct
        return ("name" if shared else "mismatch"), f"shared name tokens {sorted(shared)} ({sorted(mnames)[:2]} vs {sorted(cnames)[:2]})"
    return "unverified", f"member cik {mcik} names {sorted(mnames)[:1]}; code {code} cik {ccik} names {sorted(cnames)[:1]}"


def overrides(root):
    p = os.path.join(root, "ops", "data", "sp_identity_overrides.json")
    if not os.path.exists(p):
        return {}
    return {k: v for k, v in json.load(open(p)).items() if not k.startswith("_")}


def checked(root, t, doc):
    """verdict with reviewed overrides applied: a mismatch is accepted ONLY if the
    override names this exact EODHD code."""
    v, why = verdict(root, t, doc)
    if v == "mismatch":
        o = overrides(root).get(t)
        if o and o.get("code") == doc.get("eodhd_code"):
            return "override", f"{why}; override: {o['evidence'][:80]}"
    return v, why
