# CI log workflow

Use this branch when the raw logs were produced by the CI pipeline.

## Parsing

- Read the pipeline identifier from the first `##[section]` marker in the log.
- Treat any line beginning `##[error]` as a failure finding, in order.
- Ignore retry attempts that later succeeded, but record how many occurred.
- Read the exit status from the final `##[section]Finishing` block.

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
