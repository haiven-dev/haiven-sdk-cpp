"""Tests for the shadow builder request and encrypted output contract."""

import argparse
import contextlib
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import build

AGE_AVAILABLE = shutil.which("age") and shutil.which("age-keygen")


def make_key(path):
    subprocess.run(("age-keygen", "-o", path), check=True, capture_output=True)
    return subprocess.check_output(("age-keygen", "-y", path), text=True).strip()


def make_request(recipient="age1" + "p" * 58):
    return {
        "target": "cpp",
        "lane": "main",
        "builder_sha": "c" * 40,
        "workflow_sha": "b" * 40,
        "source": {"carrier": "a" * 40, "base": "b" * 40, "sha256": "d" * 64},
        "dotnet": None,
        "request_id": "123",
        "recipient": recipient,
    }


class ShadowBuildTests(unittest.TestCase):
    def test_request_limits_sources_and_runner_classes(self):
        request = make_request()
        self.assertEqual(build.request(json.dumps(request)), request)
        for replace in (
            {"source": {"carrier": "refs/heads/main"}}, {"request_id": "123\nother=value"},
            {"builder_sha": "main"}, {"target": "dotnet", "dotnet": request["source"]},
            {"target": "unity-core"}, {"target": "bossroom", "workflow_sha": None},
            {"target": "godot"}, {"version": "main"}, {"recipient": "AGE-SECRET-KEY-EXAMPLE"},
            {"lane": "shadow-source"}, {"lane": None}, {"lane": "pr"},
        ):
            with self.subTest(replace=replace), self.assertRaises(ValueError):
                build.request(json.dumps({**request, **replace}))
        for target in sorted(build.LICENSED):
            licensed = {**request, "target": target}
            self.assertEqual(build.request(json.dumps(licensed)), licensed)
        missing = dict(request)
        del missing["lane"]
        with self.assertRaises(ValueError):
            build.request(json.dumps(missing))

    def test_licensed_placement_uses_pinned_engines_or_named_runners(self):
        variables = {
            name: "" for names in build.LANE_RUNNER_VARIABLES.values() for name in names.values()
        }
        with patch.dict(build.os.environ, variables):
            for lane in build.SOURCE_ENVIRONMENTS:
                for platform, image in build.UNITY_IMAGES.items():
                    self.assertIn(build.UNITY_VERSION, image)
                    self.assertRegex(image, r"@sha256:[0-9a-f]{64}$")
                    self.assertEqual(
                        build.placement("bossroom", platform, lane), ("ubuntu-24.04", image)
                    )
                    self.assertEqual(build.host("bossroom", platform), "linux-x64")
                self.assertRegex(build.UNREAL_IMAGE, r"dev-slim-5\.8\.2@sha256:[0-9a-f]{64}$")
                for target in ("unreal", "unreal-chat"):
                    self.assertEqual(
                        build.placement(target, "linux-x64", lane),
                        ("ubuntu-24.04", build.UNREAL_IMAGE),
                    )
                for target, platform in (
                    ("unreal", "windows-x64"), ("unreal-chat", "macos"), ("lyra", "linux-x64"),
                ):
                    with (
                        self.subTest(target=target, platform=platform, lane=lane),
                        self.assertRaises(ValueError),
                    ):
                        build.placement(target, platform, lane)
                self.assertEqual(build.placement("cpp", "macos", lane), ("macos-15", ""))
        named = {build.RUNNER_VARIABLES["windows-x64"]: "haiven-unreal-windows"}
        with patch.dict(build.os.environ, named):
            self.assertEqual(
                build.placement("lyra", "windows-x64", "main"), ("haiven-unreal-windows", "")
            )
            self.assertEqual(build.host("lyra", "windows-x64"), "windows-x64")
        for label in ("", "a b", "x" * 101, "-flag", "label\nother=value"):
            for variables in (build.RUNNER_VARIABLES, build.PR_RUNNER_VARIABLES):
                with patch.dict(build.os.environ, {variables["macos"]: label}):
                    lane = "main" if variables is build.RUNNER_VARIABLES else "pull_request"
                    with self.subTest(label=label, lane=lane), self.assertRaises(ValueError):
                        build.placement("unreal", "macos", lane)

    def test_pull_request_jobs_never_share_a_named_runner_with_main(self):
        main = build.RUNNER_VARIABLES["macos"]
        pull = build.PR_RUNNER_VARIABLES["macos"]
        with patch.dict(build.os.environ, {main: "haiven-unreal-macos", pull: ""}):
            self.assertEqual(
                build.placement("unreal", "macos", "main"), ("haiven-unreal-macos", "")
            )
            # No fallback to the main label for a pull-request job.
            with self.assertRaisesRegex(ValueError, pull):
                build.placement("unreal", "macos", "pull_request")
        with patch.dict(build.os.environ, {main: "haiven-unreal-macos", pull: "haiven-pr-macos"}):
            for lane, label in (("main", "haiven-unreal-macos"), ("pull_request", "haiven-pr-macos")):
                self.assertEqual(build.placement("unreal-chat", "macos", lane), (label, ""))
        # Equal labels are refused in both lanes, ignoring case as GitHub does.
        for pr_label in ("haiven-unreal-macos", "Haiven-Unreal-MacOS"):
            with patch.dict(build.os.environ, {main: "haiven-unreal-macos", pull: pr_label}):
                for lane in build.SOURCE_ENVIRONMENTS:
                    with (
                        self.subTest(pr_label=pr_label, lane=lane),
                        self.assertRaisesRegex(ValueError, "separate runners"),
                    ):
                        build.placement("lyra", "macos", lane)
        # A default self-hosted label could pick the other lane's runner.
        for label in ("self-hosted", "Self-Hosted", "macOS", "ARM64", "x64"):
            for lane, variable in (("main", main), ("pull_request", pull)):
                with (
                    patch.dict(build.os.environ, {main: "", pull: "", variable: label}),
                    self.subTest(label=label, lane=lane),
                    self.assertRaisesRegex(ValueError, "every self-hosted runner"),
                ):
                    build.placement("unreal", "macos", lane)
        # Fresh-VM placements stay shared between the lanes.
        with patch.dict(build.os.environ, {main: "", pull: ""}):
            self.assertEqual(
                build.placement("unreal", "linux-x64", "pull_request"),
                build.placement("unreal", "linux-x64", "main"),
            )

    def test_plan_places_pull_request_jobs_on_their_own_runners(self):
        request = {**make_request(), "target": "lyra", "lane": "pull_request"}
        labels = {
            **{name: f"main-{platform}" for platform, name in build.RUNNER_VARIABLES.items()},
            **{name: f"pr-{platform}" for platform, name in build.PR_RUNNER_VARIABLES.items()},
        }
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "github-output"
            env = {
                "EXPECTED_TARGET": "lyra", "GITHUB_SHA": "b" * 40, "GITHUB_OUTPUT": str(output),
                **labels,
            }
            with patch.dict(build.os.environ, env), patch.object(build, "require_builder"):
                build.plan(json.dumps(request))
                text = output.read_text()
                self.assertIn("environment=shadow-source-pr\n", text)
                for platform in build.RUNNERS:
                    self.assertIn(f'"runner":"pr-{platform}"', text)
                self.assertNotIn('"runner":"main-', text)
                output.write_text("")
                build.plan(json.dumps({**request, "lane": "main"}))
                self.assertIn('"runner":"main-macos"', output.read_text())
                self.assertNotIn('"runner":"pr-', output.read_text())

    def test_readme_names_the_engine_secrets_and_runner_variables(self):
        root = Path(__file__).resolve().parents[2]
        readme = (root / ".github/shadow-build/README.md").read_text()
        for name in (
            "UNITY_LICENSE", "UNITY_SERIAL", "EPIC_GHCR_TOKEN", "SDK_SHADOW_RUNNER_MACOS",
            "SDK_SHADOW_PR_RUNNER_MACOS",
        ):
            self.assertIn(name, readme)

    def test_cpp_workflow_is_dispatch_only_and_read_only(self):
        root = Path(__file__).resolve().parents[2]
        workflow = (root / ".github/workflows/shadow-build.yml").read_text()
        # The source key comes from the environment the request's lane selects.
        self.assertIn("      environment: ${{ steps.plan.outputs.environment }}\n", workflow)
        self.assertIn("    environment: ${{ needs.request.outputs.environment }}\n", workflow)
        self.assertNotIn("environment: shadow-source", workflow)
        self.assertRegex(workflow, r"on:\n  workflow_dispatch:")
        self.assertRegex(workflow, r"permissions:\n  contents: read\n\n")
        self.assertIn("EXPECTED_TARGET: 'cpp'", workflow)
        self.assertNotRegex(workflow, r"(?m)^  (?:push|create|release):|gh release |packages: write")

    def test_plan_refuses_another_destination_or_revision(self):
        request = make_request()
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "github-output"
            env = {"EXPECTED_TARGET": "cpp", "GITHUB_SHA": "b" * 40, "GITHUB_OUTPUT": str(output)}
            with patch.dict(build.os.environ, env), patch.object(build, "require_builder"):
                build.plan(json.dumps(request))
                self.assertIn("base_sha=" + "b" * 40, output.read_text())
                self.assertIn("environment=shadow-source\n", output.read_text())
                output.write_text("")
                build.plan(json.dumps({**request, "lane": "pull_request"}))
                self.assertIn("environment=shadow-source-pr\n", output.read_text())
                self.assertIn('"runner":"windows-2025","image":""', output.read_text())
                for change in (
                    {**request, "target": "cli"},
                    {**request, "workflow_sha": "a" * 40},
                    {**request, "source": {**request["source"], "base": "9" * 40}},
                ):
                    with self.assertRaises(ValueError):
                        build.plan(json.dumps(change))

    @unittest.skipUnless(AGE_AVAILABLE, "age unavailable")
    def test_encrypts_diagnostics_and_retains_owned_payload_only_on_success(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            key = root / "age-key"
            request = make_request(make_key(key))
            trees = {"source": "4" * 40, "dotnet": None}
            verification = root / "verification.json"
            verification.write_text(json.dumps(trees))
            platform = build.HOSTS[sys.platform]
            for ok in (True, False):
                with self.subTest(success=ok):
                    work = root / f"work-{ok}"
                    encrypted = root / f"report-{ok}.age"
                    stdout = io.StringIO()

                    def compile_source(source, output, directory, outcome=ok):
                        print("compile diagnostic only")
                        (output / "file").write_bytes(b"synthetic compiled result")
                        if not outcome:
                            raise ValueError("compile failed")

                    with patch.object(build, "require_tree") as require, \
                         patch.object(build, "cpp", side_effect=compile_source), \
                         contextlib.redirect_stdout(stdout):
                        self.assertIs(build.build(argparse.Namespace(
                            request=json.dumps(request), platform=platform,
                            source=root, dotnet=root, work=work, bundle=encrypted, verification=verification,
                        )), ok)
                    require.assert_called_once_with(root.resolve(), trees["source"])
                    self.assertNotIn("compile diagnostic", stdout.getvalue())
                    self.assertNotIn(b"synthetic compiled result", encrypted.read_bytes())
                    plain = root / f"report-{ok}.tar"
                    subprocess.run(("age", "-d", "-i", key, "-o", plain, encrypted), check=True)
                    self.assertFalse(work.exists())
                    with tarfile.open(plain) as archive:
                        with archive.extractfile("build.log") as log:
                            self.assertIn(b"compile diagnostic only", log.read())
                        with archive.extractfile("receipt.json") as receipt:
                            identity = json.load(receipt)
                        self.assertEqual(identity["source"], request["source"])
                        self.assertEqual(identity["trees"], trees)
                        self.assertIs(identity["success"], ok)
                        self.assertNotIn("recipient", identity)
                        if ok:
                            with archive.extractfile("payload.tar.gz") as payload:
                                self.assertEqual(
                                    hashlib.file_digest(payload, "sha256").hexdigest(),
                                    identity["payload_sha256"],
                                )
                        else:
                            self.assertNotIn("payload.tar.gz", archive.getnames())


    @unittest.skipUnless(AGE_AVAILABLE, "age unavailable")
    def test_reconstruct_validates_base_carrier_ciphertext_and_applied_tree(self):
        cases = ("valid", "wrong_key", "stale_base", "moved_ref", "tampered_digest",
                 "wrong_tree", "unsafe_patch", "plaintext", "empty")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                publisher = root / "publisher"
                publisher.mkdir()
                key = root / "source.age-key"
                recipient = make_key(key)
                git = build.git
                git(publisher, "init", "-b", "main")
                git(publisher, "config", "user.name", "Shadow test")
                git(publisher, "config", "user.email", "shadow@example.invalid")
                (publisher / "base.txt").write_text("published", encoding="utf-8")
                git(publisher, "add", "base.txt")
                git(publisher, "commit", "-m", "Public base")
                base = git(publisher, "rev-parse", "HEAD").decode().strip()
                fixture_bytes = b"test-only plaintext diff\x00\xff"
                name = "sample.bin"
                if case != "empty":
                    (publisher / name).write_bytes(fixture_bytes)
                    git(publisher, "add", name)
                tree = git(publisher, "write-tree").decode().strip()
                patch = git(publisher, "diff", "--cached", "--binary", "--full-index", "HEAD")
                if case == "unsafe_patch":
                    patch = (
                        b"diff --git a/../outside b/../outside\nnew file mode 100644\n"
                        b"--- /dev/null\n+++ b/../outside\n@@ -0,0 +1 @@\n+unsafe\n"
                    )
                git(publisher, "reset", "--hard", base)
                size = len(build.PATCH_HEADER)
                header = build.PATCH_HEADER + (("0" * 40 if case == "wrong_tree" else tree).encode()) + b"\n"
                self.assertEqual(len(header), size + 41)
                cipher = publisher / build.CARRIER_PATH
                subprocess.run(("age", "-r", recipient, "-o", cipher), input=header + patch, check=True)
                git(publisher, "add", build.CARRIER_PATH)
                if case == "plaintext":
                    (publisher / "unapproved.txt").write_bytes(fixture_bytes)
                    git(publisher, "add", "unapproved.txt")
                digest = hashlib.sha256(cipher.read_bytes()).hexdigest()
                git(publisher, "commit", "-m", "Encrypted carrier")
                carrier = git(publisher, "rev-parse", "HEAD").decode().strip()
                ref = "refs/heads/ci/preview/123"
                git(publisher, "update-ref", ref, base if case == "moved_ref" else carrier)
                git(publisher, "reset", "--hard", base)
                if case == "stale_base":
                    git(publisher, "commit", "--allow-empty", "-m", "Public base advanced")
                source = root / "source"
                git(root, "clone", publisher.as_uri(), str(source))
                git(source, "checkout", "--detach", base)
                if case == "wrong_key":
                    key = root / "incorrect.age-key"
                    make_key(key)
                diff = {"carrier": carrier, "base": base,
                        "sha256": "0" * 64 if case == "tampered_digest" else digest}
                if case in ("valid", "empty"):
                    self.assertEqual(build.restore_source(source, diff, "123", key.read_text()), tree)
                    build.require_tree(source, tree)
                    self.assertFalse((source / build.CARRIER_PATH).exists())
                    if case == "valid":
                        self.assertEqual((source / name).read_bytes(), fixture_bytes)
                else:
                    with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                        build.restore_source(source, diff, "123", key.read_text())
                    self.assertFalse((root / "outside").exists())


if __name__ == "__main__":
    unittest.main()
