# LLM sandbox

Run coding agents in a rootless Podman container with the current project mounted at `/workspace`. Agent settings and credentials persist under `~/.config/llm-sandbox` on the host. Agents can modify the mounted project and their own mounted configuration.

## Set up and update

Run these commands on the **host**, from this repository. You need Linux, rootless Podman with pasta networking, Bash, and Python 3 for the proxy and tests. Add `~/.local/bin` to your PATH.

```sh
./build.sh --no-cache
./install.sh
```

Agent installers intentionally fetch current releases. Rebuild with `--no-cache` to update them; `install.sh` updates host launchers and the allowlist, not binaries inside an existing image. Rebuilding does not change running containers. The Fedora base digest and Playwright version remain explicit in `Containerfile`; update Playwright and its baked browser downloads together.

`install.sh` backs up a differing installed allowlist to `egress-allowlist.txt.bak` before replacing it. Restart a running proxy after updating its code or allowlist.

## Run an agent

Change into the project you want to work on, then run an installed entry point:

```sh
,claude-sandbox.sh
,codex-sandbox.sh
,opencode-sandbox.sh
,aider-sandbox.sh
,pi-sandbox.sh
,omp-sandbox.sh
```

Missing configuration directories are created on first launch. Authenticate from inside the appropriate sandbox using the agent's own login flow. Codex configuration is mounted at `/home/appuser/.config/codex`, separate from its executable installation. Claude's global JSON configuration lives inside its mounted directory so file replacement is atomic; a legacy configuration is copied only when the new file is absent or empty.

`,deepseek-claude-code.sh` is an alias for Claude with the DeepSeek backend. It shares Claude's configuration and sessions, and reads the DeepSeek key from `DEEPSEEK_API_KEY` or the host file `~/.config/deepseek.api`.

Every agent supports `--shell` as its first argument. `,llm-sandbox.sh` opens a shell without agent credentials; use `SANDBOX_LLM_AGENTS=claude,codex,opencode` to opt into those mounts. Additional arguments are passed through unchanged.

```sh
,codex-sandbox.sh --shell
SANDBOX_DRY_RUN=1 ,codex-sandbox.sh --resume
```

Dry runs print command arguments without loading backend API keys. Credentials supplied by the backend selector are passed to Podman by environment variable name, not embedded in its arguments.

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

DeepSeek defaults to `deepseek-v4-pro` with `deepseek-v4-flash` for fast tasks. Claude defaults to the `opus` alias. The installed ChatGPT profile uses `gpt-6-astra` for Codex and `gpt-5.6-sol` for Pi, OMP, and OpenCode, with `gpt-5.6-luna` for fast tasks, using subscription login without an OpenAI API key. Model access depends on your account; override the selection with `--model MODEL_ID`. These are the current coding models described in the [official model guide](https://learn.chatgpt.com/docs/models). Every named backend must resolve to an explicit model, including on resume, so a session cannot silently restore a model from the previous provider. No model catalog or software version is pinned by these profiles.

```sh
,pi-sandbox.sh --backend chatgpt --resume
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
    "opencode": "chatgpt",
    "aider": "deepseek"
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

A profile can set `provider` (`anthropic`, `openai`, or `deepseek`), `auth` (`login` or `api_key`), `model`, `fast_model`, `key_env`, `key_file`, `base_url`, and `anthropic_base_url`. Built-in profiles supply their defaults; new profile names must supply their provider, authentication, and credential source. `harnesses` provides per-harness overrides. Selection order is command line, environment, then the harness entry in `defaults`; model selection follows command line, environment, harness override, profile default.

`key_env` names a host environment variable. If it is empty or unset, the helper reads `key_file`. Keep secrets out of the JSON itself. `SANDBOX_BACKENDS_FILE` selects an alternate host configuration file. Login profiles cannot override endpoints.

Fast models are mapped to Claude's Haiku/subagent settings, OMP's fast role, OpenCode's small model, and Aider's weak model. OMP's slow and planning roles use the selected main model. Pi uses its selected main model. Explicit agent definitions or native options can override these harness defaults.

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

Then run `,claude-sandbox.sh --backend gpt-gateway --resume`. The credential is the gateway credential, which need not be your OpenAI API key. The gateway must be reachable from the container. A host-loopback gateway needs its own forwarding setup; the egress proxy's port forwarding does not forward an additional gateway port. The egress allowlist may also need the gateway hostname. A ChatGPT login is not a substitute for this gateway configuration.

The direct DeepSeek adapters use its [Anthropic-compatible endpoint](https://api-docs.deepseek.com/guides/anthropic_api/) for Claude Code and its [Responses endpoint](https://api-docs.deepseek.com/guides/responses_api/) for Codex. Pi and OMP load a temporary provider registration from a read-only mounted extension; no model configuration files or session paths are replaced. DeepSeek's [OMP integration guide](https://api-docs.deepseek.com/quick_start/agent_integrations/oh_my_pi/) describes the tool-call compatibility fields used here. Registered custom models currently use zero cost metadata, so harness cost estimates are not a billing estimate. Check the provider's usage dashboard for actual charges.

## Network policy

The runner uses pasta networking. Setting `SANDBOX_PROXY` configures cooperating clients to use the proxy; **it does not block direct connections or direct DNS traffic**. The proxy's enforce mode filters only traffic sent through that proxy. An external network boundary may impose additional restrictions, but the runner does not establish or verify those restrictions.

The former `SANDBOX_CONFINE=1` implementation relied on rootless systemd scope filtering that the repository records as ineffective. It now exits before starting a container. `SANDBOX_CONFINE_ACK` does not override that failure. Leave confinement unset only when advisory proxying is acceptable; this repository currently has no supported enforced egress mode for the whole sandbox.

To use the proxy, start it on the host:

```sh
,egress-proxy.py --mode enforce --listen 127.0.0.1:8080
```

In another host terminal:

```sh
SANDBOX_PROXY=127.0.0.1:8080 ,claude-sandbox.sh
SANDBOX_PROXY=127.0.0.1:8080 ,claude-sandbox.sh --check
```

Container mode forwards that loopback port through pasta. A loopback proxy is not reachable through this forwarding mechanism in gVisor mode; use an appropriately restricted address reachable from that sandbox instead.

For discovery, run the proxy with `--mode log` and checks with `SANDBOX_PROXY_MODE=log`. Review a candidate allowlist before replacing the repository copy:

```sh
,egress-proxy.py --summarize > candidate-allowlist.txt
```

Only events recorded after a successful upstream connection become active entries. Older `allow` and `allow-unlisted` events remain commented out because they did not prove connection success. The proxy rejects private destinations even in log mode.

## Isolation modes and verification

| Mode | Status |
| --- | --- |
| `container` | Default rootless Podman container; shares the host kernel. |
| `gvisor` | Requires runsc; disables SELinux labeling for runtime compatibility. Test on the host. |
| `vm` | Experimental and known to have ownership and mount problems with krun. Not a supported daily-use mode. |

The runner's comments describe additional environment settings, including runtime paths, temporary storage, and SELinux mount labels. `vm-migration-plan.md` contains historical experiments, not current guarantees.

Run local regression tests from this repository; these need no Podman, credentials, or public network:

```sh
python3 -B -m unittest discover -s tests -v
```

With Pi and OMP installed, an optional test sends requests only to a local fake API with dummy credentials:

```sh
SANDBOX_NATIVE_TESTS=1 python3 -B -m unittest discover -s tests -p test_native_adapters.py -v
```

`./test-argv.sh` runs the launcher subset. The tests check ordered arguments, installed entry points, fresh configuration, backend selection, shared session paths, legacy imports, credential handling, error propagation, proxy policy responses, and successful-connection logging.

On the host, after rebuilding and installing, run each agent's `--check`. It checks mounts, ownership, and proxy responses. It explicitly reports network policy as advisory and does not certify egress confinement.

```sh
,codex-sandbox.sh --check
,claude-sandbox.sh --check
,opencode-sandbox.sh --shell -c 'opencode --version'
,llm-sandbox.sh --shell -c 'browser-smoke.mjs /workspace'
```

Inspect the generated browser screenshots for missing glyphs. The default image includes Firefox and Chromium; a custom image built with only one browser will fail the smoke test for the omitted browser.

## QEMU backend

QEMU is available as an opt-in isolation mode for Claude, Codex, Pi, OMP,
OpenCode and the agent shell. Install the launcher files with the existing
installer, then select the already built five-agent disk:

```sh
sh install.sh
export SANDBOX_ISOLATION=qemu
export SANDBOX_QEMU_DISK=/var/home/duve/qemu-agents-gtxjsoe5/building.qcow2
export SANDBOX_ALLOW_FILE="$PWD/egress-allowlist.txt"
,codex-sandbox.sh
```

The current directory becomes /workspace. The same backend/model arguments
and per-agent directories under ~/.config/llm-sandbox are used as by the
existing launchers. Two Codex commands can run concurrently in the same
directory: each gets an independent guest disk snapshot, QEMU network stack
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
SQLite state is discarded with each launch's snapshot. Separate databases do
not coordinate background work across VMs; concurrent updates to the remaining
shared Codex files have not been validated. Remove any explicit `sqlite_home`
setting in Codex config, which otherwise overrides this environment variable.

No Podman, pasta, host firewall changes, or sudo are used by QEMU launches.
QEMU, virtiofsd, KVM access and an ISO maker must be installed already.
The image is built separately with prototypes/qemu/build_image.py; launchers
never download or build an image implicitly. Rebuild to add tools to the
shared base image. Aider is not in the five-agent image and is rejected.

SANDBOX_PROXY does not select QEMU's proxy: every launch starts its own proxy
in enforce mode. SANDBOX_ALLOW_FILE selects its allowlist; the default is the
installed ~/.config/llm-sandbox/egress-allowlist.txt. Restart a run to load
allowlist changes. No API keys are printed in dry-run output. Explicit backend
secrets passed as environment values travel in the private seed ISO, removed
on normal exit; a killed launcher can leave private temporary artifacts.

Use --shell for a shell with the selected agent's state, or --check for
a noninteractive guest smoke check. --check tests identity, state access,
proxy denial and direct TCP failures; it is not a replacement for the
controlled host boundary suite.

Before making QEMU your default, run the new installed-launcher acceptance:

```sh
python3 prototypes/qemu/accept_launcher.py \
  --disk /var/home/duve/qemu-agents-gtxjsoe5/building.qcow2
```

It uses a fresh temporary home, project and Codex state, installs only into
that temporary home, and starts two simultaneous launcher instances (4 GiB
total RAM). It verifies shared state, private VM homes, the ability to bind
the same guest port, host file ownership, and one VM surviving the other's
exit. It performs no agent login and exports no real project or credentials.

Remaining host gates: the acceptance command above; launcher --check; two
authenticated instances of the same agent streaming concurrently from the
same project; login persistence; Git/file-watching workflows; and terminal
closure/helper-failure cleanup. The earlier QEMU boundary suite covers
filesystem socket isolation, controlled direct-network probes, DNS capture
and actual proxy-process crashes. Repeat its key checks if launch network or
filesystem wiring changes.
