# Public shadow build

The [C++ workflow](../workflows/shadow-build.yml) accepts first-attempt
dispatches for C++ from the configured GitHub App on this repository's
protected `main`. It runs on standard Ubuntu, Windows, and macOS runners.
Other targets have their own dispatch workflows and source keys, and
check out this repository's builder script at an independently reviewed
commit. Standard hosted runner usage is free and unlimited in public
repositories, subject to GitHub Actions job and concurrency limits.
[GitHub-hosted runners](https://docs.github.com/en/actions/reference/runners/github-hosted-runners#standard-github-hosted-runners-for-public-repositories)

## Request

The caller reviews the complete export, computes the binary diff against
the exact public base, and encrypts it to the builder's age recipient. It
publishes one commit on `ci/preview/<request_id>` with the public base as
its parent and only the added ciphertext file `.haiven-shadow.age`. The
encrypted message begins with `haiven-shadow-diff:v1 <expected-tree>` and a
newline, followed by the binary Git patch.

The workflow accepts the positive decimal correlation ID as a
`request_id` input. Its `request` input is a JSON object with these fields.

| Property | Value |
| --- | --- |
| `request_id` | Same correlation ID as the workflow input |
| `target` | `cpp` in this workflow |
| `lane` | `main` for a commit on the caller's protected `main`, or `pull_request` for the head of an unmerged pull request |
| `builder_sha` | Reviewed shared builder revision |
| `workflow_sha` | Reviewed revision of this workflow on protected `main` |
| `source` | Object with `base`, `carrier`, and ciphertext `sha256` |
| `dotnet` | Null in this workflow. The Unity core and Godot .NET workflows supply a companion diff. |
| `recipient` | Separate public age recipient controlled by the caller for encrypting build results |

## Environment

Set `SDK_SHADOW_BUILDER_SHA` to the reviewed common builder commit.
Set `SDK_SHADOW_DISPATCH_ACTOR_ID` to the allowed GitHub App bot's numeric
account ID. The build job runs in the environment the request's `lane`
selects, and each holds its own source age identity as the environment
secret `SDK_SHADOW_SOURCE_AGE_IDENTITY`:

| Lane | Environment | Decrypts |
| --- | --- | --- |
| `main` | `shadow-source` | diffs staged from the caller's protected `main` |
| `pull_request` | `shadow-source-pr` | diffs staged from unmerged pull-request heads |

A pull-request build runs unmerged code in the same job that decrypted its
source, and a hosted runner gives that code root. It could read the
identity from the runner's memory, and ciphertext stays fetchable by SHA
after its ref is deleted. The separate identity limits that leak to other
pull-request stages for this target; it never opens a diff staged from
`main`, provided the two lanes never share a runner host. Standard runners
and the engine-image jobs start every job on a fresh VM. Named runners are
separate per lane, down to the machine (see
[Licensed engines](#licensed-engines)). Restrict both environments to
`main` and require an independent deployment reviewer. Disable administrative bypass. Keep each identity in
its environment and use a distinct identity for decrypting outputs.
[GitHub environments](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments)

The reconstruction step verifies the exact public base, carrier and digest.
It applies the authenticated diff to a clean index and checks the expected
tree and regular-file modes before materializing the source. The build runs
after the decryption step exits. The Actions service and the assigned runner
can see reconstructed sources during execution. Public observers can see
metadata, timing, and ciphertext sizes.

## Licensed engines

Boss Room, the Unreal plugin, Unreal Chat, and Lyra also build here, each
from its own public repository's workflow. The plan step places each
platform job:

| Target | Linux | Windows | macOS |
| --- | --- | --- | --- |
| Boss Room | `ubuntu-24.04` | `ubuntu-24.04`, cross-built | `ubuntu-24.04`, cross-built |
| Unreal plugin, Unreal Chat | `ubuntu-24.04` | `SDK_SHADOW_RUNNER_WINDOWS_X64` | `SDK_SHADOW_RUNNER_MACOS` |
| Lyra | `SDK_SHADOW_RUNNER_LINUX_X64` | `SDK_SHADOW_RUNNER_WINDOWS_X64` | `SDK_SHADOW_RUNNER_MACOS` |

The table names the `main` lane's variables. A `pull_request` job reads
`SDK_SHADOW_PR_RUNNER_<PLATFORM>` instead (`SDK_SHADOW_PR_RUNNER_LINUX_X64`,
`SDK_SHADOW_PR_RUNNER_WINDOWS_X64`, `SDK_SHADOW_PR_RUNNER_MACOS`) and never
falls back to the `main` label.

- **Boss Room** runs Unity `6000.0.52f1` in GameCI's editor image for each
  player's build module, pinned by digest.
  [GameCI images](https://game.ci/docs/docker/docker-images)
  Both source environments hold `UNITY_EMAIL`, `UNITY_PASSWORD`,
  and either `UNITY_LICENSE` (the contents of a Personal `.ulf` file) or
  `UNITY_SERIAL` (a Plus or Pro serial, returned after the build).
  [GameCI activation](https://game.ci/docs/github/activation)
- **Unreal plugin and Unreal Chat** on Linux install Unreal Engine `5.8.2`
  from Epic's `dev-slim` image, pinned by digest. Only the engine
  directory is extracted onto the runner's `/mnt` disk. `EPIC_GHCR_TOKEN`
  in both source environments is a token for a GitHub account linked to Epic, with
  `read:packages`. Epic's image terms follow the Unreal Engine EULA, so the
  workflow never uploads or republishes the engine.
  [Unreal Engine containers](https://dev.epicgames.com/documentation/en-us/unreal-engine/overview-of-containers-in-unreal-engine)
- **Every other Unreal job** runs on the runner label named by the
  repository variable in the table. That runner must already hold the
  engine and export `UNREAL_ENGINE_ROOT`. A Lyra runner also exports
  `HAIVEN_LYRA_CONTENT`, a Lyra project created from Fab for the same
  engine. Epic publishes no Windows or macOS development image, and the
  standard runners have 14 GB of guaranteed disk, so these jobs need a
  larger runner with a custom image or a self-hosted runner. The plan
  fails and names the variable when it is unset.
- **The lanes never share a runner host.** A pull-request job runs
  unmerged code with the runner's privileges. A persistent host can keep
  a process or a changed toolchain (Go, `age`, Python, the engine) from
  that job, and the next `main` job there would expose the `main` source
  identity. The plan refuses a label equal to the other lane's, ignoring
  case, and refuses the labels every self-hosted runner carries
  (`self-hosted`, `linux`, `windows`, `macos`, `x64`, `arm`, `arm64`).
  Labels cannot prove which machines carry them, so the operator must also
  keep the hosts apart:
  - No runner may carry both a pull-request label and a `main` label, in
    this or any other destination.
  - The two lanes' runners must not share a machine, VM, OS user, home
    directory, tool cache (such as the Go cache or `$(go env GOPATH)/bin`)
    or engine install. Two runner registrations on one host count as one
    runner.
  - The pull-request lane must use ephemeral just-in-time runners, each
    taking one job on a fresh VM or machine image, or a machine of its own
    that never runs a `main` job.
  - A larger runner is a fresh VM per job and meets this, but the
    pull-request lane still needs its own runner definition, so its label
    differs.
  [Just-in-time runners](https://docs.github.com/en/actions/reference/runners/self-hosted-runners#just-in-time-runners)

The engine secrets reach only the step that installs or activates the
engine, never the reconstruction or build steps. A pull-request build can
still reach an active engine licence, so `shadow-source-pr` may hold
separate engine credentials. A Unity serial is returned
in an `always()` step. The build step still runs approved project code
while the Unity licence is active.

## Results

The workflow has contents-read permissions and uses Actions artifacts for output.
Each platform uploads `shadow-<request_id>-<platform>` containing one
`build.age` file. The bundle encrypts the detailed diagnostics, compiled
payload, and a receipt binding the reconstruction, platform verdict, and
payload digest. The caller verifies run and artifact identities and the
decrypted receipt before retiring its shadow refs.
