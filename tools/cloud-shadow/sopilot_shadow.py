"""Read-only SoPilot capture and offline reproduction; no production imports."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import re
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import sopilot_watch as producer

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
SOURCE_SHA256 = "1c5577a96731a3539285e98e6e689d8f10ee39b7edbf409eff4ddbd8ae123b66"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save_json(path, value):
    # Explicit UTF-8 bytes keep frozen fingerprints identical on Windows/Linux.
    path.write_bytes((json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def canonical_entries(capture):
    return [{**entry, "published": entry["published"].isoformat()} for entry in capture.entries]


def canonical_rss(capture):
    # The original renderer is unchanged; only its wall-clock build date is frozen.
    return re.sub(rb"<lastBuildDate>.*?</lastBuildDate>",
                  b"<lastBuildDate>Thu, 01 Jan 1970 00:00:00 +0000</lastBuildDate>",
                  producer.render_rss(capture), count=1)


def tree_snapshot(root):
    """Content hashes outside output/.git, including untracked files, without reading credentials."""
    paths = subprocess.check_output(["git", "ls-files", "-c", "-o", "--exclude-standard"], cwd=root, text=True).splitlines()
    result = {}
    for relative in paths:
        path = root / relative
        if relative.startswith("tools/cloud-shadow/out/") or not path.is_file():
            continue
        # Do not open potential credentials even if accidentally tracked.
        if path.name.startswith(".env") or path.name in ("auth.json", "rss-ingest-local.env", "rss-ingest.env") or path.suffix in (".key", ".pem"):
            continue
        result[relative] = sha(path.read_bytes())
    return result


def run(output: Path, replay: Path | None = None):
    output = output.resolve()
    if output == OUT or not output.is_relative_to(OUT):
        raise ValueError("Output must be a child directory of tools/cloud-shadow/out/")
    if output.exists():
        raise ValueError("Refusing to overwrite existing shadow output")
    root = HERE.parent.parent
    before = tree_snapshot(root)
    output.mkdir(parents=True)
    inputs = output / "input"
    inputs.mkdir()
    source_sha = sha((HERE / "sopilot_watch.py").read_bytes())
    summary = {
        "schema_version": 1, "mode": "replay" if replay else "live", "started_at": now(),
        "source_url": producer.SOURCE_URL, "categories": list(producer.CATEGORIES),
        "source_sha256": source_sha, "source_sha256_expected": SOURCE_SHA256,
        "wrapper_sha256": sha(Path(__file__).read_bytes()),
        "git_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
        "runner": {"system": platform.system(), "platform": platform.platform(), "python": platform.python_version(),
                   "runner_os": os.environ.get("RUNNER_OS"), "run_id": os.environ.get("GITHUB_RUN_ID"),
                   "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"), "runner_name": os.environ.get("RUNNER_NAME")},
        "pages": [], "errors": [], "status": "failed", "discovered": None, "prefiltered": None, "entries": None,
        "writes": {"production_feed": 0, "runtime": 0, "feishu": 0, "publisher": 0, "ingest_dispatch": 0,
                   "allowed_output_directory": str(output.relative_to(root)),
                   "mechanism": "stdlib public HTTP GET + isolated file writes; no credentials, production imports, publication or dispatch code"},
    }
    lock = threading.Lock()
    local = threading.local()
    original_urlopen = producer.urlopen

    def audited_urlopen(*args, **kwargs):
        request = args[0]
        if request.get_method() != "GET" or urlparse(request.full_url).netloc != "sopilot.net":
            raise ValueError("Shadow network boundary only allows public SoPilot GET")
        response = original_urlopen(*args, **kwargs)
        local.status = getattr(response, "status", None)
        local.final_url = response.geturl()
        return response

    replay_pages = {}
    try:
        if source_sha != SOURCE_SHA256:
            raise ValueError("Vendored production source fingerprint mismatch")
        if replay:
            manifest = json.loads((replay / "manifest.json").read_text(encoding="utf-8"))
            replay_pages = {page["url"]: page for page in manifest["pages"]}
        producer.urlopen = audited_urlopen

        def fetch(url):
            query = parse_qs(urlparse(url).query)
            category, page = query["category"][0], int(query.get("page", ["1"])[0])
            name = f"{category}-{page}.html"
            record = {"url": url, "category": category, "page": page, "file": name,
                      "request_method": None if replay else "GET",
                      "http_success": None if replay else False, "parse_success": False, "started_at": now()}
            started = time.monotonic()
            local.status = None
            try:
                if replay:
                    frozen = replay_pages[url]
                    candidate = (replay / frozen["file"]).resolve()
                    if not candidate.is_relative_to(replay.resolve()):
                        raise ValueError("Input file escapes replay directory")
                    raw_bytes = candidate.read_bytes()
                    if sha(raw_bytes) != frozen["sha256"]:
                        raise ValueError("Frozen input SHA mismatch")
                    raw = raw_bytes.decode("utf-8")
                    record["frozen_http_status"] = frozen.get("http_status")
                else:
                    raw = producer._fetch_text(url)
                    record.update(http_success=True, http_status=local.status, final_url=local.final_url)
                    raw_bytes = raw.encode("utf-8")
                (inputs / name).write_bytes(raw_bytes)
                record.update(sha256=sha(raw_bytes), decoded_utf8_bytes=len(raw_bytes))
                props = producer.parse_rank_page(raw, category, page)
                record.update(parse_success=True, totalPages=props["totalPages"],
                              rising=len(props["risingTweets"]), hot=len(props["hotTweets"]))
                return raw
            except Exception as exc:
                record.update(error_type=type(exc).__name__, error=str(exc), http_status=getattr(exc, "code", record.get("http_status")))
                raise
            finally:
                record.update(ended_at=now(), elapsed_seconds=round(time.monotonic() - started, 3))
                with lock:
                    summary["pages"].append(record)

        capture = producer.fetch_sopilot(fetch)
        entries = canonical_entries(capture)
        payload = producer.render_rss(capture)
        canonical_payload = canonical_rss(capture)
        save_json(output / "canonical-entries.json", entries)
        (output / "sopilot.xml").write_bytes(payload)
        (output / "canonical-sopilot.xml").write_bytes(canonical_payload)
        summary.update(status="success", discovered=capture.discovered, prefiltered=capture.prefiltered,
                       entries=len(entries), rss_sha256=sha(payload), canonical_rss_sha256=sha(canonical_payload),
                       canonical_entries_sha256=sha((output / "canonical-entries.json").read_bytes()),
                       empty_reason=("all parsed ranking lists are empty" if capture.discovered == 0 else "all discovered tweets failed unchanged relevance filter") if not entries else None)
    except Exception as exc:
        summary["errors"].append({"type": type(exc).__name__, "message": str(exc)})
    finally:
        producer.urlopen = original_urlopen
        summary["pages"].sort(key=lambda p: (p["category"], p["page"]))
        manifest = {"schema_version": 1, "encoding": "decoded UTF-8 returned by production _fetch_text", "pages": summary["pages"]}
        save_json(inputs / "manifest.json", manifest)
        summary["input_manifest_sha256"] = sha((inputs / "manifest.json").read_bytes())
        after = tree_snapshot(root)
        summary["writes"]["repository_files_changed_outside_output"] = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
        if summary["writes"]["repository_files_changed_outside_output"]:
            summary["status"] = "failed"
            summary["errors"].append({"type": "WriteBoundaryViolation", "message": "Files outside isolated output changed"})
        summary["ended_at"] = now()
        save_json(output / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["status"] == "success" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--replay", type=Path)
    args = parser.parse_args()
    return run(args.output, args.replay)


if __name__ == "__main__":
    raise SystemExit(main())
