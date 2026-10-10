#!/usr/bin/env bash
# PuTTY link handler for the NetDevices page (Linux).
#   putty://host   putty://user@host:2222   putty://[fe80::1]:22   putty://session/My%20Session
#
#   putty-handler.sh <putty://link>   open PuTTY for a link (what the browser calls)
#   putty-handler.sh --check          show setup status, saved sessions and recent log
#   putty-handler.sh --watch          diagnose, then wait for a click from the page and report
#   putty-handler.sh --test [NAME]    open a session link via each route browsers use
#   putty-handler.sh --forget-portal  clear the desktop portal's remembered app for putty://
#
# Every click is logged to ~/.cache/netdevices-putty-handler.log
HANDLER_VERSION="1.0.2"
LOG="${XDG_CACHE_HOME:-$HOME/.cache}/netdevices-putty-handler.log"
SCHEME_TYPE="x-scheme-handler/putty"
mkdir -p "$(dirname "$LOG")" 2>/dev/null
log() { printf '%s  %s\n' "$(date '+%F %T')" "$*" >>"$LOG" 2>/dev/null; }
if [[ -f $LOG ]] && (( $(wc -l <"$LOG") > 500 )); then tail -n 250 "$LOG" >"$LOG.tmp" && mv "$LOG.tmp" "$LOG"; fi

fail() {
  log "ERROR: ${1//$'\n'/ | }"
  if [[ -t 2 ]]; then echo "$1" >&2
  elif command -v zenity >/dev/null; then zenity --error --title="PuTTY link handler" --no-markup --text="$1" 2>/dev/null
  elif command -v kdialog >/dev/null; then kdialog --title "PuTTY link handler" --error "$1" 2>/dev/null
  elif command -v notify-send >/dev/null; then notify-send "PuTTY link handler" "$1"
  else echo "$1" >&2; fi
  exit 1
}
urldecode() { local s="$1"; printf '%b' "${s//%/\\x}"; }
launch() { log "run: putty $(printf '%q ' "$@")"; nohup putty "$@" >/dev/null 2>&1 & disown; }

# Saved sessions are files named with %-encoded session names.
SESSION_DIRS=("$HOME/.putty/sessions" "${XDG_CONFIG_HOME:-$HOME/.config}/putty/sessions")
saved_sessions() {
  local d f
  for d in "${SESSION_DIRS[@]}"; do
    [[ -d $d ]] || continue
    for f in "$d"/*; do [[ -f $f ]] && urldecode "${f##*/}" && echo; done
  done | awk 'NF && !s[$0]++' | sort -f
}

[[ ${1:-} == --list-sessions ]] && { saved_sessions; exit 0; }
[[ ${1:-} == --version ]] && { echo "$HANDLER_VERSION"; exit 0; }

# ------------------------------------------------------------------ diagnostics helpers
ok() { echo "  [ OK ] $*"; }; bad() { echo "  [FAIL] $*"; }; note() { echo "  [ -- ] $*"; }
log_lines() { [[ -f $LOG ]] && wc -l <"$LOG" || echo 0; }
urlencode() { local s="$1" o="" c i; for ((i = 0; i < ${#s}; i++)); do c="${s:i:1}"
  [[ $c =~ [A-Za-z0-9._~-] ]] && o+="$c" || o+="$(printf '%%%02X' "'$c")"; done; printf '%s' "$o"; }
PORTAL_STORE=(gdbus call --session --dest org.freedesktop.impl.portal.PermissionStore
              --object-path /org/freedesktop/impl/portal/PermissionStore)

firefox_overrides() {   # prints handlers.json files that send putty:// straight to a program
  local r h ffs=()
  for r in "$HOME/.mozilla/firefox" "$HOME/snap/firefox/common/.mozilla/firefox" "$HOME/.var/app/org.mozilla.firefox/.mozilla/firefox" \
           "$HOME/.librewolf" "$HOME/.var/app/io.gitlab.librewolf-community/.librewolf" "$HOME/.waterfox" "$HOME/.floorp" \
           "$HOME/.zen" "$HOME/.var/app/app.zen_browser.zen/.zen"; do
    for h in "$r"/*/handlers.json; do [[ -f $h ]] && ffs+=("$h"); done
  done
  ((${#ffs[@]})) && command -v python3 >/dev/null || return 0
  python3 -c '
import json, sys
for f in sys.argv[1:]:
    try: s = json.load(open(f)).get("schemes", {}).get("putty")
    except Exception: continue
    if s is not None and s.get("action") not in (1, 4):
        hs = ", ".join(h.get("path") or h.get("name", "?") for h in s.get("handlers", []))
        print(f + "\t" + (hs or "action %s" % s.get("action")))' "${ffs[@]}"
}

portal_memory() {       # what the desktop portal remembers for putty:// (used by Flatpak/Snap browsers)
  command -v gdbus >/dev/null || return 0
  "${PORTAL_STORE[@]}" --method org.freedesktop.impl.portal.PermissionStore.Lookup desktop-used-apps "$SCHEME_TYPE" 2>/dev/null
}

browsers() {
  local b
  for b in firefox firefox-esr librewolf brave-browser brave google-chrome google-chrome-stable chromium chromium-browser vivaldi microsoft-edge; do
    command -v "$b" >/dev/null && echo "native: $b"
  done
  command -v flatpak >/dev/null && flatpak list --app --columns=application 2>/dev/null | grep -Ei 'firefox|brave|chrom|librewolf|vivaldi|edge|zen|floorp|waterfox' | sed 's/^/flatpak: /'
  command -v snap >/dev/null && snap list 2>/dev/null | awk 'NR>1{print $1}' | grep -Ei 'firefox|brave|chrom|vivaldi' | sed 's/^/snap: /'
}

diagnose() {
  echo "Desktop: ${XDG_CURRENT_DESKTOP:-unknown} (${XDG_SESSION_TYPE:-?})"
  echo "Handler: $(readlink -f "$0") v$HANDLER_VERSION"
  echo
  if p="$(command -v putty)"; then ok "putty found: $p"; else bad "putty is not on PATH (install the 'putty' package)"; fi
  local def dfile exe hits pm
  def="$(xdg-mime query default "$SCHEME_TYPE" 2>/dev/null)"
  if [[ $def == netdevices-putty.desktop ]]; then ok "system default for putty:// is the NetDevices handler"
  elif [[ -n $def ]]; then bad "system default for putty:// is '$def' (re-run install.sh)"
  else bad "nothing is registered for putty:// (run install.sh)"; fi
  dfile="${XDG_DATA_HOME:-$HOME/.local/share}/applications/netdevices-putty.desktop"
  if [[ -f $dfile ]]; then
    exe="$(sed -n 's/^Exec="\{0,1\}\([^"]*\)"\{0,1\} %u$/\1/p' "$dfile")"
    if [[ -x $exe ]]; then ok "launcher points at $exe"; else bad "launcher points at a missing file: ${exe:-?} (re-run install.sh)"; fi
  fi
  hits="$(firefox_overrides)"
  if [[ -n $hits ]]; then
    while IFS=$'\t' read -r f h; do bad "Firefox sends putty:// straight to '$h', skipping the handler ($f)"; done <<<"$hits"
    echo "         Fix: re-run install.sh (it removes this). On the next click choose 'PuTTY link handler (NetDevices)', NOT 'PuTTY'."
  else ok "no Firefox override for putty://"; fi
  pm="$(portal_memory)"
  if [[ -n $pm && $pm != *"{}"* && $pm == *"'"* ]]; then
    if [[ $pm == *netdevices-putty* ]]; then ok "desktop portal remembers the NetDevices handler: $pm"
    else bad "desktop portal (Flatpak/Snap browsers) remembers a different app for putty://: $pm"
         echo "         Fix: re-run install.sh, or: putty-handler.sh --forget-portal"; fi
  else ok "desktop portal has no remembered choice for putty://"; fi
  echo
  echo "Browsers found:"; browsers | sed 's/^/    /'; echo
}

route_test() {          # route_test "label" cmd...   -> did the handler get called?
  local label="$1"; shift
  local before; before="$(log_lines)"
  "$@" >/dev/null 2>&1 &
  local i
  for i in 1 2 3 4 5 6 7 8; do sleep 0.5; (( $(log_lines) > before )) && { ok "$label reaches the handler"; return 0; }; done
  bad "$label does NOT reach the handler"; return 1
}

case "${1:-}" in
  --forget-portal)
    command -v gdbus >/dev/null || { echo "gdbus not installed"; exit 1; }
    "${PORTAL_STORE[@]}" --method org.freedesktop.impl.portal.PermissionStore.Delete desktop-used-apps "$SCHEME_TYPE" >/dev/null 2>&1 \
      && echo "Forgot the portal's remembered app for putty:// links." || echo "Nothing remembered (or no portal)."
    exit 0 ;;
  --test)
    # Open a link through each route a browser might use; PuTTY may open once per working route.
    name="${2:-$(saved_sessions | head -n1)}"
    [[ -n $name ]] || { echo "Usage: $0 --test \"Saved Session Name\""; exit 1; }
    url="putty://session/$(urlencode "$name")"
    echo "Opening $url through each route a browser can use (PuTTY may open several times):"
    command -v gio >/dev/null      && route_test "gio open   (Firefox, GNOME apps)    " gio open "$url"
    command -v xdg-open >/dev/null && route_test "xdg-open   (Chrome, Brave, Chromium)" xdg-open "$url"
    if command -v gdbus >/dev/null && gdbus introspect --session --dest org.freedesktop.portal.Desktop \
         --object-path /org/freedesktop/portal/desktop >/dev/null 2>&1; then
      route_test "portal     (Flatpak/Snap browsers)  " gdbus call --session \
        --dest org.freedesktop.portal.Desktop --object-path /org/freedesktop/portal/desktop \
        --method org.freedesktop.portal.OpenURI.OpenURI "" "$url" "{}" \
        || echo "         (if a 'choose an app' window appeared, pick 'PuTTY link handler (NetDevices)')"
    else
      note "portal     (Flatpak/Snap browsers)   no desktop portal running - only matters for Flatpak/Snap browsers"
    fi
    exit 0 ;;
  --watch)
    diagnose
    before="$(log_lines)"
    echo "Now click a PuTTY button on the NetDevices page. Waiting up to 60 s (Ctrl+C to stop)..."
    for i in $(seq 120); do
      if (( $(log_lines) > before )); then
        sleep 1; echo
        ok "the click reached the handler:"
        tail -n +"$((before + 1))" "$LOG" | sed 's/^/         /'
        if tail -n +"$((before + 1))" "$LOG" | grep -q 'run: putty -load'; then
          echo "         The handler asked PuTTY to load that session. If PuTTY still opened empty, open PuTTY,"
          echo "         Load the session, check Host Name is filled in, and Save it again."
        fi
        exit 0
      fi
      sleep 0.5
    done
    echo; bad "no click reached the handler: the browser is opening putty:// links some other way."
    echo "         Look for a [FAIL] above. If there is none, tell me the browser name and how it's installed"
    echo "         (from 'Browsers found'), and the output of:  $0 --test"
    exit 1 ;;
esac

if [[ ${1:-} == --check || ( -z ${1:-} && -t 1 ) ]]; then
  echo "PuTTY link handler check"
  diagnose
  mapfile -t names < <(saved_sessions)
  if ((${#names[@]})); then
    echo "Saved PuTTY sessions (${#names[@]}) - use these exact names on the device page:"
    printf '    %s\n' "${names[@]}"
  else
    note "No saved PuTTY sessions found in ${SESSION_DIRS[*]}"
  fi
  echo
  echo "Last log entries ($LOG):"
  if [[ -s $LOG ]]; then tail -n 12 "$LOG" | sed 's/^/    /'; else echo "    (empty - the handler has never been called)"; fi
  echo
  echo "Next: run '$0 --watch' and click a PuTTY button to see whether the click arrives."
  exit 0
fi

# ------------------------------------------------------------------ handle a link
uri="${1:-}"
log "link: $uri"
[[ -n $uri ]] || fail "No putty:// link was passed in."
[[ $uri == putty:* ]] || fail "Not a putty:// link: $uri"
command -v putty >/dev/null || fail "Couldn't find putty on PATH. Install the 'putty' package."

rest="${uri#putty:}"; rest="${rest#//}"; rest="${rest%/}"
rest="$(urldecode "$rest")"

if [[ "$rest" =~ ^session/(.+)$ ]]; then
  want="${BASH_REMATCH[1]}"
  [[ "$want" == *\"* || "$want" == *\\* ]] && fail "Invalid session name: $want"
  mapfile -t names < <(saved_sessions)

  match=""
  for n in "${names[@]}"; do [[ $n == "$want" ]] && { match="$n"; break; }; done
  if [[ -z $match ]]; then   # forgive capitalisation / stray spaces
    for n in "${names[@]}"; do
      a="${n,,}"; b="${want,,}"; [[ ${a// /} == "${b// /}" ]] && { match="$n"; log "matched '$want' to saved session '$n'"; break; }
    done
  fi
  if [[ -z $match ]]; then
    ((${#names[@]})) || fail "No saved PuTTY sessions found (looked in ${SESSION_DIRS[*]})."
    if command -v zenity >/dev/null && [[ ! -t 2 ]]; then
      match="$(printf '%s\n' "${names[@]}" | zenity --list --title="PuTTY link handler" \
               --text="No saved session named \"$want\".\nPick one to open (and fix the name on the device page):" \
               --column="Saved sessions" --height=400 2>/dev/null)" || exit 0
      [[ -n $match ]] || exit 0
      log "user picked '$match' instead of '$want'"
    else
      fail "No saved session named \"$want\". Saved sessions are:
$(printf '  %s\n' "${names[@]}")"
    fi
  fi
  launch -load "$match"

elif [[ "$rest" =~ ^(([A-Za-z0-9._\\-]+)@)?([A-Za-z0-9.-]+|\[[0-9A-Fa-f:.%]+\])(:([0-9]{1,5}))?$ ]]; then
  user="${BASH_REMATCH[2]}"; host="${BASH_REMATCH[3]}"; port="${BASH_REMATCH[5]}"
  host="${host#[}"; host="${host%]}"
  args=(-ssh)
  if [[ -n $port ]]; then
    (( 10#$port >= 1 && 10#$port <= 65535 )) || fail "Invalid port: $port"
    args+=(-P "$((10#$port))")
  fi
  [[ -n $user ]] && args+=(-l "$user")
  args+=("$host")
  launch "${args[@]}"
else
  fail "Couldn't understand this link: $uri"
fi
