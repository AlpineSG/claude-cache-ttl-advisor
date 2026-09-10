# Claude Code prompt-cache TTL advisor

A single-file Python script that reads the Claude Code session transcripts already on your machine, measures how far apart your API requests actually are, prices your real token traffic under both prompt-cache lifetimes (5 minutes vs 1 hour), and recommends the `promptCacheTtl` and `subagentPromptCacheTtl` settings for you.

Nothing leaves your machine. The script only reads local files and has no dependencies beyond Python 3.9+.

## Why this exists

Claude Code lets you pick the prompt-cache TTL, but the default depends on how you sign in:

| Request bucket | Claude subscription, within plan usage | Usage credits, API key, Bedrock, Vertex |
|---|---|---|
| Main conversation | 1 hour | 5 minutes |
| Subagents, workflows, helpers | 5 minutes | 5 minutes |

The API bills cache writes at 1.25x the base input price for the 5-minute TTL and 2x for the 1-hour TTL. Cache reads cost 0.1x (0.025x on Fable 5.1). So the 1-hour TTL only pays off if it rescues enough cache hits that the 5-minute TTL would have missed.

The intuition "most of my requests are seconds apart, so 5 minutes is fine" turns out to be wrong for typical Claude Code usage, because the two costs are asymmetric:

- The 2x write premium applies only to the **new tokens each turn appends** (a tool result, a reply). Those are small.
- A miss after a 10-minute break re-writes the **entire conversation prefix**, often hundreds of thousands of tokens, at 1.25x instead of reading it at 0.1x.

Whether that trade favors 1h depends on your own pattern of pauses and context sizes. `/usage` in Claude Code shows the live cache state for one session but nothing looks at history. This script does.

## Run it

```bash
git clone git@github.com:AlpineSG/claude-cache-ttl-advisor.git
cd claude-cache-ttl-advisor
python3 cache_ttl_advisor.py
```

Options:

```
--days N                 only consider requests from the last N days
--json                   machine-readable output
--price MODEL=RATE       override base input price (USD per 1M tokens), repeatable
--threshold PCT          minimum savings before recommending a change (default 5)
--min-requests N         requests needed for high confidence (default 300)
--min-warm-events N      5-60 minute gap events needed for high confidence (default 30)
--min-days N             data span needed for high confidence (default 14)
--projects-dir PATH      Claude Code projects directory (default ~/.claude/projects)
```

## What it does

1. Walks `~/.claude/projects/*/*.jsonl` (main conversations) and `~/.claude/projects/*/*/subagents/*.jsonl` (subagent runs). Each file is one chain of requests sharing a cache prefix.
2. For each assistant record it takes the timestamp, model, and `usage` block, which includes `cache_creation_input_tokens`, `cache_read_input_tokens`, and the `cache_creation` breakdown that says which TTL the write used.
3. Buckets the gap to the previous request in the same chain:
   - **hot**, under 5 minutes: both TTLs hit
   - **warm**, 5 to 60 minutes: only the 1-hour TTL hits
   - **cold**, 60 minutes or more, or the first request: both miss
4. Prices the input side of every request under both TTLs. Warm-gap requests are the only place the two diverge. If the traffic was recorded on 1h, the 5m counterfactual turns the cache read into a 1.25x write. If it was recorded on 5m, the 1h counterfactual turns the re-write into a read.
5. Recommends the cheaper TTL per bucket, gated by a confidence check, and prints the exact settings snippet to apply it.

## Sample output

From the author's machine, 102 main sessions and 536 subagent runs over about five weeks. Full output in [examples/sample-output.txt](examples/sample-output.txt).

```
MAIN  (102 chains, 18488 requests)
Data span: 2026-06-29 -> 2026-09-10 (73 days)
Gap to previous request in the same chain:
  < 5 min   (both TTLs hit)                    17226   93.2%
  5-60 min  (only 1h hits)                       905    4.9%
  >= 60 min or first request (both miss)         357    1.9%

  model                    reqs       5m TTL       1h TTL     1h - 5m   warm-gap hits (tokens)
  claude-fable-5           8144    $4,271.00    $3,295.71     -975.28   353 (116.9M)
  claude-fable-5-1         4050    $2,448.63      $902.85    -1545.77   312 (141.5M)
  claude-opus-5            3094      $693.13      $628.36      -64.77   79 (26.2M)
  claude-sonnet-5          3063      $468.19      $351.95     -116.24   141 (63.9M)
  TOTAL                            $7,980.98    $5,260.05    -2720.93

Confidence: high
Recommendation: 1h  (saves 34.1% of input-side cost vs the other TTL)

SUBAGENT  (536 chains, 49620 requests)
  TOTAL                            $5,264.87    $5,121.50     -143.37
Confidence: high
Recommendation: either TTL (difference 2.7%, under the 5% threshold). Keep the default.
```

Only 5% of requests followed a 5-to-60 minute pause, yet those 905 requests carried about 355M tokens of cached prefix. Re-writing that under a 5-minute TTL costs far more than the 2x premium on the small per-turn writes. The break-even here was at roughly 20%: the 1-hour TTL would still win if only one in five of those warm-gap hits had been real.

## Applying the recommendation

Add to `~/.claude/settings.json` (all projects) or `.claude/settings.json` (one project):

```json
{
  "promptCacheTtl": "1h",
  "subagentPromptCacheTtl": "5m"
}
```

Or set `CLAUDE_CODE_PROMPT_CACHE_TTL` and `CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL`. Both require Claude Code 2.1.242 or later. See the [prompt caching docs](https://code.claude.com/docs/en/prompt-caching) for the full precedence rules.

## How much to trust it

- **Dollar figures are API-rate equivalents** of your token traffic. If you are on a Claude subscription, they describe plan usage rather than a bill. If you are on an API key, Bedrock, or Vertex, they approximate real spend at first-party rates (partner pricing differs).
- **Output tokens are excluded** because they cost the same under either TTL.
- **The warm-gap approximation** assumes the prefix would have fully hit had the entry survived. When a turn after a pause also appends a lot of new content, this slightly overstates the 1-hour savings. The sensitivity check above is the reason the recommendation still holds.
- **Lookback is capped.** Claude Code deletes transcripts older than `cleanupPeriodDays` (default 30) in a background sweep. Light users may not accumulate enough 5-to-60 minute pause events in 30 days for a confident answer. The script reports data span and a confidence level, and refuses to recommend a change on low confidence. If you want a better answer later, raise `cleanupPeriodDays` now and re-run in a few weeks.
- **The decision hinges on warm-gap events**, not on request volume. A user who never pauses mid-session will see "either" no matter how much they use Claude Code. A user with big contexts who steps away for 15 minutes a few times a day will see a strong 1h signal quickly.
- **The transcript format is internal** to Claude Code and can change between releases. The parser is defensive and skips anything it doesn't understand, but a format change could silently reduce coverage. Compare the request count it reports against what you expect.
- **Prices are a snapshot** (2026-09-10, in the script header). Override with `--price` if they drift. Models without a known price are listed as skipped.

## Sharing results

`--json` produces a structured document with per-model costs, gap histograms, confidence, and the recommendation, suitable for collecting across a team without sharing transcripts.
