#!/usr/bin/env python3
"""
Claude Code prompt-cache TTL advisor.

Reads the Claude Code session transcripts already on this machine
(~/.claude/projects/**/*.jsonl), measures how far apart your API requests
actually are, and prices your real token traffic under both cache TTLs
(5 minutes vs 1 hour). It then recommends a `promptCacheTtl` and
`subagentPromptCacheTtl` setting for Claude Code.

No dependencies beyond the Python 3.9+ standard library. Nothing leaves
your machine; the script only reads local files.

Usage:
    python3 cache_ttl_advisor.py                # analyze everything
    python3 cache_ttl_advisor.py --days 30      # last 30 days only
    python3 cache_ttl_advisor.py --json         # machine-readable output
    python3 cache_ttl_advisor.py --price claude-opus-5=5 --price claude-sonnet-5=2
"""
import argparse
import collections
import glob
import json
import os
import sys
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Pricing (USD per 1M input tokens, Anthropic first-party API rates).
# Snapshot date: 2026-09-10. Override with --price MODEL=RATE if these drift.
# Matching is by prefix, so "claude-haiku-4-5-20251001" matches "claude-haiku-4-5".
# ---------------------------------------------------------------------------
BASE_INPUT_PRICE = {
    "claude-fable-5-1": 10.0,
    "claude-mythos-5-1": 10.0,
    "claude-fable-5": 10.0,
    "claude-mythos-5": 10.0,
    "claude-opus-5": 5.0,
    "claude-opus-4-8": 5.0,
    "claude-opus-4-7": 5.0,
    "claude-opus-4-6": 5.0,
    "claude-opus-4-5": 5.0,
    "claude-opus-4-1": 15.0,
    "claude-opus-4": 15.0,
    "claude-sonnet-5": 2.0,
    "claude-sonnet-4-6": 3.0,
    "claude-sonnet-4-5": 3.0,
    "claude-sonnet-4": 3.0,
    "claude-haiku-4-5": 1.0,
    "claude-haiku-3-5": 0.8,
}
# Cache read multiplier vs base input price. 0.1x everywhere except the
# Fable 5.1 / Mythos 5.1 tier, which reads at 0.025x.
READ_MULT_DEFAULT = 0.10
READ_MULT_OVERRIDE = {"claude-fable-5-1": 0.025, "claude-mythos-5-1": 0.025}
WRITE_MULT_5M = 1.25
WRITE_MULT_1H = 2.00

TTL_5M_SEC = 5 * 60
TTL_1H_SEC = 60 * 60


def price_for(model):
    """Return (base_price, read_mult) for a model id, or (None, None) if unknown."""
    for prefix in sorted(BASE_INPUT_PRICE, key=len, reverse=True):
        if model == prefix or model.startswith(prefix + "-"):
            return BASE_INPUT_PRICE[prefix], READ_MULT_OVERRIDE.get(prefix, READ_MULT_DEFAULT)
    return None, None


def parse_ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def load_requests(path):
    """Yield (timestamp, model, usage) for each distinct API request in one transcript.

    A streamed response can produce several assistant rows with the same
    requestId; the last one carries the final usage numbers.
    """
    by_req = collections.OrderedDict()
    try:
        fh = open(path, "r", errors="ignore")
    except OSError:
        return []
    with fh:
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("type") != "assistant":
                continue
            msg = d.get("message") or {}
            usage = msg.get("usage")
            ts = parse_ts(d.get("timestamp", ""))
            if not usage or ts is None:
                continue
            rid = d.get("requestId") or msg.get("id") or d.get("uuid")
            by_req[rid] = (ts, msg.get("model") or "unknown", usage)
    return list(by_req.values())


def observed_ttl(usage):
    cc = usage.get("cache_creation") or {}
    if cc.get("ephemeral_1h_input_tokens", 0) > 0:
        return "1h"
    if cc.get("ephemeral_5m_input_tokens", 0) > 0:
        return "5m"
    return None


class ScopeStats:
    """Accumulates gap histogram and simulated cost for one scope (main or subagent)."""

    def __init__(self, name):
        self.name = name
        self.chains = 0
        self.requests = 0
        self.first_ts = None
        self.last_ts = None
        self.warm_events = 0
        self.skipped_unknown_model = collections.Counter()
        self.gaps = {"hot": 0, "warm": 0, "cold": 0}
        self.observed = collections.Counter()
        self.per_model = collections.defaultdict(lambda: {
            "requests": 0, "cost_5m": 0.0, "cost_1h": 0.0,
            "warm_gap_requests": 0, "warm_gap_tokens": 0,
            "write_tokens": 0, "read_tokens": 0,
        })

    def add_chain(self, rows, since):
        rows = sorted(rows, key=lambda r: r[0])
        if since is not None:
            rows = [r for r in rows if r[0] >= since]
        if not rows:
            return
        self.chains += 1
        prev_ts = None
        for ts, model, usage in rows:
            self.requests += 1
            if self.first_ts is None or ts < self.first_ts:
                self.first_ts = ts
            if self.last_ts is None or ts > self.last_ts:
                self.last_ts = ts
            gap = (ts - prev_ts).total_seconds() if prev_ts else None
            prev_ts = ts
            if gap is None or gap >= TTL_1H_SEC:
                bucket = "cold"
            elif gap >= TTL_5M_SEC:
                bucket = "warm"
            else:
                bucket = "hot"
            self.gaps[bucket] += 1
            if bucket == "warm":
                self.warm_events += 1
            ttl = observed_ttl(usage)
            self.observed[ttl or "none"] += 1

            base, read_mult = price_for(model)
            if base is None:
                self.skipped_unknown_model[model] += 1
                continue
            per_tok = base / 1e6
            i = usage.get("input_tokens", 0) or 0
            w = usage.get("cache_creation_input_tokens", 0) or 0
            r = usage.get("cache_read_input_tokens", 0) or 0
            pm = self.per_model[model]
            pm["requests"] += 1
            pm["write_tokens"] += w
            pm["read_tokens"] += r

            # --- Counterfactual under a 5-minute TTL -------------------------
            # A "warm" gap (5-60 min) is the only case where the two TTLs
            # diverge. With 5m, the entry has expired: whatever was read from
            # cache must be re-written at 1.25x. If the traffic was already on
            # 5m, the recorded numbers already include that miss.
            if bucket == "warm" and ttl == "1h" and r > 0:
                cost_5m = per_tok * (i + WRITE_MULT_5M * (w + r))
                pm["warm_gap_requests"] += 1
                pm["warm_gap_tokens"] += r
            else:
                cost_5m = per_tok * (i + WRITE_MULT_5M * w + read_mult * r)

            # --- Counterfactual under a 1-hour TTL ---------------------------
            # With 1h the warm-gap request would have hit. If the traffic was
            # on 5m, the recorded write after a warm gap is (mostly) a re-write
            # of the previous prefix; treat it as a read. This slightly
            # overstates 1h savings when the turn also added a lot of new
            # content, so 1h is not favored beyond that approximation.
            if bucket == "warm" and ttl == "5m" and w > 0:
                cost_1h = per_tok * (i + read_mult * (w + r))
                pm["warm_gap_requests"] += 1
                pm["warm_gap_tokens"] += w
            else:
                cost_1h = per_tok * (i + WRITE_MULT_1H * w + read_mult * r)

            pm["cost_5m"] += cost_5m
            pm["cost_1h"] += cost_1h

    def totals(self):
        t5 = sum(m["cost_5m"] for m in self.per_model.values())
        t1 = sum(m["cost_1h"] for m in self.per_model.values())
        return t5, t1

    def span_days(self):
        if self.first_ts is None:
            return 0
        return (self.last_ts - self.first_ts).days

    def confidence(self, min_requests, min_warm, min_days):
        """Return (level, reasons). The 5m/1h decision is driven almost entirely by
        warm-gap events (5-60 min pauses), so too few of those means the answer is noise."""
        reasons = []
        if self.requests < min_requests:
            reasons.append("only {} requests (want >= {})".format(self.requests, min_requests))
        if self.warm_events < min_warm:
            reasons.append("only {} warm-gap events (want >= {})".format(self.warm_events, min_warm))
        if self.span_days() < min_days:
            reasons.append("data spans only {} days (want >= {})".format(self.span_days(), min_days))
        if not reasons:
            return "high", reasons
        if len(reasons) == 1 and self.warm_events >= min_warm // 2:
            return "medium", reasons
        return "low", reasons

    def recommendation(self, threshold_pct):
        t5, t1 = self.totals()
        if t5 == 0 and t1 == 0:
            return None, 0.0
        cheaper, other = ("1h", t5) if t1 < t5 else ("5m", t1)
        best = min(t5, t1)
        pct = 100.0 * (other - best) / other if other else 0.0
        if pct < threshold_pct:
            return "either", pct
        return cheaper, pct


def fmt_money(x):
    return "${:,.2f}".format(x)


def render_text(scopes, args, window_desc):
    out = []
    out.append("Claude Code prompt-cache TTL advisor")
    out.append("Transcripts: {}".format(args.projects_dir))
    out.append("Window: {}".format(window_desc))
    out.append("Prices: first-party API rates, snapshot 2026-09-10 (override with --price)")
    out.append("")
    settings = {}
    for sc in scopes:
        out.append("=" * 72)
        out.append("{}  ({} chains, {} requests)".format(sc.name.upper(), sc.chains, sc.requests))
        out.append("=" * 72)
        if sc.requests == 0:
            out.append("  no requests found")
            out.append("")
            continue
        n = sum(sc.gaps.values())
        out.append("Data span: {} -> {} ({} days)".format(sc.first_ts.date(), sc.last_ts.date(), sc.span_days()))
        out.append("Gap to previous request in the same chain:")
        for k, label in (("hot", "< 5 min   (both TTLs hit)"),
                         ("warm", "5-60 min  (only 1h hits)"),
                         ("cold", ">= 60 min or first request (both miss)")):
            out.append("  {:42} {:7}  {:5.1f}%".format(label, sc.gaps[k], 100.0 * sc.gaps[k] / n))
        obs = ", ".join("{}: {}".format(k, v) for k, v in sc.observed.most_common())
        out.append("Observed TTL on cache writes: {}".format(obs))
        out.append("")
        out.append("Simulated input-side cost (uncached input + cache writes + cache reads):")
        out.append("  {:22}{:>7}{:>13}{:>13}{:>12}   {}".format(
            "model", "reqs", "5m TTL", "1h TTL", "1h - 5m", "warm-gap hits (tokens)"))
        for model, pm in sorted(sc.per_model.items(), key=lambda kv: -max(kv[1]["cost_5m"], kv[1]["cost_1h"])):
            out.append("  {:22}{:>7}{:>13}{:>13}{:>+12.2f}   {} ({:.1f}M)".format(
                model, pm["requests"], fmt_money(pm["cost_5m"]), fmt_money(pm["cost_1h"]),
                pm["cost_1h"] - pm["cost_5m"], pm["warm_gap_requests"], pm["warm_gap_tokens"] / 1e6))
        t5, t1 = sc.totals()
        out.append("  {:22}{:>7}{:>13}{:>13}{:>+12.2f}".format("TOTAL", "", fmt_money(t5), fmt_money(t1), t1 - t5))
        if sc.skipped_unknown_model:
            out.append("  skipped (no price known): {}".format(dict(sc.skipped_unknown_model)))
        rec, pct = sc.recommendation(args.threshold)
        level, reasons = sc.confidence(args.min_requests, args.min_warm_events, args.min_days)
        out.append("")
        out.append("Confidence: {}{}".format(level, ("  (" + "; ".join(reasons) + ")") if reasons else ""))
        if rec == "either":
            out.append("Recommendation: either TTL (difference {:.1f}%, under the {:.0f}% threshold). Keep the default.".format(pct, args.threshold))
        elif rec and level == "low":
            out.append("Recommendation: not enough data to recommend a change (the numbers lean {}, {:.1f}%).".format(rec, pct))
            out.append("  Raise cleanupPeriodDays in ~/.claude/settings.json (default 30) so transcripts")
            out.append("  accumulate, then re-run in a few weeks.")
        elif rec:
            out.append("Recommendation: {}  (saves {:.1f}% of input-side cost vs the other TTL)".format(rec, pct))
            settings[sc.name] = rec
        out.append("")

    if settings:
        out.append("-" * 72)
        out.append("To apply, add to ~/.claude/settings.json (user) or .claude/settings.json (project):")
        out.append("")
        body = {}
        if "main" in settings:
            body["promptCacheTtl"] = settings["main"]
        if "subagent" in settings:
            body["subagentPromptCacheTtl"] = settings["subagent"]
        out.append(json.dumps(body, indent=2))
        out.append("")
        out.append("Or as environment variables:")
        if "main" in settings:
            out.append("  export CLAUDE_CODE_PROMPT_CACHE_TTL={}".format(settings["main"]))
        if "subagent" in settings:
            out.append("  export CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL={}".format(settings["subagent"]))
        out.append("")
        out.append("Requires Claude Code 2.1.242 or later. Docs: https://code.claude.com/docs/en/prompt-caching")
    out.append("")
    out.append("Caveats: dollar figures are API-rate equivalents of your token traffic (on a Claude")
    out.append("subscription they describe plan usage, not a bill). Output tokens are excluded because")
    out.append("they cost the same under either TTL. Claude Code deletes transcripts older than")
    out.append("cleanupPeriodDays (default 30), which caps how far back this can look. The transcript")
    out.append("format is internal to Claude Code and may change between releases.")
    return "\n".join(out)


def render_json(scopes, args, window_desc):
    doc = {"projects_dir": args.projects_dir, "window": window_desc, "scopes": {}}
    for sc in scopes:
        t5, t1 = sc.totals()
        rec, pct = sc.recommendation(args.threshold)
        level, reasons = sc.confidence(args.min_requests, args.min_warm_events, args.min_days)
        doc["scopes"][sc.name] = {
            "chains": sc.chains, "requests": sc.requests, "gaps": sc.gaps,
            "span_days": sc.span_days(), "warm_gap_events": sc.warm_events,
            "confidence": level, "confidence_reasons": reasons,
            "observed_ttl": dict(sc.observed),
            "cost_5m": round(t5, 2), "cost_1h": round(t1, 2),
            "recommendation": rec, "savings_pct": round(pct, 2),
            "per_model": {m: {k: (round(v, 2) if isinstance(v, float) else v) for k, v in pm.items()}
                          for m, pm in sc.per_model.items()},
            "skipped_unknown_model": dict(sc.skipped_unknown_model),
        }
    return json.dumps(doc, indent=2)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--projects-dir", default=os.path.expanduser("~/.claude/projects"),
                    help="Claude Code projects directory (default: ~/.claude/projects)")
    ap.add_argument("--days", type=int, default=None, help="only consider requests from the last N days")
    ap.add_argument("--threshold", type=float, default=5.0,
                    help="minimum %% savings before recommending a change (default: 5)")
    ap.add_argument("--min-requests", type=int, default=300, help="requests needed for high confidence (default: 300)")
    ap.add_argument("--min-warm-events", type=int, default=30,
                    help="5-60 min gap events needed for high confidence (default: 30)")
    ap.add_argument("--min-days", type=int, default=14, help="data span in days needed for high confidence (default: 14)")
    ap.add_argument("--price", action="append", default=[], metavar="MODEL=USD_PER_MTOK",
                    help="override base input price for a model prefix (repeatable)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = ap.parse_args()

    for spec in args.price:
        try:
            model, rate = spec.split("=", 1)
            BASE_INPUT_PRICE[model.strip()] = float(rate)
        except ValueError:
            sys.exit("bad --price value: {!r} (expected MODEL=RATE)".format(spec))

    if not os.path.isdir(args.projects_dir):
        sys.exit("projects directory not found: {}".format(args.projects_dir))

    since = None
    window_desc = "all time"
    if args.days:
        since = datetime.now(timezone.utc) - timedelta(days=args.days)
        window_desc = "last {} days (since {})".format(args.days, since.date())

    main_scope = ScopeStats("main")
    sub_scope = ScopeStats("subagent")

    # Each transcript file is one chain of requests sharing a cache prefix.
    for path in glob.glob(os.path.join(args.projects_dir, "*", "*.jsonl")):
        main_scope.add_chain(load_requests(path), since)
    for path in glob.glob(os.path.join(args.projects_dir, "*", "*", "subagents", "*.jsonl")):
        sub_scope.add_chain(load_requests(path), since)

    scopes = [main_scope, sub_scope]
    print(render_json(scopes, args, window_desc) if args.json else render_text(scopes, args, window_desc))


if __name__ == "__main__":
    main()
