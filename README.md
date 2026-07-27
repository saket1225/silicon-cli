# silicon-cli

This is the single source for the installable **`silicon`** command. The PyPI
package is named `silicon-cli`, and the code lives here in this `silicon-cli`
repo.

The command manages silicon instances on a machine: create them from the
[silicon-stemcell](https://github.com/teamofsilicons/silicon-stemcell) base,
start/stop them under an auto-restart watchdog, stream logs, and back them up to
Glass. It reads the same `~/.silicon/registry.json`, so existing installs carry
over unchanged.

## Install

```bash
pip install --upgrade silicon-cli
```

The package has a small runtime dependency footprint and installs the complete
`silicon` team-management command.

## Commands

```
silicon                      Show status or list instances
silicon new [dir]            Create a new Silicon (hydrate from stemcell)
silicon new .                Hydrate the current folder into a runnable silicon
silicon start <target>       Start silicon(s). target = name, *, all, 1,2,4, or name,name
silicon stop [--full] <target>  Stop silicon(s). target = name, *, all, 1,2,4, or name,name
silicon restart <target>     Restart silicon(s). target = name, *, all, 1,2,4, or name,name
silicon agent <start|stop|status> [name]   Manage the per-silicon glass agent
silicon status [name]        Show instance status
silicon browser [name]       Open a headed browser for login
silicon browser-profile setup          Create a cloud browser profile through Glass
silicon browser-profile finish <session_id>  Finish profile setup
silicon debug [name]         Tail a running instance's logs
silicon attach [path]        Register an existing silicon directory
silicon pull [api_token]     Pull a Glass team or silicon into local folders
silicon push [name] [now|stop|status]   Daily 23:59 GMT backups to Glass
silicon backup [name] [now|stop|status] Alias for silicon push
silicon update <target>      Task-safely update silicon(s) from the latest stemcell
silicon update --dry-run <target>  Verify and show the exact update/merge plan
silicon update status [name]       Show active generation and transaction state
silicon update cancel [name]       Cancel before the service-stop boundary
silicon update resume [name]       Recover an interrupted transaction
silicon update history [name]      Show durable transaction history
silicon update rollback [name]     Task-safely reactivate the prior generation
silicon list                 List all instances
silicon docker init [--root ~/silicons] [--image registry/repository@sha256:<digest>]
                             Check Docker and enable one-container-per-Silicon runtime
silicon docker doctor        Check/repair Docker runtime setup
silicon docker login [claude|codex|all]
                             Set up shared Claude/Codex auth for Docker silicons
silicon docker compose       Print generated Compose file path
silicon claude [args...]     Run Claude Code with shared Docker auth
silicon codex [args...]      Run Codex with shared Docker auth
silicon script update        Update this CLI itself
silicon package inventory --json
                             Report host, Docker, and per-instance package versions
silicon package update <package> [--silicon-id ID]
                             Safely update every selected copy of one package
silicon help                 Show help
```

`silicon package update` accepts `silicon`, `silicon-cli`,
`silicon-browser`, `silicon-extend`, `silicon-interface`, `claude`, or
`codex`. Shared pip/npm copies update once on the host. Local instance copies
update only across a safe stop boundary, and Docker copies move through the
published, digest-pinned runtime instead of being modified inside a running
container.
Glass uses the JSON form of these commands to provide team-scoped inventory
and durable update jobs.

## Configuration (env vars)

| Var | Default | Purpose |
| --- | --- | --- |
| `SILICON_HOME` | `~/.silicon` | registry + CLI state |
| `GLASS_SERVER_URL` | `https://glass.teamofsilicons.com` | Glass sync server (pull/push) |
| `SILICON_STEMCELL_REPO` | `teamofsilicons/silicon-stemcell` | GitHub repository used by `new`, `pull`, and `update` |
| `SILICON_PYTHON` | `python3` | interpreter used to run a silicon's `main.py` |
| `SILICON_INTERFACE_CLI_PACKAGE` | immutable Interface CLI release asset | package spec used to install the Silicon Interface CLI |
| `SILICON_INTERFACE_CLI_TARBALL` | same immutable release asset | fallback package spec for an explicitly overridden primary source |
| `SILICON_INTERFACE_CLI_SOURCE` | *(empty)* | local package dir or `silicon-interface.mjs` path for dev installs |
| `SILICON_INTERFACE_CLI_SKIP` | *(empty)* | set to `1` to skip interface CLI setup |
| `SILICON_INTERFACE_DAEMON_SKIP` | *(empty)* | set to `1` to install the CLI without starting its listener daemon |
| `SILICON_RUNTIME` | *(empty)* | default pull uses Docker; set to `local` to opt out |
| `SILICON_RUNTIME_IMAGE` | published Stemcell tag metadata | bootstrap-only exact digest when no runtime digest is persisted; it cannot retarget an installed release |
| `SILICON_DOCKER_ROOT` | `~/silicons` | Docker-backed instance root |
| `SILICON_DOCKER_COMPOSE` | `<root>/compose.yml` | generated Compose file path |
| `SILICON_DOCKER_SHARED_HOME` | `<root>/.shared-home` | VM-wide Claude/Codex auth home mounted into every container |
| `SILICON_DOCKER_SUDO` | *(empty)* | set to `1` to run Docker commands through `sudo docker` |
| `SILICON_DOCKER_ALLOW_UNPINNED_IMAGE` | *(empty)* | unsafe local-development-only escape hatch; production must leave this unset |
| `SILICON_UPDATE_RETAIN_GENERATIONS` | `3` | bounded number of immutable update generations to retain (minimum safety floor: 2) |

## Docker runtime

Docker mode keeps the existing `silicon` command but runs each Silicon in its own
container. Mutable instance state lives on the host under `~/silicons/<name>` and
is bind-mounted at `/silicon` inside the container. Provider secrets are still
Glass-managed: the container stores only the Silicon's `.glass.json` key and the
stemcell fetches provider keys from Glass on boot.

Claude Code and Codex account state is shared across all Docker-backed silicons
on the VM. The shared auth home defaults to `~/silicons/.shared-home` and is
mounted into every container. During an interactive `silicon pull`, the CLI asks
whether to set up Claude Code, Codex, or both before installing the team.

On a fresh server, install Docker Engine and Compose v2 from the operating
system's trusted package repository, verifying the vendor repository key as
documented by Docker. The CLI never downloads or executes `get.docker.com` (or
any other mutable installer) as root.

`silicon pull` first checks Docker and Compose without selecting an image. It
then resolves the highest published stable Stemcell Git tag, verifies that
tag's exact object, commit, version, files, and tree, and reads its exact
`registry/repository@sha256:<digest>` runtime identity. It pulls the image and
verifies Docker reports that same repository digest before any instance or
credential is created. The digest is persisted on each instance and generation;
updates switch it only at activation, and rollback restores the prior
generation's digest. A cached image is reusable only when that exact digest
verifies. Pull completes only after each active Docker Silicon exposes the
pinned Silicon Extend package and its `silicon-extend` command.

```bash
pip install silicon-cli
silicon pull sct_live_...
```

To run the same checks explicitly:

```bash
silicon docker bootstrap --root ~/silicons
silicon docker doctor
```

To set up or repair shared Claude/Codex login manually:

```bash
silicon docker login        # asks which accounts to set up
silicon docker login claude # Claude Code only
silicon docker login codex  # Codex only
silicon docker login all    # both
```

Codex login uses `codex login --device-auth` inside the runtime container so it
works on remote/headless servers without browser port forwarding.

To use those same shared accounts directly from the VM without installing Claude
or Codex on the host:

```bash
silicon claude
silicon codex
silicon claude --version
silicon codex --version
```

The runtime image contains the complete Silicon runtime contract: Silicon CLI,
Silicon Browser, Silicon Extend, Silicon Interface CLI, Claude Code, Codex,
Node 22+, Python tooling, and Git. `silicon pull` verifies every command,
minimum supported version, and the Python dependency graph in the exact pinned
image digest before it asks Glass to claim or register a Silicon. Immutable
releases use the updater's pre-staged, hash-locked dependency environment. Only
a legacy flat installation falls back to creating `/silicon/.venv` from its
`requirements.txt`, so per-Silicon Python dependencies do not pollute the host.

To force the older host-local install path:

```bash
SILICON_RUNTIME=local silicon pull sct_live_...
```

Local mode deliberately bypasses the bundled image, so the pull verifies Node
22+, npm, Python, Git, Silicon Browser, Silicon Extend, and the selected
Claude/Codex CLI on the host. It installs and verifies the per-Silicon Interface
CLI while the pull is still staged; a missing prerequisite aborts the pending
Glass claim with an actionable update command.

After that, the normal commands continue to work:

```bash
silicon list
silicon start all
silicon stop ada          # stops the Silicon process; container stays up
silicon stop --full ada   # stops the container
silicon debug ada
silicon update ada
silicon backup ada now
```

The generated Compose file is written to `~/silicons/compose.yml`. You can inspect
it with:

```bash
silicon docker compose
```

Build and publish the runtime image from this repo, then record the registry's
immutable digest as `runtime_image` in the Stemcell's `silicon.info` before
creating and pushing the matching stable Git tag:

```bash
docker build -f docker/runtime/Dockerfile -t ghcr.io/teamofsilicons/silicon-runtime:<version> .
docker push ghcr.io/teamofsilicons/silicon-runtime:<version>
docker image inspect --format '{{json .RepoDigests}}' ghcr.io/teamofsilicons/silicon-runtime:<version>
```

For local CLI, Silicon Browser, and Silicon Extend development, build their
wheels into separate directories and layer them over the published runtime
without publishing first:

```bash
python -m pip wheel . --no-deps --wheel-dir /tmp/silicon-cli-wheel
(cd ../silicon-browser && uv build --wheel --out-dir /tmp/silicon-browser-wheel)
(cd ../silicon-extend && uv build --wheel --out-dir /tmp/silicon-extend-wheel)
docker build \
  --build-arg BASE_IMAGE=ghcr.io/teamofsilicons/silicon-runtime@sha256:<digest> \
  --build-context silicon_cli_wheel=/tmp/silicon-cli-wheel \
  --build-context silicon_browser_wheel=/tmp/silicon-browser-wheel \
  --build-context silicon_extend_wheel=/tmp/silicon-extend-wheel \
  -f docker/runtime/Dockerfile.local \
  -t silicon-runtime:v2-local .
```

## Silicon Interface CLI

For Docker-backed installs, the Silicon Interface CLI is installed inside the
runtime image/container. For host-local installs, `silicon new`, `silicon install`,
and `silicon pull` set up the Silicon Interface CLI in the silicon folder when
Node 22+ is available. When a Glass `.glass.json` is present, setup also starts
the background listener daemon so the silicon receives live conversation frames
without polling.

`silicon pull` is token-native. Generate a team setup token from Glass >
Silicons > Team setup token, then run:

```bash
silicon pull
# paste the token when prompted

# or, less private because it lands in shell history:
silicon pull sct_live_...
```

The command validates the token with Glass and opens one idempotent,
time-bounded credential claim for the entire team. An ambiguous retry replays
that same encrypted claim instead of minting duplicate valid Silicon keys.
Every target name is preflighted first; hydration then happens in hidden
owner-only staging directories. Only after every generation and secret-file
permission verifies does the CLI atomically rename the stages into place,
commit the Glass claim, register the exact identities, regenerate Compose for
Docker-backed installs, and start each Silicon.

Pull progress is journaled without credentials under
`~/.silicon/pull-transactions/` using owner-only files. A crash before commit
resumes the same hidden stages and release identity. A normal pre-commit failure
removes only transaction-marked stages and asks Glass to revoke the abandoned
claim keys. A crash after a rename resumes registration/start without creating
duplicate registry, service, or container identities.

During team pull, setup asks for the default brain/fallback settings once. You
can apply those settings to every silicon, or select specific silicons that
need different settings and answer the brain prompt for those silicons only.
Older per-silicon `scs_live_...` tokens still pull just that one silicon.

Provider API keys for voice, browser profiles, billing, and architecture
generation are configured on the Glass backend. After the silicon API token is
validated, `silicon pull` reads the team's provider-key metadata from Glass and
automatically marks the pulled silicon(s) to use Glass-managed keys when every
provider is available. Plaintext secrets are never returned to the CLI. A
Glass-pulled silicon only stores its Glass API token plus a local marker that
provider keys come from Glass.

The local wrappers are written to:

```bash
<silicon>/.silicon-interface/bin/si
<silicon>/.silicon-interface/bin/silicon-interface
<silicon>/.silicon-interface/inbox.jsonl
<silicon>/.silicon-interface/state.json
```

For Glass-pulled silicons, those wrappers automatically use the folder's
`.glass.json` (`server_url` + `api_key`) for conversation API auth.
Docker startup atomically rewrites these two small per-instance launchers on
every boot. They export the instance root and execute the absolute Interface
CLI bundled in the selected runtime image; the package itself is never copied
over durable inbox or cursor state. Selecting an older runtime image therefore
selects that image's Interface CLI as well, including the first rollback to
the immediately preceding pre-fix image because both images expose the same
absolute runtime command. Images older than the Interface runtime contract,
which do not contain `/usr/local/bin/silicon-interface`, require a supported
intermediate release rather than direct rollback.

During local development, point `SILICON_INTERFACE_CLI_SOURCE` at the package:

```bash
SILICON_INTERFACE_CLI_SOURCE=../silicon-interface/packages/silicon-interface-cli silicon new ./ada
```

## Backups

`silicon push <name> now` and `silicon backup <name> now` use the
active immutable Stemcell generation's canonical backup implementation. First,
the host CLI captures all current source and DNA customizations against the
exact cached upstream release into `.silicon/overlays`, and records a verified
`latest.json` reference that is included in the protected-data snapshot. The
Stemcell then snapshots its mandatory policy plus any additive
`.backupsilicon` paths and uploads the verified snapshot to Glass. Instances
that predate the canonical backup module must first be stopped and updated with
`silicon update <name>`; the CLI does not install or fall back to a second
backup implementation.

`silicon push <name>` starts the background loop. Scheduled manifest backups run
daily at 23:59 GMT and retry transient failures with bounded backoff. The
host-owned supervisor also covers Docker instances, maintains a durable
heartbeat, and refuses duplicate loops. Its enabled/disabled intent survives a
host reboot; normal Silicon startup and every regular CLI startup reconcile a
missing supervisor, while `silicon backup <name> stop` persists an explicit
opt-out. Use `silicon backup <name> status` to inspect it; `now` remains
available for one-shot manual backups.

## Transactional updates

`silicon update <name>` stages and validates a complete immutable generation
while the current Silicon keeps working. It then asks the running Stemcell to
enter maintenance and waits until active work reaches a safe boundary. New
Carbon messages remain durably accepted by Glass during this window; the sender
sees that the Silicon is updating and that the message is queued, without
needing to resend it.

Before stopping anything, the updater verifies a canonical recovery snapshot.
It records the exact services that were running, atomically switches the active
generation, restores that service state, and requires stable health checks
before committing. A failure after the stop boundary automatically reactivates
a verified reconstruction of the prior generation rather than trusting a
writable dormant directory. The mutable data root, memory, credentials,
conversations, and local customization overlay are kept outside immutable
release generations.

Updates are journaled and crash-resumable. A second update cannot overlap an
unfinished transaction:

```bash
silicon update --dry-run ada
silicon update --deadline 30m ada
silicon update status ada
silicon update cancel ada
silicon update resume ada
silicon update history ada
silicon update rollback ada
```

Cancellation is accepted only before the service-stop boundary. `resume`
finishes or safely recovers an interrupted update according to its fsynced
journal; it never guesses from a partially copied directory. Retention keeps
the active, prior-known-good, and every nonterminal transaction generation even
when old unreferenced generations are pruned. A durable highest-accepted
published-version floor remains in force even after an explicit runtime
rollback.

Git is the sole source of truth for normal Stemcell releases. The updater lists
the canonical repository's tags, considers only exact stable
`vMAJOR.MINOR.PATCH` tags, and numerically selects the highest version. It never
uses `main`, timestamps, prereleases, GitHub's separate “latest release” object,
or an older fallback tag.

The selected tag is pinned to both its advertised tag object and peeled commit.
The updater fetches that exact ref, detects a tag that moves during the fetch,
requires `silicon.info.version` to match the tag, requires an immutable runtime
digest, and seals the downloaded tree into a deterministic SHA-256-verified
artifact. Candidate Stemcell code is never executed by the updater. A durable
SemVer floor rejects downgrades and detects a rewritten tag that reuses an
accepted version with different content.

Published stable tags are immutable release records: protect the `v*` tag
namespace in GitHub and never force-push, move, or reuse one. Build the final
runtime first, commit its digest and matching version in `silicon.info`, and
only then create and push the tag.

## Implementation notes

- The PyPI package provides the `silicon` console entry point.
- The auto-restart watchdog runs as `silicon _watchdog` (a detached supervisor
  process) with crash-loop detection and `.silicon.stop`,
  `.silicon.pid`, and `.silicon.log` state.
- `silicon script update` runs
  `python -m pip install --upgrade silicon-cli` with the interpreter that owns
  the current command.
- Everything (server, stemcell repo) is env-overridable.
