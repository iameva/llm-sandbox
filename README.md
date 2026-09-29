# LLM sandbox

Run coding agents in a disposable QEMU virtual machine with the current project mounted at `/workspace`. Agent settings and credentials persist under `~/.config/llm-sandbox` on the host. Agents can modify the mounted project and their own mounted configuration; every other change inside the VM is discarded when it exits.

## Set up and update

Run these commands on the **host**, from this repository, as your normal user. You need Linux with KVM access, QEMU (`qemu-system-x86_64`, `qemu-img`), virtiofsd with `--allow-mmap` support, an ISO maker (genisoimage, xorrisofs or mkisofs), GNU coreutils, Bash and Python 3. Add `~/.local/bin` to your PATH.

```sh
sh install.sh
,sandbox-image configure --source-disk /path/to/verified-fedora-44-cloud.qcow2
,sandbox-image update --check
,sandbox-image update --allow-downloads
```

`install.sh` installs the launchers, the image manager and the runtime; it never builds or downloads an image. `,sandbox-image update` builds a new image in an isolated build VM, boots it to check every agent, tool and both browsers, then selects it for future launches. Running VMs keep their base. Rerun `install.sh` after pulling changes, and `update` to refresh agents and tools. See the [image lifecycle](qemu/IMAGE_LIFECYCLE.md) for configuration, staged activation and rollback.

The image holds Claude, Codex, Pi, OMP and OpenCode at their current releases, plus Rust stable (with `rust-src`), Go, Node and npm, zsh, Neovim, Vim and the other tools listed in `qemu/guest_verify.py`. Playwright's Firefox, Chromium and WebKit live read-only in `/opt/ms-playwright` (`PLAYWRIGHT_BROWSERS_PATH`). WebKit is Playwright's Ubuntu 24.04 build, so the image adds pinned Ubuntu copies of ICU 74, libjpeg8 and libx264 that Fedora lacks; a project pinned to another Playwright version should point that variable under `/workspace`. The Playwright version is pinned in `qemu/provision_agents.py`, because each release expects one browser build. Packages installed during a session disappear with its VM; change the recipe and rebuild to keep them.

`install.sh` backs up a differing installed allowlist to `egress-allowlist.txt.bak` before replacing it. Each launch loads the allowlist when it starts.

## Run an agent

Change into the project you want to work on, then run an installed entry point:

```sh
,claude-sandbox.sh
,codex-sandbox.sh
,opencode-sandbox.sh
,pi-sandbox.sh
,omp-sandbox.sh
```

Missing configuration directories are created on first launch. Authenticate from inside the appropriate sandbox using the agent's own login flow. Codex configuration is mounted at `/home/fedora/.config/codex`, separate from its executable installation. Claude's global JSON configuration lives inside its mounted directory so file replacement is atomic; a legacy configuration is copied only when the new file is absent or empty.

`,deepseek-claude-code.sh` is an alias for Claude with the DeepSeek backend. It shares Claude's configuration and sessions, and reads the DeepSeek key from `DEEPSEEK_API_KEY` or the host file `~/.config/deepseek.api`.

Every agent supports `--shell` as its first argument. `,llm-sandbox.sh` opens a shell without agent credentials; use `SANDBOX_LLM_AGENTS=claude,codex,opencode` to opt into those mounts. Additional arguments are passed through unchanged.

```sh
,codex-sandbox.sh --shell
SANDBOX_DRY_RUN=1 ,codex-sandbox.sh --resume
```

Dry runs print the runtime arguments without loading backend API keys. Credentials supplied by the backend selector are passed to the runtime by environment variable name, not embedded in its arguments.

## Switch backends and resume sessions

Choose the harness with the command name and the backend with `--backend`:

```sh
,claude-sandbox.sh --backend deepseek --resume
,codex-sandbox.sh --backend deepseek resume
,pi-sandbox.sh --backend deepseek --resume
,omp-sandbox.sh --backend deepseek --resume
,opencode-sandbox.sh --backend deepseek --continue
```

Sessions belong to the **harness**, not to the backend. Every backend uses that harness's existing mounts. For example, start with Claude, then use its resume picker with DeepSeek:

```sh
,claude-sandbox.sh --backend claude
,claude-sandbox.sh --backend deepseek --resume
,claude-sandbox.sh --backend claude --resume
```

For a particular Codex session, pass its ID: `,codex-sandbox.sh --backend deepseek resume SESSION_ID`. Native pickers may filter by provider; changing the provider does not move or rewrite the session files. These wrappers preserve session storage, but a harness/provider can still reject history containing unsupported content or tools. They do not convert sessions between different harnesses.

On the first Claude harness launch after updating, old files under `deepseek-claude/projects` are copied into `claude/projects`. Existing shared files win collisions, originals remain untouched, and the launch reports the counts. Stop old sessions before this one-time import so their files are complete. If both stores contain different versions of the same session ID, the legacy version remains in `deepseek-claude/projects` for manual recovery; histories are not merged. `,copy-session.sh` now explains the shared-session workflow.

### Built-in backends

| Backend | Authentication | Harnesses |
| --- | --- | --- |
| `claude` | Existing Claude login | Claude Code |
| `chatgpt` | Each harness's existing ChatGPT login | Codex, Pi, OMP, OpenCode |
| `openai` | `OPENAI_API_KEY` or `~/.config/openai.api` | Codex, Pi, OMP, OpenCode, Aider; Claude Code needs a gateway |
| `deepseek` | `DEEPSEEK_API_KEY` or `~/.config/deepseek.api` | Claude Code, Codex, Pi, OMP, OpenCode, Aider |

An explicit API-key backend requires its key and never silently falls back to a subscription. For `chatgpt`, use each harness's own login flow: Codex `login`, Pi/OMP `/login`, or OpenCode `/connect`. The wrapper does not copy OAuth tokens between harnesses. Mounted harness configuration can still contain credentials for other providers; this feature selects runtime authentication, not a credential isolation boundary.

DeepSeek defaults to `deepseek-v4-pro` with `deepseek-v4-flash` for fast tasks. Claude defaults to the `opus` alias. The installed ChatGPT profile uses `gpt-6-astra` for Codex and `gpt-5.6-sol` for Pi, OMP, and OpenCode, with `gpt-5.6-luna` for fast tasks, using subscription login without an OpenAI API key. Model access depends on your account; override the selection with `--model MODEL_ID`. These are the current coding models described in the [official model guide](https://learn.chatgpt.com/docs/models). Pi forwards a model only when you give one; without `--model` it keeps Pi's own model state, so a resumed session restores the model it last used and new sessions use Pi's saved default. Every named backend other than Pi must resolve to an explicit model, including on resume, so a session cannot silently restore a model from the previous provider. No model catalog or software version is pinned by these profiles.

```sh
,pi-sandbox.sh --backend chatgpt --resume                       # restores the session's model
,pi-sandbox.sh --backend chatgpt --model gpt-5.6-luna --resume  # this run uses gpt-5.6-luna
,omp-sandbox.sh --backend chatgpt --model gpt-5.6-luna --resume
,claude-sandbox.sh --backend deepseek --model deepseek-v4-flash --resume
```

Put wrapper options before harness-specific arguments or subcommands. Parsing stops at the first other argument, so prompts and native options remain intact. Both `--backend=NAME` and `--model=ID` work. `SANDBOX_BACKEND` and `SANDBOX_MODEL` provide environment defaults; command-line options take precedence. Without a backend selection or configured default, existing harness behavior is preserved.

### Saved profiles

On the host, `./install.sh` creates `~/.config/llm-sandbox/backends.json` from `backends.example.json` if it is missing. Reinstalling preserves existing profiles. To apply these defaults to an existing configuration, edit that file using the example below.

Codex, Pi, OMP, and OpenCode default to ChatGPT login. Claude Code defaults to Claude login, and Aider defaults to DeepSeek because its adapter requires an API key. DeepSeek remains selectable with `--backend deepseek` in every supported harness. Sign in to ChatGPT separately through each harness; no OpenAI API key is needed for this profile.

```json
{
  "defaults": {
    "claude": "claude",
    "codex": "chatgpt",
    "pi": "chatgpt",
    "omp": "chatgpt",
    "opencode": "chatgpt"
  },
  "profiles": {
    "claude": {
      "provider": "anthropic",
      "auth": "login",
      "model": "opus",
      "fast_model": "haiku"
    },
    "chatgpt": {
      "provider": "openai",
      "auth": "login",
      "model": "gpt-5.6-sol",
      "fast_model": "gpt-5.6-luna",
      "harnesses": {
        "codex": {
          "model": "gpt-6-astra"
        }
      }
    },
    "deepseek": {
      "provider": "deepseek",
      "auth": "api_key",
      "model": "deepseek-v4-pro",
      "fast_model": "deepseek-v4-flash",
      "key_env": "DEEPSEEK_API_KEY",
      "key_file": "~/.config/deepseek.api"
    }
  }
}
```

A profile can set `provider` (`anthropic`, `openai`, or `deepseek`), `auth` (`login` or `api_key`), `model`, `fast_model`, `key_env`, `key_file`, `base_url`, and `anthropic_base_url`. Built-in profiles supply their defaults; new profile names must supply their provider, authentication, and credential source. `harnesses` provides per-harness overrides. Selection order is command line, environment, then the harness entry in `defaults`; model selection follows command line, environment, harness override, profile default. Pi is the exception for the `chatgpt` and `openai` backends: the wrapper forwards `--model` only when you set one, and otherwise leaves model selection to Pi's own saved default and session state.

`key_env` names a host environment variable. If it is empty or unset, the helper reads `key_file`. Keep secrets out of the JSON itself. `SANDBOX_BACKENDS_FILE` selects an alternate host configuration file. Login profiles cannot override endpoints.

Fast models are mapped to Claude's Haiku/subagent settings, OMP's fast role, OpenCode's small model, and Aider's weak model. OMP's slow and planning roles use the selected main model. Pi ignores `fast_model`; for the `chatgpt` and `openai` backends it also keeps its own model selection unless `--model` or `SANDBOX_MODEL` is given. Explicit agent definitions or native options can override these harness defaults.

### GPT through Claude Code

This combination requires a separately configured gateway that exposes the Anthropic Messages API and translates requests to GPT. The wrapper does not install a gateway or convert API protocols. Set a profile like this, with real values:

```json
{
  "profiles": {
    "gpt-gateway": {
      "provider": "openai",
      "auth": "api_key",
      "model": "YOUR_GATEWAY_MODEL_ID",
      "anthropic_base_url": "https://YOUR_GATEWAY_HOST",
      "key_env": "LLM_GATEWAY_API_KEY"
    }
  }
}
```

Then run `,claude-sandbox.sh --backend gpt-gateway --resume`. The credential is the gateway credential, which need not be your OpenAI API key. The gateway must be a public HTTPS host on the egress allowlist. The proxy refuses private and loopback destinations, so a gateway running on the host is not reachable from the VM. A ChatGPT login is not a substitute for this gateway configuration.

The direct DeepSeek adapters use its [Anthropic-compatible endpoint](https://api-docs.deepseek.com/guides/anthropic_api/) for Claude Code and its [Responses endpoint](https://api-docs.deepseek.com/guides/responses_api/) for Codex. Pi and OMP load a temporary provider registration from a read-only mounted extension; no model configuration files or session paths are replaced. DeepSeek's [OMP integration guide](https://api-docs.deepseek.com/quick_start/agent_integrations/oh_my_pi/) describes the tool-call compatibility fields used here. Registered custom models currently use zero cost metadata, so harness cost estimates are not a billing estimate. Check the provider's usage dashboard for actual charges.

## Network policy

Each launch starts its own egress proxy in enforce mode. The VM has QEMU user networking with `restrict=on`: its only route out is that proxy, which accepts HTTPS `CONNECT` to hosts on the allowlist and refuses everything else, including private destinations. `SANDBOX_ALLOW_FILE` selects the allowlist; the default is the installed `~/.config/llm-sandbox/egress-allowlist.txt`. Restart a run to load allowlist changes. The proxy never intercepts TLS.

Each run records proxy decisions in `sandbox.process-decisions.jsonl` in its run directory. Keep it with `SANDBOX_QEMU_KEEP_ARTIFACTS=1`, then draft a candidate allowlist from it:

```sh
,egress-proxy.py --summarize --log ~/.cache/llm-sandbox/qemu/run-XXXX/sandbox.process-decisions.jsonl
```

Only events recorded after a successful upstream connection become active entries. Review the result before replacing the repository copy.

## Verification

Run local regression tests from this repository; they start no VM and need no credentials or public network:

```sh
python3 -B -m unittest discover -s tests -v
python3 -B -m unittest discover -s qemu -v
python3 -B -m unittest discover -s prototypes/qemu -v
```

With Pi and OMP installed, an optional test sends requests only to a local fake API with dummy credentials:

```sh
SANDBOX_NATIVE_TESTS=1 python3 -B -m unittest discover -s tests -p test_native_adapters.py -v
```

`./test-argv.sh` runs the launcher subset: ordered arguments, installed entry points, fresh configuration, backend selection, shared session paths, legacy imports, credential handling and error propagation.

On the host, run each agent's `--check`. It boots that agent's configuration and checks identity, state access, SQLite WAL access, proxy denial and direct TCP failures. It is a smoke check, not a certification of isolation.

```sh
,codex-sandbox.sh --check
,claude-sandbox.sh --check
,llm-sandbox.sh --shell -c 'browser-smoke.mjs /workspace'
```

Inspect the browser screenshots for missing glyphs: only the fullwidth plus should draw as a box.

`legacy/` holds the plans and host probes from the retired Podman workflow; they record history, not current guarantees.

## QEMU runtime

`,sandbox-image` keeps the image selection in `~/.config/llm-sandbox/qemu.json`, and `SANDBOX_QEMU_DISK` overrides it for one run. The builder publishes a read-only `agents.qcow2` after its structure check, SHA-256 digest and manifest. The launcher rejects any base image with owner, group or other write permission; it does not rehash the image on every launch. File permissions prevent accidental writes, but do not prove provenance or stop the owner from changing permissions.

The current directory becomes /workspace. Two Codex commands can run concurrently in the same
directory: each gets an independent disposable disk overlay, QEMU network stack
and enforcing proxy, while both intentionally share project files and
Codex state. Workspace and shared agent-state changes persist. Other guest
disk changes are discarded.

State exposure follows the chosen launcher. For example, Codex shares codex
and orca, Claude shares claude, and OpenCode shares opencode and local-opencode.
The shell launcher shares no agent state unless SANDBOX_LLM_AGENTS selects it.
This is live sharing of sandbox-specific state, not a mount of your whole
home. Tools can read and modify all selected state. Concurrent edits and
credential refresh behavior still need acceptance testing with real agents.

Codex's SQLite databases use a private guest directory through
`CODEX_SQLITE_HOME=/var/lib/llm-sandbox/codex-sqlite`. Config, credentials and
session files remain shared, and existing host databases are left untouched.
SQLite state is discarded with each launch's overlay. Separate databases do
not coordinate background work across VMs; concurrent updates to the remaining
shared Codex files have not been validated. Remove any explicit `sqlite_home`
setting in Codex config, which otherwise overrides this environment variable.

Launches use no host firewall changes or sudo, and never download or build
an image implicitly.

The QEMU builder installs Pi with npm under `~/.local`, outside its shared
`~/.pi` state directory. OMP is installed directly at `~/.local/bin/omp`,
outside shared `~/.omp` state. Image activation checks both agents with empty
state mounts to catch installations hidden during normal launches. If either
agent fails with `No such file or directory` after an image update, run
`sh install.sh` from the updated repository, then
`,sandbox-image update --allow-downloads` to rebuild with this layout.

OMP's `SQLITE_IOERR_SHMMAP` error is a separate runtime storage problem:
its database needs WAL shared-memory mapping. The launcher now enables
virtiofsd's `--allow-mmap` only for shared OMP state and holds an exclusive
host lock until the VM and its state helper stop. A second OMP sandbox using
the same state fails with a message to close the first. Other agents can
still run concurrently. Credentials, settings and history stay in the same
persistent host directory; there is no temporary copy or fork.

Install this runtime fix with `sh install.sh`, then run
`,omp-sandbox.sh --check` followed by `,omp-sandbox.sh`. No image rebuild is
needed for this error. Host virtiofsd must support `--allow-mmap`; the launcher
checks this before boot. SQLite WAL is tested on the shared filesystem before
OMP starts and during image activation. Do not run host OMP, an older launcher,
or another tool against this state while the sandbox is running: those tools
do not honor the launcher lock. Stop old OMP sandboxes before using the fix.

No API keys are printed in dry-run output. Explicit backend
secrets passed as environment values travel in the private seed ISO, removed
on normal exit; a killed launcher can leave private temporary artifacts.

Use --shell for a shell with the selected agent's state, or --check for
the guest smoke check described under Verification; it is not a replacement
for the controlled host boundary suite.

After changing launch wiring, run the installed-launcher acceptance:

```sh
python3 prototypes/qemu/accept_launcher.py \
  --disk "$(,sandbox-image path)"
```

It uses a fresh temporary home, project and Codex state, installs only into
that temporary home, and starts two simultaneous launcher instances (4 GiB
total RAM). It verifies shared state, private VM homes, the ability to bind
the same guest port, host file ownership, and one VM surviving the other's
exit. With --runtime-checks it also verifies terminal sizing and live resize;
it checks explicit overlay selection and cleanup in every run. It performs no agent login and exports no real project or credentials.

Remaining host gates: the acceptance command above; launcher --check; two
authenticated instances of the same agent streaming concurrently from the
same project; login persistence; Git/file-watching workflows; and terminal
closure/helper-failure cleanup. The earlier QEMU boundary suite covers
filesystem socket isolation, controlled direct-network probes, DNS capture
and actual proxy-process crashes. Repeat its key checks if launch network or
filesystem wiring changes.

### QEMU runtime controls

Supported runtime and image-build code lives in `qemu/`; `prototypes/qemu/`
contains source-image preparation and acceptance tools, plus compatibility
entry points.
Reinstall with `sh install.sh` after runtime changes. No image rebuild is needed.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SANDBOX_QEMU_CONFIG` | `~/.config/llm-sandbox/qemu.json` | Image source, store and active/previous versions |
| `SANDBOX_QEMU_DISK` | Configured active image | Explicit base image override |
| `SANDBOX_QEMU_MEMORY_MIB` | `8192` | Guest RAM in MiB (512–1048576); the guest's `/tmp` tmpfs shares it |
| `SANDBOX_QEMU_CPUS` | `4` | Guest vCPUs (1–1024) |
| `SANDBOX_QEMU_BOOT_TIMEOUT` | `600` | Seconds to guest readiness |
| `SANDBOX_QEMU_BATCH_TIMEOUT` | `0` | Command seconds after readiness; zero means unlimited for ordinary batch runs |
| `SANDBOX_QEMU_IDLE_TIMEOUT` | `0` | Established proxy tunnel inactivity seconds; zero keeps idle tunnels open |
| `SANDBOX_QEMU_CACHE_DIR` | `$XDG_CACHE_HOME/llm-sandbox/qemu` | Private disk-backed storage; defaults to `~/.cache/llm-sandbox/qemu` when XDG_CACHE_HOME is unset |
| `SANDBOX_QEMU_KEEP_ARTIFACTS` | `0` | Set to 1 to retain diagnostics after successful runs |

Timeout settings accept up to 604800 seconds; the boot timeout must be positive.
Verification commands default to a 900-second command deadline. The memory
backend size follows the RAM setting. No arbitrary QEMU arguments are accepted.

The runtime creates one qcow2 overlay per launch, backed by the selected image.
Keep that base image unchanged while any VM is running. Storage must be outside
all guest exports and cannot be tmpfs or ramfs. Short Unix socket paths remain
in a separate private temporary directory, which is removed on exit.

Successful runs remove their artifacts by default. Failed runs and explicit
retention keep only diagnostics: overlays and seed files are removed, guest
reports are extracted, and each retained file is capped at its last 1 MiB.
On launch and exit, completed diagnostics older than seven days or beyond the
five newest runs are pruned. Logs can contain terminal output; retained directories
are private to the host user. SIGKILL or host failure can leave incomplete run
artifacts; these are not automatically deleted because child processes may
still be using them.

Boot and shutdown messages go to `machine.log`; the CLI uses a separate serial
terminal, with its output recorded in `console.log`. Interactive launches show
`Starting sandbox…` until the guest is ready. Shutdown leaves the CLI output
visible. Both logs follow the run artifact retention policy; use
`--keep-artifacts` when invoking `qemu/sandbox.py` to retain successful run logs.

Terminal dimensions are applied before the agent starts and checked once per
second for host resizes. Pixel dimensions are ignored when comparing row/column sizes. The size record carries only rows and columns; there is no
additional network channel. Established tunnels have no idle deadline by
default; initial proxy connections still have bounded timeouts.

The normal filesystem cache policy remains `never`. To compare performance,
run the disposable acceptance tool twice:

```sh
python3 prototypes/qemu/accept_launcher.py --runtime-checks --cache-benchmark never \
  --disk "$SANDBOX_QEMU_DISK"
python3 prototypes/qemu/accept_launcher.py --runtime-checks --cache-benchmark auto \
  --disk "$SANDBOX_QEMU_DISK"
```

Only the temporary installed runtime uses the requested benchmark policy.
The probes time three scans of 500 files per VM and check visibility of a host
file replacement while both guests are active. These are small benchmarks,
not a certification of cache coherence for every application. They use no
credentials, public downloads or real workspace exports. Acceptance artifacts
are retained under `~/.cache/llm-sandbox/qemu-acceptance` for manual review.
Add `--long-batch-seconds 960` to test a batch command beyond the old fifteen-minute
limit; this deliberately adds sixteen minutes to the run.

### Unattended runs

An unlimited batch command can stay alive after readiness if an agent waits
for input or a tool hangs. For unattended work, choose a finite deadline, for
example one hour after readiness:

```sh
SANDBOX_BATCH=1 SANDBOX_QEMU_BATCH_TIMEOUT=3600 ,codex-sandbox.sh exec 'your task'
```

The separate boot deadline still applies before readiness. Ordinary batch
runs remain unlimited by default; interactive sessions have no command deadline.

For the manually recovered image used during development, a successful
`qemu-img check` was reported, but the build did not publish a final manifest.
It is no longer a quickstart example. If retaining that recovered base, stop
its VMs, verify the image, and remove its write permissions before launching
again. Making it read-only does not retroactively complete image publication.
