"""Production preparation capture using the already frozen, unchanged SoPilot collector."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
from urllib.parse import parse_qs, urlparse

from runtime_publish import digest, now, save

HERE = Path(__file__).resolve().parent
PRODUCER = HERE.parent / "cloud-shadow" / "sopilot_watch.py"
EXPECTED_SHA = "1c5577a96731a3539285e98e6e689d8f10ee39b7edbf409eff4ddbd8ae123b66"


def capture(output):
    output.mkdir(parents=True, exist_ok=True)
    if (output / "capture.json").exists():
        raise ValueError("capture already exists; resume publication rather than recapture")
    summary = {"status": "failed", "started_at": now(), "pages": [], "errors": [],
               "source_sha256": digest(PRODUCER.read_bytes()), "entries": None,
               "discovered": None, "prefiltered": None, "rss_sha256": None}
    lock = threading.Lock()
    module = None
    original_urlopen = None
    local = threading.local()
    try:
        if summary["source_sha256"] != EXPECTED_SHA:
            raise ValueError("collector fingerprint changed")
        spec = importlib.util.spec_from_file_location("production_sopilot_capture", PRODUCER)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        original_urlopen = module.urlopen

        def audited_urlopen(*args, **kwargs):
            response = original_urlopen(*args, **kwargs)
            local.http_status = getattr(response, "status", None)
            local.final_url = response.geturl()
            return response

        module.urlopen = audited_urlopen

        def fetch(url):
            query = parse_qs(urlparse(url).query)
            category, page = query["category"][0], int(query.get("page", ["1"])[0])
            row = {"category": category, "page": page, "url": url, "started_at": now(),
                   "http_success": False, "parse_success": False}
            local.http_status = None
            try:
                raw = module._fetch_text(url)
                row.update(http_success=True, http_status=local.http_status, final_url=local.final_url,
                           sha256=digest(raw.encode("utf-8")))
                props = module.parse_rank_page(raw, category, page)
                row.update(parse_success=True, totalPages=props["totalPages"],
                           rising=len(props["risingTweets"]), hot=len(props["hotTweets"]))
                return raw
            except Exception as error:
                row.update(error_type=type(error).__name__, error=str(error))
                raise
            finally:
                row["finished_at"] = now()
                with lock:
                    summary["pages"].append(row)

        value = module.fetch_sopilot(fetch)
        payload = module.render_rss(value)
        (output / "sopilot.xml").write_bytes(payload)
        summary.update(status="success", entries=len(value.entries), discovered=value.discovered,
                       prefiltered=value.prefiltered, rss_sha256=digest(payload),
                       empty_reason=("all lists empty" if not value.discovered else "all discovered tweets filtered") if not value.entries else None)
    except Exception as error:
        summary["errors"].append({"type": type(error).__name__, "message": str(error)})
    finally:
        if module is not None and original_urlopen is not None:
            module.urlopen = original_urlopen
    summary["pages"].sort(key=lambda row: (row["category"], row["page"]))
    summary["finished_at"] = now()
    save(output / "capture.json", summary)
    return summary


def receipt(output):
    capture_path = output / "capture.json"
    # Resumed pending preserves original capture evidence, never manufactures a new capture.
    pending_path = output / "pending" / "pending.json"
    pending = json.loads(pending_path.read_text(encoding="utf-8")) if pending_path.exists() else {}
    capture_value = json.loads(capture_path.read_text(encoding="utf-8")) if capture_path.exists() else pending.get("capture", {})
    publish_path = output / "pending" / "publish-receipt.json"
    published = json.loads(publish_path.read_text(encoding="utf-8")) if publish_path.exists() else {"status": "not_started"}
    entry = published.get("target_blob", "")
    # Public health schema uses a blob SHA; the full tree entry stays in pending evidence.
    published["target_blob"] = entry.split()[-1] if entry else None
    verified_here = (published.get("verification_run_id") == os.environ.get("GITHUB_RUN_ID", "") and
                     published.get("verification_run_attempt") == os.environ.get("GITHUB_RUN_ATTEMPT", ""))
    if not verified_here and published.get("status") != "not_started":
        published.update(status="not_verified_this_run", target_blob_verified=False)
    published.pop("capture", None)
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    result = {"schema_version": 1, "owner": "cloud", "mode": "production", "source": "sopilot",
              "workflow": "sopilot-producer.yml", "run": {"repository": repository, "id": run_id,
              "attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""), "code_sha": os.environ.get("GITHUB_SHA", ""),
              "url": "https://github.com/" + repository + "/actions/runs/" + run_id},
              "capture": capture_value, "publish": published}
    barrier = output / "barrier.json"
    if barrier.exists():
        result["barrier"] = json.loads(barrier.read_text(encoding="utf-8"))
    save(output / "receipt.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["capture", "receipt"])
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    value = capture(args.output) if args.action == "capture" else receipt(args.output)
    return 1 if args.action == "capture" and value["status"] != "success" else 0


if __name__ == "__main__":
    raise SystemExit(main())
