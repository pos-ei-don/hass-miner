# Fork-Strategie & Divergenz-Ledger — `pos-ei-don/hass-miner`

Dieser Fork führt eine **eigene Linie** der HA-`miner`-Integration, basierend auf b-rowans
asic-rs-Rewrite. Diese Datei ist der eine lesbare Ort, der festhält **was wir selbst pflegen**
und **wie wir zu Upstream stehen** — damit wir, falls Upstream (b-rowan/Roman, Schnitzel) bei
unseren Einreichungen dauerhaft in eine für uns inakzeptable Richtung geht, **einen Dauer-Fork
betreiben und Änderungen selektiv in beide Richtungen integrieren** können.

> Stand: 2026-06-19. Lebt aktuell auf der Release-Linie `asic-rs-2.0-alphaNN` (neueste = `alpha22`).
> Beim Übergang auf eine eigenständige Linie wandert diese Datei mit und wird die Wurzel-Doku.

## Upstream-Landschaft & Remotes
| Remote | URL | Rolle |
|---|---|---|
| `origin` | `pos-ei-don/hass-miner` | **unser Fork** — Releases `v2.0.0-alphaNN` (asic-rs-Linie) |
| `upstream` | `Schnitzel/hass-miner` | kanonischer Upstream der `miner`-Domain |
| `b-rowan` | `b-rowan/hass-asic-rs` | Romans asic-rs-Rewrite = **Basis** unserer 2.0-Linie (Schnitzel#601) |
| `tntvlad` | `tntvlad/hass-miner` | alte pyasic-Release-Linie (Referenz) |

Re-add nach frischem Clone:
```sh
git remote add upstream https://github.com/Schnitzel/hass-miner.git
git remote add b-rowan  https://github.com/b-rowan/hass-asic-rs.git
git remote add tntvlad  https://github.com/tntvlad/hass-miner.git
```

## Divergenz-Basis
Unsere 2.0-Linie baut auf b-rowans Rewrite „swap to using asic-rs" (**Schnitzel#601**,
Branch `rewrite-to-asic-rs`), Stand **pyasic-rs 0.6.2**. Lib-Fixes/Features kommen aus dem
Schwester-Fork `pos-ei-don/asic-rs` (eigene FORK.md dort).

## Patch-Stack (fork-lokal) + Upstream-Status
| Änderung | Wo | Upstream-Status |
|---|---|---|
| Sensor-Design: Capabilities-Gating, Safety-Alarm, Sensor-Kategorien (OptionsFlow), saubere Entity-IDs (`has_entity_name`), Brand-Icon | alpha12+ | Koordination offen (Schnitzel#601); Safety-Form braucht Romans Design-OK |
| Native VNish Preset/Throttle (`vnish.py`, `select.py`, `number.py`) | alpha4+ | **BETA-Interim** bis asic-rs VNish nativ schreiben kann |
| `scan_interval` konfigurierbar + OptionsFlow für FW-Web-PW | alpha12+ | `upstream-candidate` |
| Power-aware Polling (kein Poll bei ausgeschaltetem Switch) | #612 | `upstream-candidate` |
| 4-Feld-Temp-Modell (chip/coolant getrennt) | alpha18 | hängt an Lib-PRs #285/#286 |
| **Kein Mining-Restart nach Preset-Wechsel** (PR #2) | alpha22 | `upstream-candidate` (ai_mainprojekt#620) |

## Integrations-Policy — zwei Achsen
- **Unsere Changes → Upstream:** Labels `upstream-candidate` / `upstream-submitted` /
  `upstream-merged` / `upstream-fork-only` (auf den `ai_mainprojekt`-Tracking-Issues, auf PRs gespiegelt).
- **Upstream-Changes → wir:** Labels `downstream-eval` / `downstream-adopt` / `downstream-skip`.
  Übernahme via `git cherry-pick <sha>` vom passenden Remote (`b-rowan`/`upstream`).
- **Milestones = Version** (`alphaNN`): in welcher unserer Releases eine Änderung live ging.

## Trigger für den Voll-Fork
Wenn b-rowan/Schnitzel bei den Kern-Einreichungen (v.a. **Safety-Alarm-Form** und
**`chip_temperature`-Platzierung**, Lib-PR #285) dauerhaft eine inkompatible Richtung wählen:
→ eigene stabile Linie (`main`/`stable` + **eigene Versionierung**) von der letzten guten Alpha
abzweigen; ab dann Upstream nur noch **selektiv per cherry-pick** (Achse `downstream-*`) ziehen.
Bis dahin bewusst NICHT umbenennen/abkoppeln (verfrüht, erzeugt Reibung).

## Build-Autarkie
Die Lib (Wheel) kommt aus `pos-ei-don/asic-rs` (`build-wheels-fork.yml`, `workflow_dispatch`,
nur `secrets.GITHUB_TOKEN`, keine fremden Secrets) → **autark**. Die Integration hängt an keinem
Upstream-CI. Damit ist die Linie auch ohne Upstream voll baubar.
