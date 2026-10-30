# Changelog

## 1.1.2
- Fixed startup inside an unprivileged LXC (e.g. Docker in a Proxmox container): the ping permission now covers only the app's own group, which LXCs accept.

## 1.1.1
- The ▾ "more links" menu now opens above the list instead of being cut off by the table.
- Scan dialog layout tidy-up.

## 1.1.0 - homelab edition
- Locations (households/networks) instead of "sites"; device types, Model / OS, tags, filter chips.
- Buttons only for what's configured: Web UI when a web address is set, PuTTY / Web SSH when SSH is ticked.
- Extra links per device in a ▾ menu. Click a name or IP to copy it.
- TLS certificate expiry warnings for https web UIs.
- Status history: 24 h strip, uptime for 24 h / 7 days, last down; details popover on each status dot.
- Network scan of private ranges with suggested names, types and web addresses.
- Daily automatic JSON backups (14 kept). Editable page title.

## 1.0.5
- Sort by name, IP, model or status; "Keep this order".

## 1.0.4
- Fixed ping checks inside the container (uvloop doesn't support `sock_sendto`).

## 1.0.3
- `setup.sh --update` finds an existing install from any folder and can recover lost settings/data from the running container.

## 1.0.2
- Version shown on every page; stale-page notice after an update.
- PuTTY link handler: fixed xdg-open (Chrome/Brave) launches, added `--watch` / `--test` diagnostics.

## 1.0.1
- Fixed Web SSH "Unknown local username" inside the container.

## 1.0.0
- First Docker release: server-side storage, login, status dots, PuTTY and Web SSH buttons.
