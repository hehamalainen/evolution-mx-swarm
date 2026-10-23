# Provenance and Third-Party Notices

This document records the provenance of first-party and imported components in
**MX Swarm**, together with attribution and license notices required by
third-party dependencies referenced in `pyproject.toml`, `requirements.txt`,
and `dashboard_v2/package-lock.json`.

---

## 1. First-Party Code and Imported Prototype Provenance

- **Core Python Packages (`cli/`, `shared/`, `planner/`, `worker/`, `judge/`, `boss/`, `dashboard/`, `simulation/`, `scripts/`, `tools/`, `tests/`)**
  - Copyright (c) 2026 Heikki Hämäläinen and MX Swarm Contributors.
  - Licensed under the [MIT License](LICENSE).
- **Dashboard V2 (`dashboard_v2/`)**
  - Copyright (c) 2026 Heikki Hämäläinen and MX Swarm Contributors.
  - Licensed under the [MIT License](LICENSE).
  - **Provenance and Release Authority Confirmation:** The repository owner and
    maintainer (Heikki Hämäläinen) confirms that `dashboard_v2/` (adapted from the
    internal `temp_analysis/gas_town_ui_poc/client` prototype authored within the
    Project MX workspace) is original/authorized first-party work and is approved
    for public release under the repository's [MIT License](LICENSE).
  - Third-party UI library primitives and icons used by `dashboard_v2/` (React,
    Radix UI, Lucide, Tailwind CSS, Vite, Three.js / Force Graph) are declared in
    `dashboard_v2/package.json` and `dashboard_v2/package-lock.json` and remain
    governed by their respective upstream licenses noted in Section 2 below.
  - Placeholder brand assets previously present in early prototype commits
    (`dashboard_v2/public/x-logo.png`, `dashboard_v2/public/xai-logo.png`, and
    `dashboard_v2/public/opengraph.jpg`) have been removed from the release tree
    and purged from the sanitized publication history.

---

## 2. Third-Party Notices Requiring Distribution-Aware Attribution

First-party MX Swarm source code is licensed under MIT, which does not replace
the license terms of third-party packages installed or bundled during builds.

### 2.1 `caniuse-lite` (CC-BY-4.0)

- **Package:** `caniuse-lite` (declared in `dashboard_v2/package-lock.json`)
- **License:** Creative Commons Attribution 4.0 International (`CC-BY-4.0`)
- **License URL:** https://creativecommons.org/licenses/by/4.0/
- **Attribution:** Data provided by [Can I use](https://caniuse.com/) (Alexis
  Deveria and contributors) and packaged by the Browserslist / `caniuse-lite`
  maintainers (https://github.com/browserslist/caniuse-lite).

### 2.2 `lightningcss` (MPL-2.0)

- **Package:** `lightningcss` and platform-specific binary packages
  (`lightningcss-*` in `dashboard_v2/package-lock.json`)
- **License:** Mozilla Public License 2.0 (`MPL-2.0`)
- **License URL:** https://www.mozilla.org/en-US/MPL/2.0/
- **Source Code Notice:** MX Swarm does not modify `lightningcss`. Unmodified
  upstream source code is available at
  https://github.com/parcel-bundler/lightningcss in accordance with the
  [Mozilla MPL 2.0 FAQ](https://www.mozilla.org/en-US/MPL/2.0/FAQ/).

### 2.3 Python Environment Dependencies with MPL-2.0 Components

When installing the Python runtime or development environment (`pip install -e ".[dev]"`),
transitive packages may include MPL-2.0-licensed components (notably `certifi`
and `pathspec`):

- **`certifi`**: Licensed under `MPL-2.0`. Source code is available at
  https://github.com/certifi/python-certifi.
- **`pathspec`**: Licensed under `MPL-2.0`. Source code is available at
  https://github.com/cpburnz/python-pathspec.

### 2.4 Permissive Dependencies (MIT / ISC / BSD / Apache-2.0)

All other dependencies in `pyproject.toml` and `dashboard_v2/package-lock.json`
(including FastAPI, Pydantic, Uvicorn, HTTPX, Anthropic SDK, React, Vite,
Tailwind CSS, Radix UI primitives, and Lucide icons) are used under their
respective permissive licenses (`MIT`, `ISC`, `BSD-2-Clause`, `BSD-3-Clause`,
`Apache-2.0`, `Python-2.0`). Their copyright notices and license metadata are
preserved in `dashboard_v2/package-lock.json` and standard Python package
dist-info directories.
