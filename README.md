# hass-miner

> [!WARNING]
> **This branch is an experimental fork — `pos-ei-don`'s asic-rs `2.0.0-alphaN` line.**
> It is the [asic-rs rewrite](https://github.com/Schnitzel/hass-miner/pull/601) (by [@b-rowan](https://github.com/b-rowan)) **plus local adaptations**, run in production on real hardware (Antminer S19 Pro Hydro / VNish + S19k Pro / BraiinsOS). It is **not** the official `pyasic` release line and not on HACS — see *Installation (this fork)* below.

## About this fork (asic-rs alpha)

`pyasic` is deprecated; this line runs entirely on [256foundation/asic-rs](https://github.com/256foundation/asic-rs) (Rust) via a **VNish-patched wheel** built from [`pos-ei-don/asic-rs`](https://github.com/pos-ei-don/asic-rs).

> [!TIP]
> **Want a stable version? Use `beta7` (pyasic).** The `pyasic`-based **`v1.3.9-beta7`** line is the proven, stable one and runs rock-solid here — huge thanks to [@Schnitzel](https://github.com/Schnitzel) for the original integration and to **tntvlad** for keeping the beta line maintained. It lives on the [`main`](https://github.com/pos-ei-don/hass-miner/tree/main) branch here (archived) and as release `v1.3.9-beta7`. This `asic-rs` line is **experimental** by comparison.

### Why move to Rust (asic-rs)?

- **`pyasic` is deprecated** — its author moved development to `asic-rs`, so the pyasic line is a dead end long-term, however stable it is today.
- **Responsive, actively-maintained upstream** — asic-rs fixes land fast (our VNish hardware fix was merged within hours).
- **Structurally more robust** — much of the beta line's effort went into working *around* pyasic fragility (coordinator freezes, transient-failure handling, sensors flapping to 0). A typed, async Rust core removes a whole class of those by construction.
- **Faster / lighter** — no GIL, quicker scans and parsing, lower load on the HA host.

Trade-off today: asic-rs is earlier-stage (fewer features wired, VNish writes were a stub — hence the patches in this fork). So: **beta7 for stability now, this line for where it's heading.**

**What this fork adds on top of the upstream rewrite:**
- 0.6.0 adaptations so the rewrite runs against current `pyasic-rs`: `const.DOMAIN` lowercase, `DeviceInfo` via `model_dump()`, `number.py` on `TuningTarget`/`tuning_target.watts` (NUMBER platform re-enabled).
- An `OptionsFlow` to set the firmware web password post-setup (needed for the VNish unlock token).
- Pinned to the fork wheel (`pos-ei-don/asic-rs` release `wheels-vnish-*`, `cp314` / `musllinux_1_2` — Home Assistant Core is Alpine/musl).

**Upstream contributions from this work (in `256foundation/asic-rs`):**
- #277 — VNish model alias + per-board water temps *(merged)*
- #281 — weak `?` python features so firmwares actually gate in the build *(merged)*
- #282 — serialize `Duration` as `timedelta`, not `float` *(open)*
- #284 — implement `SetPowerLimit` for VNish (preset-based) *(open)*
- #285 — add `BoardData.chip_temperature`, populated for VNish *(open)*

### Installation (this fork)

**Not via HACS** — the domain is `miner`, which collides with the official `hass-miner`. Install manually:
1. Copy `custom_components/miner/` from this branch into your HA `config/custom_components/`.
2. The pinned wheel in `manifest.json` is `cp314`/`musllinux_1_2`/`x86_64` (HA OS). On a same-version wheel swap, force-reinstall it in the core venv (`pip install --force-reinstall --no-deps <wheel-url>`) and restart HA.
3. Add miners via the config flow; set the firmware web password via the integration's *Configure* (options) for VNish preset/throttle control.

Releases of this line are tagged `v2.0.0-alphaN` on **this fork only** (the official `pyasic` line lives on the upstream repo).

---

[![GitHub Release][releases-shield]][releases]
[![GitHub Activity][commits-shield]][commits]
[![License][license-shield]](LICENSE)

[![pre-commit][pre-commit-shield]][pre-commit]
[![Ruff][ruff-shield]][ruff]
[![Conventional Commits][conventional-commits-shield]][conventional-commits]

[![hacs][hacs-shield]][hacs]
[![Project Maintenance][maintenance1-shield]][user1_profile]
[![Project Maintenance][maintenance2-shield]][user2_profile]

Control and monitor your Bitcoin Miners from Home Assistant.

[![Add Integration to Home Assistant](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=miner)

Great for Heat Reusage, Solar Mining or any usecase where you don't need your miners running 24/7 or with a specific wattage.

Works great in coordination with [ESPHome](https://www.home-assistant.io/integrations/esphome/) for Sensors (like temperature) and [Grafana](https://github.com/hassio-addons/addon-grafana) for Dashboards.

### Support for:

- Antminers
- Whatsminers
- Avalonminers
- Auradine
- BitAxe
- Braiins Firmware
- Vnish Firmware
- ePIC Firmware
- LuxOS Firmware
- Mara Firmware

[Full list of supported miners](https://256foundation.github.io/asic-rs/supported-devices/#support-matrix).

**This component will set up the following platforms -**

| Platform | Description               |
| -------- | ------------------------- |
| `sensor` | Show info from miner API. |
| `number` | Set Power Limit of Miner. |
| `switch` | Switch Miner on and off   |

**This component will add the following services -**

| Service           | Description                          |
| ----------------- | ------------------------------------ |
| `reboot`          | Reboot a miner by IP                 |
| `restart_backend` | Restart the backend of a miner by IP |

## Installation

Use HACS, add the custom repo https://github.com/Schnitzel/hass-miner to it

[![Installation and usage Video](http://img.youtube.com/vi/eL83eYLbgQM/0.jpg)](https://www.youtube.com/watch?v=6HwSQag7NU8)

## Contributions are welcome!

If you want to contribute to this please read the [Contribution guidelines](CONTRIBUTING.md)

## Credits

This project was generated from [@oncleben31](https://github.com/oncleben31)'s [Home Assistant Custom Component Cookiecutter](https://github.com/oncleben31/cookiecutter-homeassistant-custom-component) template.

Code template was mainly taken from [@Ludeeus](https://github.com/ludeeus)'s [integration_blueprint][integration_blueprint] template.

Miner control and data is handled using [@256foundation](https://github.com/256foundation)'s [asic-rs](https://github.com/256foundation/asic-rs).

---

[//]: # "Links"
[integration_blueprint]: https://github.com/custom-components/integration_blueprint
[ruff]: https://github.com/astral-sh/ruff
[buymecoffee]: https://www.buymeacoffee.com/Schnitzel
[commits]: https://github.com/Schnitzel/hass-miner/commits/main
[conventional-commits]: https://conventionalcommits.org
[hacs]: https://hacs.xyz
[discord]: https://discord.gg/Qa5fW2R
[releases]: https://github.com/Schnitzel/hass-miner/releases
[user1_profile]: https://github.com/Schnitzel
[user2_profile]: https://github.com/b-rowan
[forum]: https://community.home-assistant.io/
[pre-commit]: https://github.com/pre-commit/pre-commit
[//]: # "Shields"
[ruff-shield]: https://img.shields.io/badge/-Ruff-D7FF64.svg?style=for-the-badge&color=orange
[buymecoffee-shield]: https://img.shields.io/badge/buy%20me%20a%20coffee-donate.svg?style=for-the-badge&color=orange
[commits-shield]: https://img.shields.io/github/commit-activity/y/Schnitzel/hass-miner.svg?style=for-the-badge&color=orange
[conventional-commits-shield]: https://img.shields.io/badge/Conventional%20Commits-1.0.0-orange?style=for-the-badge&color=orange
[hacs-shield]: https://img.shields.io/badge/HACS-Custom.svg?style=for-the-badge&color=orange
[discord-shield]: https://img.shields.io/discord/330944238910963714.svg?style=for-the-badge&color=orange
[forum-shield]: https://img.shields.io/badge/community-forum.svg?style=for-the-badge&color=orange
[license-shield]: https://img.shields.io/github/license/Schnitzel/hass-miner.svg?style=for-the-badge&color=orange
[maintenance1-shield]: https://img.shields.io/badge/maintainer-%40Schnitzel.svg?style=for-the-badge&color=orange
[maintenance2-shield]: https://img.shields.io/badge/maintainer-%40b--rowan.svg?style=for-the-badge&color=orange
[pre-commit-shield]: https://img.shields.io/badge/pre--commit-enabled?style=for-the-badge&color=orange
[releases-shield]: https://img.shields.io/github/release/Schnitzel/hass-miner.svg?style=for-the-badge&color=orange
[//]: # "Other"
[exampleimg]: example.png
