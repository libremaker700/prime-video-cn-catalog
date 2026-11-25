#!/usr/bin/env python3
"""Wait for this run's uploaded Pages artifact before starting the official deploy action."""
import json
import os
import re
import subprocess
import sys
import time


class ArtifactWaitError(RuntimeError):
    pass


def run_artifacts(repository, run_id):
    """Read the same workflow run through GitHub's artifact REST API; never deploy here."""
    try:
        result = subprocess.run(
            ["gh", "api", "--method", "GET",
             f"repos/{repository}/actions/runs/{run_id}/artifacts",
             "--field", "name=github-pages", "--field", "per_page=100",
             "--header", "Accept: application/vnd.github+json",
             "--header", "X-GitHub-Api-Version: 2022-11-28"],
            capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ArtifactWaitError("artifact discovery request did not complete") from error
    if result.returncode:
        # Do not echo CLI output, which may include authentication or runner configuration.
        raise ArtifactWaitError(f"artifact discovery request failed (exit {result.returncode})")
    try:
        payload = json.loads(result.stdout)
    except (ValueError, TypeError) as error:
        raise ArtifactWaitError("artifact discovery returned invalid JSON") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("artifacts"), list):
        raise ArtifactWaitError("artifact discovery returned no artifact list")
    return payload["artifacts"]


def wait_for_artifact(repository, run_id, artifact_id, *, attempts=20, interval=3,
                      fetch=run_artifacts, sleep=time.sleep, log=print):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository or ""):
        raise ArtifactWaitError("GITHUB_REPOSITORY must identify the current repository")
    if not re.fullmatch(r"[1-9][0-9]*", str(run_id or "")):
        raise ArtifactWaitError("GITHUB_RUN_ID must identify the current workflow run")
    if not re.fullmatch(r"[1-9][0-9]*", str(artifact_id or "")):
        raise ArtifactWaitError("PAGES_ARTIFACT_ID must be the successful upload step's artifact_id")
    if attempts < 2 or interval <= 0:
        raise ArtifactWaitError("artifact discovery needs at least two bounded observations")
    stable = 0
    last_error = "uploaded artifact is not visible yet"
    for attempt in range(1, attempts + 1):
        try:
            artifacts = fetch(repository, str(run_id))
        except ArtifactWaitError as error:
            artifacts = []
            last_error = str(error)
        matches = [row for row in artifacts if isinstance(row, dict)
                   and row.get("name") == "github-pages" and str(row.get("id")) == str(artifact_id)]
        ready = False
        if len(matches) == 1:
            artifact = matches[0]
            if artifact.get("expired") is True:
                raise ArtifactWaitError("the uploaded Pages artifact has expired")
            source_run = artifact.get("workflow_run", {}).get("id")
            if source_run is not None and str(source_run) != str(run_id):
                raise ArtifactWaitError("the uploaded Pages artifact belongs to another run")
            ready = artifact.get("expired") is False and artifact.get("size_in_bytes", 0) > 0
            if ready and any(isinstance(row, dict) and row.get("name") == "github-pages"
                             and str(row.get("id")) != str(artifact_id) and row.get("expired") is False
                             for row in artifacts):
                raise ArtifactWaitError("multiple live github-pages artifacts would make deployment ambiguous")
        stable = stable + 1 if ready else 0
        if stable >= 2:
            log(f"Pages artifact {artifact_id} is visible for run {run_id}; ready for the official deploy action.")
            return
        log(f"Waiting for Pages artifact {artifact_id}: observation {attempt}/{attempts}.")
        if attempt < attempts:
            sleep(interval)
    raise ArtifactWaitError(f"Pages artifact {artifact_id} was not ready after {attempts} observations: {last_error}")


def main():
    try:
        wait_for_artifact(os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_RUN_ID"),
                          os.environ.get("PAGES_ARTIFACT_ID"))
    except ArtifactWaitError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
