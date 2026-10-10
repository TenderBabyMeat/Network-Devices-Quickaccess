(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const devId = new URLSearchParams(location.search).get("d") || "";
  let ws = null, connected = false, term = null, fit = null;

  function setState(kind, text) {
    $("stateDot").className = "dot " + kind;
    $("stateText").textContent = text;
    $("btnReconnect").hidden = !(kind === "down" || kind === "off");
  }

  // Device name for the title bar (purely cosmetic; the server looks the device up itself).
  fetch("/api/data", { credentials: "same-origin" }).then(r => r.ok ? r.json() : null).then(d => {
    if (!d) return;
    for (const s of d.sites) for (const x of s.devices) if (x.id === devId) {
      $("devName").textContent = x.hostname;
      document.title = `${x.hostname} · Web SSH`;
      const host = x.ssh_host || x.ip;
      $("devTarget").textContent = `${x.ssh_user ? x.ssh_user + "@" : ""}${host}${+x.ssh_port !== 22 ? ":" + x.ssh_port : ""}`;
    }
  }).catch(() => { });

  if (typeof window.Terminal !== "function") {
    $("term").innerHTML = `<div class="term-missing">The terminal component (xterm.js) is missing from this build.
      Rebuild the image with <span class="mono">./setup.sh</span> or <span class="mono">docker compose build</span>.</div>`;
    setState("down", "Unavailable");
    return;
  }

  term = new window.Terminal({
    cursorBlink: true, scrollback: 5000, fontSize: 14,
    fontFamily: '"Cascadia Mono", "JetBrains Mono", "DejaVu Sans Mono", Consolas, Menlo, monospace',
    theme: { background: "#0d1117", foreground: "#e6e8eb", cursor: "#e6e8eb", selectionBackground: "#264f78" },
  });
  if (window.FitAddon) { fit = new window.FitAddon.FitAddon(); term.loadAddon(fit); }
  term.open($("term"));
  const refit = () => { try { fit && fit.fit(); } catch { } };
  refit();
  new ResizeObserver(refit).observe($("term"));

  const send = (o) => { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); };
  term.onData(d => { if (connected) send({ type: "input", data: d }); });
  term.onResize(({ cols, rows }) => send({ type: "resize", cols, rows }));
  const info = (t, color = "2") => term.write(`\x1b[${color}m${t}\x1b[0m\r\n`);

  // ------------------------------------------------------------ prompts
  let pending = null;
  function prompt(p) {
    const title = $("pTitle"), body = $("pBody"), input = $("pInput"), ok = $("pOk");
    $("pMsg").textContent = "";
    title.className = ""; ok.className = "primary"; input.hidden = true; input.value = "";
    if (p.kind === "hostkey") {
      if (p.changed) {
        title.textContent = "⚠ Host key has CHANGED";
        title.className = "danger-title";
        body.innerHTML = `<p>The key presented by <b class="mono">${esc(p.target)}</b> is different from the one saved earlier.
          That's expected if the device was replaced, reset or re-keyed. Otherwise someone may be intercepting the connection.</p>
          <div>Saved:</div><div class="fp old">${esc(p.old)}</div>
          <div>Now (${esc(p.algorithm)}):</div><div class="fp">${esc(p.fingerprint)}</div>`;
        ok.textContent = "Replace saved key & connect"; ok.className = "danger-fill";
      } else {
        title.textContent = "Verify host key";
        body.innerHTML = `<p>First connection to <b class="mono">${esc(p.target)}</b>. If you can, compare this with the
          fingerprint shown on the device before trusting it.</p>
          <div>${esc(p.algorithm)}:</div><div class="fp">${esc(p.fingerprint)}</div>`;
        ok.textContent = "Trust & connect";
      }
      pending = (v) => v ? "yes" : "no";
    } else if (p.kind === "username") {
      title.textContent = "Username";
      body.innerHTML = `<p>Log in to <b class="mono">${esc(p.target)}</b> as:</p>`;
      input.type = "text"; input.autocomplete = "username"; input.hidden = false;
      ok.textContent = "Continue";
      pending = (v) => v ? input.value : "";
    } else if (p.kind === "password") {
      title.textContent = "Password";
      body.innerHTML = `<p>Password for <b class="mono">${esc(p.user)}@${esc(p.target)}</b></p>`;
      if (p.retry) $("pMsg").textContent = "That didn't work. Try again.";
      input.type = "password"; input.autocomplete = "current-password"; input.hidden = false;
      ok.textContent = "Log in";
      pending = (v) => v ? input.value : "";
    } else return;
    $("overlay").hidden = false;
    (input.hidden ? ok : input).focus();
  }
  function answer(accepted) {
    if (!pending) return;
    const value = pending(accepted);
    if (accepted && !$("pInput").hidden && !value) { $("pMsg").textContent = "Required."; return; }
    pending = null;
    $("overlay").hidden = true;
    $("pInput").value = "";
    send({ type: "answer", value });
    term.focus();
  }
  $("promptForm").addEventListener("submit", (e) => { e.preventDefault(); answer(true); });
  $("pCancel").addEventListener("click", () => answer(false));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && pending) answer(false); });

  // ------------------------------------------------------------ connection
  function connect() {
    connected = false;
    setState("checking", "Connecting…");
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws/ssh?d=${encodeURIComponent(devId)}`);
    ws.binaryType = "arraybuffer";
    ws.onopen = () => send({ type: "start", cols: term.cols, rows: term.rows });
    ws.onmessage = (ev) => {
      if (typeof ev.data !== "string") { term.write(new Uint8Array(ev.data)); return; }
      let m; try { m = JSON.parse(ev.data); } catch { return; }
      if (m.type === "status") info(m.msg);
      else if (m.type === "prompt") prompt(m);
      else if (m.type === "connected") { connected = true; setState("up", "Connected"); term.focus(); }
      else if (m.type === "closed") { connected = false; info("\r\n" + m.reason, "33"); setState("off", "Disconnected"); }
    };
    ws.onclose = (ev) => {
      $("overlay").hidden = true; pending = null;
      if (ev.code === 4401) { info("Not logged in (or the session expired). Log in on the main page, then Reconnect.", "31"); }
      if (connected) info("\r\nConnection closed.", "33");
      connected = false;
      if (!$("stateDot").classList.contains("off")) setState("down", "Disconnected");
    };
  }
  $("btnReconnect").addEventListener("click", () => { term.reset(); connect(); });
  window.addEventListener("beforeunload", (e) => { if (connected) { e.preventDefault(); e.returnValue = ""; } });
  connect();
})();
