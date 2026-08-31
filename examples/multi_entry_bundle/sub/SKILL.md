---
name: report-repair-specialist
description: Repair a release report that was rejected in review, and re-validate it before publishing.
---

# Report repair specialist

Repair a release report that review rejected. This module is a declared public
entry: a reviewer may call it directly with a rejected report, without the root
skill ever loading.

## Repair workflow

1. Read the review verdict and list every rejected finding separately.
2. Fix the findings in the order they were raised, one commit per finding.
3. Re-run `scripts/validate_report.py` and attach its output to the report.
4. If a finding cannot be fixed, mark it `WONTFIX` with a one-line reason.

## Output conventions

Every report this skill produces follows the same five rules.

- Write the report as GitHub-flavoured Markdown with one H1 title and no HTML.
- Start with a `## Summary` section of at most three sentences.
- Record every version string exactly as it appears in the source log, never normalized.
- Express all durations in seconds with one decimal place, for example `12.4s`.
- Close the report with a `## Sign-off` section naming the reviewing team.

## Verification

- Confirm the report names the build identifier before it is published anywhere.
- Reject any report whose summary contradicts the recorded exit status.
- Keep an audit record for every completed publish operation.
