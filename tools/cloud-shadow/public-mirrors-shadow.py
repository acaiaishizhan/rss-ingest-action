"""Read-only public mirror probe. Writes only its isolated artifact directory."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import platform
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse
from shadow_window import RequestGuard, RequestWindow, WindowClosed

sys.dont_write_bytecode = True


def load_frozen():
    path = Path(__file__).with_name("public-mirrors-frozen.py")
    spec = importlib.util.spec_from_file_location("public_mirrors_frozen", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


frozen = load_frozen()
SOURCES = frozen.SUBSTACK_MIRROR_FEEDS + frozen.REDDIT_MIRROR_FEEDS
SELECTED_SOURCE_NAMES = ("prompthub-blog", "reddit-indiehackers-ai-revenue")


def select_sources(names=None):
    names = tuple(names or SELECTED_SOURCE_NAMES)
    if len(set(names)) != len(names) or any(name not in SELECTED_SOURCE_NAMES for name in names):
        raise ValueError("Only the two approved cloud-shadow mirror sources may be selected")
    return tuple(source for source in SOURCES if source[0] in names)


def sha256(payload):
    return hashlib.sha256(payload).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def transform(name, payload, url):
    return frozen.build_prompthub_blog_feed(payload, url) if name == "prompthub-blog" else payload


def fields(payload):
    root = ET.fromstring(payload)
    rows = []
    for item in root.iter():
        if frozen._local_name(item.tag) not in {"item", "entry"}:
            continue
        values = {}
        for child in item:
            key = frozen._local_name(child.tag)
            value = "".join(child.itertext()).strip()
            if key == "link":
                value = child.get("href") or value
            values.setdefault(key, []).append(value)
        def first(*keys):
            return next((value for key in keys for value in values.get(key, []) if value), "")
        rows.append({"title": first("title"), "link": first("link"),
                     "id": first("guid", "id"), "date": first("pubdate", "published", "updated"),
                     "body": first("encoded", "content", "description", "summary"),
                     "xml_sha256": sha256(ET.tostring(item, encoding="utf-8"))})
    return rows


def field_report(rows):
    return {key: {"present": sum(bool(row[key]) for row in rows),
                  "missing_indices": [index for index, row in enumerate(rows) if not row[key]]}
            for key in ("title", "link", "id", "date", "body")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--source", action="append", choices=SELECTED_SOURCE_NAMES)
    args = parser.parse_args(argv)
    sources = select_sources(args.source)
    window = RequestWindow()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    # Exclusive claim prevents accidental overwrite/retry into an existing evidence set.
    with (out / "run-start.json").open("x", encoding="utf-8") as handle:
        json.dump({"started_at": dt.datetime.now(dt.timezone.utc).isoformat()}, handle)
    allowed_hosts = {urlparse(url).hostname for _, url, _ in sources}
    guard = RequestGuard(window, allowed_hosts)
    active = {"source": None}

    original_urlopen = urllib.request.urlopen
    responses = []

    class ObservedResponse:
        def __init__(self, response, attempt):
            self.response, self.attempt = response, attempt
        def __getattr__(self, name):
            return getattr(self.response, name)
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            self.response.close()
        def read(self, *arguments):
            payload = self.response.read(*arguments)
            target = out / active["source"] / ("response-%02d.bin" % self.attempt["attempt"])
            target.write_bytes(payload)
            self.attempt.update(raw_path=target.relative_to(out).as_posix(), raw_sha256=sha256(payload), bytes=len(payload))
            return payload

    def observed_urlopen(request, *arguments, **kwargs):
        attempt = {"source": active["source"], "attempt": 1 + sum(row["source"] == active["source"] for row in responses),
                   "method": request.get_method(), "url": request.full_url,
                   "timeout": kwargs.get("timeout"), "started_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        responses.append(attempt)
        try:
            response = guard.open(request, *arguments, **kwargs)
            attempt.update(status=response.status, final_url=response.geturl(),
                           content_type=response.headers.get("Content-Type"))
            return ObservedResponse(response, attempt)
        except urllib.error.HTTPError as error:
            payload = error.read(50 * 1024 * 1024 + 1)
            target = out / active["source"] / ("response-%02d-error.bin" % attempt["attempt"])
            target.write_bytes(payload)
            attempt.update(status=error.code, error_type=type(error).__name__, error=str(error),
                           raw_path=target.relative_to(out).as_posix(), raw_sha256=sha256(payload), bytes=len(payload))
            raise
        except Exception as error:
            attempt.update(error_type=type(error).__name__, error=str(error))
            raise

    urllib.request.urlopen = observed_urlopen
    records = []
    started = time.monotonic()
    for name, url, target in sources:
        active["source"] = name
        directory = out / name
        directory.mkdir()
        row = {"name": name, "url": url, "production_target_reference_only": target}
        try:
            window.require_open()
            raw = frozen._default_source_reader(frozen.SourceSpec(name, url, target))
            payload = transform(name, raw, url)
            count = frozen.validate_feed_bytes(payload)
            item_fields = fields(payload)
            (directory / "feed.xml").write_bytes(payload)
            write_json(directory / "fields.json", item_fields)
            row.update(status="success", item_count=count, raw_sha256=sha256(raw),
                       output_sha256=sha256(payload), item_fingerprint=frozen.feed_fingerprint(payload),
                       fields=field_report(item_fields), output_path=name + "/feed.xml")
        except WindowClosed as error:
            row.update(status="skipped", skip_reason=str(error), item_count=None)
        except Exception as error:
            row.update(status="failed", error_type=type(error).__name__, error=str(error))
        row["attempts"] = [attempt for attempt in responses if attempt["source"] == name]
        records.append(row)
        write_json(directory / "result.json", row)
        print(json.dumps({key: row[key] for key in ("name", "status", "item_count", "error") if key in row}), flush=True)
    urllib.request.urlopen = original_urlopen
    code_paths = sorted(Path(__file__).parent.glob("public-mirrors*.py"))
    code_files = {path.name: sha256(path.read_bytes()) for path in code_paths}
    input_value = [{"name": name, "url": url, "target_reference_only": target} for name, url, target in sources]
    write_json(out / "inputs.json", input_value)
    write_json(out / "request-audit.json", guard.events)
    summary = {"mode": "public-http-read-only-shadow", "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
               "elapsed_seconds": round(time.monotonic() - started, 3), "platform": platform.platform(),
               "python": platform.python_version(),
               "runner": {key: os.environ.get(key) for key in ("GITHUB_SHA", "GITHUB_REF", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "RUNNER_OS", "RUNNER_ARCH", "RUNNER_ENVIRONMENT")},
               "publisher_sha256": frozen.PUBLISHER_SHA256, "frozen_node_ast_sha256": frozen.NODE_AST_SHA256,
               "window": window.metadata(),
               "network_requests": sum(row["stage"] == "send" and row["allowed"] for row in guard.events),
               "code_files_sha256": code_files,
               "code_bundle_sha256": sha256(json.dumps(code_files, sort_keys=True).encode()),
               "inputs_sha256": sha256((out / "inputs.json").read_bytes()),
               "side_effect_contract": {"network_methods": sorted({row["method"] for row in guard.events if row["allowed"]}),
                                        "network_hosts": sorted({urlparse(row["url"]).hostname for row in guard.events if row["allowed"]}),
                                        "credential_files_read": False, "production_modules_imported": False,
                                        "feishu_calls": 0, "runtime_writes": 0, "dispatch_calls": 0,
                                        "write_destination": str(out), "artifact_writes_authorized": True},
               "sources": records, "success_count": sum(row["status"] == "success" for row in records),
               "failure_count": sum(row["status"] == "failed" for row in records),
               "skipped_count": sum(row["status"] == "skipped" for row in records)}
    summary["status"] = "failed" if summary["failure_count"] else ("skipped" if summary["skipped_count"] else "success")
    write_json(out / "summary.json", summary)
    return 1 if summary["failure_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
