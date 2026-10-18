"""Temp research root for gate / harness tests: a git repo with a bare 'origin',
the real gate code copied in (fold_gate requires it identical on origin/main),
a family registry, an Aaron test signing key, and helpers to write preregs.
Everything lives under tmp_path; finpaths.set_test_root() points the gate,
the chains, the anchors and the fold copies at it."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import yaml

import finpaths

REPO = Path(__file__).resolve().parent.parent
GIT = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false"]
GATE_CODE = ("fold_gate.py", "chainlog.py", "finpaths.py", "fold_data.py", "witness.py", "codetree.py")
SPEC = {"hold_trading_days": 63, "direction": "long", "same_day_ok": False, "min_price": 5.0,
        "costs": {"fee_bps": 15, "half_spread_bps": {"large": 5, "mid": 15, "small": 40}, "slip_bps": 10,
                  "large_adv": 50000000, "mid_adv": 5000000, "floor_roundtrip_bps": 0, "borrow_bps_annual": 50},
        "delisting": {"perf_base": -0.30, "other_base": 0.0}}


def git(root, *a, check=True):
    return subprocess.run(GIT + ["-C", root, *a], check=check, capture_output=True)


class Env:
    def __init__(self, tmp_path):
        self.tmp = str(tmp_path)
        self.root = os.path.join(self.tmp, "root")
        os.makedirs(os.path.join(self.root, "ops", "prereg"))
        os.makedirs(os.path.join(self.root, "scripts"))
        os.makedirs(os.path.join(self.root, "logs"))
        finpaths.set_test_root(self.root)
        self.origin = os.path.join(self.tmp, "origin.git")
        subprocess.run(GIT + ["init", "-q", "--bare", "-b", "main", self.origin], check=True)
        subprocess.run(GIT + ["init", "-q", "-b", "main", self.root], check=True)
        git(self.root, "remote", "add", "origin", self.origin)
        open(os.path.join(self.root, ".gitignore"), "w").write("logs/\nraw/\nfolds/\nuniverse/\nfacts/\n.state/\n")
        for f in GATE_CODE:
            shutil.copy(REPO / "scripts" / f, os.path.join(self.root, "scripts", f))
        self.families = {}
        self.gate = None                     # REGISTRY.yaml gate: section (None = absent = defaults)
        self.register("FAM9", ["H9"], commit=False)
        self.commit("init", ".")
        self.push()
        self.key = None
        self.init_witness()

    def init_witness(self):
        """Genesis witness tags on the bare origin (mimics ops/offbox/witness_init.sh)."""
        import witness
        head = git(self.root, "rev-parse", "HEAD").stdout.decode().strip()
        for name in ("holdout_access", "variants"):
            witness.push(name, [], head)

    # ---- git
    def commit(self, msg, *paths):
        git(self.root, "add", *paths)
        git(self.root, "commit", "-qm", msg)
        return git(self.root, "rev-parse", "HEAD").stdout.decode().strip()

    def push(self):
        git(self.root, "push", "-q", "origin", "HEAD:main")
        git(self.root, "fetch", "-q", "origin")

    # ---- registry / prereg
    def register(self, family, hyps, retired=False, commit=True, push=True):
        self.families[family] = {"hypotheses": list(hyps), **({"retired": True} if retired else {})}
        doc = {"families": self.families, **({"gate": self.gate} if self.gate is not None else {})}
        open(os.path.join(self.root, "ops", "prereg", "REGISTRY.yaml"), "w").write(yaml.safe_dump(doc))
        if commit:
            self.commit(f"register {family}", "ops/prereg/REGISTRY.yaml")
            if push:
                self.push()

    def prereg(self, h="H9", family="FAM9", extra=None, spec=None, commit=True, push=True, body=None):
        p = os.path.join(self.root, "ops", "prereg", f"{h}.yaml")
        doc = {"hypothesis": h, "family": family, "execution": spec or SPEC, **(extra or {})}
        open(p, "w").write(body if body is not None else yaml.safe_dump(doc, sort_keys=True))
        if commit:
            c = self.commit(f"prereg {h}", os.path.relpath(p, self.root))
            if push:
                self.push()
            return c
        return None

    # ---- Aaron test key + approvals (mimics ops/gate/aaron_sign_holdout_approval.sh)
    def install_key(self, push=True):
        """Aaron's test key at the PINNED location (production: root-owned
        /etc/finance-gate/allowed_signers; tests: <root>/.state/etc/allowed_signers).
        Nothing is committed: the repo copy of allowed_signers is ignored by the gate."""
        import fold_gate
        k = os.path.join(self.tmp, "aaron_key")
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "aaron-test", "-f", k], check=True)
        pub = open(k + ".pub").read().split()
        p = fold_gate.signers_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write(f'aaron namespaces="finance-holdout,finance-go" {pub[0]} {pub[1]}\n')
        self.key = k
        return k

    def approval(self, family, h, key=None, expires="2099-01-01T00:00:00-04:00", **over):
        import fold_gate
        st = fold_gate.prereg_status(h)
        a = {"v": 1, "fold": "holdout", "family": family, "hypothesis": h, "prereg_path": st["path"],
             "prereg_sha256": st["sha"], "prereg_commit": st["commit"], "nonce": os.urandom(8).hex(),
             "issued": "2026-10-18T10:00:00-04:00", "expires": expires}
        a.update(over)
        d = os.path.join(self.root, "logs", "approvals")
        os.makedirs(d, exist_ok=True)
        jf = os.path.join(d, f"{family}.json")
        open(jf, "w").write(json.dumps(a, sort_keys=True))
        if os.path.exists(jf + ".sig"):
            os.remove(jf + ".sig")
        subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", key or self.key, "-n", "finance-holdout", jf],
                       check=True, capture_output=True)
        return jf

    # ---- v5: code freeze amendments + go files (mimic ops/offbox/aaron_sign_dev_go.sh)
    def freeze(self, h="H9", files=None, n=None, push=True, over=None):
        """Commit ops/prereg/amendments/<h>/NNN.yaml freezing `files` (default: a helper
        scripts/<h>_events.py written here) at their current blobs.  -> freeze_sha."""
        import fold_gate
        if files is None:
            f = os.path.join(self.root, "scripts", f"{h.lower()}_events.py")
            if not os.path.exists(f):
                open(f, "w").write("# synthetic frozen event code\nEVENTS = []\n")
                self.commit("events", os.path.relpath(f, self.root))
            files = [os.path.relpath(f, self.root)]
        st = fold_gate.prereg_status(h)
        d = os.path.join(self.root, "ops", "prereg", "amendments", h)
        os.makedirs(d, exist_ok=True)
        n = n or len([x for x in os.listdir(d) if x.endswith(".yaml")]) + 1
        cf = {r: git(self.root, "hash-object", r).stdout.decode().strip() for r in files}
        doc = {"hypothesis": h, "amendment": n, "prereg_sha256": st["sha"], "code_freeze": cf,
               "reason": "test freeze", **(over or {})}
        p = os.path.join(d, f"{n:03d}.yaml")
        open(p, "w").write(yaml.safe_dump(doc, sort_keys=True))
        self.commit(f"amend {h} {n}", os.path.relpath(p, self.root))
        if push:
            self.push()
        return fold_gate.freeze_sha(cf)

    def go(self, h, fold, freeze_sha, key=None, root_owned=False, **over):
        """A go file: standing dev go (max_dev_trials) or a per-run validation/holdout go."""
        import fold_gate
        doc = {"v": 1, "hypothesis": h, "fold": fold, "freeze_sha": freeze_sha,
               "issued": "2026-10-18T10:00:00-04:00", "expires": "2099-01-01T00:00:00-04:00"}
        if fold == "dev":
            doc.update(kind="standing_dev_go", max_dev_trials=3)
        else:
            st = fold_gate.prereg_status(h)
            doc.update(kind="run_go", prereg_sha256=st["sha"], nonce=os.urandom(8).hex())
        doc.update(over)
        signed, sig, ro = fold_gate.go_files(h, fold)
        for q in (signed, sig, ro):
            if os.path.exists(q):
                os.remove(q)
        target = ro if root_owned else signed
        os.makedirs(os.path.dirname(target), exist_ok=True)
        open(target, "w").write(json.dumps(doc, sort_keys=True))
        if not root_owned:
            subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", key or self.key, "-n", "finance-go", target],
                           check=True, capture_output=True)
        return target

    def log(self):
        p = os.path.join(self.root, "logs", "holdout_access.jsonl")
        return [json.loads(x) for x in open(p)] if os.path.exists(p) else []
