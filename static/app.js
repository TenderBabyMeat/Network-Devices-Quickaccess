(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const uid = () => Array.from(crypto.getRandomValues(new Uint8Array(6)), b => b.toString(16).padStart(2, "0")).join("");
  const store = {   // per-browser conveniences only
    get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { } },
  };

  let data = { rev: 0, sites: [] };
  let status = {}, history = {};
  let statusMeta = { interval: 30, cert_warn_days: 30 };
  let TYPES = {};
  let collapsed = new Set(store.get("netdevices.collapsed", []));
  const saveCollapsed = () => store.set("netdevices.collapsed", [...collapsed]);
  let filt = { types: [], tags: [], ...store.get("netdevices.filters", {}) };
  const saveFilt = () => store.set("netdevices.filters", filt);
  const TYPE_SHORT = { server: "Server", vm: "VM", container: "LXC", pc: "PC", pi: "Pi", nas: "NAS", router: "Router",
                       switch: "Switch", ap: "Wi-Fi AP", printer: "Printer", iot: "IoT", media: "Media", phone: "Phone", other: "Other" };

  // ------------------------------------------------------------ api
  async function api(method, path, body) {
    const opt = { method, headers: { "X-ND": "1" }, credentials: "same-origin" };
    if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
    const r = await fetch(path, opt);
    if (r.status === 401) { location.href = "/login"; throw new Error("not logged in"); }
    let j = null;
    try { j = await r.json(); } catch { }
    if (!r.ok) { const e = new Error((j && j.error) || `HTTP ${r.status}`); e.status = r.status; throw e; }
    return j;
  }
  function toast(msg) {
    const t = $("toast"); t.textContent = msg; t.classList.add("show");
    clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove("show"), 2600);
  }
  async function copy(text, what) {
    let ok = false;
    try { if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); ok = true; } } catch { }
    if (!ok) {   // plain http:// pages can't use the Clipboard API
      const ta = document.createElement("textarea");
      ta.value = text; ta.setAttribute("readonly", ""); ta.style.cssText = "position:fixed;top:-100px;opacity:0";
      document.body.appendChild(ta); ta.select();
      try { ok = document.execCommand("copy"); } catch { }
      ta.remove();
    }
    toast(ok ? `Copied ${what}: ${text}` : "Couldn't copy - select it by hand");
  }

  async function load() {
    data = await api("GET", "/api/data");
    render();
  }
  // Apply a change to a copy, save it, and only keep it if the server accepted it.
  async function commit(mutate) {
    const draft = structuredClone(data);
    mutate(draft);
    try {
      const r = await api("PUT", "/api/data", draft);
      draft.rev = r.rev;
      data = draft;
      render();
      setTimeout(pollStatus, 1500);
      return true;
    } catch (e) {
      alert(e.message);
      if (e.status === 409) await load();
      return false;
    }
  }

  // ------------------------------------------------------------ links
  function fillIp(u, d) { return u.replace(/\{ip\}/gi, d.ip.trim()); }
  function absUrl(u, d) {
    u = fillIp((u || "").trim(), d);
    if (!u) return "";
    if (!/^https?:\/\//i.test(u)) u = "https://" + u.replace(/^[a-z][a-z0-9+.-]*:\/\//i, "");
    return u;
  }
  function deviceLinks(d) {   // [{label, url}] - the web UI first, then extras
    const out = [];
    if ((d.web_url || "").trim()) out.push({ label: "Web UI", url: absUrl(d.web_url, d) });
    for (const l of d.links || []) if (l.url) out.push({ label: l.label || "Link", url: absUrl(l.url, d) });
    return out;
  }
  function puttyLink(d) {
    if ((d.putty_session || "").trim()) return "putty://session/" + encodeURIComponent(d.putty_session.trim());
    let host = (d.ssh_host || "").trim() || d.ip.trim();
    if (host.includes(":") && !host.startsWith("[")) host = "[" + host + "]";
    const user = (d.ssh_user || "").trim() ? encodeURIComponent(d.ssh_user.trim()) + "@" : "";
    const port = d.ssh_port && +d.ssh_port !== 22 ? ":" + d.ssh_port : "";
    return "putty://" + user + host + port;
  }

  // ------------------------------------------------------------ status
  const STATE_TEXT = { up: "Up", partial: "Partly reachable", down: "Down", off: "Not monitored", unknown: "Checking…" };
  const CHECK_TEXT = { ping: "Ping", ssh: "SSH", web: "Web UI" };
  const fmtTime = (t) => t ? new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
  function fmtAgo(t) {
    if (!t) return "";
    const s = Date.now() / 1000 - t;
    if (s < 90) return "just now";
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400 * 2) return `${Math.round(s / 3600)} h ago`;
    return new Date(t * 1000).toLocaleDateString([], { month: "short", day: "numeric" });
  }
  function hasChecks(d) {
    return d.chk_ping || (d.chk_ssh && d.ssh_enabled) || (d.chk_web && (d.web_url || "").trim());
  }
  function stateOf(d) {
    if (!hasChecks(d)) return "off";
    const s = status[d.id];
    return s ? s.state : "unknown";
  }
  function certOf(d) {
    const c = status[d.id] && status[d.id].cert;
    if (!c || c.days == null) return null;
    if (c.days < 0) return { cls: "bad", text: "cert expired", c };
    if (c.days <= (statusMeta.cert_warn_days || 30)) return { cls: "warn", text: `cert ${c.days}d`, c };
    return { cls: "", text: "", c };
  }
  async function pollStatus() {
    try {
      const r = await api("GET", "/api/status");
      status = r.devices || {};
      history = r.history || {};
      statusMeta = r;
      const ver = $("ver");
      if (ver && r.version && r.version !== ver.dataset.version && !ver.classList.contains("stale")) {
        ver.textContent = `Page v${ver.dataset.version} · server is now v${r.version} - click to reload`;
        ver.classList.add("stale");
        ver.addEventListener("click", () => location.reload());
      }
      if (sortBy.key === "status") render(); else updateDots();
    } catch { }
  }
  function tally(devs) {
    const c = { up: 0, partial: 0, down: 0 };
    devs.forEach(d => { const s = stateOf(d); if (s in c) c[s]++; });
    return ["up", "partial", "down"].filter(k => c[k]).map(k =>
      `<span class="tally" title="${STATE_TEXT[k]}"><span class="dot ${k}"></span>${c[k]}</span>`).join("");
  }
  function updateDots() {
    document.querySelectorAll("[data-dot]").forEach(el => {
      const d = findDevice(el.dataset.dot);
      if (!d) return;
      const st = stateOf(d);
      el.className = "dot " + (st === "unknown" ? "checking" : st);
      el.setAttribute("aria-label", STATE_TEXT[st]);
    });
    document.querySelectorAll("[data-cert]").forEach(el => {
      const d = findDevice(el.dataset.cert), c = d && certOf(d);
      el.hidden = !(c && c.cls);
      if (c && c.cls) { el.className = "cert-pill " + c.cls; el.textContent = c.text; el.title = `TLS certificate for the Web UI expires ${c.c.expires}`; }
    });
    data.sites.forEach(s => { const el = document.querySelector(`[data-tally="${s.id}"]`); if (el) el.innerHTML = tally(s.devices); });
    const all = data.sites.flatMap(s => s.devices);
    let extra = "";
    const certs = all.filter(d => { const c = certOf(d); return c && c.cls; }).length;
    if (certs) extra += `<span class="cert-pill warn" title="Web UI certificates expiring within ${statusMeta.cert_warn_days || 30} days">${certs} cert${certs > 1 ? "s" : ""}</span>`;
    if (statusMeta.ping_available === false) extra += `<span title="The container isn't allowed to send ping. Port checks still work. See README.">ping unavailable</span>`;
    $("summary").innerHTML = tally(all) + extra;
    if (popFor) showPop(popFor.el, popFor.id, true);
  }

  // ------------------------------------------------------------ status popover
  let popFor = null;
  function popHtml(d) {
    const s = status[d.id], h = history[d.id] || {}, st = stateOf(d);
    const rows = [];
    rows.push(`<div class="pop-head"><span class="dot ${st === "unknown" ? "checking" : st}"></span><b>${esc(STATE_TEXT[st])}</b>
      ${s && st !== "off" ? `<span class="muted">since ${esc(fmtTime(s.since))}</span>` : ""}</div>`);
    if (s && s.checks) {
      const items = Object.entries(s.checks).map(([k, c]) => {
        const v = c.ok === null ? `<span class="muted">n/a</span>` : c.ok ? `<span class="ok">✓</span>${c.ms != null ? ` ${c.ms} ms` : ""}` : `<span class="bad">✗</span>`;
        return `<span>${CHECK_TEXT[k]} ${v}${c.note && c.ok !== true ? ` <span class="muted">(${esc(c.note)})</span>` : ""}</span>`;
      });
      if (items.length) rows.push(`<div class="pop-checks">${items.join("")}</div>`);
    }
    const c = certOf(d);
    if (c) rows.push(`<div class="${c.cls ? "cert-" + c.cls : "muted"}">Certificate ${c.c.days < 0 ? "expired" : "expires"} ${esc(c.c.expires)} (${c.c.days} days)${c.c.self_signed ? " · self-signed" : ""}</div>`);
    else if (s && s.cert && s.cert.error) rows.push(`<div class="muted">Certificate: couldn't read (${esc(s.cert.error)})</div>`);
    if (h.strip) {
      const cells = [...h.strip].map(ch => `<i class="${{ u: "up", p: "partial", d: "down" }[ch] || "none"}"></i>`).join("");
      rows.push(`<div class="strip" aria-label="Last 24 hours">${cells}</div><div class="strip-axis"><span>24 h ago</span><span>now</span></div>`);
    }
    const up = (v) => v == null ? "-" : `${v}%`;
    if (h.up24 != null || h.up7d != null) rows.push(`<div>Uptime: <b>${up(h.up24)}</b> 24 h · <b>${up(h.up7d)}</b> 7 days</div>`);
    if (h.last_down) rows.push(`<div class="muted">Last down: ${esc(fmtAgo(h.last_down))}</div>`);
    if (s && st !== "off") rows.push(`<div class="muted">Checked ${esc(fmtTime(s.checked))}</div>`);
    return rows.join("");
  }
  function showPop(el, id, refresh = false) {
    const d = findDevice(id);
    if (!d || !el.isConnected) return hidePop();
    const pop = $("pop");
    pop.innerHTML = popHtml(d);
    pop.hidden = false;
    if (refresh) return;
    popFor = { el, id };
    const r = el.getBoundingClientRect();
    const w = pop.offsetWidth, h = pop.offsetHeight;
    const x = Math.min(window.innerWidth - w - 8, Math.max(8, r.left - 10));
    let y = r.bottom + 8;
    if (y + h > window.innerHeight - 8) y = Math.max(8, r.top - h - 8);
    pop.style.left = x + "px"; pop.style.top = y + "px";
  }
  function hidePop() { $("pop").hidden = true; popFor = null; }
  $("sites").addEventListener("mouseover", (e) => {
    const t = e.target.closest("[data-dot]");
    if (t && (!popFor || popFor.el !== t)) showPop(t, t.dataset.dot);
  });
  $("sites").addEventListener("mouseout", (e) => {
    const t = e.target.closest("[data-dot]");
    if (t && !t.contains(e.relatedTarget) && popFor && !popFor.pinned) hidePop();
  });
  document.addEventListener("click", (e) => {
    const t = e.target.closest("[data-dot]");
    if (t) { showPop(t, t.dataset.dot); if (popFor) popFor.pinned = true; return; }
    if (!e.target.closest("#pop")) hidePop();
  });
  window.addEventListener("scroll", hidePop, { passive: true });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") hidePop();
    if ((e.key === "Enter" || e.key === " ") && e.target.matches && e.target.matches("[data-dot]")) {
      e.preventDefault(); showPop(e.target, e.target.dataset.dot); popFor.pinned = true;
    }
  });

  // ------------------------------------------------------------ sorting (per browser; doesn't change saved order)
  let sortBy = { key: "", dir: 1, ...store.get("netdevices.sort", {}) };
  const saveSort = () => store.set("netdevices.sort", sortBy);
  const SORT_LABEL = { hostname: "name", ip: "IP", model: "type / model", status: "status" };
  const STATUS_RANK = { down: 0, partial: 1, unknown: 2, up: 3, off: 4 };
  function ipParts(ip) {
    const p = String(ip).trim().split(".");
    return p.length === 4 && p.every(n => /^\d{1,3}$/.test(n)) ? p.map(Number) : null;
  }
  const natural = (a, b) => String(a || "").localeCompare(String(b || ""), undefined, { numeric: true, sensitivity: "base" });
  function compareBy(key, a, b) {
    switch (key) {
      case "ip": {
        const x = ipParts(a.ip), y = ipParts(b.ip);
        if (x && y) { for (let i = 0; i < 4; i++) if (x[i] !== y[i]) return x[i] - y[i]; return 0; }
        return natural(a.ip, b.ip);
      }
      case "model": return natural(TYPE_SHORT[a.dtype] || "~", TYPE_SHORT[b.dtype] || "~") || natural(a.model, b.model);
      case "status": return STATUS_RANK[stateOf(a)] - STATUS_RANK[stateOf(b)];
      default: return natural(a.hostname, b.hostname);
    }
  }
  const missing = (k, d) => k === "ip" ? !ipParts(d.ip) : k === "model" ? !(d.dtype || d.model) : false;
  function sorted(devs) {
    if (!sortBy.key) return devs;
    const k = sortBy.key;
    return [...devs].sort((a, b) => (missing(k, a) - missing(k, b)) || (compareBy(k, a, b) * sortBy.dir)
      || natural(a.hostname, b.hostname) || compareBy("ip", a, b));
  }
  function sortHead(key, label, extra = "") {
    const on = sortBy.key === key;
    const arrow = on && key !== "status" ? (sortBy.dir > 0 ? " ▲" : " ▼") : "";
    const next = key === "status" ? (!on ? "problems first" : sortBy.dir > 0 ? "healthy first" : "saved order")
      : !on ? "ascending" : sortBy.dir > 0 ? "descending" : "saved order";
    return `<button type="button" class="th-sort${on ? " active" : ""} ${extra}" data-sort="${key}" title="Sort by ${SORT_LABEL[key]} (click: ${next})">${label}${arrow}</button>`;
  }
  function statusHeadDot() {
    const fill = sortBy.key === "status" ? (sortBy.dir > 0 ? " down" : " up") : "";
    return `<span class="dot hdr-dot${fill}"></span>`;
  }
  function setSort(key, dir) {
    sortBy = { key, dir }; saveSort();
    $("sortSel").value = key ? `${key}:${dir}` : "";
    render();
  }

  // ------------------------------------------------------------ filters (types + tags)
  function matches(d, q) {
    if (filt.types.length && !filt.types.includes(d.dtype || "")) return false;
    if (filt.tags.length && !(d.tags || []).some(t => filt.tags.includes(t.toLowerCase()))) return false;
    if (!q) return true;
    return [d.hostname, d.ip, d.model, d.notes, d.putty_session, TYPES[d.dtype], ...(d.tags || [])]
      .some(v => (v || "").toLowerCase().includes(q));
  }
  function renderFilters() {
    const all = data.sites.flatMap(s => s.devices);
    const types = {}, tags = {};
    all.forEach(d => { if (d.dtype) types[d.dtype] = (types[d.dtype] || 0) + 1;
                       (d.tags || []).forEach(t => { const k = t.toLowerCase(); tags[k] = tags[k] || { name: t, n: 0 }; tags[k].n++; }); });
    filt.types = filt.types.filter(t => t in types);
    filt.tags = filt.tags.filter(t => t in tags);
    const box = $("filters");
    const tKeys = Object.keys(TYPES).filter(t => t in types);
    const gKeys = Object.keys(tags).sort();
    if (tKeys.length + gKeys.length < 2) { box.hidden = true; box.innerHTML = ""; return; }
    box.hidden = false;
    const chip = (kind, key, label, n) => {
      const on = filt[kind].includes(key);
      return `<button type="button" class="chip${on ? " on" : ""}${kind === "tags" ? " tag" : ""}" data-filter="${kind}" data-key="${esc(key)}" aria-pressed="${on}">${esc(label)} <span class="n">${n}</span></button>`;
    };
    box.innerHTML = tKeys.map(t => chip("types", t, TYPE_SHORT[t] || t, types[t])).join("")
      + (gKeys.length && tKeys.length ? `<span class="sep"></span>` : "")
      + gKeys.map(k => chip("tags", k, "#" + tags[k].name, tags[k].n)).join("")
      + (filt.types.length || filt.tags.length ? `<button type="button" class="chip clear" data-filter="clear">Clear</button>` : "");
  }
  $("filters").addEventListener("click", (e) => {
    const b = e.target.closest("[data-filter]");
    if (!b) return;
    if (b.dataset.filter === "clear") filt = { types: [], tags: [] };
    else {
      const list = filt[b.dataset.filter], k = b.dataset.key;
      list.includes(k) ? list.splice(list.indexOf(k), 1) : list.push(k);
    }
    saveFilt(); render();
  });

  // ------------------------------------------------------------ render
  const findSite = (id) => data.sites.find(s => s.id === id);
  const findDevice = (id) => { for (const s of data.sites) { const d = s.devices.find(x => x.id === id); if (d) return d; } return null; };

  function actionsHtml(s, d) {
    const links = deviceLinks(d);
    let h = "";
    if (links.length) {
      const first = links[0];
      h += `<span class="btn-group"><a class="btn small web" href="${esc(first.url)}" target="_blank" rel="noopener noreferrer" title="${esc(first.url)}">${esc(first.label)}</a>`;
      if (links.length > 1) {
        h += `<details class="menu link-menu"><summary class="btn small web" aria-label="More links for ${esc(d.hostname)}" title="More links">▾</summary><div class="menu-list">`
          + links.slice(1).map(l => `<a href="${esc(l.url)}" target="_blank" rel="noopener noreferrer" title="${esc(l.url)}">${esc(l.label)}</a>`).join("")
          + `</div></details>`;
      }
      h += `</span>`;
    }
    if (d.ssh_enabled) {
      h += `<a class="btn small ssh putty-btn" href="${esc(puttyLink(d))}" title="Open in PuTTY on this PC: ${esc(puttyLink(d))}">PuTTY</a>`;
      h += `<a class="btn small ssh" href="/term?d=${encodeURIComponent(d.id)}" target="_blank" rel="noopener" title="SSH in a browser tab">Web SSH</a>`;
    }
    h += `<button class="small" data-act="edit" data-site="${s.id}" data-dev="${d.id}">Edit</button>`;
    h += `<button class="small icon danger" data-act="del" data-site="${s.id}" data-dev="${d.id}" title="Delete device" aria-label="Delete ${esc(d.hostname)}">✕</button>`;
    return h;
  }

  function render() {
    renderFilters();
    const q = $("search").value.trim().toLowerCase();
    const root = $("sites");
    if (!data.sites.length) {
      root.innerHTML = `<div class="site"><div class="empty">Nothing here yet. Click <b>+ Add location</b> (e.g. “Home”), then add devices -
        or use <b>Scan</b> to find what's on your network, or ⋯ → Import to restore a backup.</div></div>`;
      updateDots();
      return;
    }
    const filtering = q || filt.types.length || filt.tags.length;
    const html = data.sites.map((s, si) => {
      const devs = sorted(s.devices).filter(d => matches(d, q));
      if (filtering && !devs.length && !(q && s.name.toLowerCase().includes(q))) return "";
      const isCol = collapsed.has(s.id) && !filtering;
      const rows = devs.map(d => `
        <tr>
          <td class="host"><span class="dot checking" data-dot="${d.id}" tabindex="0" role="button"></span><button type="button" class="copyable" data-copy="${esc(d.hostname)}" data-what="name" title="Click to copy">${esc(d.hostname)}</button><span class="cert-pill" data-cert="${d.id}" hidden></span></td>
          <td class="mono c-ip"><button type="button" class="copyable" data-copy="${esc(d.ip)}" data-what="IP" title="Click to copy">${esc(d.ip)}</button></td>
          <td class="c-model">${d.dtype ? `<span class="type-pill" title="${esc(TYPES[d.dtype] || d.dtype)}">${esc(TYPE_SHORT[d.dtype] || d.dtype)}</span>` : ""}${esc(d.model)}</td>
          <td class="notes" title="${esc(d.notes)}">${(d.tags || []).map(t => `<button type="button" class="tag-chip" data-tag="${esc(t.toLowerCase())}" title="Show only #${esc(t)}">#${esc(t)}</button>`).join("")}${esc(d.notes)}</td>
          <td class="actions">${actionsHtml(s, d)}</td>
        </tr>`).join("");
      return `
        <section class="site${isCol ? " collapsed" : ""}">
          <div class="site-head">
            <h2 data-act="toggle" data-site="${s.id}"><span class="caret">▾</span> ${esc(s.name)}</h2>
            <span class="count">${s.devices.length} device${s.devices.length === 1 ? "" : "s"} <span data-tally="${s.id}"></span></span>
            <span style="flex:1"></span>
            <button class="small primary" data-act="add" data-site="${s.id}">+ Add device</button>
            ${sortBy.key && s.devices.length > 1 ? `<button class="small" data-act="keeporder" data-site="${s.id}" title="Save the current ${SORT_LABEL[sortBy.key]} sort as this location's order, for every browser">Keep this order</button>` : ""}
            <button class="small icon" data-act="up" data-site="${s.id}" title="Move location up" ${si === 0 ? "disabled" : ""}>↑</button>
            <button class="small icon" data-act="down" data-site="${s.id}" title="Move location down" ${si === data.sites.length - 1 ? "disabled" : ""}>↓</button>
            <button class="small" data-act="rename" data-site="${s.id}">Rename</button>
            <button class="small icon danger" data-act="delsite" data-site="${s.id}" title="Delete location" aria-label="Delete location ${esc(s.name)}">✕</button>
          </div>
          <div class="site-body">
            ${devs.length ? `<table>
              <colgroup><col class="c1"><col class="c2"><col class="c3"><col class="c4"><col class="c5"></colgroup>
              <thead><tr><th>${sortHead("status", statusHeadDot(), "th-dot")}${sortHead("hostname", "Name")}</th><th class="c-ip">${sortHead("ip", "IP")}</th><th class="c-model">${sortHead("model", "Type / model")}</th><th class="c-notes">Tags / notes</th><th></th></tr></thead>
              <tbody>${rows}</tbody></table>`
            : `<div class="empty">${filtering ? "No matching devices." : "No devices yet."}</div>`}
          </div>
        </section>`;
    }).join("");
    root.innerHTML = html || `<div class="site"><div class="empty">Nothing matches${q ? ` “${esc(q)}”` : ""}${filt.types.length || filt.tags.length ? " with the selected filters" : ""}.</div></div>`;
    updateDots();
  }

  // ------------------------------------------------------------ list interactions
  $("sites").addEventListener("click", (e) => {
    const sortBtn = e.target.closest("[data-sort]");
    if (sortBtn) {
      const key = sortBtn.dataset.sort;
      if (sortBy.key !== key) setSort(key, 1); else if (sortBy.dir > 0) setSort(key, -1); else setSort("", 1);
      return;
    }
    const cp = e.target.closest("[data-copy]");
    if (cp) { copy(cp.dataset.copy, cp.dataset.what); return; }
    const tg = e.target.closest("[data-tag]");
    if (tg) { filt.tags = [tg.dataset.tag]; filt.types = []; saveFilt(); render(); window.scrollTo({ top: 0 }); return; }
    const el = e.target.closest("[data-act]");
    if (!el) return;
    const site = findSite(el.dataset.site);
    if (!site) return;
    const sid = site.id;
    switch (el.dataset.act) {
      case "toggle":
        collapsed.has(sid) ? collapsed.delete(sid) : collapsed.add(sid);
        saveCollapsed(); render(); break;
      case "add": openDevice(sid, null); break;
      case "edit": openDevice(sid, el.dataset.dev); break;
      case "del": {
        const d = site.devices.find(x => x.id === el.dataset.dev);
        if (d && confirm(`Delete "${d.hostname}"?`))
          commit(dr => { const s = dr.sites.find(x => x.id === sid); s.devices = s.devices.filter(x => x.id !== d.id); });
        break;
      }
      case "keeporder": {
        const order = sorted(site.devices).map(d => d.id);
        commit(dr => dr.sites.find(x => x.id === sid).devices.sort((a, b) => order.indexOf(a.id) - order.indexOf(b.id)))
          .then(ok => ok && toast(`Saved ${site.name}'s order.`));
        break;
      }
      case "up": case "down": {
        const dir = el.dataset.act === "up" ? -1 : 1;
        commit(dr => { const i = dr.sites.findIndex(x => x.id === sid), j = i + dir;
          if (j >= 0 && j < dr.sites.length) [dr.sites[i], dr.sites[j]] = [dr.sites[j], dr.sites[i]]; });
        break;
      }
      case "rename": openSite(sid); break;
      case "delsite": {
        const n = site.devices.length;
        if (confirm(`Delete location "${site.name}"${n ? ` and its ${n} device${n === 1 ? "" : "s"}` : ""}?`))
          commit(dr => { dr.sites = dr.sites.filter(x => x.id !== sid); }).then(ok => { if (ok) { collapsed.delete(sid); saveCollapsed(); } });
        break;
      }
    }
  });
  // Per-device link menus. The table cells clip anything that overflows them, so an open menu is
  // pinned to the viewport next to its ▾ button instead of being positioned inside the cell.
  function placeLinkMenu(m) {
    const list = m.querySelector(".menu-list"), r = m.querySelector("summary").getBoundingClientRect();
    list.style.position = "fixed";
    list.style.right = "auto";
    list.style.top = list.style.left = "0px";
    const w = list.offsetWidth, h = list.offsetHeight;
    list.style.left = Math.max(8, Math.min(window.innerWidth - w - 8, r.right - w)) + "px";
    list.style.top = (r.bottom + h + 6 > window.innerHeight ? Math.max(8, r.top - h - 6) : r.bottom + 6) + "px";
  }
  const closeLinkMenus = (except) => document.querySelectorAll("details.link-menu[open]").forEach(m => { if (m !== except) m.open = false; });
  document.addEventListener("toggle", (e) => {
    const m = e.target;
    if (!(m instanceof HTMLDetailsElement) || !m.classList.contains("link-menu") || !m.open) return;
    closeLinkMenus(m);
    hidePop();
    placeLinkMenu(m);
  }, true);
  document.addEventListener("click", (e) => {
    document.querySelectorAll("details.link-menu[open]").forEach(m => { if (!m.contains(e.target)) m.open = false; });
    const a = e.target.closest("details.link-menu .menu-list a");
    if (a) setTimeout(() => a.closest("details").open = false, 0);   // close after the link opens its tab
  });
  window.addEventListener("scroll", () => closeLinkMenus(), { passive: true });
  window.addEventListener("resize", () => closeLinkMenus());
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeLinkMenus(); });
  $("sortSel").addEventListener("change", (e) => { const [k, d] = e.target.value.split(":"); setSort(k || "", +d || 1); });
  $("sortSel").value = sortBy.key ? `${sortBy.key}:${sortBy.dir}` : "";

  // ------------------------------------------------------------ location dialog
  let editingSite = null;
  function openSite(id) {
    editingSite = id;
    const s = id ? findSite(id) : null;
    $("siteTitle").textContent = s ? "Rename location" : "Add location";
    $("fSiteName").value = s ? s.name : "";
    $("siteDlg").showModal(); $("fSiteName").focus();
  }
  $("btnAddSite").addEventListener("click", () => openSite(null));
  $("siteCancel").addEventListener("click", () => $("siteDlg").close());
  $("siteForm").addEventListener("submit", (e) => {
    const name = $("fSiteName").value.trim();
    if (!name) { e.preventDefault(); return; }
    const id = editingSite;
    commit(dr => { if (id) dr.sites.find(x => x.id === id).name = name; else dr.sites.push({ id: uid(), name, devices: [] }); });
  });

  // ------------------------------------------------------------ device dialog
  let editing = null;
  function linkRow(l = { label: "", url: "" }) {
    const div = document.createElement("div");
    div.className = "link-row";
    div.innerHTML = `<input class="lk-label" placeholder="Label, e.g. iDRAC" maxlength="30" value="${esc(l.label)}" aria-label="Link label">
      <input class="lk-url mono" placeholder="https://{ip}:8443" value="${esc(l.url)}" aria-label="Link address">
      <button type="button" class="small icon danger" title="Remove link" aria-label="Remove link">✕</button>`;
    div.querySelector("button").addEventListener("click", () => div.remove());
    return div;
  }
  function syncSsh() {
    const on = $("fSshOn").checked;
    $("sshFields").hidden = !on; $("sshOffHint").hidden = on;
    $("fChkSsh").disabled = !on;
    $("fChkWeb").disabled = !$("fWeb").value.trim();
  }
  $("fSshOn").addEventListener("change", syncSsh);
  $("fWeb").addEventListener("input", syncSsh);
  $("btnAddLink").addEventListener("click", () => { const r = linkRow(); $("linkRows").appendChild(r); r.querySelector("input").focus(); });

  function openDevice(siteId, devId) {
    const site = findSite(siteId);
    const d = devId ? site.devices.find(x => x.id === devId) : null;
    editing = { siteId, devId };
    $("devTitle").textContent = d ? `Edit ${d.hostname}` : `Add device to ${site.name}`;
    $("fSite").innerHTML = data.sites.map(s => `<option value="${s.id}">${esc(s.name)}</option>`).join("");
    $("fSite").value = siteId;
    $("fType").innerHTML = `<option value="">-</option>` + Object.entries(TYPES).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
    const v = d || { hostname: "", ip: "", model: "", web_url: "", ssh_host: "", ssh_user: "", ssh_port: 22, putty_session: "",
                     legacy_ssh: false, chk_ping: true, chk_ssh: true, chk_web: true, notes: "", dtype: "", tags: [], links: [],
                     ssh_enabled: false };
    $("fHost").value = v.hostname; $("fIp").value = v.ip; $("fModel").value = v.model; $("fWeb").value = v.web_url;
    $("fType").value = v.dtype || ""; $("fTags").value = (v.tags || []).join(", ");
    $("fSshOn").checked = !!v.ssh_enabled;
    $("fSshHost").value = v.ssh_host; $("fSshUser").value = v.ssh_user;
    $("fSshPort").value = v.ssh_port && +v.ssh_port !== 22 ? v.ssh_port : "";
    $("fPutty").value = v.putty_session; $("fLegacy").checked = !!v.legacy_ssh;
    $("fChkPing").checked = !!v.chk_ping; $("fChkSsh").checked = !!v.chk_ssh; $("fChkWeb").checked = !!v.chk_web;
    $("fNotes").value = v.notes;
    $("linkRows").innerHTML = "";
    (v.links || []).forEach(l => $("linkRows").appendChild(linkRow(l)));
    $("btnForgetKey").hidden = !d;
    $("devMsg").textContent = "";
    syncSsh();
    $("devDlg").showModal(); $("fHost").focus();
  }
  $("devCancel").addEventListener("click", () => $("devDlg").close());
  $("btnForgetKey").addEventListener("click", async () => {
    if (!editing || !editing.devId) return;
    if (!confirm("Forget the saved SSH host key for this device? Web SSH will ask you to verify the key again next time.")) return;
    try {
      const r = await api("POST", "/api/hostkey/forget", { device_id: editing.devId });
      toast(r.removed ? "Saved host key forgotten." : "No host key was saved for this device.");
    } catch (e) { alert(e.message); }
  });
  const URL_OK = (u) => !u || /^https?:\/\//i.test(u) || !/^[a-z][a-z0-9+.-]*:(?!\d)/i.test(u);
  $("devForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = (t) => { $("devMsg").textContent = t; };
    const hostname = $("fHost").value.trim(), ip = $("fIp").value.trim();
    if (!hostname || !ip) return msg("Name and IP are required.");
    const sshOn = $("fSshOn").checked;
    const port = $("fSshPort").value.trim() === "" ? 22 : parseInt($("fSshPort").value, 10);
    if (sshOn && !(port >= 1 && port <= 65535)) return msg("SSH port must be between 1 and 65535.");
    const sshHost = $("fSshHost").value.trim();
    if (sshHost && !/^[A-Za-z0-9.\-:\[\]%]+$/.test(sshHost)) return msg("SSH host may only contain letters, numbers, dots, hyphens and (IPv6) colons.");
    const sshUser = $("fSshUser").value.trim();
    if (sshUser && !/^[A-Za-z0-9._\-\\@]+$/.test(sshUser)) return msg("Username has characters that can't be used.");
    const putty = $("fPutty").value.trim();
    if (/["\\]/.test(putty)) return msg("PuTTY session names can't contain quotes or backslashes.");
    const web = $("fWeb").value.trim();
    if (!URL_OK(web)) return msg("The Web UI address must start with http:// or https://");
    const links = [...$("linkRows").querySelectorAll(".link-row")].map(r => ({
      label: r.querySelector(".lk-label").value.trim(), url: r.querySelector(".lk-url").value.trim() })).filter(l => l.url);
    if (links.some(l => !URL_OK(l.url))) return msg("Link addresses must start with http:// or https://");
    const tags = $("fTags").value.split(",").map(t => t.trim()).filter(Boolean);
    const bad = tags.find(t => !/^[\w .+&/-]{1,24}$/.test(t));
    if (bad) return msg(`Tag "${bad}": letters, numbers, spaces and . + & / - only, 24 characters max.`);
    const { siteId, devId } = editing;
    const rec = {
      id: devId || uid(), hostname, ip, model: $("fModel").value.trim(), web_url: web, dtype: $("fType").value, tags, links,
      ssh_enabled: sshOn, ssh_host: sshHost, ssh_user: sshUser, ssh_port: sshOn ? port : 22, putty_session: putty, legacy_ssh: $("fLegacy").checked,
      chk_ping: $("fChkPing").checked, chk_ssh: $("fChkSsh").checked, chk_web: $("fChkWeb").checked, notes: $("fNotes").value.trim(),
    };
    const toId = $("fSite").value;
    const ok = await commit(dr => {
      const from = dr.sites.find(x => x.id === siteId), to = dr.sites.find(x => x.id === toId) || from;
      if (devId) {
        const i = from.devices.findIndex(x => x.id === devId);
        if (to === from) from.devices[i] = rec; else { from.devices.splice(i, 1); to.devices.push(rec); }
      } else to.devices.push(rec);
    });
    if (ok) { delete status[rec.id]; $("devDlg").close(); }
  });

  // ------------------------------------------------------------ network scan
  let scanTimer = null, scanJob = null;
  const PORT_NAMES = {};
  function scanRow(r, i) {
    const s = r.suggest;
    const portList = r.ports.map(p => `<span class="port" title="${esc(PORT_NAMES[p] || "")}">${p}</span>`).join("");
    if (r.known) return `<tr class="known"><td></td><td class="mono">${esc(r.ip)}</td><td colspan="3" class="muted">already added as <b>${esc(r.known)}</b></td><td>${portList}</td></tr>`;
    return `<tr>
      <td><input type="checkbox" class="sc-pick" data-i="${i}" data-ip="${esc(r.ip)}" aria-label="Add ${esc(r.ip)}"></td>
      <td class="mono">${esc(r.ip)}${r.ping_ms != null ? `<div class="muted small-note">${r.ping_ms} ms</div>` : ""}</td>
      <td><input class="sc-name" data-i="${i}" value="${esc(s.hostname)}" aria-label="Name for ${esc(r.ip)}">${r.name ? `<div class="muted small-note" title="Reverse DNS">${esc(r.name)}</div>` : ""}</td>
      <td><select class="sc-type" data-i="${i}" aria-label="Type for ${esc(r.ip)}"><option value="">-</option>${Object.entries(TYPES).map(([k, v]) => `<option value="${k}"${k === s.dtype ? " selected" : ""}>${esc(v)}</option>`).join("")}</select>${s.model ? `<div class="muted small-note">${esc(s.model)}</div>` : ""}</td>
      <td class="mono small-note">${s.web_url ? esc(s.web_url) : `<span class="muted">-</span>`}${s.ssh_enabled ? `<div class="muted">+ SSH</div>` : ""}</td>
      <td>${portList || `<span class="muted">ping only</span>`}</td>
    </tr>`;
  }
  function renderScan() {
    const j = scanJob;
    const running = j && j.state === "running";
    $("scanGo").hidden = running; $("scanStop").hidden = !running; $("scanCidr").disabled = running;
    $("scanProgress").hidden = !j;
    if (!j) { $("scanResults").innerHTML = ""; $("scanState").textContent = ""; updateScanAdd(); return; }
    $("scanBar").style.width = (j.total ? Math.round(100 * j.done / j.total) : 0) + "%";
    const found = j.results.length, fresh = j.results.filter(r => !r.known).length;
    $("scanState").textContent = running ? `Scanning ${j.cidr}… ${j.done}/${j.total} checked, ${found} found`
      : j.state === "done" ? `${j.cidr}: ${found} found, ${fresh} new` : `Scan ${j.state}${j.error ? ": " + j.error : ""}`;
    const showKnown = $("scanShowKnown").checked;
    // Keep ticks and edits across refreshes. Rows are matched by IP: the list is re-sorted when the scan ends.
    const keep = {};
    document.querySelectorAll(".sc-pick").forEach(c => {
      const i = c.dataset.i;
      keep[c.dataset.ip] = { on: c.checked, name: document.querySelector(`.sc-name[data-i="${i}"]`).value,
                             type: document.querySelector(`.sc-type[data-i="${i}"]`).value };
    });
    const rows = j.results.map((r, i) => (!r.known || showKnown) ? scanRow(r, i) : "").join("");
    $("scanResults").innerHTML = found ? `<div class="scan-table-wrap"><table class="scan-table">
      <thead><tr><th><input type="checkbox" id="scanAll" aria-label="Select all new"></th><th>IP</th><th>Name</th><th>Type</th><th>Web UI / SSH</th><th>Open ports</th></tr></thead>
      <tbody>${rows}</tbody></table></div>` : (running ? "" : `<div class="empty">Nothing answered in that range.</div>`);
    document.querySelectorAll(".sc-pick").forEach(c => {
      const k = keep[c.dataset.ip], i = c.dataset.i;
      if (!k) return;
      c.checked = k.on;
      document.querySelector(`.sc-name[data-i="${i}"]`).value = k.name;
      document.querySelector(`.sc-type[data-i="${i}"]`).value = k.type;
    });
    updateScanAdd();
  }
  function updateScanAdd() {
    const n = document.querySelectorAll(".sc-pick:checked").length;
    $("scanAdd").disabled = !n || !data.sites.length;
    $("scanAdd").textContent = n ? `Add ${n} device${n > 1 ? "s" : ""}` : "Add selected";
  }
  async function pollScan() {
    try {
      const r = await api("GET", "/api/scan");
      Object.assign(PORT_NAMES, r.ports || {});
      if (!$("scanCidr").value) $("scanCidr").value = (r.job && r.job.cidr) || r.suggest;
      scanJob = r.job;
      if (!document.querySelector(".sc-name:focus, .sc-type:focus")) renderScan();
      if (scanJob && scanJob.state === "running") scanTimer = setTimeout(pollScan, 1000);
    } catch (e) { $("scanState").textContent = e.message; }
  }
  function openScan() {
    $("scanSite").innerHTML = data.sites.map(s => `<option value="${s.id}">${esc(s.name)}</option>`).join("")
      || `<option value="">(add a location first)</option>`;
    $("scanDlg").showModal();
    pollScan();
  }
  $("btnScan").addEventListener("click", openScan);
  $("scanForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("POST", "/api/scan", { cidr: $("scanCidr").value.trim() });
      clearTimeout(scanTimer); pollScan();
    } catch (err) { $("scanState").textContent = err.message; }
  });
  $("scanStop").addEventListener("click", () => api("POST", "/api/scan", { action: "cancel" }).catch(() => { }));
  $("scanClose").addEventListener("click", () => { clearTimeout(scanTimer); $("scanDlg").close(); });
  $("scanDlg").addEventListener("close", () => clearTimeout(scanTimer));
  $("scanShowKnown").addEventListener("change", renderScan);
  $("scanResults").addEventListener("change", (e) => {
    if (e.target.id === "scanAll") document.querySelectorAll(".sc-pick").forEach(c => c.checked = e.target.checked);
    updateScanAdd();
  });
  $("scanAdd").addEventListener("click", async () => {
    const siteId = $("scanSite").value;
    if (!siteId) return;
    const recs = [...document.querySelectorAll(".sc-pick:checked")].map(c => {
      const i = c.dataset.i, r = scanJob.results[i], s = r.suggest;
      return {
        id: uid(), hostname: document.querySelector(`.sc-name[data-i="${i}"]`).value.trim() || s.hostname, ip: r.ip,
        dtype: document.querySelector(`.sc-type[data-i="${i}"]`).value, model: s.model, web_url: s.web_url,
        ssh_enabled: s.ssh_enabled, ssh_host: "", ssh_user: "", ssh_port: 22, putty_session: "", legacy_ssh: false,
        chk_ping: r.ping_ms != null, chk_ssh: true, chk_web: true, tags: [], links: [], notes: "",
      };
    });
    if (!recs.length) return;
    const ok = await commit(dr => dr.sites.find(x => x.id === siteId).devices.push(...recs));
    if (ok) {
      recs.forEach(r => { const hit = scanJob.results.find(x => x.ip === r.ip); if (hit) hit.known = r.hostname; });
      document.querySelectorAll(".sc-pick:checked").forEach(c => c.checked = false);
      renderScan();
      toast(`Added ${recs.length} device${recs.length > 1 ? "s" : ""}. Edit them to add SSH users, tags or notes.`);
    }
  });

  // ------------------------------------------------------------ backups / settings / menu
  async function openBackups() {
    $("bkList").innerHTML = `<div class="muted">Loading…</div>`;
    if (!$("bkDlg").open) $("bkDlg").showModal();
    try {
      const r = await api("GET", "/api/backups");
      $("bkList").innerHTML = r.backups.length ? r.backups.map(b =>
        `<div class="bk-row"><a href="/api/backups/${encodeURIComponent(b.name)}" download>${esc(b.name)}</a>
         <span class="muted">${new Date(b.mtime * 1000).toLocaleString()} · ${(b.size / 1024).toFixed(1)} KB</span></div>`).join("")
        : `<div class="muted">No backups yet. The first one is written a minute after the app starts.</div>`;
    } catch (e) { $("bkList").textContent = e.message; }
  }
  $("bkNow").addEventListener("click", async () => {
    try { const r = await api("POST", "/api/backups", {}); toast(`Saved ${r.name}`); openBackups(); } catch (e) { alert(e.message); }
  });
  $("bkClose").addEventListener("click", () => $("bkDlg").close());

  $("setCancel").addEventListener("click", () => $("setDlg").close());
  $("setForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const r = await api("PUT", "/api/settings", { title: $("fTitle").value.trim() });
      $("pageTitle").textContent = r.title; document.title = r.title;
      $("setDlg").close(); toast("Saved.");
    } catch (err) { $("setMsg").textContent = err.message; }
  });

  const menu = $("menu");
  document.addEventListener("click", (e) => { if (menu.open && !menu.contains(e.target)) menu.open = false; });
  menu.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-menu]");
    if (!b) return;
    menu.open = false;
    switch (b.dataset.menu) {
      case "scan": openScan(); break;
      case "refresh":
        Object.keys(status).forEach(k => status[k] = { ...status[k], state: "unknown" }); updateDots();
        await api("POST", "/api/status/refresh", {}); setTimeout(pollStatus, 2500); toast("Checking all devices…"); break;
      case "export": location.href = "/api/export"; break;
      case "import": $("importFile").click(); break;
      case "backups": openBackups(); break;
      case "settings": $("fTitle").value = $("pageTitle").textContent; $("setMsg").textContent = ""; $("setDlg").showModal(); break;
      case "putty": $("puttyDlg").showModal(); break;
      case "password": $("pwForm").reset(); $("pwMsg").textContent = ""; $("pwDlg").showModal(); break;
      case "logout": await api("POST", "/api/logout", {}).catch(() => { }); location.href = "/login"; break;
    }
  });
  $("puttyClose").addEventListener("click", () => $("puttyDlg").close());

  $("importFile").addEventListener("change", async (e) => {
    const f = e.target.files[0];
    e.target.value = "";
    if (!f) return;
    let d;
    try { d = JSON.parse(await f.text()); } catch (err) { return alert("That file isn't valid JSON: " + err.message); }
    if (!d || !Array.isArray(d.sites)) return alert("That file doesn't contain any locations.");
    const n = d.sites.reduce((t, s) => t + ((s.devices || []).length), 0);
    if (!confirm(`Replace ALL current locations and devices with ${d.sites.length} location(s) and ${n} device(s) from "${f.name}"?`)) return;
    if (await commit(dr => { dr.sites = d.sites; })) { await load(); toast("Imported."); }
  });

  $("pwCancel").addEventListener("click", () => $("pwDlg").close());
  $("pwForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    if ($("pwNew").value !== $("pwNew2").value) { $("pwMsg").textContent = "New passwords don't match."; return; }
    try {
      await api("POST", "/api/password", { current: $("pwCur").value, new: $("pwNew").value });
      $("pwDlg").close(); toast("Password changed. Other sessions were signed out.");
    } catch (err) { $("pwMsg").textContent = err.message; }
  });

  $("search").addEventListener("input", render);

  // ------------------------------------------------------------ start
  Promise.all([api("GET", "/api/settings"), api("GET", "/api/data")]).then(([st, d]) => {
    TYPES = st.types || {};
    data = d;
    render();
    pollStatus();
    const every = () => Math.max(5, Math.min(15, (statusMeta.interval || 30) / 2)) * 1000;
    const loop = () => { if (!document.hidden) pollStatus(); setTimeout(loop, every()); };
    setTimeout(loop, every());
    document.addEventListener("visibilitychange", () => { if (!document.hidden) pollStatus(); });
  }).catch(e => { $("sites").innerHTML = `<div class="site"><div class="empty">Couldn't load: ${esc(e.message)}</div></div>`; });
})();
