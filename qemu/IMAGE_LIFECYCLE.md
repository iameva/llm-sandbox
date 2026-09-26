# QEMU image lifecycle

The configuration selects a versioned base image for future launches. Each
launch resolves that path once and creates its own disposable overlay. Updating
the selection does not restart running VMs or change their base disk. Multiple
instances of the same agent can continue running from the same base.

The host command is `,sandbox-image`, installed by `sh install.sh`. From a
checkout, the equivalent is `python3 qemu/images.py`. Run it as your normal
user, without sudo. The required QEMU tools and KVM access must already exist.

## Configure once

The default file is `~/.config/llm-sandbox/qemu.json`. Reinstalling the launchers
preserves it. Use `SANDBOX_QEMU_CONFIG` to select another config for both the
manager and launchers, or `--config PATH` before a manager subcommand.

```sh
sh install.sh
,sandbox-image configure \
  --source-disk /var/home/duve/qemu-guest-nf9508pc/Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2
```

`configure` records paths; it does not download, boot or modify an image.
The source must be a verified, clean Fedora 44 cloud qcow2 image, such as the
one produced by `prototypes/qemu/prepare_guest.py`. Do not configure an agent
image or a disk containing credentials as the build source. The builder uses
Fedora 44 repositories; a Fedora major-version upgrade also needs a recipe
change and testing.

The default image store is `~/.local/share/llm-sandbox/images`. Set
`configure --source-disk PATH --store /disk/path` to choose another private,
disk-backed directory. Changing the store does not move existing images.

The manager maintains this structure; the paths below are examples:

```json
{
  "version": 1,
  "source_disk": "/disk/Fedora-Cloud-Base-Generic-44.qcow2",
  "image_store": "/home/user/.local/share/llm-sandbox/images",
  "active_image": "/disk/images/qemu-agents-NEW/agents.qcow2",
  "previous_image": "/disk/images/qemu-agents-OLD/agents.qcow2"
}
```

Keep active and previous selection changes in the manager so they go through
validation. Launchers check that the selected base has no write permission
bits; they do not hash a multi-GiB file on every launch.

## Adopt the recovered image

The recovered `building.qcow2` installed all five agents but never received
the normal publication manifest. It does not need another download or build:

```sh
,sandbox-image adopt /var/home/duve/qemu-agents-gtxjsoe5/building.qcow2
```

First stop any process that might be writing that source disk. Adoption makes
a full copy in a new store directory, checks the qcow2 structure, makes the
copy read-only, and boots it through a disposable overlay. It checks all five
agent versions and the launcher's identity, filesystem, SQLite and network
smoke checks. It then records the digest and observed versions in a manifest
and selects the published `agents.qcow2`. The original file and its permissions
remain unchanged. Adoption validates usability; it does not establish the
provenance of an arbitrary disk or remove secrets already stored in it.

Then use the configured selection:

```sh
unset SANDBOX_QEMU_DISK
export SANDBOX_ISOLATION=qemu
export SANDBOX_ALLOW_FILE="$PWD/egress-allowlist.txt"
,codex-sandbox.sh
```

Remove any old `export SANDBOX_QEMU_DISK=...` from your shell configuration
if you want future terminals to follow the managed selection. An explicit
`SANDBOX_QEMU_DISK` always overrides the config, which is useful for a separate
test run. `,sandbox-image path` prints the managed path, regardless of that
override.

## Update

```sh
,sandbox-image update --check
,sandbox-image update --allow-downloads
```

The first command only checks prerequisites and creates no files. The second:

1. Copies the configured clean source into a fresh build directory. It never
   modifies the source or an existing agent image.
2. Runs the current installation recipe in a build VM, permitting public HTTPS
   through its dedicated proxy. No project, host credentials or shared agent
   state are exported. Only a temporary report directory is shared.
3. Grows the copy to a 30G virtual disk, then installs the current releases
   of Claude, Codex, Pi, OMP and OpenCode and the toolchain the old container
   image carried: Rust stable with `rust-src`, Go, Node and npm, Playwright
   with Firefox and Chromium in `/opt/ms-playwright`, the browser libraries
   and symbol fonts, and the CLI tools listed in `qemu/guest_verify.py`.
   Agent and tool versions are recorded, not pinned in advance; Playwright is
   the exception, pinned in `qemu/provision_agents.py` because each release
   expects one browser build. Fedora dependencies come from the configured
   Fedora 44 repositories. The current recipe does not perform a full OS
   upgrade; configure a newer verified Fedora 44 cloud source when refreshing
   the underlying OS.
4. Waits for shutdown, runs `qemu-img check`, hashes the image, writes the
   manifest and publishes `agents.qcow2` with mode `0400`.
5. Boots the candidate in a disposable run with a fresh empty workspace, no
   shared agent state and an enforcing proxy. All five version checks, every
   tool check, a launch of both browsers and the runtime smoke checks must
   pass, with agent versions matching the manifest. The browser screenshots
   stay in the printed boot-check directory under `workspace/browser-smoke`;
   look at them for boxes in place of symbol glyphs.
6. Rechecks the digest, then atomically replaces the configured active path
   and saves the previous selection for rollback.

A build, validation or boot failure leaves the previous selection in place.
Interrupted builds and failed candidates retain diagnostic files for review.
The builder requires at least 16 GiB free; it uses a full disk copy per build.
Only one image operation may run for a given config at a time. Ordinary
launches continue during builds and activation.

To separate building from selection:

```sh
,sandbox-image build --allow-downloads
# Use the candidate path printed by that command:
,sandbox-image activate /disk/images/qemu-agents-BUILD/agents.qcow2
```

`build` publishes without changing the selection. `activate` checks the
manifest, digest and qcow2 structure, then boots and tests before selecting.
It can also register an image published by a previous standalone builder.
An image's `0400` mode guards against accidental writes; its owner can still
change permissions. These checks do not defend against a compromised host user.

## Rollback and retention

```sh
,sandbox-image status
,sandbox-image list
,sandbox-image rollback
```

`status`, `list` and `path` are read-only. Rollback verifies and boots the
previous image before swapping the active and previous selections. To select
an older retained version, use `activate` with its path.

Images are never automatically deleted: running overlays can still refer to
older bases. Keep the active and previous images. Remove other version
directories manually only after all VMs using them have stopped. Build and
probe logs also remain in the store for review. Per-launch disposable overlays
are removed by the runtime on exit.

Rollback changes the software for new VMs. It does **not** undo edits to the
workspace or live shared agent configuration, credentials and session files.
An older agent may not understand a newer agent's state format. Back up that
state separately when needed. Codex's private SQLite databases remain per-VM.
Packages installed inside an ordinary sandbox disappear with its overlay;
change the build recipe and build a new image to retain additional tools.

The candidate smoke check establishes basic usability, not authenticated
agent behavior or complete network isolation. Changes to the runtime's
network or filesystem wiring still require the host boundary acceptance tests.
