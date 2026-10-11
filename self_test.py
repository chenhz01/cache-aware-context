#!/usr/bin/env python3
"""Self-test for cache-aware-context.
Tiers: [A] functional / [B] exception / [C] extreme edges. Run: python self_test.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from main import (MISS_MODEL, MISS_REWRITE, MISS_TTL, MISS_UNKNOWN,
                  analyze, compress, est_tokens, load_events, main, panel,
                  prefix_hash)

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


def ev(ts, model="m1", pt=1000, ct=900, ph=None):
    return {"ts": ts, "model": model, "prompt_tokens": pt, "cached_tokens": ct,
            "prefix_hash": ph}


# ---------------------------------------------------------------- A
print("[A] functional unit tests")
rep = analyze([ev(1), ev(2)])
check("healthy log hit rate", rep["hit_rate"] == 0.9 and rep["requests"] == 2)
check("healthy log has no misses", rep["misses"] == [])

r = analyze([ev(1), ev(2, model="m2", ct=100)])
check("model switch attributed", r["misses"][0]["attribution"] == MISS_MODEL)

r = analyze([ev(1, ph="A"), ev(2, ph="B", ct=100)])
check("prefix rewrite attributed", r["misses"][0]["attribution"] == MISS_REWRITE)

r = analyze([ev(1, ph="A"), ev(2 + 999, ph="A", ct=100)], ttl=300)
check("ttl expiry attributed", r["misses"][0]["attribution"] == MISS_TTL)

r = analyze([ev(1), ev(2, ct=100)])
check("unexplained miss -> unknown", r["misses"][0]["attribution"] == MISS_UNKNOWN)

# hit AFTER ttl gap resets last-hit tracking (no false ttl)
r = analyze([ev(1, ph="A"), ev(2 + 999, ph="A"), ev(3 + 999, ph="A", ct=100)], ttl=300)
check("fresh hit after gap prevents false TTL on next miss",
      r["misses"][0]["attribution"] != MISS_TTL)

# per-model breakdown
r = analyze([ev(1, model="a"), ev(2, model="b")])
check("per_model counts", set(r["per_model"]) == {"a", "b"} and
      all(v["requests"] == 1 for v in r["per_model"].values()))

# compression: stable prefix invariant
msgs = [{"role": "system", "content": "S" * 400},
        {"role": "user", "content": "U" * 400},
        {"role": "assistant", "content": "A" * 4000},
        {"role": "user", "content": "Q" * 4000}]
c = compress(msgs, keep=2, budget=1200)
check("prefix intact", c["prefix_intact"] is True)
check("fits budget", c["est_tokens_out"] <= 1200)
check("dropped some tail", c["dropped"] >= 1)
check("prefix_hash stable", c["prefix_hash"] == prefix_hash(msgs, 2))
c2 = compress(msgs, keep=2, budget=1200)
check("deterministic output", c["messages"] == c2["messages"] and
      c["prefix_hash"] == c2["prefix_hash"])
c3 = compress(msgs, keep=0, budget=0)
check("zero keep+budget drops all", c3["messages"] == [] and c3["dropped"] == len(msgs))
c4 = compress(msgs, keep=2, budget=sum(est_tokens(str(m["content"])) for m in msgs[:2]))
check("prefix-only budget passes prefix through", len(c4["messages"]) == 2 and
      c4["prefix_intact"] is True)

# panel rendering
p = panel(rep if rep["requests"] else analyze([ev(1)]))
check("panel renders bar", "#" in p and "per model" in p)
check("panel empty log message", panel(analyze([])).startswith("no requests"))

# CLI smoke
fd, LOG = tempfile.mkstemp(suffix=".jsonl")
os.close(fd)
with open(LOG, "w", encoding="utf-8") as f:
    for e in [ev(1, ph="A"), ev(2, ph="A", ct=100), ev(3, model="m2", ct=50)]:
        f.write(json.dumps(e) + "\n")
rc = main(["analyze", "--log", LOG])
check("CLI analyze exit 0", rc == 0)
rc = main(["panel", "--log", LOG])
check("CLI panel exit 0", rc == 0)
rc = main(["analyze", "--log", LOG + ".missing"])
check("CLI missing log exit 2", rc == 2)
# round-2: alert gating for cron/CI
rc = main(["panel", "--log", LOG, "--alert", "0.99"])
check("CLI alert exit 3 when hit rate below threshold", rc == 3)
rc = main(["panel", "--log", LOG, "--alert", "0.01"])
check("CLI alert exit 0 when healthy", rc == 0)
rc = main(["panel", "--log", LOG, "--alert", "1.5"])
check("CLI invalid alert exit 2", rc == 2)
out = subprocess.run([sys.executable, "main.py", "compress", "--keep", "1",
                      "--budget", "100"], input=json.dumps(msgs),
                     capture_output=True, text=True, cwd=HERE)
check("CLI compress via stdin", out.returncode == 0 and
      json.loads(out.stdout)["prefix_intact"] is True)
out = subprocess.run([sys.executable, "main.py", "compress", "--keep", "1",
                      "--budget", "100"], input="{bad json",
                     capture_output=True, text=True, cwd=HERE)
check("CLI compress bad stdin exit 2", out.returncode == 2)

# ---------------------------------------------------------------- B
print("[B] exception / error paths")
try:
    analyze([], ttl=0)
    check("ttl=0 rejected", False)
except ValueError:
    check("ttl=0 rejected", True)
try:
    compress(msgs, keep=2, budget=5)  # prefix alone exceeds budget
    check("prefix-over-budget refused", False)
except ValueError:
    check("prefix-over-budget refused", True)
try:
    compress(msgs, keep=-1, budget=100)
    check("negative keep rejected", False)
except ValueError:
    check("negative keep rejected", True)
try:
    compress(msgs, keep=0, budget=-5)
    check("negative budget rejected", False)
except ValueError:
    check("negative budget rejected", True)
# dirty log handling: malformed lines skipped with warnings, not fatal
dirty = json.dumps(ev(1)) + "\nNOT JSON\n" + json.dumps({"ts": 2}) + "\n[1,2]\n"
events, warns = load_events(None, text=dirty)
check("malformed lines skipped", len(events) == 1 and len(warns) == 3)
events, warns = load_events(None, text=json.dumps(ev(1, pt=-5)))
check("negative tokens skipped w/ warning", events == [] and len(warns) == 1)
check("est_tokens positive floor", est_tokens("") == 1)

# ---------------------------------------------------------------- C
print("[C] extreme edges")
check("empty log analyze", analyze([])["hit_rate"] is None and analyze([])["requests"] == 0)
events, warns = load_events(None, text="")
check("empty log text loads", events == [] and warns == [])
events, warns = load_events(None, text="\n\n  \n")
check("whitespace-only log", events == [])
check("empty messages compress", compress([], keep=0, budget=10)["messages"] == [])
# huge: 5000-event log within time budget
big = [ev(i / 10, ph="A" if i % 2 else "B", pt=1000, ct=100 if i % 3 == 0 else 900)
       for i in range(5000)]
t0 = time.perf_counter()
r = analyze(big)
dt = time.perf_counter() - t0
check(f"5000-event analysis < 2s (measured {dt*1000:.0f} ms)", dt < 2.0)
check("5000-event misses counted", len(r["misses"]) > 0)
# huge single message compression
huge_msgs = [{"role": "user", "content": "x" * 1_000_000}]
c = compress(huge_msgs, keep=0, budget=500)
check("1MB message truncated to budget", c["est_tokens_out"] <= 500 and
      c["messages"][0]["content"].endswith("…"))
# broken prefix_hash types must not crash analysis
r = analyze([ev(1, ph=123), ev(2, ph=None, ct=100)])
check("odd prefix_hash types tolerated", r["misses"][0]["attribution"] == MISS_UNKNOWN)
# corrupted binary file
fd, CORRUPT = tempfile.mkstemp(suffix=".jsonl")
os.write(fd, b"\x00\x01\x02\xff\xfe not utf8 \xff")
os.close(fd)
try:
    load_events(CORRUPT)
    ok = False
except (UnicodeDecodeError, OSError):
    ok = True
check("binary garbage raises (documented failure mode)", ok)
os.unlink(CORRUPT)
os.unlink(LOG)

print(f"\nRESULT: PASS {PASS}/{PASS + FAIL}")
sys.exit(0 if FAIL == 0 else 1)
