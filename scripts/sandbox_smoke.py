#!/usr/bin/env python3
"""sandbox_smoke.py - tiny dev-only smoke check, ONLY inside ops/gate/h1_sandbox.sh --smoke.

Not the H1 prereg run: no window is requested, nothing is granted, no variant is
logged, run()/run_events() are not called.  It shows that inside the sandbox
numpy / edge_harness / PriceStore load, the real dev slice validates against the
committed prereg's price_slice_sha256 and the HEAD pin, and a few dev-slice bars can
be read and reduced with numpy.  Output: RUN/out/smoke.json.
"""
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import numpy as np  # noqa: E402

import edge_harness  # noqa: E402
import finpaths  # noqa: E402
import fold_data  # noqa: E402
import fold_gate  # noqa: E402

H1 = "H1_opportunistic_insider_buys"


def main():
    sb = finpaths.sandbox_run()
    if not sb:
        raise SystemExit("refused: sandbox_smoke runs only inside ops/gate/h1_sandbox.sh --smoke")
    out = {"numpy": np.__version__, "PriceStore": edge_harness.PriceStore.__qualname__,
           "data_root_listing": sorted(os.listdir(finpaths.REAL_ROOT))}
    want, why = fold_gate.prereg_slice_sha(H1)
    if want is None:
        raise SystemExit(f"refused: {why}")
    rel = fold_data.SLICES[0]
    root = fold_data.copy_root("dev")
    files, sha = fold_data.checked_slice(root, "dev", rel, expected=want)
    out.update(slice_sha256=sha, prereg_price_slice_sha256=want, pin=fold_data.slice_pin("dev", rel),
               slice_files=len(files))
    stats = []
    for name in sorted(files)[:3]:
        p = os.path.join(root, rel, name)
        b = open(p, "rb").read()
        if hashlib.sha256(b).hexdigest() != files[name]:
            raise SystemExit(f"refused: {name} sha != manifest")
        bars = json.loads(b)["bars"]
        a = np.array([x["a"] for x in bars if x.get("a")], dtype=float)
        r = np.diff(np.log(a))
        stats.append({"file": name, "bars": len(bars), "max_date": bars[-1]["d"] if bars else None,
                      "mean_log_ret": float(r.mean()) if r.size else None})
        if bars and bars[-1]["d"] > fold_data.CUTOFF["dev"]:
            raise SystemExit(f"refused: {name} past the dev cutoff")
    out["sample"] = stats
    out["sandbox_chain_files"] = sorted(os.listdir(os.path.join(sb, "logs")))
    with open(os.path.join(sb, "out", "smoke.json"), "w") as fh:
        json.dump(out, fh, indent=1)
    print(f"SMOKE OK slice={sha[:12]} == prereg {want[:12]} == pin; files={len(files)}; "
          f"sample={[(s['file'], s['bars'], s['max_date']) for s in stats]}; chains={out['sandbox_chain_files']}")


if __name__ == "__main__":
    main()
