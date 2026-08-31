# Coverage section

Use this branch only when the release report must include coverage numbers.

## Parsing

- Read the total line-coverage percentage from the coverage summary table.
- Report per-package coverage only for packages below the release threshold.
- State the coverage tool and its version on the same line as the total.
- If no coverage artifact exists, write `coverage: not measured` and stop.

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
