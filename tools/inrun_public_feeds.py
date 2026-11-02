"""Refresh approved public feeds in this runner's runtime checkout only."""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import queue
import sys
import tempfile
import threading
import time
from pathlib import Path


SHADOW = Path(__file__).resolve().parent / "cloud-shadow"
INRUN_DEADLINE_SECONDS = 60
TARGETS = {
    "prompthub-blog": "feeds/public-mirror/prompthub-blog.xml",
    "reddit-indiehackers-ai-revenue": "feeds/public-mirror/reddit-indiehackers-ai-revenue.xml",
    "sopilot": "feeds/sopilot.xml",
}


def load_producer(filename):
    name = "inrun_" + filename.replace("-", "_").removesuffix(".py")
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, SHADOW / filename)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def refresh(runtime_dir, enabled, *, now=None, mirror_reader=None, sopilot_reader=None):
    selected = {name.strip() for name in enabled.split(",")} & TARGETS.keys()
    # Empty configuration must not read the checkout, import producers or send HTTP.
    if not selected:
        return []
    runtime = Path(runtime_dir).resolve()
    if not (runtime / "source-map.json").is_file():
        return []
    try:
        seconds = float(os.environ.get("INRUN_DEADLINE_SECONDS", INRUN_DEADLINE_SECONDS))
    except ValueError:
        seconds = INRUN_DEADLINE_SECONDS
    deadline = time.monotonic() + max(0, seconds)
    minute = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc).minute
    results = {name: {"source": name, "status": "timeout"} for name in TARGETS if name in selected}
    if "sopilot" in selected and minute >= 15:
        results["sopilot"]["status"] = "skipped"
    active = {name for name in results if results[name]["status"] != "skipped"}
    # Only these two fixed parent directories; no source-map or feed directory scan by ingest.
    for parent in {runtime / Path(relative).parent for relative in TARGETS.values()}:
        for temporary in parent.glob(".inrun-*"):
            if temporary.is_file():
                temporary.unlink(missing_ok=True)
    # Initialize modules on this thread before any worker can observe sys.modules.
    try:
        mirrors = load_producer("public-mirrors-frozen.py") if active else None
        producer = load_producer("sopilot_watch.py") if "sopilot" in active else None
    except Exception as error:
        for name in active:
            results[name].update(status="failed", error_type=type(error).__name__)
        return list(results.values())
    completed = queue.Queue()

    def collect(name):
        try:
            if name == "sopilot":
                capture = producer.fetch_sopilot(sopilot_reader or producer._fetch_text)
                payload = producer.render_rss(capture)
            else:
                source = next(source for source in mirrors.SUBSTACK_MIRROR_FEEDS + mirrors.REDDIT_MIRROR_FEEDS if source[0] == name)
                raw = (mirror_reader or mirrors._default_source_reader)(mirrors.SourceSpec(*source))
                payload = mirrors.build_prompthub_blog_feed(raw, source[1]) if name == "prompthub-blog" else raw
            count = mirrors.validate_feed_bytes(payload, allow_empty=name == "sopilot")
            completed.put((name, payload, count, None))
        except Exception as error:
            completed.put((name, None, None, type(error).__name__))

    for name in active:
        threading.Thread(target=collect, args=(name,), daemon=True).start()
    pending = set(active)
    while pending and time.monotonic() < deadline:
        try:
            name, payload, count, error_type = completed.get(timeout=max(0, deadline - time.monotonic()))
        except queue.Empty:
            break
        if time.monotonic() >= deadline:
            break
        pending.remove(name)
        row = results[name]
        if error_type:
            row.update(status="failed", error_type=error_type)
            continue
        temporary = None
        try:
            target = (runtime / TARGETS[name]).resolve()
            if not target.is_relative_to(runtime):
                raise ValueError("Target outside runtime checkout")
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".inrun-", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
            # Workers only return candidates. No thread can replace a late result.
            if time.monotonic() < deadline:
                os.replace(temporary, target)
                row.update(status="success", item_count=count)
        except Exception as error:
            # No exception text, URLs, runtime paths, source-map or feed bodies in public logs.
            row.update(status="failed", error_type=type(error).__name__)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return list(results.values())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    for row in refresh(args.runtime_dir, os.environ.get("INRUN_FEEDS", "")):
        print(json.dumps(row), flush=True)
    return 0


if __name__ == "__main__":
    # Skip executor/thread shutdown waits after flushed summaries, including nested SoPilot workers.
    os._exit(main())
