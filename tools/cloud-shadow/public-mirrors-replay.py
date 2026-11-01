"""Compare current publisher and frozen cloud logic on exactly the same bytes.

Never imports the publisher or evaluates its configuration; only selected AST nodes.
"""
from __future__ import annotations
import argparse
import ast
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True


def load_shadow():
    spec = importlib.util.spec_from_file_location("public_mirrors_shadow", Path(__file__).with_name("public-mirrors-shadow.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publisher", required=True, type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--live-baseline", type=Path, help="Separate live response evidence; differences are time/exit comparisons")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    shadow = load_shadow()
    frozen = shadow.frozen
    publisher_bytes = args.publisher.read_bytes()
    tree = ast.parse(publisher_bytes.decode("utf-8-sig"))
    selected, node_results = [], {}
    for node in tree.body:
        name = getattr(node, "name", "")
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
        if name in frozen.NODE_AST_SHA256:
            digest = shadow.sha256(ast.dump(node, include_attributes=False).encode())
            node_results[name] = {"current_sha256": digest, "frozen_sha256": frozen.NODE_AST_SHA256[name],
                                  "identical": digest == frozen.NODE_AST_SHA256[name]}
            selected.append(node)
    current = dict(frozen.__dict__)
    exec(compile(ast.Module(body=selected, type_ignores=[]), "current-publisher-selected-nodes", "exec"), current)
    cases = []

    def evaluate(namespace, name, payload, url):
        try:
            output = namespace["build_prompthub_blog_feed"](payload, url) if name == "prompthub-blog" else payload
            count = namespace["validate_feed_bytes"](output)
            return {"status": "success", "output_sha256": shadow.sha256(output), "item_count": count,
                    "fingerprint": namespace["feed_fingerprint"](output), "fields": shadow.fields(output)}
        except Exception as error:
            return {"status": "failed", "error_type": type(error).__name__, "error": str(error)}

    def compare(case, name, payload, url):
        old = evaluate(current, name, payload, url)
        new = evaluate(frozen.__dict__, name, payload, url)
        cases.append({"case": case, "source": name, "raw_sha256": shadow.sha256(payload),
                      "all_output_fields_equal": old == new, "current": old, "frozen": new})

    fixtures = [
        ("rss-unicode", "rss", '<rss><channel><item><title>中文 &amp; AI</title><link>https://example.org/1</link><guid>1</guid><pubDate>Mon, 02 Nov 2026 00:00:00 +0000</pubDate><description>body</description></item></channel></rss>'),
        ("atom", "atom", '<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>abc</id><title>Title</title><link href="https://example.org/a"/><updated>2026-11-02T00:00:00Z</updated><content>content</content></entry></feed>'),
        ("invalid-xml", "rss", '<rss>'),
        ("non-feed", "rss", '<html/>'),
        ("empty-feed", "rss", '<rss><channel/></rss>'),
        ("prompthub-dates-dedupe-entities", "prompthub-blog", '<a class="blog-post-preview-content" href="/blog/a"><h2 class="blog-title-list-page">AI &amp; You</h2><p class="blog-date">October 31, 2026</p></a><a class="blog-post-preview-content" href="/blog/a"><h2 class="blog-title-list-page">duplicate</h2></a><a class="blog-post-preview-content" href="/blog/b"><h2 class="blog-title-list-page">No date</h2><p class="blog-date">invalid</p></a>'),
        ("prompthub-no-posts", "prompthub-blog", '<html/>'),
    ]
    for case, name, value in fixtures:
        compare(case, name, value.encode(), "https://www.prompthub.us/blog")
    skipped = []
    if args.evidence:
        summary = json.loads((args.evidence / "summary.json").read_text(encoding="utf-8"))
        for source in summary["sources"]:
            successes = [row for row in source["attempts"] if row.get("status") == 200 and row.get("raw_path")]
            if not successes:
                skipped.append({"name": source["name"], "reason": source.get("error", "no HTTP 200 response"),
                                "status": source["status"]})
                continue
            response = successes[-1]
            payload = (args.evidence / response["raw_path"]).read_bytes()
            assert shadow.sha256(payload) == response["raw_sha256"], "raw response evidence hash mismatch"
            compare("recorded-response", source["name"], payload, source["url"])
    report = {"publisher_sha256": shadow.sha256(publisher_bytes), "frozen_publisher_sha256": frozen.PUBLISHER_SHA256,
              "all_selected_ast_nodes_present": len(node_results) == len(frozen.NODE_AST_SHA256),
              "nodes": node_results, "cases": cases, "skipped": skipped,
              "comparison": "same frozen response bytes; exact output SHA, item fingerprint and every item field compared",
              "real_time_note": "This is response replay equivalence. Independent live fetches can differ over time."}
    report["passed"] = (report["all_selected_ast_nodes_present"] and all(row["identical"] for row in node_results.values())
                        and all(row["all_output_fields_equal"] for row in cases))
    if args.live_baseline:
        if not args.evidence:
            parser.error("--live-baseline requires --evidence")
        baseline = json.loads((args.live_baseline / "summary.json").read_text(encoding="utf-8"))
        by_name = {row["name"]: row for row in baseline["sources"]}
        comparisons = []
        for remote in summary["sources"]:
            local = by_name[remote["name"]]
            item = {"name": remote["name"], "baseline_status": local["status"], "evidence_status": remote["status"],
                    "baseline_error": local.get("error"), "evidence_error": remote.get("error"),
                    "baseline_item_count": local.get("item_count"), "evidence_item_count": remote.get("item_count")}
            if local["status"] == remote["status"] == "success":
                local_rows = json.loads((args.live_baseline / local["name"] / "fields.json").read_text(encoding="utf-8"))
                remote_rows = json.loads((args.evidence / remote["name"] / "fields.json").read_text(encoding="utf-8"))
                left = {row["id"] or row["link"]: row for row in local_rows}
                right = {row["id"] or row["link"]: row for row in remote_rows}
                shared = left.keys() & right.keys()
                item.update(raw_sha256_equal=local["raw_sha256"] == remote["raw_sha256"],
                            output_sha256_equal=local["output_sha256"] == remote["output_sha256"],
                            item_fingerprint_equal=local["item_fingerprint"] == remote["item_fingerprint"],
                            shared_item_keys=len(shared), baseline_only_keys=sorted(left.keys() - right.keys()),
                            evidence_only_keys=sorted(right.keys() - left.keys()),
                            field_differences={key: [identity for identity in sorted(shared) if left[identity][key] != right[identity][key]]
                                               for key in ("title", "link", "id", "date", "body", "xml_sha256")})
            comparisons.append(item)
        report["independent_live_comparison"] = {"baseline_finished_at": baseline["finished_at"],
                                                  "evidence_finished_at": summary["finished_at"], "sources": comparisons,
                                                  "note": "Not a same-response equivalence test; live endpoints and exits may differ."}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    shadow.write_json(args.out, report)
    print(json.dumps({"passed": report["passed"], "compared_cases": len(cases), "skipped": skipped}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
