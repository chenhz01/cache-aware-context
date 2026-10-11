#!/usr/bin/env python3
"""cache-aware-context: prefix-cache health monitoring + attribution for context compression.

A compression middleware with one rule above all others: never touch the
stable prefix of a conversation, because rewriting the prefix destroys the
provider-side prompt cache you are paying for.

Usage:
  python main.py analyze  --log events.jsonl [--ttl 300]
  python main.py panel    --log events.jsonl [--ttl 300]
  python main.py compress --keep 2 --budget 500 < messages.json
  python main.py version

Event schema (JSONL, one per API call):
  {"ts": 1781000000.0, "model": "gpt-x", "prompt_tokens": 1000,
   "cached_tokens": 950, "prefix_hash": "a1b2c3"}

`prefix_hash` is a hash of the first (stable-prefix) messages of the prompt.
Pure stdlib.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys

MISS_TTL = "ttl_expired"
MISS_MODEL = "model_switch"
MISS_REWRITE = "prefix_rewrite"
MISS_UNKNOWN = "unknown"
MISS_ATTRS = (MISS_TTL, MISS_MODEL, MISS_REWRITE, MISS_UNKNOWN)


class LogError(ValueError):
    pass


# ---------------------------------------------------------------- events
def load_events(path: str | None, text: str | None = None) -> tuple[list[dict], list[str]]:
    """Parse JSONL; returns (events, warnings). Malformed lines are skipped,
    never fatal — a monitoring tool must not die on a dirty log."""
    raw = text
    if raw is None:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    events, warnings = [], []
    for i, line in enumerate(raw.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError as e:
            warnings.append(f"line {i}: skipped malformed JSON ({e})")
            continue
        if not isinstance(ev, dict):
            warnings.append(f"line {i}: skipped non-object line")
            continue
        missing = [k for k in ("ts", "model", "prompt_tokens", "cached_tokens") if k not in ev]
        if missing:
            warnings.append(f"line {i}: skipped, missing {missing}")
            continue
        if not isinstance(ev["prompt_tokens"], (int, float)) or not \
           isinstance(ev["cached_tokens"], (int, float)) or ev["prompt_tokens"] < 0 or \
           ev["cached_tokens"] < 0:
            warnings.append(f"line {i}: skipped, token counts must be non-negative numbers")
            continue
        ev.setdefault("prefix_hash", None)
        events.append(ev)
    events.sort(key=lambda e: e["ts"])
    return events, warnings


# ---------------------------------------------------------------- analysis
def analyze(events: list[dict], ttl: float = 300.0) -> dict:
    """Compute cache health + attribute every low-hit request."""
    if ttl <= 0:
        raise ValueError("ttl must be > 0")
    if not events:
        return {"requests": 0, "prompt_tokens": 0, "cached_tokens": 0,
                "hit_rate": None, "misses": [], "per_model": {}}

    total_prompt = sum(e["prompt_tokens"] for e in events)
    total_cached = sum(e["cached_tokens"] for e in events)
    misses, per_model = [], {}
    prev = None
    last_hit_ts: dict[str, float] = {}  # prefix_hash -> last ts seen with a good hit

    for e in events:
        m = e["model"]
        pm = per_model.setdefault(m, {"requests": 0, "prompt_tokens": 0, "cached_tokens": 0})
        pm["requests"] += 1
        pm["prompt_tokens"] += e["prompt_tokens"]
        pm["cached_tokens"] += e["cached_tokens"]

        ratio = e["cached_tokens"] / e["prompt_tokens"] if e["prompt_tokens"] > 0 else 0.0
        attr = None
        if ratio < 0.5:  # a "miss" = less than half the prefix was cached
            if prev is not None and m != prev["model"]:
                attr = MISS_MODEL
            elif prev is not None and e.get("prefix_hash") and prev.get("prefix_hash") \
                    and e["prefix_hash"] != prev["prefix_hash"]:
                attr = MISS_REWRITE
            elif e.get("prefix_hash") and e["prefix_hash"] in last_hit_ts \
                    and e["ts"] - last_hit_ts[e["prefix_hash"]] > ttl:
                attr = MISS_TTL
            else:
                attr = MISS_UNKNOWN
            misses.append({"ts": e["ts"], "model": m, "ratio": round(ratio, 4),
                           "attribution": attr})
        if ratio >= 0.5 and e.get("prefix_hash"):
            last_hit_ts[e["prefix_hash"]] = e["ts"]
        prev = e

    return {"requests": len(events), "prompt_tokens": total_prompt,
            "cached_tokens": total_cached,
            "hit_rate": round(total_cached / total_prompt, 4) if total_prompt else None,
            "misses": misses, "per_model": per_model}


# ---------------------------------------------------------------- panel
def _bar(pct: float, width: int = 20) -> str:
    n = int(round(max(0.0, min(1.0, pct)) * width))
    return "#" * n + "." * (width - n)


def panel(rep: dict) -> str:
    if rep["requests"] == 0:
        return "no requests in log — nothing to report"
    lines = []
    hr = rep["hit_rate"]
    lines.append(f"prefix-cache health panel  ({rep['requests']} requests)")
    lines.append(f"hit rate  {hr:6.1%}  {_bar(hr)}")
    lines.append(f"tokens    {rep['cached_tokens']}/{rep['prompt_tokens']} cached")
    attr_counts: dict[str, int] = {}
    for miss in rep["misses"]:
        attr_counts[miss["attribution"]] = attr_counts.get(miss["attribution"], 0) + 1
    lines.append(f"misses    {len(rep['misses'])}/{rep['requests']} requests below 50% hit")
    if attr_counts:
        total = sum(attr_counts.values())
        for k in MISS_ATTRS:
            if k in attr_counts:
                share = attr_counts[k] / total
                lines.append(f"  {k:<16} {attr_counts[k]:>4}  {_bar(share, 14)} {share:5.1%}")
    lines.append("per model:")
    for m, pm in sorted(rep["per_model"].items()):
        r = pm["cached_tokens"] / pm["prompt_tokens"] if pm["prompt_tokens"] else 0.0
        lines.append(f"  {m:<16} {pm['requests']:>4} req  hit {r:5.1%}")
    return "\n".join(lines)


# ---------------------------------------------------------------- compression
def est_tokens(text: str) -> int:
    """Rough estimate: 1 token ~ 4 chars. Deliberately crude; see README."""
    return max(1, (len(text) + 3) // 4)


def prefix_hash(messages: list[dict], keep: int) -> str:
    h = hashlib.sha256()
    for m in messages[:keep]:
        h.update(json.dumps(m, sort_keys=True, ensure_ascii=False).encode())
    return h.hexdigest()[:16]


def compress(messages: list[dict], keep: int, budget: int) -> dict:
    """Compress the tail of a message list to fit `budget` estimated tokens.

    Guarantees: messages[:keep] pass through UNMODIFIED (stable prefix, the
    cache-critical invariant); budget=0 drops the whole tail; result never
    grows the input.
    """
    if keep < 0:
        raise ValueError("keep must be >= 0")
    if budget < 0:
        raise ValueError("budget must be >= 0")
    if keep > len(messages):
        keep = len(messages)
    head = [dict(m) for m in messages[:keep]]
    tail = messages[keep:]

    head_cost = sum(est_tokens(str(m.get("content", ""))) for m in head)
    if head_cost > budget:
        raise ValueError(
            f"stable prefix alone costs ~{head_cost} tokens > budget {budget}: "
            "refusing to break the cache invariant (raise budget or lower keep)")

    remaining = budget - head_cost
    out_tail = []
    for m in tail:
        if remaining <= 0:
            break
        content = m.get("content", "")
        cost = est_tokens(str(content))
        if cost <= remaining:
            out_tail.append(dict(m))
            remaining -= cost
        else:
            cut = max(0, remaining * 4 - 1)
            if cut > 0:
                out_tail.append({**m, "content": str(content)[:cut] + "…",
                                 "compressed": True})
            remaining = 0
    out = head + out_tail
    return {
        "messages": out,
        "prefix_hash": prefix_hash(messages, keep),
        "est_tokens_in": sum(est_tokens(str(m.get("content", ""))) for m in messages),
        "est_tokens_out": sum(est_tokens(str(m.get("content", ""))) for m in out),
        "dropped": len(messages) - len(out),
        "prefix_intact": out[:keep] == [dict(m) for m in messages[:keep]],
    }


# ---------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cache-aware-context")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("analyze", "panel"):
        sp = sub.add_parser(name)
        sp.add_argument("--log", required=True)
        sp.add_argument("--ttl", type=float, default=300.0)
        sp.add_argument("--alert", type=float, default=None, metavar="RATE",
                        help="exit 3 when hit rate falls below RATE (0-1); for cron/CI gating")
    sp = sub.add_parser("compress")
    sp.add_argument("--keep", type=int, default=2)
    sp.add_argument("--budget", type=int, required=True)
    sp.add_argument("--out", default=None)
    sub.add_parser("version")

    a = p.parse_args(argv)
    if a.cmd == "version":
        print("cache-aware-context 0.1.1")
        return 0
    if a.cmd == "compress":
        raw = sys.stdin.read()
        try:
            messages = json.loads(raw) if raw.strip() else []
        except json.JSONDecodeError as e:
            print(f"ERROR: stdin is not valid JSON: {e}", file=sys.stderr)
            return 2
        try:
            result = compress(messages, a.keep, a.budget)
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        payload = json.dumps(result, ensure_ascii=False, indent=1)
        if a.out:
            with open(a.out, "w", encoding="utf-8") as f:
                f.write(payload)
        else:
            print(payload)
        return 0
    # analyze / panel
    try:
        events, warnings = load_events(a.log)
        rep = analyze(events, ttl=a.ttl)
    except (OSError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    for w in warnings:
        print(f"WARN: {w}", file=sys.stderr)
    if a.alert is not None and not 0 < a.alert <= 1:
        print("ERROR: --alert must be in (0, 1]", file=sys.stderr)
        return 2
    if a.cmd == "analyze":
        print(json.dumps(rep, indent=1))
    else:
        print(panel(rep))
    if a.alert is not None:
        hr = rep["hit_rate"]
        if hr is None or hr < a.alert:
            print(f"ALERT: hit rate {hr} below threshold {a.alert}", file=sys.stderr)
            return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
