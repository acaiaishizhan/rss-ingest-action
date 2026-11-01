"""Compare actual local producer against frozen cloud HTTP inputs without network."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

from sopilot_shadow import OUT, canonical_entries, now, save_json, sha


def compare(inputs: Path, cloud: Path, production_source: Path, output: Path):
    output = output.resolve()
    if not output.is_relative_to(OUT) or output == OUT or output.exists():
        raise ValueError("Output must be a new child of tools/cloud-shadow/out/")
    output.mkdir(parents=True)
    source_sha = sha(production_source.read_bytes())
    result = {"started_at": now(), "status": "failed", "production_source_sha256": source_sha,
              "network_calls": 0, "errors": [], "input": str(inputs), "cloud": str(cloud)}
    try:
        module_name = "sopilot_actual_local_producer"
        spec = importlib.util.spec_from_file_location(module_name, production_source)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        def forbidden_network(*args, **kwargs):
            result["network_calls"] += 1
            raise AssertionError("Network forbidden in frozen production comparison")
        module.urlopen = forbidden_network
        manifest = json.loads((inputs / "manifest.json").read_text(encoding="utf-8"))
        pages = {item["url"]: item for item in manifest["pages"]}
        read_urls = []

        def get(url):
            item = pages[url]
            path = (inputs / item["file"]).resolve()
            if not path.is_relative_to(inputs.resolve()):
                raise ValueError("Input path escapes frozen directory")
            raw = path.read_bytes()
            if sha(raw) != item["sha256"]:
                raise ValueError("Frozen input SHA mismatch")
            read_urls.append(url)
            return raw.decode("utf-8")

        capture = module.fetch_sopilot(get)
        local_entries = canonical_entries(capture)
        save_json(output / "canonical-entries.json", local_entries)
        local_rss = re.sub(rb"<lastBuildDate>.*?</lastBuildDate>",
                           b"<lastBuildDate>Thu, 01 Jan 1970 00:00:00 +0000</lastBuildDate>",
                           module.render_rss(capture), count=1)
        (output / "canonical-sopilot.xml").write_bytes(local_rss)
        cloud_entries_bytes = (cloud / "canonical-entries.json").read_bytes()
        cloud_entries = json.loads(cloud_entries_bytes)
        cloud_rss = (cloud / "canonical-sopilot.xml").read_bytes()
        cloud_summary = json.loads((cloud / "summary.json").read_text(encoding="utf-8"))
        fields = ("id", "link", "title", "published", "content")
        field_matches = {field: [item.get(field) for item in local_entries] == [item.get(field) for item in cloud_entries] for field in fields}
        local_counts = {"discovered": capture.discovered, "prefiltered": capture.prefiltered, "entries": len(capture.entries)}
        cloud_counts = {key: cloud_summary[key] for key in local_counts}
        local_entries_bytes = (output / "canonical-entries.json").read_bytes()
        checks = {"source_sha256_match": source_sha == cloud_summary["source_sha256"],
                  "canonical_entries_bytes_match": local_entries_bytes == cloud_entries_bytes,
                  "canonical_rss_bytes_match": local_rss == cloud_rss,
                  "counts_match": local_counts == cloud_counts,
                  "pages_match": list(capture.pages) == [p["url"] for p in sorted(manifest["pages"], key=lambda p: (("AI", "Creator").index(p["category"]), p["page"]))],
                  "all_inputs_used": sorted(read_urls) == sorted(pages), "no_network_calls": result["network_calls"] == 0}
        result.update(checks=checks, field_matches=field_matches, local_counts=local_counts, cloud_counts=cloud_counts,
                      canonical_entries_sha256={"local": sha(local_entries_bytes), "cloud": sha(cloud_entries_bytes)},
                      canonical_rss_sha256={"local": sha(local_rss), "cloud": sha(cloud_rss)},
                      cloud_source_sha256=cloud_summary["source_sha256"], cloud_git_sha=cloud_summary["git_sha"],
                      cloud_run_id=cloud_summary["runner"]["run_id"], frozen_input_manifest_sha256=sha((inputs / "manifest.json").read_bytes()),
                      status="success" if all(checks.values()) and all(field_matches.values()) else "failed")
    except Exception as exc:
        result["errors"].append({"type": type(exc).__name__, "message": str(exc)})
    result["ended_at"] = now()
    save_json(output / "comparison.json", result)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "success" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--cloud", required=True, type=Path)
    parser.add_argument("--production-source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    return compare(args.input, args.cloud, args.production_source, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
