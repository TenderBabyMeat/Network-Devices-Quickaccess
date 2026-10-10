#!/usr/bin/env bash
# Removes the NetDevices putty:// link handler for the current user.
set -u
SELF="$(readlink -f "${BASH_SOURCE[0]}")"

# ---- double-clicked from a file manager: reopen in a terminal
if [[ ! -t 0 || ! -t 1 ]] && [[ "${ND_IN_TERM:-}" != 1 ]]; then
  export ND_IN_TERM=1
  cmd=("$SELF" --pause "$@")
  for t in "${TERMINAL:-}" x-terminal-emulator xdg-terminal-exec kgx gnome-terminal konsole xfce4-terminal \
           mate-terminal tilix terminator lxterminal kitty alacritty wezterm foot xterm; do
    [[ -n "$t" ]] && command -v "$t" >/dev/null || continue
    case "$t" in
      gnome-terminal|kgx)                      exec "$t" -- "${cmd[@]}" ;;
      xfce4-terminal|mate-terminal|terminator) exec "$t" -x "${cmd[@]}" ;;
      tilix|lxterminal)                        exec "$t" -e "$(printf '%q ' "${cmd[@]}")" ;;
      wezterm)                                 exec "$t" start -- "${cmd[@]}" ;;
      xdg-terminal-exec|kitty|foot)            exec "$t" "${cmd[@]}" ;;
      *)                                       exec "$t" -e "${cmd[@]}" ;;
    esac
  done
  command -v notify-send >/dev/null && notify-send "NetDevices" "No terminal found. Run from a terminal: $SELF"
  exit 1
fi
PAUSE=0; [[ ${1:-} == --pause ]] && { PAUSE=1; shift; }
trap 'rc=$?; ((PAUSE)) && { echo; read -rp "Press Enter to close..." _; }; exit $rc' EXIT

DATA="${XDG_DATA_HOME:-$HOME/.local/share}"; CFG="${XDG_CONFIG_HOME:-$HOME/.config}"
ID="netdevices-putty.desktop"
rm -f "$DATA/applications/$ID" && echo "removed launcher"
rm -rf "$DATA/netdevices" && echo "removed handler"
for f in "$CFG"/*mimeapps.list "$DATA"/applications/*mimeapps.list; do
  [[ -f $f ]] && sed -i "/^[[:space:]]*x-scheme-handler\/putty[[:space:]]*=[[:space:]]*$ID;\{0,1\}[[:space:]]*$/d" "$f"
done
echo "removed putty:// default"
command -v update-desktop-database >/dev/null && update-desktop-database "$DATA/applications" 2>/dev/null
for kb in kbuildsycoca6 kbuildsycoca5; do command -v $kb >/dev/null && { $kb >/dev/null 2>&1; break; }; done
echo "Done. (Log kept at ${XDG_CACHE_HOME:-$HOME/.cache}/netdevices-putty-handler.log)"
