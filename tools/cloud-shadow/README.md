# SoPilot cloud shadow

Only the AI / Creator six-hour public ranking capture runs here. No runtime checkout,
production feed, publisher, ingestion, Feishu, credentials or production schedule is used.
`sopilot_watch.py` is byte-identical to the current local producer, SHA-256
`1c5577a96731a3539285e98e6e689d8f10ee39b7edbf409eff4ddbd8ae123b66`.
The wrapper calls its existing ordinary HTTP function, parser, relevance filter and RSS renderer.
`sopilot.xml` retains the original renderer's wall-clock build date. The separate
`canonical-sopilot.xml` freezes `lastBuildDate` to 1970-01-01 for byte comparison.

```sh
python -m unittest discover -s tools/cloud-shadow/tests -v
python tools/cloud-shadow/sopilot_shadow.py --output tools/cloud-shadow/out/live
python tools/cloud-shadow/sopilot_shadow.py --replay tools/cloud-shadow/out/live/input --output tools/cloud-shadow/out/replay
```

Download the GitHub artifact and point `--replay` at its `live/input` directory to
repeat the same comparison locally. Replay validates each frozen input SHA and
does not make HTTP requests. Compare `canonical-entries.json` and `sopilot.xml`
byte for byte using `canonical-sopilot.xml` for the RSS comparison. Output must be a new child of `tools/cloud-shadow/out/`.
Output is ignored by the existing repository `out/` ignore rule.

`summary.json` records platform, code and input fingerprints, start/end times,
HTTP success/status/failure, each parsed page's rising/hot/pagination counts,
discovered/prefiltered/retained counts and output hashes. Parsing errors fail the run;
valid empty lists explicitly produce a successful empty result. Failures still save
available HTML and metadata and upload the artifact. `input/*.html` contains decoded
UTF-8 text returned by the unchanged producer HTTP function, not compressed wire bytes.
No retry silently substitutes another route. A failed category fails the whole snapshot,
while evidence for the other independently fetched category remains available.

The workflow uses a branch-specific push event and optional dispatch, GitHub-hosted
Ubuntu, read-only repository permission and checkout without persisted credentials.
It has no schedule. New workflows outside the default branch run through push;
dispatch availability and schedules require the default branch workflow. This trial
therefore establishes one real execution, not continuous shadow operation.

The write evidence includes a before/after hash audit of tracked and nonignored
repository files outside output, and the workflow contains no publication step.
Potential credential files are never opened by that audit. The receipt's zero write
counters describe the absent publication/Feishu/dispatch code paths; they are not an
external audit of production systems. The artifact upload is the only remote write.

Fixtures are frozen generated inputs exercising the current production parser and
expected renderer output: duplicates use the longest original body, relevance excludes
an unrelated tweet, full text/code escaping and original tweet identity are preserved.
They are synthetic regression fixtures; the live run artifact supplies the actual
captured HTTP inputs and replay evidence required for migration acceptance.
