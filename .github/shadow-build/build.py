"""Build approved public source and seal diagnostics and files for the caller."""

from __future__ import annotations

import argparse
import functools
import hashlib
import http.server
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import traceback
import urllib.request
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import TypedDict

HOSTED = frozenset(("dotnet", "unity-core", "godot-dotnet", "godot-demo", "cpp", "cli"))
LICENSED = frozenset(("bossroom", "unreal", "lyra", "unreal-chat"))
MANAGED = frozenset(("dotnet", "unity-core", "godot-dotnet"))
HOSTS = {"linux": "linux-x64", "win32": "windows-x64", "darwin": "macos"}
TARGETS = HOSTED | LICENSED
RUNNERS = {"linux-x64": "ubuntu-24.04", "windows-x64": "windows-2025", "macos": "macos-15"}
UNITY_VERSION = "6000.0.52f1"
# Boss Room cross-builds every desktop player on a standard Linux runner, in
# GameCI's Unity editor image for that player's build module, pinned by digest.
UNITY_IMAGES = {
    "linux-x64": "unityci/editor:ubuntu-6000.0.52f1-linux-il2cpp-3"
    "@sha256:b10c4080079be02e270f714b06bab863bfdd5e11d0dbfadfb7adf9417e711ae5",
    "windows-x64": "unityci/editor:ubuntu-6000.0.52f1-windows-mono-3"
    "@sha256:1111f4f20840232c36a362e8f73ebcff7828990fc2624d60662c286d3a0313a8",
    "macos": "unityci/editor:ubuntu-6000.0.52f1-mac-mono-3"
    "@sha256:69ae4ce9cfc92ae6ebac3b812e7335c6c3206a0a207701bad4ddfe7dd465e45b",
}
UNREAL_VERSION = "5.8.2"
# Epic publishes a Linux development image only (linux/amd64 manifest digest).
# The Unreal plugin and Unreal Chat build their Linux players from it on a
# standard runner; Windows, macOS, and every Lyra player need a runner that
# already holds the engine (and, for Lyra, the Fab content).
UNREAL_IMAGE = (
    "ghcr.io/epicgames/unreal-engine:dev-slim-5.8.2"
    "@sha256:5d42f9f0f3f994143ff2a3b9c8b55ab91deed4b7c1eec213a20757664c3e2b28"
)
RUNNER_VARIABLES = {
    "linux-x64": "SDK_SHADOW_RUNNER_LINUX_X64",
    "windows-x64": "SDK_SHADOW_RUNNER_WINDOWS_X64",
    "macos": "SDK_SHADOW_RUNNER_MACOS",
}
# Pull-request jobs that need a named runner use their own label. A runner
# that kept state from unmerged code could capture the `main` source identity
# in the next `main` job it took, so the two lanes never share a label, and
# a pull-request job never falls back to the `main` label.
PR_RUNNER_VARIABLES = {
    "linux-x64": "SDK_SHADOW_PR_RUNNER_LINUX_X64",
    "windows-x64": "SDK_SHADOW_PR_RUNNER_WINDOWS_X64",
    "macos": "SDK_SHADOW_PR_RUNNER_MACOS",
}
LANE_RUNNER_VARIABLES = {"main": RUNNER_VARIABLES, "pull_request": PR_RUNNER_VARIABLES}
# GitHub gives every self-hosted runner these labels, so a job asking for one
# could land on any runner, including one that serves the other lane.
DEFAULT_RUNNER_LABELS = frozenset(("self-hosted", "linux", "windows", "macos", "x64", "arm", "arm64"))
# The request's lane picks the environment, and so the source key, that the
# build job decrypts with. Unmerged pull-request code runs in the same job as
# its decrypted source, so pull-request stages use their own key per target:
# a key that code leaks cannot open a diff staged from `main`.
SOURCE_ENVIRONMENTS = {"main": "shadow-source", "pull_request": "shadow-source-pr"}
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")
CARRIER_PATH = ".haiven-shadow.age"
PATCH_HEADER = b"haiven-shadow-diff:v1 "
MAX_CIPHERTEXT = 95 * 1024**2
IDENTIFIER = re.compile(r"[1-9][0-9]{0,19}")
RECIPIENT = re.compile(r"age1[0-9a-z]{58}")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?")
GODOT_VERSION = "4.5.1"
GODOT_ASSETS = {
    "linux-x64": (
        "linux.x86_64.zip",
        "5bccbed65a94b82c7c319fdb15719ee8113a6e503976cc54e16f1c61fe95f3d74e5e40b8449b5bb89ff7f424574c20af01a4f5ef08b389e4dc338b245185b0b9",
        "Godot_v4.5.1-stable_linux.x86_64",
    ),
    "windows-x64": (
        "win64.exe.zip",
        "ab84df90ead5a888530faaabe744a27678ef7635068883900174fae37f7fc6178d033b551d88963c7dc2466580915a582bbc79aca97c19085900655761f56a27",
        "Godot_v4.5.1-stable_win64.exe",
    ),
    "macos": (
        "macos.universal.zip",
        "a91a7c3bb2faefafef41624fbd75705b91aec509823d41db595a38b79819e0f2e9c85eecdc7429c3159d8070604a3f4b4425aaa6094d7bcac147f52c89181ce9",
        "Godot.app/Contents/MacOS/Godot",
    ),
}
TEMPLATES_SHA512 = (
    "8a65c73541184fbcf1a7afedb37c9c15ed0f3babdaafe36ff47ccc515548ac84bc7563ba5b94c253e6085b121ac608b7e03d35dfc0b2c8c690a646a8755b57e5"
)
GODOT_TARGETS = {
    "linux-x64": ("Linux", "HaivenGodotDemo.x86_64"),
    "windows-x64": ("Windows Desktop", "HaivenGodotDemo.exe"),
    "macos": ("macOS", "HaivenGodotDemo.zip"),
}


def platforms(target: str) -> tuple[str, ...]:
    return ("portable",) if target in MANAGED else tuple(RUNNERS)


def host(target: str, platform: str) -> str:
    """The runner platform that builds `platform`: Boss Room cross-builds on Linux."""
    return "linux-x64" if target == "bossroom" and platform != "portable" else platform


def placement(target: str, platform: str, lane: str) -> tuple[str, str]:
    """Return the runner label and engine image; no image means the runner holds the engine.

    Standard runners and the pinned engine images run every job on a fresh VM,
    so both lanes share them. A named runner is the lane's own: the label must
    differ from the other lane's (runner labels ignore case) and must not be a
    label GitHub gives every self-hosted runner.
    """
    if target == "bossroom":
        return RUNNERS["linux-x64"], UNITY_IMAGES[platform]
    if target in {"unreal", "unreal-chat"} and platform == "linux-x64":
        return RUNNERS[platform], UNREAL_IMAGE
    if target in LICENSED:
        variable = LANE_RUNNER_VARIABLES[lane][platform]
        label = os.environ.get(variable, "")
        if not LABEL.fullmatch(label):
            raise ValueError(
                f"set the {variable} repository variable to a runner that holds "
                f"Unreal Engine {UNREAL_VERSION}"
                + (" and never runs a main-lane job" if lane == "pull_request" else "")
            )
        if label.lower() in DEFAULT_RUNNER_LABELS:
            raise ValueError(
                f"{variable} names {label}, a label every self-hosted runner carries; "
                "name a label only this lane's runners carry"
            )
        other = next(name for name in SOURCE_ENVIRONMENTS if name != lane)
        other_variable = LANE_RUNNER_VARIABLES[other][platform]
        if label.lower() == os.environ.get(other_variable, "").lower():
            raise ValueError(
                f"{variable} equals {other_variable}: pull-request and main-lane jobs "
                "must run on separate runners"
            )
        return label, ""
    return RUNNERS.get(platform, "ubuntu-24.04"), ""


class SourceDiff(TypedDict):
    carrier: str
    base: str
    sha256: str


class BuildRequest(TypedDict):
    target: str
    lane: str
    builder_sha: str
    workflow_sha: str | None
    source: SourceDiff
    dotnet: SourceDiff | None
    request_id: str
    recipient: str


def require_diff(raw: object) -> SourceDiff:
    if (
        not isinstance(raw, dict)
        or raw.keys() != {"carrier", "base", "sha256"}
        or not all(isinstance(value, str) for value in raw.values())
        or not SHA.fullmatch(raw["carrier"])
        or not SHA.fullmatch(raw["base"])
        or raw["carrier"] == raw["base"]
        or not DIGEST.fullmatch(raw["sha256"])
    ):
        raise ValueError("invalid encrypted diff descriptor")
    return raw


def request(data: str) -> BuildRequest:
    raw = json.loads(data)
    if not isinstance(raw, dict) or set(raw) != {
        "target", "lane", "builder_sha", "workflow_sha", "source", "dotnet", "request_id", "recipient"
    } or not all(
        isinstance(raw[name], str) for name in ("target", "lane", "builder_sha", "request_id", "recipient")
    ):
        raise ValueError("invalid build request")
    target = raw["target"]
    companion = target in {"unity-core", "godot-dotnet"}
    if (
        target not in TARGETS
        or raw["lane"] not in SOURCE_ENVIRONMENTS
        or not SHA.fullmatch(raw["builder_sha"])
        or not isinstance(raw["workflow_sha"], str)
        or not SHA.fullmatch(raw["workflow_sha"])
        or not IDENTIFIER.fullmatch(raw["request_id"])
        or not RECIPIENT.fullmatch(raw["recipient"])
        or companion != (raw["dotnet"] is not None)
    ):
        raise ValueError("invalid build request")
    require_diff(raw["source"])
    if companion:
        require_diff(raw["dotnet"])
    return raw


def plan(data: str) -> None:
    values = request(data)
    target = values["target"]
    if (
        target != os.environ["EXPECTED_TARGET"]
        or values["workflow_sha"] != os.environ["GITHUB_SHA"]
        or values["source"]["base"] != values["workflow_sha"]
    ):
        raise ValueError("request differs from this destination workflow")
    require_builder(values["builder_sha"])
    rows = []
    for platform in platforms(target):
        runner, image = placement(target, platform, values["lane"])
        rows.append({"platform": platform, "runner": runner, "image": image})
    matrix = {"include": rows}
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        for name in ("target", "request_id"):
            print(f"{name}={values[name]}", file=output)
        print(f"environment={SOURCE_ENVIRONMENTS[values['lane']]}", file=output)
        print(f"base_sha={values['source']['base']}", file=output)
        print(f"dotnet_base={values['dotnet']['base'] if values['dotnet'] else ''}", file=output)
        print(f"matrix={json.dumps(matrix, separators=(',', ':'))}", file=output)


def command(*args: str | Path, cwd: Path, **environment: str) -> None:
    env = {
        name: value for name, value in os.environ.items()
        if not name.startswith(("HAIVEN_DEMO_", "HAIVEN_LYRA_"))
        and name not in {
            "GITHUB_TOKEN", "GH_TOKEN", "GIT_ASKPASS", "GITHUB_ENV", "GITHUB_OUTPUT",
            "GITHUB_PATH", "GITHUB_STATE", "GITHUB_STEP_SUMMARY",
        }
    }
    subprocess.run(
        tuple(str(arg) for arg in args),
        cwd=cwd,
        env={**env, **environment},
        check=True,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )


def git(source: Path, *args: str, **kwargs: object) -> bytes:
    return subprocess.run(
        ("git", "-c", f"core.hooksPath={os.devnull}", *args),
        cwd=source,
        capture_output=True,
        check=True,
        **kwargs,
    ).stdout


def restore_source(source: Path, diff: SourceDiff, request_id: str, identity: str) -> str:
    base, carrier = diff["base"], diff["carrier"]
    if (
        git(source, "rev-parse", "HEAD").decode().strip() != base
        or git(source, "status", "--porcelain", "--untracked-files=all")
        or git(source, "ls-remote", "--exit-code", "origin", "refs/heads/main")
        != f"{base}\trefs/heads/main\n".encode()
        or git(source, "ls-remote", "--exit-code", "origin", f"refs/heads/ci/preview/{request_id}")
        != f"{carrier}\trefs/heads/ci/preview/{request_id}\n".encode()
    ):
        raise ValueError("shadow ref or public base differs from the approved request")
    git(source, "fetch", "--no-tags", "--depth=2", "origin", carrier)
    commit = git(source, "cat-file", "commit", carrier).split(b"\n\n", 1)[0].splitlines()
    parents = [entry for entry in commit if entry.startswith(b"parent ")]
    changes = git(source, "diff-tree", "--name-status", "-z", "--no-renames", "-r", base, carrier)
    entry = git(source, "ls-tree", carrier, "--", CARRIER_PATH)
    if (
        parents != [f"parent {base}".encode()]
        or changes != f"A\0{CARRIER_PATH}\0".encode()
        or not entry.startswith(b"100644 blob ")
    ):
        raise ValueError("carrier must contain only one encrypted diff above the approved base")

    blob = f"{carrier}:{CARRIER_PATH}"
    size = int(git(source, "cat-file", "-s", blob))
    if not 0 < size <= MAX_CIPHERTEXT:
        raise ValueError("invalid shadow ciphertext size")
    ciphertext = git(source, "cat-file", "blob", blob)
    if (
        not ciphertext.startswith(b"age-encryption.org/v1\n")
        or hashlib.sha256(ciphertext).hexdigest() != diff["sha256"]
    ):
        raise ValueError("shadow ciphertext digest differs from the approved request")

    with tempfile.TemporaryDirectory(prefix="haiven-shadow-") as temporary:
        encrypted = Path(temporary) / CARRIER_PATH
        encrypted.write_bytes(ciphertext)
        with tempfile.TemporaryFile(mode="w+b", buffering=0, dir=temporary) as plaintext:
            result = subprocess.run(
                ("age", "--decrypt", "--identity", "-", str(encrypted)),
                input=(identity.rstrip() + "\n").encode(),
                stdout=plaintext,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if result.returncode:
                raise ValueError("could not decrypt the approved source diff")
            plaintext.seek(0)
            header = plaintext.readline(len(PATCH_HEADER) + 42)
            tree = header.removeprefix(PATCH_HEADER).rstrip(b"\n").decode("ascii")
            if header != PATCH_HEADER + tree.encode() + b"\n" or not SHA.fullmatch(tree):
                raise ValueError("invalid encrypted source diff header")
            offset = plaintext.tell()
            if plaintext.read(1):
                plaintext.seek(offset)
                git(source, "apply", "--cached", "--whitespace=nowarn", "-", stdin=plaintext)

    actual = git(source, "write-tree").decode().strip()
    paths = git(source, "ls-tree", "-r", "-z", actual).split(b"\0")
    if (
        actual != tree
        or any(row and row.split(b" ", 1)[0] not in {b"100644", b"100755"} for row in paths)
        or git(source, "ls-tree", actual, "--", CARRIER_PATH)
    ):
        raise ValueError("reconstructed tree differs from the approved source")
    epoch = git(source, "show", "-s", "--format=%ct", base).decode().strip()
    date = f"@{epoch} +0000"
    revision = git(
        source,
        "-c", "user.name=Haiven Shadow Build",
        "-c", "user.email=shadow-build@users.noreply.github.com",
        "commit-tree", tree, "-p", base, "-m", "Reconstruct approved shadow source",
        env={**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date},
    ).decode().strip()
    git(
        source,
        "-c", "filter.lfs.smudge=",
        "-c", "filter.lfs.process=",
        "-c", "filter.lfs.required=false",
        "-c", "core.autocrlf=false",
        "reset", "--hard", revision,
    )
    return tree


def require_builder(sha: str) -> None:
    tooling = Path(__file__).resolve().parents[2]
    if git(tooling, "rev-parse", "HEAD").decode().strip() != sha:
        raise ValueError("request requires the approved shared builder revision")


def reconstruct(args: argparse.Namespace) -> None:
    values = request(args.request)
    identity = os.environ.pop("SDK_SHADOW_SOURCE_AGE_IDENTITY", "")
    require_builder(values["builder_sha"])
    if not identity.strip():
        raise ValueError("protected source identity is unavailable")
    trees = {"source": restore_source(args.source, values["source"], values["request_id"], identity),
             "dotnet": None}
    if values["dotnet"] is not None:
        trees["dotnet"] = restore_source(args.dotnet, values["dotnet"], values["request_id"], identity)
    with args.verification.open("x", encoding="utf-8") as verification:
        json.dump(trees, verification)


def require_tree(source: Path, tree: str) -> None:
    if (
        not SHA.fullmatch(tree)
        or git(source, "rev-parse", "HEAD^{tree}").decode().strip() != tree
        or git(source, "write-tree").decode().strip() != tree
        or git(source, "status", "--porcelain", "--untracked-files=all")
    ):
        raise ValueError("checkout no longer matches the reconstructed tree")


def managed(target: str, source: Path, dotnet: Path, output: Path, work: Path) -> None:
    if target == "dotnet":
        for name in ("Haiven.Sdk", "Haiven.Sdk.SystemTextJson"):
            command("dotnet", "pack", source / name / f"{name}.csproj", "-c", "Release",
                    "-o", output, "-p:ContinuousIntegrationBuild=true", cwd=source)
            if not list(output.glob(f"{name}.[0-9]*.nupkg")):
                raise ValueError("missing portable package")
        return
    if target == "unity-core":
        project = dotnet / "Haiven.Sdk.Core/Haiven.Sdk.Core.csproj"
        assemblies = ("Haiven.Sdk.Core.dll",)
    else:
        project = source / "Haiven.Godot.csproj"
        assemblies = ("Haiven.Godot.dll", "Haiven.Sdk.Core.dll")
    compiled = work / "compiled"
    # Running below the version-pinned .NET checkout selects its global.json.
    command("dotnet", "build", project, "-c", "Release", "-o", compiled,
            "-p:ContinuousIntegrationBuild=true", cwd=dotnet)
    for name in assemblies:
        shutil.copyfile(compiled / name, output / name)


def cpp(source: Path, output: Path, work: Path) -> None:
    build, consumer = work / "cpp-build", work / "cpp-consumer"
    command("cmake", "-S", source, "-B", build, "-DCMAKE_BUILD_TYPE=Release", cwd=source)
    command("cmake", "--build", build, "--config", "Release", "--parallel", cwd=source)
    command("ctest", "--test-dir", build, "-C", "Release", "--output-on-failure",
            "--no-tests=error", cwd=source)
    command("cmake", "--install", build, "--config", "Release", "--prefix", output, cwd=source)
    command("cmake", "-S", source / "test/consumer", "-B", consumer,
            f"-DCMAKE_PREFIX_PATH={output}", "-DCMAKE_BUILD_TYPE=Release", cwd=source)
    command("cmake", "--build", consumer, "--config", "Release", cwd=source)
    executable = next((consumer / path for path in (
        "consumer", "consumer.exe", "Release/consumer", "Release/consumer.exe"
    ) if (consumer / path).is_file()), None)
    if executable is None:
        raise ValueError("missing compiled package consumer")
    command(executable, cwd=source)


class QuietMirror(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


def cli(source: Path, output: Path, work: Path, version: str) -> None:
    if (source / "version.txt").read_text().strip() != version:
        raise ValueError("CLI release version differs from its approved source")
    epoch = subprocess.check_output(("git", "log", "-1", "--format=%ct"), cwd=source, text=True, stderr=sys.stderr)
    command("go", "test", "-count=1", "./tools/release", "./internal/pin", "./scripts", cwd=source)
    command("go", "run", "./tools/release", "-version", version, "-out", output, cwd=source,
            SOURCE_DATE_EPOCH=epoch.strip(), GOTOOLCHAIN="local", GOWORK="off")
    entries = (output / "checksums.txt").read_text().splitlines()
    if not entries:
        raise ValueError("CLI archives are missing")
    for entry in entries:
        checksum, name = entry.split("  ", 1)
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name) or name in {".", ".."}:
            raise ValueError("invalid CLI archive name")
        with (output / name).open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != checksum:
                raise ValueError("CLI archive differs from its checksum")
    mirror = work / "mirror"
    shutil.copytree(output, mirror / f"v{version}")
    destination = work / "installed"
    handler = functools.partial(QuietMirror, directory=str(mirror))
    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            download_base = f"http://127.0.0.1:{server.server_port}"
            if sys.platform == "win32":
                command("pwsh", "-NoProfile", "-File", source / "scripts/install.ps1", cwd=source,
                        HAIVEN_VERSION=version, HAIVEN_INSTALL_DIR=str(destination),
                        HAIVEN_NO_PATH="1", HAIVEN_DOWNLOAD_BASE=download_base)
                executable = destination / "haiven.exe"
            else:
                command("sh", source / "scripts/install.sh", "--version", version, "--dir",
                        destination, cwd=source, HAIVEN_DOWNLOAD_BASE=download_base)
                executable = destination / "haiven"
            receipt = subprocess.check_output((str(executable), "version", "--json"), cwd=source, stderr=sys.stderr)
            if json.loads(receipt)["data"]["version"] != version:
                raise ValueError("installed CLI version differs from the approved release")
        finally:
            server.shutdown()
            thread.join()


def godot_asset(work: Path, suffix: str, digest: str) -> Path:
    file = f"Godot_v{GODOT_VERSION}-stable_{suffix}"
    url = f"https://github.com/godotengine/godot-builds/releases/download/{GODOT_VERSION}-stable/{file}"
    destination = work / file
    with urllib.request.urlopen(url, timeout=120) as response, destination.open("xb") as stream:
        shutil.copyfileobj(response, stream)
    with destination.open("rb") as stream:
        if hashlib.file_digest(stream, "sha512").hexdigest() != digest:
            raise ValueError("Godot release asset differs from its pinned digest")
    return destination


def godot_demo(source: Path, output: Path, work: Path, platform: str) -> None:
    archive, digest, binary = GODOT_ASSETS[platform]
    editor = godot_asset(work, archive, digest)
    tools = work / "editor"
    tools.mkdir()
    if platform == "macos":
        command("ditto", "-x", "-k", editor, tools, cwd=work)
    else:
        with zipfile.ZipFile(editor) as bundle:
            bundle.extract(binary, tools)
    godot = tools / binary
    godot.chmod(0o755)
    data = {
        "linux-x64": Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "godot",
        "windows-x64": Path(os.environ.get("APPDATA", Path.home())) / "Godot",
        "macos": Path.home() / "Library/Application Support/Godot",
    }[platform]
    templates = data / "export_templates" / f"{GODOT_VERSION}.stable"
    templates.mkdir(parents=True, exist_ok=True)
    member = {
        "linux-x64": "linux_release.x86_64",
        "windows-x64": "windows_release_x86_64.exe",
        "macos": "macos.zip",
    }[platform]
    with zipfile.ZipFile(godot_asset(work, "export_templates.tpz", TEMPLATES_SHA512)) as bundle:
        for name in (member, "version.txt"):
            with bundle.open(f"templates/{name}") as item, (templates / name).open("wb") as stream:
                shutil.copyfileobj(item, stream)
    preset, launcher = GODOT_TARGETS[platform]
    executable = output / launcher
    import_log = work / "godot.log"
    for args in (("--import",), ("--export-release", preset, str(executable))):
        result = subprocess.run(
            (str(godot), "--headless", "--path", str(source), "--log-file", str(import_log), *args),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
            check=False, cwd=source, env={**os.environ, "NO_COLOR": "1"},
        )
        print(result.stdout)
        if result.returncode or re.search(r"^(?:SCRIPT ERROR|ERROR):", result.stdout, re.MULTILINE):
            raise ValueError("Godot export failed")
    if platform == "macos":
        unpacked = work / "smoke"
        unpacked.mkdir()
        command("ditto", "-x", "-k", executable, unpacked, cwd=work)
        (app,) = unpacked.glob("*.app")
        command("codesign", "--verify", "--deep", "--strict", app, cwd=work)
        with (app / "Contents/Info.plist").open("rb") as stream:
            name = plistlib.load(stream)["CFBundleExecutable"]
        if not isinstance(name, str) or Path(name).name != name:
            raise ValueError("invalid Godot launcher")
        executable = app / "Contents/MacOS" / name
    elif not executable.with_suffix(".pck").is_file():
        raise ValueError("missing exported Godot data pack")
    log = work / "player.log"
    command(executable, "--headless", "--quit-after", "120", "--log-file", log, cwd=executable.parent)
    diagnostics = log.read_text(encoding="utf-8", errors="replace")
    print(diagnostics)
    if "Godot Engine v" not in diagnostics or re.search(r"^(?:SCRIPT ERROR|ERROR):", diagnostics, re.MULTILINE):
        raise ValueError("Godot player launch failed")


def executable_path(variable: str) -> Path:
    result = Path(os.environ.get(variable, ""))
    if not result.is_absolute() or not result.exists():
        raise ValueError(f"{variable} must name an installed licensed build input")
    return result


def licensed(target: str, source: Path, output: Path, work: Path, platform: str) -> None:
    if target == "bossroom":
        unity_target, launcher = {
            "linux-x64": ("linux64", "HaivenBossRoom.x86_64"),
            "windows-x64": ("win64", "HaivenBossRoom.exe"),
            "macos": ("osxuniversal", "HaivenBossRoom.app"),
        }[platform]
        command(executable_path("UNITY_EDITOR"), "-batchmode", "-nographics", "-quit", "-projectPath",
                source, "-buildTarget", unity_target, "-executeMethod",
                "HaivenCrossPlatformPlayerBuild.BuildCurrent", "-logFile", "-", cwd=source)
        payload = source / "Builds/players" / platform
        if not (payload / launcher).exists():
            raise ValueError("missing built Unity player")
        shutil.copytree(payload, output, dirs_exist_ok=True)
        return
    engine = executable_path("UNREAL_ENGINE_ROOT")
    installed = json.loads((engine / "Engine/Build/Build.version").read_text(encoding="utf-8-sig"))
    version = ".".join(str(installed[key]) for key in ("MajorVersion", "MinorVersion", "PatchVersion"))
    if version != UNREAL_VERSION:
        raise ValueError("installed Unreal Engine differs from the supported version")
    uat = engine / "Engine/Build/BatchFiles" / ("RunUAT.bat" if platform == "windows-x64" else "RunUAT.sh")
    unreal_platform = {"linux-x64": "Linux", "windows-x64": "Win64", "macos": "Mac"}[platform]
    if target == "unreal":
        command(uat, "BuildPlugin", f"-Plugin={source / 'Haiven/Haiven.uplugin'}",
                f"-Package={output / 'Haiven'}", f"-TargetPlatforms={unreal_platform}",
                "-Rocket", "-Unattended", "-NoP4", cwd=source)
        suffix = {"linux-x64": ".so", "windows-x64": ".dll", "macos": ".dylib"}[platform]
        modules = output / "Haiven/Binaries" / unreal_platform
        if not any(path.stat().st_size for path in modules.glob(f"*Haiven*{suffix}")):
            raise ValueError("missing compiled plugin")
        return
    if target == "lyra":
        project = work / "Lyra"
        shutil.copytree(executable_path("HAIVEN_LYRA_CONTENT"), project)
        for relative in ("Plugins/Haiven", "Plugins/HaivenLyraDemo", "Plugins/HaivenLyraInventory",
                         "Content/HaivenTestFixtures"):
            shutil.copytree(source / relative, project / relative, dirs_exist_ok=True)
        command("git", "apply", source / "patches/0001-feat-lyra-enable-the-Haiven-plugins.patch", cwd=project)
        descriptor = project / "Lyra.uproject"
        game = "LyraGame"
    else:
        project = work / "HaivenChatPlayer"
        shutil.copytree(source, project, ignore=shutil.ignore_patterns(".git"))
        descriptor = project / "HaivenChatPlayer.uproject"
        (project / "HaivenChatStarter.uproject").rename(descriptor)
        with (project / "Config/DefaultEngine.ini").open("a", encoding="utf-8") as config:
            config.write("\n[BuildConfiguration]\nbAllowHotReloadFromIDE=False\n")
        game = "HaivenChatPlayer"
    command(uat, "BuildCookRun", f"-project={descriptor}", f"-platform={unreal_platform}",
            "-clientconfig=Shipping", "-build", "-cook", "-stage", "-pak", "-archive",
            f"-archivedirectory={output}", "-unattended", "-utf8output", "-noP4", cwd=project)
    if platform == "macos":
        launchers = [*output.rglob(f"{game}*.app/Contents/Info.plist")]
        if target == "lyra":
            launchers.extend(output.rglob("Lyra.app/Contents/Info.plist"))
    else:
        extension = "exe" if platform == "windows-x64" else "sh"
        launchers = list(output.rglob(f"{game}.{extension}"))
    if not any(path.stat().st_size for path in launchers):
        raise ValueError("missing packaged Unreal launcher")


def portable_metadata(member: tarfile.TarInfo) -> tarfile.TarInfo:
    member.uid = member.gid = 0
    member.uname = member.gname = ""
    return member


def build(args: argparse.Namespace) -> bool:
    values = request(args.request)
    target = values["target"]
    platform = args.platform
    if platform not in platforms(target) or (
        platform != "portable" and HOSTS[sys.platform] != host(target, platform)
    ):
        raise ValueError("incorrect build platform")
    source, dotnet = args.source.resolve(), args.dotnet.resolve()
    work = args.work.resolve()
    work.mkdir()
    output = work / "output"
    output.mkdir()
    diagnostics = work / "build.log"
    checked = False
    with (
        diagnostics.open("w", encoding="utf-8") as log,
        redirect_stdout(log),
        redirect_stderr(log),
    ):
        try:
            trees = json.loads(args.verification.read_bytes())
            require_tree(source, trees["source"])
            if values["dotnet"] is not None:
                require_tree(dotnet, trees["dotnet"])
            if target in MANAGED:
                managed(target, source, dotnet, output, work)
            elif target == "cpp":
                cpp(source, output, work)
            elif target == "cli":
                version = (source / "version.txt").read_text().strip()
                if not VERSION.fullmatch(version):
                    raise ValueError("invalid reconstructed CLI version")
                cli(source, output, work, version)
            elif target == "godot-demo":
                godot_demo(source, output, work, platform)
            else:
                licensed(target, source, output, work, platform)
            checked = True
        except Exception:  # noqa: BLE001 - preserve failure details inside the encrypted report
            traceback.print_exc()
    decision = {key: value for key, value in values.items() if key != "recipient"}
    decision["trees"] = json.loads(args.verification.read_bytes())
    payload = work / "payload.tar.gz"
    if checked:
        # Keep package permissions, including internal app bundle links, inside
        # an opaque archive. The collector never extracts executable outputs.
        with tarfile.open(payload, "x:gz", compresslevel=3) as binary:
            binary.add(output, arcname="output", filter=portable_metadata)
        with payload.open("rb") as stream:
            decision["payload_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
    (work / "receipt.json").write_text(json.dumps({
        **decision, "platform": platform, "success": checked,
    }, sort_keys=True), encoding="utf-8")
    archive = work / "bundle.tar"
    with tarfile.open(archive, "x") as bundle:
        bundle.add(diagnostics, arcname="build.log", filter=portable_metadata)
        bundle.add(work / "receipt.json", arcname="receipt.json", filter=portable_metadata)
        if checked:
            bundle.add(payload, arcname=payload.name, filter=portable_metadata)
    try:
        result = subprocess.run(
            ("age", "--encrypt", "--recipient", values["recipient"], "--output",
             str(args.bundle.resolve()), str(archive)), capture_output=True, check=False,
        )
        if result.returncode:
            raise ValueError("could not seal build output")
    finally:
        shutil.rmtree(work)
    print("Shadow build passed." if checked else "Shadow build failed. Encrypted diagnostics retained.")
    return checked


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan")
    source = sub.add_parser("reconstruct")
    for name in ("source", "dotnet", "verification"):
        source.add_argument(f"--{name}", type=Path, required=True)
    builder = sub.add_parser("build")
    builder.add_argument("--platform", required=True)
    for name in ("source", "dotnet", "work", "bundle", "verification"):
        builder.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    args.request = os.environ.get("SHADOW_REQUEST", "")
    try:
        if args.command == "plan":
            plan(args.request)
            return 0
        if args.command == "reconstruct":
            reconstruct(args)
            print("Approved encrypted source diff reconstructed.")
            return 0
        return 0 if build(args) else 1
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        print("Could not prepare or seal the shadow build.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
