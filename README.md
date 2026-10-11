Prefix-cache health monitoring and attribution for context compression. Compress your agent's context without silently destroying the provider-side prompt cache: this middleware guarantees the stable prefix is never rewritten, then measures what your cache hit rate actually became and attributes every miss to a cause — model switch, prefix rewrite, TTL expiry, or unknown.

If you compress context and can't answer "why did my cache hit rate drop from 90% to 20%?", this is for you. If you never compress, you still get the monitoring panel for free.

## Problem

Context compressors compete on smarter summaries. The metric almost nobody optimizes or even measures is **prefix-cache economics**: providers cache repeated prompt prefixes, so a compressor that rewrites the beginning of a conversation can save 50% of tokens while *increasing* total cost and latency by killing every cache hit. One compression plugin (ranxianglei/billion-context) made this constraint explicit — claiming ≤2% compression cost while keeping 95-97% prefix-cache hits — and shipped a diagnostic for it. The overlooked angle: cache hit rate is an *operable, attributable* metric, not a vendor statistic. Restated in our words: context engineering needs observability, not cleverness.

Confidence: **High (≥80%)** that the measurement/attribution mechanics are correct (fully self-tested); **Medium (60-79%)** that the 50% miss threshold and 4-way attribution taxonomy match your workload — both are defaults, not truths.

## How it works

1. **Log.** Your harness (or the compression middleware) appends one JSON line per API call: `ts, model, prompt_tokens, cached_tokens, prefix_hash` — where `prefix_hash` is a hash of the stable-prefix messages you send.
2. **Analyze.** `analyze` computes overall and per-model hit rates. Every request with <50% cached ratio is attributed:
   - `model_switch` — the model changed vs. the previous request;
   - `prefix_rewrite` — `prefix_hash` changed vs. the previous request (something rewrote your stable prefix — the compression bug this repo exists to catch);
   - `ttl_expired` — same prefix, but more than `--ttl` seconds since its last good hit;
   - `unknown` — none of the above (the honest bucket).
3. **Compress.** `compress(messages, keep, budget)` trims the tail to an estimated token budget. The head (`messages[:keep]`) is passed through *unmodified* and hashed — if the prefix alone exceeds the budget, it refuses rather than break the invariant.
4. **Gate.** `--alert RATE` makes the CLI exit 3 when hit rate falls below RATE, so a cron job or CI check can fail loudly.

## Quickstart (~5 min)

```bash
# 1. self-test (~5 s)
python self_test.py

# 2. feed it a log of API-call events (~1 min)
cat > events.jsonl <<'EOF'
{"ts": 1, "model": "gpt-x", "prompt_tokens": 1000, "cached_tokens": 900, "prefix_hash": "a1"}
{"ts": 2, "model": "gpt-x", "prompt_tokens": 1000, "cached_tokens": 50,  "prefix_hash": "b2"}
{"ts": 3, "model": "gpt-y", "prompt_tokens": 1000, "cached_tokens": 100, "prefix_hash": "b2"}
EOF
python main.py panel --log events.jsonl --ttl 300

# 3. compress with the stable-prefix guarantee (~1 min)
echo '[{"role":"user","content":"<4000 chars>"},{"role":"assistant","content":"<long>"}]' \
  | python main.py compress --keep 1 --budget 300

# 4. wire as a cron gate (~1 min): exit 3 = page someone
python main.py panel --log events.jsonl --alert 0.7 || echo "cache degraded"
```

## Why not X

| Alternative | What it does | Why not enough |
|---|---|---|
| Provider usage dashboards | Show aggregate cached-token stats | Per-request attribution absent; no compression coupling; not actionable inside your loop |
| Smart compressors (LLM summarizers) | Better summaries, more tokens saved | Rarely measure or preserve prefix-cache hits; the two goals conflict and they don't show you the conflict |
| LangChain/memory libraries | Context management built in | Attribution is internal or absent; no standalone log format you can audit; hard to gate CI on |
| DIY spreadsheet of cache stats | Tracks hit rate manually | No attribution rules, no prefix-hash check, drifts the first week |

## Minimal Fallback (PLAN B)

No Python: keep the invariant and one metric.

1. Never modify the first K messages of your prompt (hardcode K).
2. Log `cached_tokens/prompt_tokens` per call to a CSV.
3. Alert when the 20-call moving average drops 20%: a one-line `awk` job.

You lose attribution and the hash check, but you keep the invariant that matters most.

## Design Trade-offs

Priority order: **cache invariant > measurement honesty > compression ratio.**

- **Refuse, don't fudge.** If the prefix alone exceeds the budget, `compress` raises instead of silently rewriting the prefix. A loud failure beats a quiet cache kill.
- **Crude token estimate** (~4 chars/token, no tokenizer). Deliberate: zero dependencies, and estimates only gate compression, never billing. All numbers derived from it are `est_tokens` / `[estimated]` by definition.
- **Rule-based attribution.** Four rules, deterministic, testable — vs. an ML classifier that would be unauditable and wrong in new ways.
- **50% miss threshold is a default, not a law.** Provider caches differ; the constant is exposed and the CLI takes `--ttl`, but the miss ratio is currently fixed (see Limits).
- **Dirty-tolerant log parsing.** Malformed lines are skipped with a warning, never fatal — a monitor that crashes on the first bad line stops monitoring exactly when things get interesting.

## Limits & Second-order Effects

- **Needs `prefix_hash` from the caller.** The tool cannot see your raw prompt; if the hash is computed over the wrong slice, `prefix_rewrite` attribution is wrong. Garbage in, misattributed out.
- **Fixed 50% miss threshold** — not configurable in v0.1.
- **Token estimates are not tokenization.** A 10% estimate error is normal; don't use `est_tokens` for cost accounting.
- **Second-order effect 1 — metric theater:** once a cache-health panel exists, teams may optimize hit rate instead of task success — keeping giant stable prefixes to farm cache hits while context quality decays. Hit rate is a health metric, not an objective.
- **Second-order effect 2 — attribution anchoring:** the four buckets will tempt people to treat `unknown` as noise. In practice a rising `unknown` share is the signal that your pipeline changed in a way the rules don't model.
- **Second-order effect 3 — compressor distrust:** making prefix rewrites visible may push teams away from compression entirely, back to raw long contexts — the expensive opposite failure. The point is informed choice, not zero compression.

## FAQ

**Q: Does this talk to any LLM API?**
No. Zero network code. It reads your event log and compresses message arrays you pipe in.

**Q: What's a good hit rate?**
Reference points reported by others are 95-97% for healthy sessions [unverified, third-party claim]; your baseline is whatever your log says before you change anything. Compare against yourself.

**Q: Why is `unknown` a bucket instead of a failure?**
Because honest attribution beats confident wrongness. Rules cover model switches, rewrites, and TTL; everything else is real signal you'll want to look at.

**Q: When NOT to use this tool?** *(ask this first)*
- Your provider doesn't do prefix caching — the metric measures nothing.
- You don't compress and don't care about cache economics — the panel alone may still help, but it's optional.
- Your prompts are single-turn and never repeated — hit rate will be structurally ~0%, not a problem.
- You need billing-grade token counts — the estimator is deliberately crude.

**Q: Why 4 chars ≈ 1 token?**
Zero-dependency order-of-magnitude estimate. Swap in a real tokenizer by replacing `est_tokens` — one function, one test.

## [Core Assumptions]

1. Your provider supports prefix caching and reports `cached_tokens` (or equivalent) per request. Without that report, all hit-rate numbers are fiction.
2. The stable prefix of a conversation (system prompt + early history) is what the provider caches; hashes computed over a different slice will misattribute.
3. Compression quality is separable from cache health — this tool only guarantees/monitors the latter.
4. Turn-based agent conversations, where a stable prefix exists and is worth preserving.
5. A >2x drop in hit rate is worth an alert; the default 50% miss threshold encodes that.

---
Contact: hcac4735@agent.qq.com · License: MIT · Measured numbers: 5000-event analysis ≈17 ms (repro: `python self_test.py`). Third-party hit-rate figures are marked [unverified]. As of 2026-10-11.
