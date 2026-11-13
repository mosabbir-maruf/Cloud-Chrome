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
        cls.retention = (ROOT / "scripts/ghcr-retain-current.sh").read_text()
        cls.dockerfile = (ROOT / ".devcontainer/Dockerfile").read_text()

    def test_devcontainer_uses_prebuilt_runtime_image(self):
        self.assertEqual(self.devcontainer["image"], f"{IMAGE}:latest")
        self.assertNotIn("dockerFile", self.devcontainer)
        self.assertNotIn("build", self.devcontainer)

    def test_publisher_targets_runtime_package_with_both_tags(self):
        self.assertEqual(self.workflow.count(f"GHCR_IMAGE: {IMAGE}"), 2)
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

    def test_retention_is_restricted_to_new_package(self):
        self.assertIn('"$PACKAGE_NAME" == cloud-chrome-runtime', self.retention)
        self.assertIn("mosabbir-maruf/cloud-chrome-runtime", self.retention)

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
            if (
                "ghcr.io/mosabbir-maruf/cloud-chrome:" in text
                and relative != "tests/unit/test_ghcr_configuration.py"
            ):
                offenders.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
