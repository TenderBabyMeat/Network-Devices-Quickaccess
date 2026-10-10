#!/usr/bin/env bash
# Sets up putty:// links for the NetDevices page (current user, no sudo).
#  - copies the handler to ~/.local/share/netdevices/ (so this folder can be moved)
#  - removes stale putty:// registrations from your user config
#  - fixes Firefox if it was told to open putty:// links with putty directly
#  - registers the handler, refreshes caches, verifies, and offers a test
# Safe to re-run any time.
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

if [[ -t 1 ]]; then B=$'\e[1m' R=$'\e[31m' G=$'\e[32m' Y=$'\e[33m' D=$'\e[2m' N=$'\e[0m'; else B= R= G= Y= D= N=; fi
ok()   { echo "  ${G}✔${N} $*"; }
warn() { echo "  ${Y}!${N} $*"; }
bad()  { echo "  ${R}✘${N} $*"; }
step() { echo; echo "${B}$*${N}"; }
pretty() { local p="$1"; [[ $p == "$HOME"/* ]] && p="~${p#"$HOME"}"; printf '%s' "$p"; }

SRC="$(dirname "$SELF")/putty-handler.sh"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"; CFG="${XDG_CONFIG_HOME:-$HOME/.config}"
DEST_DIR="$DATA/netdevices"; DEST="$DEST_DIR/putty-handler.sh"
APPS="$DATA/applications"; ID="netdevices-putty.desktop"; DESK="$APPS/$ID"
TYPE="x-scheme-handler/putty"
BACKUP="${XDG_STATE_HOME:-$HOME/.local/state}/netdevices/backup-$(date +%Y%m%d-%H%M%S)"
backup() { mkdir -p "$BACKUP$(dirname "$1")" && cp -p "$1" "$BACKUP$1"; }
FF_ROOTS=("$HOME/.mozilla/firefox" "$HOME/snap/firefox/common/.mozilla/firefox" "$HOME/.var/app/org.mozilla.firefox/.mozilla/firefox"
          "$HOME/.librewolf" "$HOME/.var/app/io.gitlab.librewolf-community/.librewolf" "$HOME/.waterfox" "$HOME/.floorp")
shopt -s nullglob

echo "${B}NetDevices - PuTTY link setup${N}  ${D}($(id -un))${N}"
[[ -f $SRC ]] || { bad "putty-handler.sh is missing next to this script."; exit 1; }
[[ $DEST =~ [\"\`\$\\] ]] && { bad "Your home path contains characters a launcher can't use: $DEST"; exit 1; }

# ---------------------------------------------------------------- 1. putty
step "1. PuTTY"
if p="$(command -v putty)"; then ok "found $p"
else warn "putty is not installed or not on PATH - install it (e.g. sudo apt install putty) before using SSH buttons"; fi

# ---------------------------------------------------------------- 2. handler
step "2. Handler"
mkdir -p "$DEST_DIR" "$APPS"
install -m 755 "$SRC" "$DEST" && ok "installed $(pretty "$DEST")"

# ---------------------------------------------------------------- 3. stale registrations
step "3. Old putty:// registrations"
found=0
for f in "$CFG"/*mimeapps.list "$APPS"/*mimeapps.list "$APPS"/defaults.list; do
  [[ -f $f ]] || continue
  lines="$(grep -i "^[[:space:]]*$TYPE[[:space:]]*=" "$f")" || continue
  others="$(grep -v "=$ID;\{0,1\}[[:space:]]*$" <<<"$lines")"
  [[ -n $others ]] && { backup "$f"; sed "s|^|    removed from $(pretty "$f"): |" <<<"$others"; found=1; }
  sed -i "/^[[:space:]]*x-scheme-handler\/putty[[:space:]]*=/Id" "$f"
done
for f in "$APPS"/*.desktop; do
  [[ $f == "$DESK" ]] && continue
  grep -qi "^MimeType[[:space:]]*=.*$TYPE" "$f" || continue
  backup "$f"
  if grep -q '^Name=PuTTY link handler$' "$f"; then
    rm -f "$f"; echo "    removed old launcher $(pretty "$f")"
  else
    awk -v t="$TYPE" '/^MimeType[ \t]*=/ { v = $0; sub(/^[^=]*=[ \t]*/, "", v); n = split(v, a, ";"); o = ""
          for (i = 1; i <= n; i++) { x = a[i]; gsub(/^[ \t]+|[ \t]+$/, "", x); if (x == "" || tolower(x) == t) continue; o = o x ";" }
          if (o != "") print "MimeType=" o; next } { print }' "$f" >"$f.tmp" && cat "$f.tmp" >"$f" && rm -f "$f.tmp"
    echo "    removed putty:// claim from $(pretty "$f")"
  fi
  found=1
done
((found)) && ok "cleaned (backups in $(pretty "$BACKUP"))" || ok "none found"

# ---------------------------------------------------------------- 4. register
step "4. Register"
# No quotes around the path: xdg-open's generic mode (used by Chrome/Brave outside GNOME/KDE)
# takes the first word literally, quotes and all, and then silently falls back to plain PuTTY.
EXEC_PATH="$DEST"
if [[ $DEST =~ [[:space:]] ]]; then
  EXEC_PATH="\"$DEST\""
  warn "your home path contains spaces; xdg-open based browsers (Chrome/Brave) may not reach the handler"
fi
cat >"$DESK" <<EOF
[Desktop Entry]
Type=Application
Name=PuTTY link handler (NetDevices)
Comment=Opens putty:// links from the NetDevices page
Exec=$EXEC_PATH %u
Icon=putty
Terminal=false
NoDisplay=true
MimeType=$TYPE;
EOF
ok "launcher $(pretty "$DESK")"
# Write the default ourselves so it doesn't depend on which desktop xdg-mime thinks it's on.
M="$CFG/mimeapps.list"; mkdir -p "$CFG"; touch "$M"
awk -v line="$TYPE=$ID" '
  { print }
  /^[ \t]*\[Default Applications\][ \t]*$/ && !done { print line; done = 1 }
  END { if (!done) { if (NR) print ""; print "[Default Applications]"; print line } }' "$M" >"$M.tmp" && mv "$M.tmp" "$M"
command -v xdg-mime >/dev/null && xdg-mime default "$ID" "$TYPE" 2>/dev/null
ok "default set in $(pretty "$M")"
if command -v update-desktop-database >/dev/null; then update-desktop-database "$APPS" 2>/dev/null && ok "desktop database refreshed"
else warn "update-desktop-database not found (package desktop-file-utils) - usually fine"; fi
for kb in kbuildsycoca6 kbuildsycoca5; do command -v $kb >/dev/null && { $kb >/dev/null 2>&1; ok "KDE cache refreshed"; break; }; done

# ---------------------------------------------------------------- 5. firefox
step "5. Firefox"
ff_files=()
for r in "${FF_ROOTS[@]}"; do for h in "$r"/*/handlers.json; do ff_files+=("$h"); done; done
ff_py='
import json, sys
mode, files = sys.argv[1], sys.argv[2:]
for f in files:
    try:
        d = json.load(open(f))
    except Exception:
        continue
    s = d.get("schemes", {}).get("putty")
    if s is None or s.get("action") in (1, 4):   # 1 = always ask, 4 = system default: both reach our handler
        continue
    hs = ", ".join(h.get("path") or h.get("uriTemplate") or h.get("name", "?") for h in s.get("handlers", []))
    print(f + "\t" + (hs or "action %s" % s.get("action")))
    if mode == "fix":
        del d["schemes"]["putty"]
        json.dump(d, open(f, "w"), separators=(",", ":"))
'
if ((${#ff_files[@]} == 0)); then ok "no Firefox profiles found"
elif ! command -v python3 >/dev/null; then
  warn "python3 not found, can't check Firefox. If clicks open a bare PuTTY window: Firefox > Settings > General >"
  warn "Applications > putty > 'Always ask', then pick 'PuTTY link handler (NetDevices)' on the next click."
else
  mapfile -t hits < <(python3 -c "$ff_py" check "${ff_files[@]}")
  if ((${#hits[@]} == 0)); then ok "Firefox will use the system handler"
  else
    for h in "${hits[@]}"; do bad "$(pretty "${h%%$'\t'*}") sends putty:// links straight to: ${h#*$'\t'}"; done
    echo "    ${D}That gives PuTTY the raw link, so it opens empty without loading the session.${N}"
    while pgrep -u "$(id -u)" -x 'firefox|firefox-bin|firefox-esr|librewolf|waterfox|floorp' >/dev/null 2>&1; do
      read -rp "    Firefox is running and would undo the fix. Close it, then press Enter (s = skip): " a || break
      [[ ${a,,} == s* ]] && break
    done
    if pgrep -u "$(id -u)" -x 'firefox|firefox-bin|firefox-esr|librewolf|waterfox|floorp' >/dev/null 2>&1; then
      warn "skipped - re-run this after closing Firefox"
    else
      for h in "${hits[@]}"; do backup "${h%%$'\t'*}"; done
      python3 -c "$ff_py" fix "${ff_files[@]}" >/dev/null && ok "fixed - on the next click Firefox asks once; choose 'PuTTY link handler (NetDevices)' and tick 'Always'"
    fi
  fi
fi

# ---------------------------------------------------------------- 5b. desktop portal
# Flatpak/Snap browsers open links through the desktop portal, which remembers its own app choice.
if command -v gdbus >/dev/null; then
  pm="$(gdbus call --session --dest org.freedesktop.impl.portal.PermissionStore --object-path /org/freedesktop/impl/portal/PermissionStore \
        --method org.freedesktop.impl.portal.PermissionStore.Lookup desktop-used-apps "$TYPE" 2>/dev/null)"
  if [[ -n $pm && $pm == *"'"* && $pm != *"$ID"* ]]; then
    "$DEST" --forget-portal >/dev/null && ok "cleared the desktop portal's remembered app for putty:// ($pm)"
  fi
fi

# ---------------------------------------------------------------- 6. verify
step "6. Verify"
pass=1
if command -v xdg-mime >/dev/null; then
  q="$(xdg-mime query default "$TYPE" 2>/dev/null)"
  if [[ $q == "$ID" ]]; then ok "xdg-mime query default $TYPE → $q"
  else bad "xdg-mime query default $TYPE → ${q:-(nothing)}"; pass=0; fi
fi
if command -v gio >/dev/null; then
  g="$(gio mime "$TYPE" 2>/dev/null | head -n1)"
  [[ $g == *"$ID"* ]] && ok "gio: $g" || { bad "gio: ${g:-(no answer)}"; pass=0; }
fi
if ((!pass)); then
  echo "    Something else still wins. Entries mentioning putty:// :"
  grep -rHi "x-scheme-handler/putty" "$CFG"/*mimeapps.list "$APPS" /etc/xdg/*mimeapps.list /usr/share/applications/*.list \
       /usr/share/applications/*.desktop /usr/local/share/applications 2>/dev/null | sed "s|^|      |"
  echo "    The XDG Association Cleaner can remove the system ones (scheme: putty), then re-run this."
fi

mapfile -t names < <("$DEST" --list-sessions 2>/dev/null)
step "7. Saved PuTTY sessions"
if ((${#names[@]})); then printf '    %s\n' "${names[@]}"; echo "    ${D}Use these exact names in the device editor's 'Saved PuTTY session' field.${N}"
else warn "none found in ~/.putty/sessions or ~/.config/putty/sessions"; fi

# ---------------------------------------------------------------- 8. test
if ((pass)) && [[ -t 0 ]]; then
  step "8. Test"
  echo "    Opens a session link through each route a browser can use. PuTTY may open up to three times."
  read -rp "    Session name to test (Enter to skip): " t || t=""
  [[ -n $t ]] && "$DEST" --test "$t" | tail -n +2
fi

echo
((pass)) && echo "${G}${B}Done.${N} Click a PuTTY button on the page. If the browser asks which app to use, pick
'PuTTY link handler (NetDevices)' - NOT plain 'PuTTY' - and tick 'Always'.
Not working from the page? Run: $DEST --watch" \
         || echo "${Y}${B}Installed, but another handler still takes putty:// links${N} - see step 6."
