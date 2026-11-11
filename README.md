Releases mirror of: [https://git.fossbirds.dev/NULL/mem-diffs](https://git.fossbirds.dev/NULL/mem-diffs)

MemDiffs makes changes to persistent model context (Memory, Skills, Self-improvement review) viewable as diffs each time they are written. 
This is done by using the middleware SDK to capture persistent context files before the existing tools write them, and then add the diffs to those tool results for display. 
The added fields are stripped from requests to the conversational model to avoid polluting context, though compaction still sees them.

These diffs are in sqlite linked per-session via state.db (read-only), and each session's associated diffs are deleted when it is.

TBD: Implement a way to click-revert individual diffs.

Made for the Hermes Agent Plugin Hackathon
For submission by @daji_dev on X/Twitter

AI usage disclaimer: Obviously, it's a hermes plugin.
