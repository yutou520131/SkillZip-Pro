---
name: release-report-builder
description: Build, validate, and publish a release report from raw build logs, delegating specialist repair to a subskill.
---

# Release report builder

Turn raw build logs into a reviewed release report. The root file decides which
branch applies; every branch reads its own reference file on demand.

## Routing

- When the logs come from a CI pipeline, read [CI logs](references/ci_logs.md).
- When the logs come from a local build, read [local logs](references/local_logs.md).
- When coverage numbers must be included, read [coverage](references/coverage.md).
- When a report was rejected in review and must be repaired, read
  [repair specialist](sub/SKILL.md).
- Run `scripts/validate_report.py` before publishing any report.

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
