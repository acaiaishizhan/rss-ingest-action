# Public mirrors cloud shadow

This isolated probe freezes the current publisher's ten public mirror definitions,
HTTP reader, PromptHub conversion, XML validation and item fingerprint nodes.
It imports no production configuration, reads no credentials, fetches public HTTPS
GETs, and writes only the requested artifact directory. It does not call Feishu,
publish runtime snapshots, dispatch ingestion or change the production schedule.
The existing local publisher and source-map remain the production owners.

The first GitHub-hosted Linux run, `41332596473`, used commit
`6f9d0de129ffc8fa62724aea42f0950171411d9f` and finished with a real failure:
PromptHub 50 items and Reddit 25 + 25 succeeded; all seven Substack endpoints
returned HTTP 403. Original HTTP response bodies and successful feeds were
preserved in its artifact. No failed source was replaced or proxied.

The local first probe succeeded for nine sources. The added audit host guard
blocked a16z's existing redirect from `a16z.substack.com/feed` to
`www.a16z.news/feed`; this was an audit restriction, not a source HTTP error.
The unmodified current publisher reader followed that public redirect and returned
20 valid items. The explicit redirect allowlist now permits that host. Source URL,
UA, timeout and retry logic remain unchanged. No production source has switched.

Run once into a new evidence directory (a pre-existing run is never overwritten):

```sh
python3 tools/cloud-shadow/public-mirrors-shadow.py --out out/public-mirrors-test
```

Any source failure makes the process and workflow fail after all sources finish.
The workflow always uploads its isolated artifact, including raw response bytes,
per-attempt errors, GET audit, SHA fingerprints and per-item field completeness.
PromptHub intentionally has no body field under the existing converter; its
missing body counts are reported without changing the production format.

Replay downloaded cloud or local raw responses against the current publisher:

```sh
python3 tools/cloud-shadow/public-mirrors-replay.py \
  --publisher /path/to/rss-ingest-local/tools/local_feed_publisher.py \
  --evidence /path/to/downloaded-artifact \
  --live-baseline /path/to/local-probe-artifact \
  --out out/public-mirrors-replay.json
```

Replay evaluates only selected publisher AST nodes, without importing production
configuration. It checks AST identity, exact output SHA, every item field and item
fingerprints on the same response bytes, plus seven synthetic valid/error cases.
Sources without HTTP 200 bodies are explicitly skipped, with their original
failure retained. The optional live comparison separately matches items by ID or
link and reports field differences and time boundaries; it is not a frozen
response test. Zero production write counts describe this closed code path and
GET allowlist; they are not a remote Feishu audit.
