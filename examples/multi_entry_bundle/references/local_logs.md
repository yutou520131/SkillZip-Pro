# Local build log workflow

Use this branch when the raw logs came from a developer's local build.

## Parsing

- Read the build identifier from the `build:` line the local runner prints first.
- Treat any line containing `FAILED` or `Traceback` as a failure finding.
- Record the toolchain version printed in the banner, character for character.
- Read the exit status from the `exit=` value on the final line.

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
