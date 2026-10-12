# `standards/`

Versioned, citable rubric and reference files. Each names its source, its version, and the
date it was retrieved; reports name the version they scored against.

This exists because **adopting a revised standard should be a data change, not a code change.**
That is not theoretical caution: NIH replaced the DMS Plan format in May 2026, mid-design.

## `afr/` — annualized failure rates

`afr-defaults-v1.json` feeds the durability model (docs/PLAN.md §8.2).

**Read this before trusting a number out of it.** The built-in rates are *generic defaults by
media class and age band*. They are not vendor-published figures, they are not attributed to
any fleet study, and they are deliberately labeled `"kind": "generic-defaults"` in the file
itself. Published per-model annualized failure rates vary by an order of magnitude between
drive families, so a plausible-looking number carrying someone else's citation would be worse
than an honest default — it would be a false claim that a user could not audit.

To use real data, populate `per_model` from a published source — Backblaze's quarterly Drive
Stats are the usual one, and the file records the URL. Each entry carries its own `source`,
`url` and `retrieved_at`, so a report can say where its numbers came from.

Anything computed from the defaults alone is reported with `confidence = "estimated"`. Anything
computed with SMART counters from the drive is `"measured"`. A drive that reports neither is
`"unknown"`, and the tool says so rather than printing a number it cannot support.
