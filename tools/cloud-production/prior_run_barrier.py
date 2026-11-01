"""Read Actions receipts before a fresh capture; unknown prior push requires explicit resume."""
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import subprocess
import zipfile

from runtime_publish import PublishError, now, save


def api(path):
    # gh owns its normal authentication. No token value is read, printed or copied here.
    result = subprocess.run(["gh", "api", path], capture_output=True)
    if result.returncode:
        raise PublishError("Actions metadata/artifact read failed")
    return result.stdout


def check(repository, run_id):
    if repository != "acaiaishizhan/rss-ingest-action" or not run_id.isdigit():
        raise PublishError("unexpected repository/run identity")
    runs = json.loads(api("repos/" + repository + "/actions/workflows/sopilot-producer.yml/runs?branch=main&per_page=100"))["workflow_runs"]
    prior = [run for run in runs if int(run["id"]) < int(run_id)]
    prior.sort(key=lambda run: int(run["id"]), reverse=True)
    if not prior:
        return {"status": "first_run", "checked_at": now()}
    for run in prior:
        previous = str(run["id"])
        if run["status"] != "completed":
            raise PublishError("prior run " + previous + " is not terminal; inspect its original handle")
        attempt = str(run["run_attempt"])
        expected = "sopilot-production-receipt-" + previous + "-" + attempt
        artifacts = json.loads(api("repos/" + repository + "/actions/runs/" + previous + "/artifacts?per_page=100"))["artifacts"]
        matching = [artifact for artifact in artifacts if artifact["name"] == expected and not artifact["expired"]]
        if len(matching) != 1:
            jobs = json.loads(api("repos/" + repository + "/actions/runs/" + previous +
                                  "/attempts/" + attempt + "/jobs?per_page=100"))["jobs"]
            # Merging an inert main workflow can generate scheduled runs before
            # owner activation. Only actual skipped produce-job metadata proves
            # there was no capture/push; workflow success alone is insufficient.
            if (len(jobs) == 1 and jobs[0].get("name") == "produce" and
                    jobs[0].get("status") == "completed" and jobs[0].get("conclusion") == "skipped" and
                    jobs[0].get("steps", []) == []):
                continue
            raise PublishError("prior run " + previous + " has missing/ambiguous receipt; inspect original pending artifact")
        raw = api("repos/" + repository + "/actions/artifacts/" + str(matching[0]["id"]) + "/zip")
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if archive.namelist() != ["receipt.json"]:
                raise PublishError("unexpected receipt artifact files")
            receipt = json.loads(archive.read("receipt.json"))
        identity = receipt.get("run", {})
        if (receipt.get("schema_version"), receipt.get("owner"), receipt.get("mode"), receipt.get("source"), receipt.get("workflow")) != (1, "cloud", "production", "sopilot", "sopilot-producer.yml"):
            raise PublishError("prior receipt schema/owner mismatch")
        if (identity.get("repository"), str(identity.get("id")), str(identity.get("attempt")), identity.get("code_sha")) != (repository, previous, attempt, run["head_sha"]):
            raise PublishError("prior receipt run fingerprint mismatch")
        published = receipt.get("publish", {})
        capture = receipt.get("capture", {})
        if receipt.get("barrier", {}).get("status") == "blocked" and not capture:
            continue  # Look through a blocked follow-up to its original unresolved run.
        if capture.get("status") == "failed" and published.get("status") == "not_started":
            continue  # A proven failed capture has no candidate or push to reconcile.
        if capture.get("status") == "success" and published.get("status") in {"confirmed", "unchanged"} and published.get("target_blob_verified") is True:
            return {"status": "confirmed", "checked_at": now(), "prior_run_id": previous,
                    "prior_run_attempt": attempt, "prior_remote_sha": published.get("remote_sha")}
        raise PublishError("prior run " + previous + " has pending/unknown publish; explicitly resume its original pending artifact")
    if len(runs) == 100:
        raise PublishError("receipt scan limit reached without resolved owner state; inspect original runs")
    return {"status": "no_prior_push", "checked_at": now()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = check(os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_RUN_ID", ""))
        code = 0
    except Exception as error:
        result = {"status": "blocked", "checked_at": now(), "error": str(error)}
        code = 1
    save(args.output / "barrier.json", result)
    print(json.dumps(result))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
