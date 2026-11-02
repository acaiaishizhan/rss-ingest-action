"""Refresh approved public feeds in this runner's runtime checkout only."""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path


SHADOW = Path(__file__).resolve().parent / "cloud-shadow"
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
    minute = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc).minute
    results = []
    for name, relative in TARGETS.items():
        if name not in selected:
            continue
        row = {"source": name, "status": "skipped"}
        if name == "sopilot" and minute >= 15:
            results.append(row)
            continue
        temporary = None
        try:
            mirrors = load_producer("public-mirrors-frozen.py")
            if name == "sopilot":
                producer = load_producer("sopilot_watch.py")
                capture = producer.fetch_sopilot(sopilot_reader or producer._fetch_text)
                payload = producer.render_rss(capture)
            else:
                source = next(source for source in mirrors.SUBSTACK_MIRROR_FEEDS + mirrors.REDDIT_MIRROR_FEEDS if source[0] == name)
                raw = (mirror_reader or mirrors._default_source_reader)(mirrors.SourceSpec(*source))
                payload = mirrors.build_prompthub_blog_feed(raw, source[1]) if name == "prompthub-blog" else raw
            count = mirrors.validate_feed_bytes(payload, allow_empty=name == "sopilot")
            target = (runtime / relative).resolve()
            if not target.is_relative_to(runtime):
                raise ValueError("Target outside runtime checkout")
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".inrun-", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
            os.replace(temporary, target)
            row.update(status="success", item_count=count)
        except Exception as error:
            # No exception text, URLs, runtime paths, source-map or feed bodies in public logs.
            row.update(status="failed", error_type=type(error).__name__)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        results.append(row)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    for row in refresh(args.runtime_dir, os.environ.get("INRUN_FEEDS", "")):
        print(json.dumps(row), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
