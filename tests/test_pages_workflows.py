import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from wait_pages_artifact import ArtifactWaitError, run_artifacts, wait_for_artifact


def artifact(**changes):
    return {"id": 15021636040, "name": "github-pages", "expired": False,
            "size_in_bytes": 1024, "workflow_run": {"id": 77}, **changes}


class ArtifactDiscoveryTests(unittest.TestCase):
    def wait(self, values, **options):
        fetch = Mock(side_effect=values)
        sleep = Mock()
        wait_for_artifact("owner/repo", "77", "15021636040", fetch=fetch, sleep=sleep,
                          log=lambda message: None, **options)
        return fetch, sleep

    def test_waits_for_the_uploaded_id_in_two_consecutive_observations(self):
        fetch, sleep = self.wait([[], [artifact(id=99)], [artifact()], [], [artifact()], [artifact()]])
        self.assertEqual(fetch.call_count, 6)
        self.assertEqual(sleep.call_count, 5)
        fetch.assert_called_with("owner/repo", "77")

    def test_transient_read_failure_is_bounded_and_retried_before_deployment(self):
        fetch, _ = self.wait([ArtifactWaitError("temporary read failure"), [artifact()], [artifact()]])
        self.assertEqual(fetch.call_count, 3)

    def test_wrong_id_name_and_empty_artifacts_do_not_release_the_gate(self):
        for row in [artifact(id=99), artifact(name="diagnostics"), artifact(size_in_bytes=0)]:
            with self.subTest(row=row), self.assertRaises(ArtifactWaitError):
                self.wait([[row], [row]], attempts=2)

    def test_expired_other_run_and_ambiguous_artifacts_fail_closed(self):
        for rows in [[artifact(expired=True)], [artifact(workflow_run={"id": 78})],
                     [artifact(), artifact(id=99)]]:
            with self.subTest(rows=rows), self.assertRaises(ArtifactWaitError):
                self.wait([rows])

    def test_missing_upload_id_never_calls_the_api(self):
        fetch = Mock()
        with self.assertRaises(ArtifactWaitError):
            wait_for_artifact("owner/repo", "77", "", fetch=fetch)
        fetch.assert_not_called()

    def test_gh_only_issues_an_authenticated_run_artifact_get(self):
        result = subprocess.CompletedProcess([], 0, json.dumps({"artifacts": [artifact()]}), "")
        with patch("wait_pages_artifact.subprocess.run", return_value=result) as run:
            self.assertEqual(run_artifacts("owner/repo", "77"), [artifact()])
        command = run.call_args.args[0]
        self.assertEqual(command[:5], ["gh", "api", "--method", "GET", "repos/owner/repo/actions/runs/77/artifacts"])
        self.assertIn("name=github-pages", command)
        self.assertEqual(run.call_args.kwargs["timeout"], 10)

    def test_cli_failures_do_not_echo_runner_authentication_output(self):
        result = subprocess.CompletedProcess([], 1, "", "private runner details")
        with patch("wait_pages_artifact.subprocess.run", return_value=result):
            with self.assertRaisesRegex(ArtifactWaitError, r"exit 1") as error:
                run_artifacts("owner/repo", "77")
        self.assertNotIn("private runner details", str(error.exception))


class PagesWorkflowTests(unittest.TestCase):
    def test_both_workflows_gate_one_separate_official_deployment_job(self):
        for filename, producer in [("update.yml", "update"), ("title-update.yml", "titles")]:
            with self.subTest(workflow=filename):
                workflow = yaml.safe_load((ROOT / ".github/workflows" / filename).read_text(encoding="utf-8"))
                build, deploy = workflow["jobs"][producer], workflow["jobs"]["deploy"]
                upload = next(step for step in build["steps"] if step.get("uses") == "actions/upload-pages-artifact@v3")
                self.assertEqual(upload["id"], "pages-artifact")
                self.assertEqual(build["outputs"]["pages_artifact_id"], "${{ steps.pages-artifact.outputs.artifact_id }}")
                self.assertEqual(deploy["needs"], producer)
                self.assertNotIn("if", deploy, "failed producer jobs must not start deployment")
                self.assertEqual(deploy["permissions"]["actions"], "read")
                self.assertNotIn("environment", deploy, "do not add an approval gate")
                self.assertFalse(any(step.get("uses") == "actions/deploy-pages@v4" for step in build["steps"]))
                steps = deploy["steps"]
                gates = [index for index, step in enumerate(steps) if step.get("run") == "python scripts/wait_pages_artifact.py"]
                actions = [index for index, step in enumerate(steps) if step.get("uses") == "actions/deploy-pages@v4"]
                self.assertEqual(len(gates), 1)
                self.assertEqual(len(actions), 1)
                self.assertLess(gates[0], actions[0])
                self.assertEqual(steps[gates[0]]["env"]["PAGES_ARTIFACT_ID"],
                                 "${{ needs." + producer + ".outputs.pages_artifact_id }}")
                self.assertEqual(steps[actions[0]]["with"]["artifact_name"], "github-pages")


if __name__ == "__main__":
    unittest.main()
