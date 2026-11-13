#!/usr/bin/env python3
"""factory/review.py — Gemini reviewer for the Finance Software Factory.

Inputs: a unified diff (text) + factory/gate_report.json.
Calls Gemini via REST (generativelanguage.googleapis.com; key read from
~/.config/gemini.key at runtime, never printed/logged/committed — redacted
from any error output).
Returns strict JSON: {verdict: PASS|PASS_WITH_FLAGS|FAIL,
findings: [{claim, evidence, severity}]}.
RULE: any gate FAIL forces verdict FAIL regardless of Gemini's opinion.
Usage: python factory/review.py <diff_path> [--gate factory/gate_report.json]
Writes factory/review_report.json.  Exit 0 if merge-eligible
(PASS or PASS_WITH_FLAGS), 1 otherwise.
"""
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY_PATH = os.path.expanduser("~/.config/gemini.key")
MODEL = "gemini-3.5-flash"  # 2.5-pro retired (404); 3.1-pro-preview quota-429 for this key
URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"

PROMPT = """You are a strict code reviewer for a finance research codebase.
You are reviewing a refactor diff whose ONLY acceptable behavior change is
none: results must be provably unchanged.  A deterministic gate already
re-ran two golden backtests and the unit-test suite; its report is given.

Decide:
- verdict: "PASS" if the refactor is behavior-preserving and clean;
  "PASS_WITH_FLAGS" if mergeable but worth noting (style, naming, minor
  risks that cannot change results); "FAIL" if you believe the diff could
  change behavior or introduces real defects the gate might miss.
- findings: list of {claim, evidence, severity} where severity is
  "info" | "warn" | "critical".  Cite concrete lines/symbols as evidence.

Respond with ONLY a JSON object, no markdown fences:
{"verdict": "PASS|PASS_WITH_FLAGS|FAIL",
 "findings": [{"claim": "...", "evidence": "...", "severity": "..."}]}
"""

DIFF_CAP = 40000  # chars of diff shown to the model


def _key():
    k = open(KEY_PATH).read().strip()
    if not k:
        raise RuntimeError("gemini key file empty")
    return k


def call_gemini(prompt, attempts=4):
    last = None
    for i in range(attempts):
        payload = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": 2048},
        })
        r = subprocess.run(
            ["curl", "-s", "-m", "120",
             "-H", "Content-Type: application/json",
             "-H", f"x-goog-api-key: {_key()}",
             "-d", payload, URL],
            capture_output=True, text=True)
        out = r.stdout
        try:
            data = json.loads(out)
        except ValueError:
            last = f"gemini non-JSON response: {out[:200].replace(_key(), '[REDACTED]')}"
            time.sleep(15)
            continue
        if "error" in data:
            code = data["error"].get("code")
            last = f"gemini error: {str(data['error'])[:200]}"
            if code in (429, 500, 503) and i < attempts - 1:
                time.sleep(20 * (i + 1))
                continue
            raise RuntimeError(last)
        parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts") or []
        return "".join(p.get("text", "") for p in parts)
    raise RuntimeError(last or "gemini unavailable")


def parse_verdict(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError(f"no JSON in gemini reply: {text[:200]}")
    v = json.loads(m.group(0))
    if v.get("verdict") not in ("PASS", "PASS_WITH_FLAGS", "FAIL"):
        raise RuntimeError(f"bad verdict: {v.get('verdict')!r}")
    v.setdefault("findings", [])
    return v


def main():
    diff_path = sys.argv[1]
    gate_path = os.path.join(ROOT, "factory", "gate_report.json")
    if "--gate" in sys.argv:
        gate_path = sys.argv[sys.argv.index("--gate") + 1]
    gate = json.load(open(gate_path))
    diff = open(diff_path).read()
    if len(diff) > DIFF_CAP:
        diff = diff[:DIFF_CAP // 2] + "\n...[truncated]...\n" + diff[-DIFF_CAP // 2:]

    prompt = (f"{PROMPT}\n\n=== GATE REPORT ===\n{json.dumps(gate, indent=1)}"
              f"\n\n=== DIFF ({diff_path}) ===\n{diff}")
    try:
        reply = call_gemini(prompt)
        review = parse_verdict(reply)
        review["gemini_raw_ok"] = True
    except Exception as e:  # noqa: BLE001
        # Model unavailable is NOT a pass: treat as FAIL with the reason.
        review = {"verdict": "FAIL", "findings": [
            {"claim": "Gemini review unavailable",
             "evidence": str(e).replace(_key(), "[REDACTED]"),
             "severity": "critical"}],
            "gemini_raw_ok": False}

    # HARD RULE: gate failure forces FAIL.
    if not gate.get("all_pass", False):
        review["verdict"] = "FAIL"
        review["findings"].append({
            "claim": "Gate failure overrides reviewer verdict (hard rule)",
            "evidence": "; ".join(c["check"] for c in gate.get("checks", [])
                                  if not c.get("pass")) or "gate all_pass=false",
            "severity": "critical"})
    review["gate_all_pass"] = gate.get("all_pass", False)
    review["model"] = MODEL
    out = os.path.join(ROOT, "factory", "review_report.json")
    json.dump(review, open(out, "w"), indent=1)
    print(f"GEMINI: {review['verdict']} "
          f"({len(review['findings'])} findings) -> {out}")
    for f in review["findings"][:5]:
        print(f"  [{f.get('severity', '?')}] {f.get('claim', '')[:110]}")
    sys.exit(0 if review["verdict"] in ("PASS", "PASS_WITH_FLAGS") else 1)


if __name__ == "__main__":
    main()
