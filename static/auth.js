(() => {
  "use strict";
  const form = document.getElementById("loginForm");
  const msg = document.getElementById("msg");
  const setup = form.dataset.mode === "setup";
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    msg.textContent = "";
    const pw = document.getElementById("pw").value;
    let path = "/api/login", body = { password: pw };
    if (setup) {
      if (pw !== document.getElementById("pw2").value) { msg.textContent = "Passwords don't match."; return; }
      path = "/api/setup";
      body = { code: document.getElementById("code").value.trim(), password: pw };
    }
    const btn = form.querySelector("button[type=submit]");
    btn.disabled = true;
    try {
      const r = await fetch(path, { method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-ND": "1" }, body: JSON.stringify(body) });
      if (r.ok) { location.href = "/"; return; }
      let j = {}; try { j = await r.json(); } catch { }
      msg.textContent = j.error || `Error ${r.status}`;
      if (r.status === 409) setTimeout(() => location.href = "/login", 1200);
    } catch (err) {
      msg.textContent = "Can't reach the server.";
    } finally {
      btn.disabled = false;
    }
  });
})();
