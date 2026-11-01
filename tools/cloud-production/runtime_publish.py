"""Three-feed, immutable Git publication. Prepare is the default; never dispatch ingest."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

TARGETS = frozenset({"feeds/sopilot.xml", "feeds/public-mirror/prompthub-blog.xml",
                     "feeds/public-mirror/reddit-indiehackers-ai-revenue.xml"})
ORIGINS = frozenset({"git@github.com:acaiaishizhan/rss-runtime-data.git",
                     "git@github.com:acaiaishizhan/rss-runtime-data",
                     "https://github.com/acaiaishizhan/rss-runtime-data",
                     "https://github.com/acaiaishizhan/rss-runtime-data.git"})


class PublishError(RuntimeError):
    pass


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def git(repo, *args, data=None, env=None, okay=(0,)):
    result = subprocess.run(["git", "-C", str(repo), *args], input=data,
                            capture_output=True, env=env)
    if result.returncode not in okay:
        # Do not emit remote transport diagnostics: configured URLs may contain credentials.
        raise PublishError("git " + args[0] + " failed (exit " + str(result.returncode) + ")")
    return result.stdout


def text(repo, *args, **kwargs):
    return git(repo, *args, **kwargs).decode().strip()


def validate_target(target):
    if target not in TARGETS:
        raise PublishError("target is not in the three-feed allowlist")


def validate_capture(payload, capture, target):
    """Same feed validation as existing publisher; empty needs complete capture evidence."""
    validate_target(target)
    if capture.get("status") != "success" or capture.get("rss_sha256") != digest(payload):
        raise PublishError("capture failed or payload fingerprint mismatched")
    if not payload or len(payload) > 50 * 1024 * 1024:
        raise PublishError("feed is empty or exceeds 50 MiB")
    root = ET.fromstring(payload)
    local = lambda tag: tag.rsplit("}", 1)[-1].lower()
    kind = local(root.tag)
    if kind not in {"rss", "feed", "rdf"}:
        raise PublishError("unexpected feed root")
    item_names = {"item"} if kind in {"rss", "rdf"} else {"entry"}
    count = sum(local(node.tag) in item_names for node in root.iter())
    if count != capture.get("entries"):
        raise PublishError("capture count mismatches RSS")
    if target == "feeds/sopilot.xml":
        pages = capture.get("pages", [])
        if capture.get("discovered") != count + capture.get("prefiltered", -1):
            raise PublishError("capture counts do not close")
        for category in ("AI", "Creator"):
            group = [page for page in pages if page.get("category") == category]
            if not group or any(not p.get("http_success") or not p.get("parse_success") for p in group):
                raise PublishError("incomplete HTTP/parse proof")
            total = group[0]["totalPages"]
            if any(p["totalPages"] != total for p in group) or sorted(p["page"] for p in group) != list(range(1, total + 1)):
                raise PublishError("incomplete pagination proof")
    elif count == 0:
        raise PublishError("mirror empty-feed policy is not yet approved")
    return count


def fingerprint(payload):
    """Existing publisher item signature, also valid for proven empty snapshots."""
    root = ET.fromstring(payload)
    kind = root.tag.rsplit("}", 1)[-1].lower()
    names = {"item"} if kind in {"rss", "rdf"} else {"entry"}
    value = hashlib.sha256()
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1].lower() in names:
            value.update(ET.tostring(element, encoding="utf-8"))
            value.update(b"\0")
    return value.hexdigest()


def verify_origin(repo, test_local=False):
    origin = text(repo, "remote", "get-url", "origin")
    if origin in ORIGINS:
        return
    if test_local and Path(origin).is_absolute() and Path(origin).is_dir():
        if text(origin, "rev-parse", "--is-bare-repository") == "true":
            return
    raise PublishError("origin is not the approved private runtime repository")


def fetch_remote(repo, test_local=False):
    verify_origin(repo, test_local)
    # ls-remote first: unknown transport outcome must never become permission to push.
    head = text(repo, "ls-remote", "origin", "refs/heads/main").split()
    if len(head) != 2:
        raise PublishError("runtime main is absent or remote status unknown")
    git(repo, "fetch", "--quiet", "origin", "+refs/heads/main:refs/remotes/origin/main")
    # fetch may see a later commit than ls-remote; fetched SHA is our new observed tip.
    return text(repo, "rev-parse", "refs/remotes/origin/main")


def entry(repo, revision, target):
    value = text(repo, "ls-tree", revision, "--", target)
    return value.split("\t", 1)[0] if value else None


def changed(repo, base, commit):
    return text(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", base, commit).splitlines()


def create_tree(repo, base, target, payload, output):
    original = entry(repo, base, target)
    if original and not original.startswith("100644 blob "):
        raise PublishError("target mode/type is not a regular nonexecutable feed")
    blob = text(repo, "hash-object", "-w", "--stdin", data=payload)
    index = (output / "index.tmp").resolve()
    index.unlink(missing_ok=True)
    env = dict(os.environ, GIT_INDEX_FILE=str(index), GIT_AUTHOR_NAME="RSS cloud producer",
               GIT_AUTHOR_EMAIL="rss-cloud@users.noreply.github.com",
               GIT_COMMITTER_NAME="RSS cloud producer", GIT_COMMITTER_EMAIL="rss-cloud@users.noreply.github.com")
    try:
        git(repo, "read-tree", base, env=env)
        git(repo, "update-index", "--add", "--cacheinfo", "100644," + blob + "," + target, env=env)
        tree = text(repo, "write-tree", env=env)
    finally:
        index.unlink(missing_ok=True)
    return tree, "100644 blob " + blob, env


def create_commit(repo, base, target, payload, output):
    tree, entry_value, env = create_tree(repo, base, target, payload, output)
    commit = text(repo, "commit-tree", tree, "-p", base, data=("Publish " + target + "\n").encode(), env=env)
    if changed(repo, base, commit) != [target]:
        raise PublishError("publication modified paths outside the target")
    return commit, entry_value


def checkpoint_pending(repo, pending, output):
    # A commit object contains only hashes, author/date and our fixed message.
    # Never export runtime trees, other feed blobs, history, .git or credentials.
    raw = git(repo, "cat-file", "commit", pending["pending_commit"])
    (output / "pending.commit").write_bytes(raw)
    pending["commit_object_sha256"] = digest(raw)


def restore_pending(repo, pending, payload, output):
    raw = (output / "pending.commit").read_bytes()
    if digest(raw) != pending["commit_object_sha256"]:
        raise PublishError("pending commit object fingerprint mismatched")
    tree, target_entry, _ = create_tree(repo, pending["base_sha"], pending["target"], payload, output)
    expected_header = ("tree " + tree + "\nparent " + pending["base_sha"] + "\n").encode()
    if not raw.startswith(expected_header) or target_entry != pending["target_blob"]:
        raise PublishError("pending tree/parent/target cannot be reconstructed exactly")
    commit = text(repo, "hash-object", "-t", "commit", "-w", "--stdin", data=raw)
    if commit != pending["pending_commit"]:
        raise PublishError("restored pending commit SHA mismatched")


def prepare(repo, payload, capture, target, output, test_local=False):
    validate_capture(payload, capture, target)
    output.mkdir(parents=True, exist_ok=True)
    if (output / "pending.json").exists():
        raise PublishError("pending evidence exists; resume it instead of preparing again")
    base = fetch_remote(repo, test_local)
    old = entry(repo, base, target)
    pending = {"schema_version": 1, "target": target, "status": "prepared", "base_sha": base,
               "base_target_entry": old, "rss_sha256": digest(payload), "started_at": now(),
               "capture": capture}
    if old and fingerprint(git(repo, "show", base + ":" + target)) == fingerprint(payload):
        pending.update(status="unchanged", remote_sha=base, target_blob=old,
                       pending_commit=None, changed_paths=[], finished_at=now())
    else:
        commit, blob = create_commit(repo, base, target, payload, output)
        pending.update(pending_commit=commit, target_blob=blob, changed_paths=[target])
        checkpoint_pending(repo, pending, output)
    (output / "candidate.xml").write_bytes(payload)
    save(output / "pending.json", pending)
    return pending


def ancestor(repo, commit, tip):
    result = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", commit, tip], capture_output=True)
    if result.returncode not in (0, 1):
        raise PublishError("cannot determine pending ancestry")
    return result.returncode == 0


def publish(repo, output, *, allow_write=False, test_local=False, push=None):
    pending_path = output / "pending.json"
    pending = json.loads(pending_path.read_text(encoding="utf-8"))
    target = pending["target"]
    validate_target(target)
    payload = (output / "candidate.xml").read_bytes()
    validate_capture(payload, pending["capture"], target)
    receipt = {**pending, "status": "dry_run", "started_at": now(), "push_attempted": False,
               "target_blob_verified": False, "verification_run_id": os.environ.get("GITHUB_RUN_ID", ""),
               "verification_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "")}
    receipt_path = output / "publish-receipt.json"
    if not allow_write:
        save(receipt_path, receipt)
        return receipt
    try:
        tip = fetch_remote(repo, test_local)
        if pending["pending_commit"]:
            restore_pending(repo, pending, payload, output)
        attempts = 0
        while True:
            commit = pending["pending_commit"]
            current_entry = entry(repo, tip, target)
            receipt.update(remote_sha=tip, pending_commit=commit)
            if commit and ancestor(repo, commit, tip):
                if current_entry != pending["target_blob"]:
                    raise PublishError("pending was published but target changed afterward; owner conflict")
                if digest(git(repo, "show", tip + ":" + target)) != pending["rss_sha256"]:
                    raise PublishError("remote target readback mismatched candidate")
                receipt.update(status="confirmed", reconciled=True, target_blob_verified=True, finished_at=now())
                break
            if not commit:
                if current_entry != pending["base_target_entry"]:
                    raise PublishError("target changed after unchanged preparation")
                if fingerprint(git(repo, "show", tip + ":" + target)) != fingerprint(payload):
                    raise PublishError("unchanged remote readback mismatched candidate")
                receipt.update(status="unchanged", target_blob_verified=True, finished_at=now())
                break
            if current_entry != pending["base_target_entry"]:
                raise PublishError("same-target remote change; refusing overwrite")
            if tip != pending["base_sha"]:
                # Pending is proven absent; rebase only our single target onto latest tree.
                commit, blob = create_commit(repo, tip, target, payload, output)
                pending.update(base_sha=tip, base_target_entry=current_entry, pending_commit=commit, target_blob=blob)
                checkpoint_pending(repo, pending, output)
                save(pending_path, pending)
                # This newly prepared commit must be durably uploaded before any push.
                receipt.update(status="reprepare_required", pending_commit=commit,
                               base_sha=tip, target_blob=blob, finished_at=now())
                break
            if attempts:
                receipt.update(status="retry_required", finished_at=now())
                break
            if changed(repo, pending["base_sha"], commit) != [target] or entry(repo, commit, target) != pending["target_blob"]:
                raise PublishError("pending commit boundary/fingerprint invalid")
            receipt.update(status="push_started", push_attempted=True)
            save(receipt_path, receipt)
            attempts += 1
            try:
                (push or (lambda: git(repo, "push", "--porcelain", "origin", commit + ":refs/heads/main")))()
            except (PublishError, subprocess.TimeoutExpired):
                pass  # Success/failure/timeout all require fresh remote proof before any retry.
            tip = fetch_remote(repo, test_local)
    except Exception as error:
        receipt.update(status="blocked", error_type=type(error).__name__, error=str(error), finished_at=now())
        save(receipt_path, receipt)
        raise
    save(receipt_path, receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "publish"])
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--target", choices=sorted(TARGETS))
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--payload", type=Path)
    parser.add_argument("--allow-write", action="store_true")
    args = parser.parse_args()
    if args.action == "prepare":
        result = prepare(args.repo, args.payload.read_bytes(), json.loads(args.capture.read_text(encoding="utf-8")), args.target, args.output)
    else:
        if args.target and json.loads((args.output / "pending.json").read_text(encoding="utf-8"))["target"] != args.target:
            raise PublishError("pending target does not match workflow owner")
        result = publish(args.repo, args.output, allow_write=args.allow_write)
    # Keep receipt in artifact; stdout exposes no private feed contents or transport config.
    print(json.dumps({"status": result["status"], "target": result["target"]}))
    return 0 if result["status"] in {"prepared", "dry_run", "confirmed", "unchanged"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
