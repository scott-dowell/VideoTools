---
mode: ask
description: Review recent failed conversions and decide if they are acceptable or need pipeline fixes.
---

You are reviewing failed conversions in this repository.

Goals:
1. Identify recent failures.
2. Verify whether each failure is a real conversion fault or an integrity-check false positive.
3. Check whether intermediate artifacts are available for inspection.
4. Recommend one of: keep as-is, re-run only, or code fix required.

Process:
1. Run this command from the repository root:
   `python VideoConverter/review_failed_converts.py --since-hours 72 --limit 20`
2. For each failed item, inspect the latest run logs in `VideoConverter/logs/<title>_<timestamp>/`.
3. Compare source/container duration vs stream durations and remux output duration.
4. If the output appears valid and intermediate bundles exist under `C:/Temp/vc_working/_failed_intermediates/`, mark as reviewable false positive.
5. If output is genuinely truncated/corrupt, flag as pipeline bug and propose a concrete ffmpeg or validation change.

Output format:
1. Findings first, ordered by severity.
2. For each finding include:
   - File title
   - Evidence paths
   - Decision (false positive / real fault)
   - Action
3. End with a short summary of how many failures were acceptable vs actionable.
