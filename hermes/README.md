# dsh-pet for Hermes

A Hermes plugin that runs [dsh-pet](https://github.com/PC2005-cloud/dsh-pet) — a small
desktop pet — as a native Hermes companion. This repository is a fork of the upstream
dsh-pet project; the Hermes adapter lives in this `hermes/` subdirectory, and that is
what a catalog install copies.

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

- Catalog installs fetch a pinned runtime on first start:
  `npm install --omit=dev --prefix <HERMES_HOME>/plugin-data/dsh-pet/runtime dsh-pet@0.4.7`
  (~80 MB, one time). The version is hard-coded in `hermes_adapter/sidecar.py`
  (`RUNTIME_VERSION`); it is not a self-updater — updates arrive through a catalog
  SHA bump in the Hermes repo plus `hermes plugins update`.
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