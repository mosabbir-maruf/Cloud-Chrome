"""Regression checks for the prebuilt GHCR image configuration."""

import json
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
IMAGE = "ghcr.io/mosabbir-maruf/cloud-chrome-runtime"


class GhcrConfigurationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.devcontainer = json.loads((ROOT / ".devcontainer/devcontainer.json").read_text())
        cls.workflow = (ROOT / ".github/workflows/ci.yml").read_text()
        cls.dockerfile = (ROOT / ".devcontainer/Dockerfile").read_text()

    def test_devcontainer_uses_prebuilt_runtime_image(self):
        self.assertEqual(self.devcontainer["image"], f"{IMAGE}:latest")
        self.assertNotIn("dockerFile", self.devcontainer)
        self.assertNotIn("build", self.devcontainer)

    def test_publisher_targets_runtime_package_with_both_tags(self):
        self.assertEqual(self.workflow.count(f"GHCR_IMAGE: {IMAGE}"), 3)
        self.assertIn('docker image tag "$image_id" "$GHCR_IMAGE:latest"', self.workflow)
        self.assertIn('docker image tag "$image_id" "$GHCR_IMAGE:sha-$GITHUB_SHA"', self.workflow)
        self.assertIn('docker push "$GHCR_IMAGE:sha-$GITHUB_SHA"', self.workflow)
        self.assertIn('docker push "$GHCR_IMAGE:latest"', self.workflow)
        self.assertIn('imagetools inspect "$GHCR_IMAGE:latest"', self.workflow)
        self.assertIn('imagetools inspect "$GHCR_IMAGE:sha-$GITHUB_SHA"', self.workflow)

    def test_publisher_uses_job_scoped_github_token_permission(self):
        publisher = self.workflow.split("  publish:\n", 1)[1].split("  cleanup-image-artifact:\n", 1)[0]
        self.assertRegex(publisher, r"(?m)^      packages: write$")
        self.assertIn("GHCR_TOKEN: ${{ secrets.GITHUB_TOKEN }}", publisher)
        self.assertIn("printf '%s' \"$GHCR_TOKEN\" | docker login", publisher)
        self.assertNotIn("secrets.GHCR", publisher)
        self.assertNotIn("packages: write", self.workflow.split("jobs:", 1)[0])

    def test_publisher_preserves_tested_artifact_and_safety_guards(self):
        self.assertIn("cloud-chrome-image", self.workflow)
        self.assertIn("Verify uploaded image identity", self.workflow)
        self.assertIn("Verify this commit is current main immediately before publication", self.workflow)
        self.assertIn("Final main HEAD check before GHCR push", self.workflow)
        self.assertIn("group: cloud-chrome-ghcr-publisher", self.workflow)
        self.assertIn("cancel-in-progress: false", self.workflow)

    def test_cleanup_uses_pinned_graph_aware_action_and_safe_policy(self):
        action = "dataaxiom/ghcr-cleanup-action@d52806a0dc70b430571a37da1fde39733ffd640f"
        self.assertEqual(self.workflow.count(action), 2)
        self.assertIn("packages: cloud-chrome-runtime", self.workflow)
        self.assertIn("keep-n-tagged: 1", self.workflow)
        self.assertIn("delete-untagged: true", self.workflow)
        self.assertIn("validate: true", self.workflow)
        self.assertIn("dry-run: true", self.workflow)
        self.assertNotIn("delete-partial-images:", self.workflow)
        self.assertNotIn("delete-ghost-images:", self.workflow)
        self.assertNotIn("delete-orphaned-images:", self.workflow)
        self.assertIn("inputs.ghcr_retention_dry_run != false", self.workflow)
        self.assertIn("Confirm manual dispatch retention dry run succeeded", self.workflow)
        self.assertIn("inputs.ghcr_retention_dry_run == false", self.workflow)
        self.assertIn("github.event_name != 'workflow_dispatch'", self.workflow)
        dry_run_step = self.workflow.split("      - name: Inspect current-image retention plan", 1)[1]
        dry_run_step = dry_run_step.split("      - name: Apply graph-aware package retention", 1)[0]
        destructive_step = self.workflow.split("      - name: Apply graph-aware package retention", 1)[1]
        destructive_step = destructive_step.split("      - name: Verify retained tags", 1)[0]
        self.assertIn("dry-run: true", dry_run_step)
        self.assertNotIn("dry-run: true", destructive_step)
        self.assertIn("id: package-retention", destructive_step)
        self.assertIn("id: package-retention-dry-run", dry_run_step)
        self.assertIn("CLEANUP_OUTCOME: ${{ steps.package-retention.outcome }}", self.workflow)
        self.assertIn("CLEANUP_OUTCOME: ${{ steps.package-retention-dry-run.outcome }}", self.workflow)
        self.assertIn('"$CLEANUP_OUTCOME" = success', self.workflow)

    def test_post_cleanup_verification_is_fail_closed_and_anonymous(self):
        marker = "      - name: Verify retained tags and anonymous AMD64 image graph"
        self.assertEqual(self.workflow.count(marker), 1)
        verification = self.workflow.split(marker, 1)[1].split("\n      - name:", 1)[0]
        self.assertIn("EXPECTED_DIGEST", verification)
        self.assertIn("steps.image-inputs.outputs.changed == 'true'", verification)
        self.assertNotIn("github.event_name != 'workflow_dispatch'", verification)
        self.assertIn('"$GHCR_IMAGE:latest"', verification)
        self.assertIn('"$GHCR_IMAGE:$sha_tag"', verification)
        self.assertIn("docker pull --platform linux/amd64", verification)
        self.assertIn("DOCKER_CONFIG", verification)
        self.assertIn("manifests[]?", verification)
        self.assertIn("linux/amd64", verification)
        self.assertIn('[ "$latest_digest" = "$EXPECTED_DIGEST" ]', verification)
        self.assertIn('[ "$sha_digest" = "$EXPECTED_DIGEST" ]', verification)
        self.assertIn('"$GHCR_IMAGE@$amd64_digest"', verification)
        self.assertIn("docker context inspect", verification)
        self.assertIn("AMD64 child manifest mediaType", verification)
        self.assertIn("Direct child pull image ID", verification)
        self.assertIn("Direct child pull RepoDigests", verification)
        self.assertIn("AMD64 config blob retrieved digest", verification)
        self.assertIn("AMD64 config blob retrieved size", verification)
        self.assertIn("pulled_config_digest", verification)
        self.assertIn("$GHCR_IMAGE@$EXPECTED_DIGEST", verification)
        self.assertIn("$GHCR_IMAGE@$amd64_digest", verification)
        self.assertNotIn("docker login", verification)

    def test_old_destructive_retention_script_is_removed(self):
        self.assertFalse((ROOT / "scripts/ghcr-retain-current.sh").exists())
        self.assertFalse((ROOT / "tests/unit/test_ghcr_retention.py").exists())

    def test_oci_source_and_local_ci_image_are_unchanged(self):
        self.assertIn('LABEL org.opencontainers.image.source="${OCI_SOURCE}"', self.dockerfile)
        self.assertIn("--build-arg OCI_SOURCE=https://github.com/mosabbir-maruf/Cloud-Chrome", self.workflow)
        self.assertIn("IMAGE_NAME: cloud-chrome:devcontainer", self.workflow)

    def test_active_files_do_not_reference_old_registry_image(self):
        offenders = []
        for path in ROOT.rglob("*"):
            relative = path.relative_to(ROOT).as_posix()
            if not path.is_file() or ".git" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if "ghcr.io/mosabbir-maruf/cloud-chrome:" in text and relative != "tests/unit/test_ghcr_configuration.py":
                offenders.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
