# Manafish mainboard

This file overrides `firmware/AGENTS.md` (and its symlink `firmware/CLAUDE.md`)
on commits, tags, releases, CI and pre-commit.

## Repo map

| Path | What | Upstream |
|---|---|---|
| `hardware/` | KiCad: `mainboard/` (4-layer CM5 + RP2354A board), `magnetic_switch/` | — |
| `firmware/` | RP2354A MCU firmware: thrusters, IMU, control loop (C11, Pico SDK 2.3.1) | manafishrov/mcu-firmware |
| `software/` | CM5 software: Python 3.13 import package `rov_firmware` + NixOS SD image | manafishrov/firmware |

**Naming trap:** upstream's repo called "firmware" lives in `software/`. Keep its
import package `rov_firmware` (distribution name `manafish`) and its device path
`/home/pi/firmware`.

Port status: `software/flake.nix` still builds upstream's Raspberry Pi 3 image
(`pi3-imx477`) and bundles upstream's pico/pico2 UF2s; the CM5 configuration
and the `manafish_mainboard` MCU target are still to come.

## Owner rules

- Ask before every commit and every push, and before `git subtree add/pull`
  (they commit at once). Push only to `main`; there are no PRs.
- The owner runs every `sudo` (also on the device), jumper and hardware step.
  Give the exact command or action, then wait for the result.
- Never connect a board running a Manafish image to a LAN: it has static
  10.10.10.10 and a DHCP server on eth0. Use a direct cable.
- Prefer simple solutions. Report every pin remap. BOM changes are
  recommendations only.
- LTC4162, WSEN-PADS and SHT45 addresses, scales and bits come from the
  datasheet (LTC4162 also cross-checked against Linux `ltc4162-l-charger.c`),
  never from memory. Charger writes stay disabled until bring-up step B9,
  except the `force_telemetry_on` read-modify-write.
- Commit messages: plain capitalised sentences, imperative, no type prefix; the
  body says why.

## Quality gates

Run them by hand before every commit. Until Phase 1 adds the root workflows
there is no CI, and there is no pre-commit config: don't run `pre-commit install`
(a hook without a config fails every commit).

Firmware, from the repo root (the dev shell provides the LLVM 19 baseline; the
host clang-format formats differently, so never format with it):

```sh
nix develop ./firmware -c make -C firmware test format-check lint-check build-pico build-pico2
```

Software, from `software/`:

```sh
uv sync
uv run ruff format --check . && uv run ruff check --no-fix . && uv run ty check
PICO_CONTROLLER_SOURCE=$PWD/../firmware/src/control/controller.c uv run pytest
```

- Plain `ruff check` rewrites files (`fix = true`, `unsafe-fixes = true`).
- Without `PICO_CONTROLLER_SOURCE` the C differential test is skipped, not passed.
  A change under `firmware/src/control/` needs this software gate too.
- After Nix changes: `nix fmt -- --ci .`, `nix flake check`,
  `nix build --accept-flake-config --dry-run .#sdImage`.

## Dev loops

- Firmware: `cd firmware && nix develop`, then `make test`, `make build-pico2`.
- Software: `uv run tools` → "Run Mock Websocket Server Locally" for app work
  without hardware. On the device (direct cable), never overwrite its config:
  `rsync -rlt --delete --exclude __pycache__ --exclude config.json software/src/ pi@10.10.10.10:firmware/src/`,
  then the owner runs
  `ssh -t pi@10.10.10.10 'sudo systemctl restart manafish-firmware; journalctl -fu manafish-firmware'`.
  New Python dependencies need an image rebuild.

## Nix gotchas

- Flakes see only git-tracked files: `git add` new files and a changed
  `flake.lock` before any `nix` command.
- Two subdirectory flakes, no root flake: `nix develop ./firmware`, `./software`.
  Run `nix fmt` from `software/`.
- The image version is the monorepo `shortRev`, so any commit renames the image.
- New Python dependency: add it to `software/pyproject.toml` as `"name==0"` (the
  sync script only rewrites existing `name==` pins) and to the python-env in
  `software/nix/firmware.nix`. A nixpkgs package goes into `PACKAGES` in
  `software/sync-python-deps.sh`; a non-nixpkgs one needs a flake input, a
  `buildPythonPackage` and a `CUSTOM_*_PACKAGES` entry. Then, from `software/`,
  run `./sync-python-deps.sh && nix develop -c uv lock`, also after any
  nixpkgs/nixos-raspberrypi pin change. Never hand-edit the synced pins,
  including `requires-python`.
- Don't bump the nixos-raspberrypi pin before its cachix has the aarch64 builds.
- The MCU UF2 is never built in Nix: the software flake fetches it as a GitHub
  release asset. `releaseVersionFromLock` in `software/flake.nix` accepts only
  `/download/v…/` URLs until Phase 3.1 adapts it to `firmware-v…`.

## Tags and releases

Never tag or release unless asked; confirm the version and the notes first.
Upstream version strings are never edited. The release workflow
(`.github/workflows/firmware-release.yml`) and `make build-mainboard` arrive in
Phase 2.1; don't tag before they exist.

- MCU: `firmware-vX.Y.Z-mb.N`. X.Y.Z comes from the
  `MCU_FIRMWARE_VERSION_MAJOR/MINOR/PATCH` macros in `firmware/src/version.h`
  (not its `"…-rc.N"` fallback string); don't bump them. N counts mainboard
  releases of that X.Y.Z. **Never put `-rc` in it**: `version.py` rejects
  `X.Y.Z-rc.N-mb.N`. Assets: `mainboard-vX.Y.Z-mb.N.uf2` (the software flake URL
  depends on this name) and the BMI270 license; published, not a draft.
- Software: `software-v<pyproject version>-mb.N`, e.g.
  `software-v1.2.0-rc.3-mb.1`. Never write `-mb` into `pyproject.toml`.
- Push one tag at a time (`git push origin <tag>`), never `git push --tags`.
- Releases are GitHub release assets in this repo (no S3, no minisign).

## Upstream sync

Never fetch upstream tags: both upstreams have `v*` tags that trigger release
workflows. On a fresh clone add the remotes first:
`git remote add --no-tags upstream-mcu https://github.com/manafishrov/mcu-firmware.git`
and `git remote add --no-tags upstream-software https://github.com/manafishrov/firmware.git`.

```sh
git fetch upstream-mcu main
git subtree pull --prefix=firmware --squash upstream-mcu main -m "Update firmware/ to manafishrov/mcu-firmware <sha>"
git fetch upstream-software main
git subtree pull --prefix=software --squash upstream-software main -m "Update software/ to manafishrov/firmware <sha>"
```

- These paths were removed after the import; if a pull brings them back or
  conflicts on them, `git rm -r` them again: `firmware/.github software/.github
  firmware/.pre-commit-config.yaml software/.pre-commit-config.yaml
  software/tests/test_ci_coverage.py`.
- Keep the local patches on conflict: the Releases section of
  `firmware/AGENTS.md`, and `git status --porcelain -- .` in
  `firmware/CMakeLists.txt` and `firmware/tests/test_build_identity.py`.
- Then run both quality gates. Read removed upstream files with
  `git show upstream-mcu/main:<path>` or `git show upstream-software/main:<path>`.
