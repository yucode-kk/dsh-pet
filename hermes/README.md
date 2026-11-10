# dsh-pet for Hermes

A Hermes plugin that runs [dsh-pet](https://github.com/PC2005-cloud/dsh-pet) — a small
desktop pet — as a native Hermes companion.

**Upstream:** this repository is a fork of
[`PC2005-cloud/dsh-pet`](https://github.com/PC2005-cloud/dsh-pet) (the parent project — pet
runtime, animations, asset pipeline and the DSH plugin all originate there, MIT licensed,
copyright PC2005-cloud). This fork adds one thing: the `hermes/` subdirectory, which wraps
the same pet as a Hermes plugin. Everything outside `hermes/` belongs to upstream; a catalog
install copies only `hermes/`.

## What it does

- Starts a standalone dsh-pet window (a Node process + an Electron window) when the
  Hermes gateway starts, and stops it on unload.
- Maps Hermes activity to the pet's work status: session start → `thinking`,
  tool finished → `working` (the tool name goes into the pet's speech bubble),
  turn end → idle. A watchdog clears `thinking/working/result/waiting` back to idle
  after 120s without new events, so a background skill review can't leave the pet
  looping a work animation forever.
- Adds `hermes dsh-pet status|start|stop|uninstall` for day-to-day control.

Configuration (`plugins.entries.dsh-pet.settings`): `autostart` (default `true`),
`node_path`, `port` (default `3080`, next free port is used), and `dsh_pet_path`
(explicit path to a dsh-pet package; leave empty for the plugin-managed runtime).

## Requirements

- Node.js >= 22.12 and `npm` on `PATH` (the plugin installs/starts a Node runtime).
- Hermes >= 0.22.0.
- Windows 10/11 is verified; macOS and Linux are untested.

## Install

- **Catalog:** `hermes plugins install dsh-pet --enable`. The plugin directory contains
  only this adapter; the runtime is fetched on first start (see below).
- **From this repository:** run `scripts\hermes-plugin-install.ps1` (add `-Force` to
  update). That path copies `dsh-pet/` into the plugin directory, runs
  `npm install --omit=dev` there, and produces a self-contained ~160 MB install that
  works offline afterwards.

## Runtime fetching (disclosure)

- Catalog installs fetch a pinned runtime on first start. It is **not**
  `npm install dsh-pet@0.4.7`: the plugin ships a lockfile (`hermes/runtime-lock/`,
  `lockfileVersion` 3) pinning `dsh-pet@0.4.7` plus all 98 transitive packages to exact
  versions with `sha512` integrities resolved from `registry.npmjs.org`, and installs it with
  `npm ci --omit=dev --ignore-scripts --no-audit --no-fund` into
  `<HERMES_HOME>/plugin-data/dsh-pet/runtime` (~80 MB, one time). Upstream's own manifest uses
  `^` ranges, so `npm install` would resolve a fresh dependency tree on every first start;
  `npm ci` fails loudly if the lock and the manifest ever disagree. Both the version constant
  (`RUNTIME_VERSION`) and the lockfile are hard-coded and are not a self-updater — updates
  arrive through a catalog SHA bump in the Hermes repo plus `hermes plugins update`.
- The Node/Electron child gets a **minimal environment**, never the inherited one: `PATH`,
  `PATHEXT`, `COMSPEC`, `SYSTEMROOT`/`WINDIR`/`SYSTEMDRIVE`, `TEMP`/`TMP`/`TMPDIR`,
  `USERPROFILE`/`HOMEDRIVE`/`HOMEPATH`/`HOME`, `APPDATA`/`LOCALAPPDATA`/`PROGRAMDATA`,
  `PROGRAMFILES`/`PROGRAMFILES(X86)`, `NUMBER_OF_PROCESSORS`, `PROCESSOR_ARCHITECTURE`,
  `LANG`/`LC_ALL`, plus `DSH_HOME` and the explicit `DSH_PET_ELECTRON_PATH` /
  `DSH_PET_ELECTRON_MIRROR` opt-ins. Hermes provider keys (`OPENAI_API_KEY`, …) are never
  passed to code fetched at install time. `npm ci` gets the same base plus `NPM_CONFIG_*`
  (registry/proxy/cache are tool configuration, not credentials).
- dsh-pet itself downloads Electron (~350 MB) into
  `<HERMES_HOME>/plugin-data/dsh-pet/runtime-home/electron` on first launch, unless a
  local `%USERPROFILE%\.dsh\electron` (or `DSH_PET_ELECTRON_PATH`) is found and reused.

## Local state and network

- The pet listens on `127.0.0.1` only, on the first free port in `3080..3089`.
- Its data (settings, AI config, chat memory, todos, logs) lives under
  `<HERMES_HOME>/plugin-data/dsh-pet`, separate from a DSH install's `~/.dsh`.
- Electron's `userData` directory is shared with any DSH install of dsh-pet
  (`%APPDATA%\dsh-pet-electron-helper`); the adapter does not rewrite it.
- No telemetry. Model calls happen only if the user configures a provider in the pet's
  own settings page.
- Uninstalling the plugin removes the plugin directory; `hermes dsh-pet uninstall -y
  --purge` also deletes the data directory.

More detail (in Chinese): [`docs/hermes-plugin.md`](https://github.com/yucode-kk/dsh-pet/blob/main/docs/hermes-plugin.md).