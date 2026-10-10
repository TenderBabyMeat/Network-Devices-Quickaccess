#!/usr/bin/env bash
# NetDevices setup: asks where and how to run, writes .env, then builds and starts the container.
#
#   ./setup.sh            configure (current settings are the defaults) and (re)start
#   ./setup.sh --update   rebuild and restart with the current settings, no questions
#   ./setup.sh --code     show the first-run setup code again
#   ./setup.sh --status   show container status and the URL(s)
set -euo pipefail
cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
APPDIR="$PWD"

if [[ -t 1 ]]; then B=$'\e[1m' D=$'\e[2m' R=$'\e[31m' G=$'\e[32m' Y=$'\e[33m' C=$'\e[36m' N=$'\e[0m'; else B= D= R= G= Y= C= N=; fi
say()  { echo "$*"; }
ok()   { echo "  ${G}✔${N} $*"; }
warn() { echo "  ${Y}!${N} $*"; }
die()  { echo "${R}Error:${N} $*" >&2; exit 1; }
hdr()  { echo; echo "${B}${C}$*${N}"; }
ask() {  # ask VAR "Question" default
  local __v="$1" q="$2" def="${3:-}" a
  read -rp "  $q${def:+ [$def]}: " a || exit 1
  printf -v "$__v" '%s' "${a:-$def}"
}
yesno() {  # yesno "Question" y|n
  local a d="${2:-n}"
  read -rp "  $1 $([[ $d == y ]] && echo '[Y/n]' || echo '[y/N]') " a || exit 1
  a="${a:-$d}"; [[ ${a,,} == y* ]]
}

# ---------------------------------------------------------------- docker
command -v docker >/dev/null || die "Docker isn't installed. See https://docs.docker.com/engine/install/"
DOCKER=(docker)
if ! docker info >/dev/null 2>&1; then
  if command -v sudo >/dev/null && sudo docker info >/dev/null 2>&1; then DOCKER=(sudo docker)
  else die "Can't talk to Docker. Is it running, and is your user in the 'docker' group?"; fi
fi
if "${DOCKER[@]}" compose version >/dev/null 2>&1; then COMPOSE=("${DOCKER[@]}" compose)
elif command -v docker-compose >/dev/null; then COMPOSE=(docker-compose); [[ ${DOCKER[0]} == sudo ]] && COMPOSE=(sudo docker-compose)
else die "Docker Compose isn't installed (package docker-compose-plugin)."; fi

# ---------------------------------------------------------------- existing install?
# If this folder has no .env but a NetDevices container exists, this is either a fresh copy
# of a newer version (unzipped somewhere else), or the install folder was replaced/deleted.
# Never start an empty install over a running one.
APP_ITEMS=(app static client Dockerfile docker-compose.yml requirements.txt setup.sh README.md .dockerignore)
insp() { "${DOCKER[@]}" inspect netdevices --format "$1" 2>/dev/null; }

recover_here() {   # rebuild .env (and data/keys if their folders are gone) from the running container
  local old_data old_keys port_line bind port user env_lines
  old_data="$(insp '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}')"
  old_keys="$(insp '{{range .Mounts}}{{if eq .Destination "/ssh"}}{{.Source}}{{end}}{{end}}')"
  port_line="$(insp '{{range $p, $b := .HostConfig.PortBindings}}{{range $b}}{{.HostIp}} {{.HostPort}}{{end}}{{end}}')"
  bind="${port_line% *}"; port="${port_line##* }"; [[ -z $bind || $bind == "$port_line" ]] && bind=0.0.0.0
  user="$(insp '{{.Config.User}}')"
  env_lines="$(insp '{{range .Config.Env}}{{.}}{{"\n"}}{{end}}')"
  envv() { sed -n "s/^$1=//p" <<<"$env_lines" | head -n1; }

  local data="./data" keys="./ssh-keys"
  if [[ -n $old_data && -f $old_data/netdevices.db ]]; then data="$old_data"; ok "your data is still in $old_data - keeping it there"
  else
    [[ -e ./data/netdevices.db ]] && die "./data already has a database here; not overwriting it. Move it aside and re-run."
    mkdir -p ./data
    say "  Rescuing your database from the running container (its folder on disk is gone) ..."
    "${DOCKER[@]}" exec netdevices python -c "import sqlite3; s = sqlite3.connect('/data/netdevices.db'); d = sqlite3.connect('/tmp/rescue.db'); s.backup(d); d.close()" \
      && "${DOCKER[@]}" exec netdevices cat /tmp/rescue.db > ./data/netdevices.db \
      && [[ -s ./data/netdevices.db ]] && ok "database rescued to ./data/netdevices.db" \
      || die "couldn't copy the database out of the container. Leave it running and send this output for help."
  fi
  if [[ -n $old_keys && -d $old_keys ]]; then keys="$old_keys"
  else
    mkdir -p ./ssh-keys
    "${DOCKER[@]}" exec netdevices tar -C /ssh -cf - . 2>/dev/null | tar -C ./ssh-keys -xf - 2>/dev/null || true
    [[ -n "$(ls -A ./ssh-keys 2>/dev/null)" ]] && ok "SSH key(s) rescued to ./ssh-keys"
  fi
  cat > .env <<EOF
# Recovered by setup.sh on $(date '+%F %T') from the running container. Re-run ./setup.sh to change.
ND_BIND=$bind
ND_PORT=${port:-8088}
ND_DATA_PATH=$data
ND_KEYS_PATH=$keys
ND_STATUS_INTERVAL=$(envv ND_STATUS_INTERVAL)
ND_STATUS_TIMEOUT=$(envv ND_STATUS_TIMEOUT)
ND_COOKIE_SECURE=$(envv ND_COOKIE_SECURE)
ND_TRUSTED_PROXY=$(envv FORWARDED_ALLOW_IPS)
ND_UID=${user%%:*}
ND_GID=${user##*:}
EOF
  sed -i 's/^\(ND_[A-Z_]*\)=$/\1=/' .env
  ok "settings recovered into $APPDIR/.env"
}

if [[ ! -f .env ]] && "${DOCKER[@]}" inspect netdevices >/dev/null 2>&1; then
  old_dir="$(insp '{{index .Config.Labels "com.docker.compose.project.working_dir"}}')"
  running_ver="$("${DOCKER[@]}" exec netdevices python -c 'from app import config; print(config.VERSION)' 2>/dev/null | tr -d '\r')"
  new_ver="$(sed -n 's/^VERSION = "\(.*\)"/\1/p' app/config.py)"
  echo "${B}NetDevices is already installed${N} (running v${running_ver:-?}; this folder has v${new_ver:-?})."
  if [[ -n $old_dir && $old_dir != "$APPDIR" && -f $old_dir/.env ]]; then
    echo "  Installed in: $old_dir"
    echo "  This folder:  $APPDIR (a new copy, no settings)"
    if yesno "Update the installed copy with the files from this folder? (settings, data and keys are kept)" y; then
      for item in "${APP_ITEMS[@]}"; do
        [[ -e $item ]] || continue
        rm -rf "${old_dir:?}/$item"; cp -a "$item" "$old_dir/"
      done
      ok "files copied to $old_dir"
      exec "$old_dir/setup.sh" --update
    fi
    echo "  Nothing changed. To update by hand: copy everything except .env, data/ and ssh-keys/ into $old_dir,"
    echo "  then run: $old_dir/setup.sh --update"
    exit 0
  fi
  echo "  Its install folder ${old_dir:-?} $([[ -d $old_dir ]] && echo "no longer has its settings (.env)" || echo "no longer exists")."
  echo "  The container is still running with your data, so it can be recovered into this folder."
  yesno "Recover the settings (and data, if needed) into $APPDIR and continue?" y || { echo "  Nothing changed."; exit 0; }
  recover_here
fi

# ---------------------------------------------------------------- current settings
declare -A CUR=()
if [[ -f .env ]]; then
  while IFS='=' read -r k v; do
    [[ $k =~ ^[A-Z_]+$ ]] || continue
    v="${v%\"}"; v="${v#\"}"; CUR[$k]="$v"
  done < .env
fi
cur() { echo "${CUR[$1]:-$2}"; }

host_ips() {
  if command -v ip >/dev/null; then ip -4 -o addr show scope global 2>/dev/null | awk '{split($4,a,"/"); print $2" "a[1]}'
  else hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9.]+$' | sed 's/^/- /'; fi | grep -vE '^(docker|br-|veth|virbr)' || true
}

urls() {
  local bind="$1" port="$2"
  if [[ $bind == 0.0.0.0 ]]; then
    host_ips | while read -r _ ip; do echo "http://$ip:$port/"; done
  else echo "http://$bind:$port/"; fi
}

container_health() { "${DOCKER[@]}" inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' netdevices 2>/dev/null || echo missing; }

show_code() {
  local code configured
  configured="$("${COMPOSE[@]}" exec -T netdevices python -c \
    'from app import db, auth; db.init(); print(auth.is_configured())' 2>/dev/null | tail -n1 | tr -d '\r')"
  if [[ $configured == True ]]; then
    echo; echo "  ${D}A login already exists. Forgot it? ${COMPOSE[*]} exec netdevices python -m app.resetpw${N}"
    return
  fi
  code="$("${COMPOSE[@]}" logs --no-color netdevices 2>/dev/null | grep -o 'setup code: [0-9A-F-]*' | tail -n1 | cut -d' ' -f3)"
  if [[ -n $code ]]; then
    echo; echo "  First-run setup code: ${B}${code}${N}"
    echo "  ${D}Open the page, enter this code and choose your password.${N}"
  else
    echo; warn "couldn't read the setup code yet - run ./setup.sh --code in a few seconds"
  fi
}

wait_healthy() {
  local i h
  printf "  Waiting for the app to start"
  for i in $(seq 1 60); do
    h="$(container_health)"
    case "$h" in
      healthy) echo; ok "running"; return 0 ;;
      exited|dead|missing) echo; warn "container is $h:"; "${COMPOSE[@]}" logs --tail 30 netdevices; return 1 ;;
    esac
    printf "."; sleep 2
  done
  echo; warn "still starting after 2 minutes - check: ${COMPOSE[*]} logs netdevices"; return 1
}

case "${1:-}" in
  --code)   show_code; exit 0 ;;
  --status) echo "Container: $(container_health)"; urls "$(cur ND_BIND 0.0.0.0)" "$(cur ND_PORT 8088)" | sed 's/^/  /'; exit 0 ;;
  --update)
    [[ -f .env ]] || die "No .env here and no NetDevices container found - run ./setup.sh to install."
    "${COMPOSE[@]}" up -d --build && wait_healthy && urls "$(cur ND_BIND 0.0.0.0)" "$(cur ND_PORT 8088)" | sed 's/^/  /'
    exit $? ;;
  "") ;;
  *) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac

echo "${B}NetDevices setup${N}  ${D}(${APPDIR})${N}"
[[ -f .env ]] && echo "${D}Current settings are shown in [brackets]; press Enter to keep them.${N}"

# ---------------------------------------------------------------- 1. address
hdr "1. Which address should it listen on?"
mapfile -t IPS < <(host_ips)
opts=("0.0.0.0" ); labels=("All of this host's addresses")
for l in "${IPS[@]}"; do opts+=("${l#* }"); labels+=("${l#* }  ${D}(${l%% *})${N}"); done
opts+=("127.0.0.1"); labels+=("This machine only (127.0.0.1)")
curbind="$(cur ND_BIND 0.0.0.0)"; defn=1
for i in "${!opts[@]}"; do [[ ${opts[i]} == "$curbind" ]] && defn=$((i + 1)); printf "    %d) %s\n" $((i + 1)) "${labels[i]}"; done
printf "    %d) Other address\n" $(( ${#opts[@]} + 1 ))
while :; do
  ask n "Choice" "$defn"
  if [[ $n =~ ^[0-9]+$ ]] && (( n >= 1 && n <= ${#opts[@]} )); then BIND="${opts[n-1]}"; break
  elif [[ $n == $(( ${#opts[@]} + 1 )) ]]; then
    ask BIND "IPv4 address" "$curbind"
    [[ $BIND =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] && break || warn "not an IPv4 address"
  else warn "pick a number from the list"; fi
done
echo "  ${D}Keep this on your LAN/VPN. It stores a map of your network and can open SSH sessions,${N}"
echo "  ${D}so don't publish it to the internet through a reverse proxy.${N}"

# ---------------------------------------------------------------- 2. port
hdr "2. Which port?"
while :; do
  ask PORT "Port" "$(cur ND_PORT 8088)"
  [[ $PORT =~ ^[0-9]+$ ]] && (( PORT >= 1 && PORT <= 65535 )) || { warn "1-65535 please"; continue; }
  if [[ $PORT != "$(cur ND_PORT x)" ]] && command -v ss >/dev/null && ss -ltnH "sport = :$PORT" 2>/dev/null | grep -q .; then
    warn "something is already listening on port $PORT"; yesno "Use it anyway?" n || continue
  fi
  (( PORT < 1024 )) && warn "ports below 1024 are fine for Docker but unusual"
  break
done

# ---------------------------------------------------------------- 3. data folder
hdr "3. Where should the data live?"
echo "  ${D}The database (your sites/devices, login, saved SSH host keys). Back this folder up.${N}"
ask DATA "Data folder" "$(cur ND_DATA_PATH ./data)"

# ---------------------------------------------------------------- 4. status checks
hdr "4. Status checks"
while :; do
  ask INTERVAL "Seconds between checks (5-3600)" "$(cur ND_STATUS_INTERVAL 30)"
  [[ $INTERVAL =~ ^[0-9]+$ ]] && (( INTERVAL >= 5 && INTERVAL <= 3600 )) && break || warn "5-3600 please"
done

# ---------------------------------------------------------------- 5. ssh key
hdr "5. SSH key for Web SSH (optional)"
KEYS="$(cur ND_KEYS_PATH ./ssh-keys)"
mkdir -p "$KEYS"
existing=()
for f in "$KEYS"/*; do [[ -f $f && $f != *.pub ]] && existing+=("$(basename "$f")"); done
echo "  ${D}Web SSH tries this key first, then asks for a password. The key is mounted read-only.${N}"
if ((${#existing[@]})); then echo "  Current key(s) in $KEYS: ${existing[*]}"; fi
echo "    1) $( ((${#existing[@]})) && echo "Keep what's there" || echo "No key - always ask for a password")"
echo "    2) Generate a new ed25519 key (modern devices)"
echo "    3) Generate a new RSA 4096 key (older devices: some NAS boxes, routers, IoT)"
echo "    4) Copy an existing private key file"
ask k "Choice" 1
newpub=""; GEN_LATER=""
case "$k" in
  2|3)
    t=ed25519; [[ $k == 3 ]] && t=rsa
    f="$KEYS/id_$t"
    if [[ -e $f ]] && ! yesno "$f exists. Replace it?" n; then f=""; fi
    if [[ -n $f ]]; then
      rm -f "$f" "$f.pub"
      if command -v ssh-keygen >/dev/null; then
        if [[ $t == rsa ]]; then ssh-keygen -q -t rsa -b 4096 -N "" -C "netdevices@$(hostname -s)" -f "$f"
        else ssh-keygen -q -t ed25519 -N "" -C "netdevices@$(hostname -s)" -f "$f"; fi
        ok "created $f"
      else
        GEN_LATER="$t"; ok "will create $f once the image is built (ssh-keygen isn't installed here)"
      fi
      newpub="$f.pub"
    fi ;;
  4)
    ask src "Path to the private key" "$HOME/.ssh/id_ed25519"
    src="${src/#\~/$HOME}"
    [[ -f $src ]] || die "$src not found"
    grep -q "ENCRYPTED" "$src" 2>/dev/null && die "That key has a passphrase; Web SSH needs a key without one. Generate a dedicated key instead (option 2/3)."
    cp "$src" "$KEYS/$(basename "$src")"; [[ -f $src.pub ]] && cp "$src.pub" "$KEYS/"
    ok "copied to $KEYS/$(basename "$src")" ;;
esac

# ---------------------------------------------------------------- 6. reverse proxy
hdr "6. Reverse proxy (optional)"
echo "  ${D}Only if you'll reach it through an HTTPS reverse proxy on your LAN (e.g. Nginx Proxy Manager).${N}"
SECURE="$(cur ND_COOKIE_SECURE 0)"; PROXY="$(cur ND_TRUSTED_PROXY 127.0.0.1)"
if yesno "Will you ALWAYS use it through an HTTPS proxy?" "$([[ $SECURE == 1 ]] && echo y || echo n)"; then
  SECURE=1
  ask PROXY "Proxy's IP address (so login throttling sees real client IPs)" "$([[ $PROXY == 127.0.0.1 ]] && echo "" || echo "$PROXY")"
  echo "  ${D}In the proxy, turn on WebSockets support for this host (Web SSH needs it).${N}"
  echo "  ${D}Plain http://IP:port logins stop working with this on; re-run setup to undo.${N}"
else SECURE=0; PROXY=127.0.0.1; fi

# ---------------------------------------------------------------- write .env
UIDN="${SUDO_UID:-$(id -u)}"; GIDN="${SUDO_GID:-$(id -g)}"
[[ $UIDN == 0 ]] && { UIDN=1000; GIDN=1000; }
cat > .env <<EOF
# Written by setup.sh on $(date '+%F %T'). Re-run ./setup.sh to change, or edit and run: docker compose up -d
ND_BIND=$BIND
ND_PORT=$PORT
ND_DATA_PATH=$DATA
ND_KEYS_PATH=$KEYS
ND_STATUS_INTERVAL=$INTERVAL
ND_STATUS_TIMEOUT=$(cur ND_STATUS_TIMEOUT 2)
ND_COOKIE_SECURE=$SECURE
ND_TRUSTED_PROXY=${PROXY:-127.0.0.1}
ND_UID=$UIDN
ND_GID=$GIDN
EOF
mkdir -p "$DATA"
chmod 700 "$KEYS"; find "$KEYS" -type f ! -name '*.pub' -exec chmod 600 {} +
if [[ $(id -u) == 0 ]]; then chown -R "$UIDN:$GIDN" "$DATA" "$KEYS"
elif [[ ! -w $DATA ]]; then warn "$DATA isn't writable by you - fix with: sudo chown -R $UIDN:$GIDN $DATA"; fi
hdr "Starting"
ok "settings saved to .env"

# ---------------------------------------------------------------- build & run
"${COMPOSE[@]}" up -d --build
wait_healthy || exit 1
if [[ -n $GEN_LATER ]]; then
  alg="ssh-ed25519"; size=""; [[ $GEN_LATER == rsa ]] && { alg="ssh-rsa"; size=", key_size=4096"; }
  "${DOCKER[@]}" run --rm --user "$UIDN:$GIDN" --network none -v "$(readlink -f "$KEYS"):/out" netdevices:local \
    python -c "import asyncssh; k = asyncssh.generate_private_key('$alg', comment='netdevices@$(hostname -s)'$size); \
k.write_private_key('/out/id_$GEN_LATER'); k.write_public_key('/out/id_$GEN_LATER.pub')" \
    && chmod 600 "$KEYS/id_$GEN_LATER" && ok "created $KEYS/id_$GEN_LATER" || { warn "key generation failed"; newpub=""; }
fi

hdr "Done"
echo "  Open:"
urls "$BIND" "$PORT" | sed 's/^/    /'
show_code
if [[ -n $newpub ]]; then
  echo
  echo "  ${B}New SSH public key${N} - add it to each device you want Web SSH to log into without a password:"
  echo "  ${D}(Linux/Pi/LXC: append it to ~/.ssh/authorized_keys. NAS/router: paste it into its SSH key settings.)${N}"
  echo; sed 's/^/    /' "$newpub"; echo
fi
echo
echo "  ${D}Useful:  ./setup.sh --status   ./setup.sh --update   ${COMPOSE[*]} logs -f netdevices${N}"
echo "  ${D}Forgot the password:  ${COMPOSE[*]} exec netdevices python -m app.resetpw${N}"
