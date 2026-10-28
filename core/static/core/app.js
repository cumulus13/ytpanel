(function () {
  "use strict";
  var CFG = JSON.parse(document.getElementById("cfg").textContent);
  var CSRF = document.querySelector('meta[name="csrf-token"]').content;
  var $ = function (id) { return document.getElementById(id); };
  var rows = {}; // id -> {tr, cells}
  var INF = "\u221e";

  function api(method, url, body) {
    var opt = { method: method, credentials: "same-origin", headers: { "X-CSRFToken": CSRF } };
    if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
    return fetch(url, opt).then(function (r) {
      if (r.status === 401) { location.href = "/login/"; throw new Error("auth"); }
      return r.json().catch(function () { return {}; }).then(function (j) { j._status = r.status; return j; });
    });
  }
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function bytes(n) {
    if (n == null) return "?"; var u = ["B", "KiB", "MiB", "GiB", "TiB"], i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i ? n.toFixed(1) : n) + " " + u[i];
  }
  function dur(s) {
    if (s == null) return ""; s = Math.round(s);
    var h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60); s = s % 60;
    return (h ? h + "h " : "") + (h || m ? m + "m " : "") + s + "s";
  }

  // ---- add form ----
  var fmt = $("format");
  CFG.presets.concat(["custom\u2026"]).forEach(function (p) { var o = el("option", null, p); o.value = p.indexOf("custom") === 0 ? "__custom" : p; fmt.appendChild(o); });
  if (CFG.presets.indexOf(CFG.default_format) >= 0) fmt.value = CFG.default_format;
  else { fmt.value = "__custom"; $("format-custom").hidden = false; $("format-custom").value = CFG.default_format; }
  fmt.addEventListener("change", function () { $("format-custom").hidden = fmt.value !== "__custom"; });
  CFG.dirs.forEach(function (d) { var o = el("option", null, d); o.value = d; $("outdir").appendChild(o); });
  if (CFG.default_retries === -1) { $("retries-inf").checked = true; $("retries").disabled = true; } else $("retries").value = CFG.default_retries;
  $("retries-inf").addEventListener("change", function () { $("retries").disabled = this.checked; });

  $("add-form").addEventListener("submit", function (ev) {
    ev.preventDefault();
    var body = {
      urls: $("urls").value,
      format: fmt.value === "__custom" ? $("format-custom").value : fmt.value,
      max_retries: $("retries-inf").checked ? "always" : $("retries").value,
      ratelimit_kib: $("ratelimit").value || 0,
      output_dir: $("outdir").value
    };
    if ($("startat").value) body.start_at = new Date($("startat").value).toISOString();
    $("add-btn").disabled = true;
    api("POST", "/api/downloads/", body).then(function (j) {
      var msg = $("add-msg");
      if (j.created && j.created.length) { $("urls").value = ""; msg.className = "ok"; msg.textContent = "Added " + j.created.length + (j.errors && j.errors.length ? ", " + j.errors.length + " rejected: " + j.errors.map(function (e) { return e.error; }).join("; ") : ""); }
      else { msg.className = "err"; msg.textContent = j.error || (j.errors || []).map(function (e) { return e.error; }).join("; ") || "failed"; }
      refresh();
    }).finally(function () { $("add-btn").disabled = false; });
  });

  // ---- table ----
  var ACTIONS = {
    queued: ["pause", "cancel", "up", "down"], running: ["pause", "cancel"], retrying: ["retry now", "pause", "cancel"],
    paused: ["resume", "cancel"], failed: ["retry", "remove"], cancelled: ["retry", "remove"], completed: ["file", "remove"]
  };
  function act(d, name) {
    var url = "/api/downloads/" + d.id + "/";
    if (name === "retry now" || name === "retry") return api("POST", url + "retry/");
    if (name === "up" || name === "down") return api("POST", url + "move/", { direction: name });
    if (name === "remove") {
      if (!confirm("Remove this entry?")) return;
      var del = d.status === "completed" && confirm("Also delete the downloaded file from disk?");
      return api("POST", url + "remove/", { delete_files: del });
    }
    if (name === "cancel" && !confirm("Cancel this download? Partial data is deleted.")) return;
    return api("POST", url + name + "/");
  }
  function build(d) {
    var tr = el("tr"), c = {};
    c.id = el("td", null, d.id); c.title = el("td", "t"); c.status = el("td"); c.prog = el("td"); c.speed = el("td"); c.retry = el("td"); c.acts = el("td");
    c.t1 = el("div"); c.t2 = el("small"); c.err = el("small", "err"); c.title.append(c.t1, c.t2, c.err);
    c.badge = el("span", "badge"); c.status.appendChild(c.badge); c.owner = el("small", "muted"); c.status.appendChild(c.owner);
    c.bar = el("div", "bar"); c.fill = el("i"); c.bar.appendChild(c.fill); c.ptxt = el("small", "muted"); c.prog.append(c.bar, c.ptxt);
    c.sp = el("div"); c.eta = el("small", "muted"); c.speed.append(c.sp, c.eta);
    c.rl = el("span"); c.rn = el("small", "muted"); c.rb = el("button", "sm ghost", "edit"); c.retry.append(c.rl, " ", c.rb, c.rn);
    c.rb.title = "change number of retries (or 'always')";
    tr.append(c.id, c.title, c.status, c.prog, c.speed, c.retry, c.acts);
    c.tr = tr; return c;
  }
  function render(d) {
    var r = rows[d.id] || (rows[d.id] = build(d)); r.d = d;
    r.t1.textContent = d.title || d.filename || d.url;
    r.t2.textContent = d.title ? d.url : ""; r.err.textContent = d.error && d.status !== "completed" ? d.error : "";
    r.badge.textContent = d.status; r.badge.className = "badge s-" + d.status; r.owner.textContent = d.owner || "";
    r.fill.style.width = (d.percent == null ? 0 : d.percent) + "%";
    r.ptxt.textContent = (d.percent == null ? "" : d.percent + "% \u00b7 ") + bytes(d.downloaded) + (d.total ? " / " + bytes(d.total) : "");
    r.sp.textContent = d.status === "running" && d.speed ? bytes(d.speed) + "/s" : "";
    r.eta.textContent = d.status === "running" && d.eta != null ? "ETA " + dur(d.eta) : "";
    r.rl.textContent = d.retries_label;
    r.rn.textContent = d.status === "retrying" && d.next_retry_in != null ? " next try in " + dur(d.next_retry_in) : "";
    r.rb.hidden = d.status === "completed" || d.status === "failed" || d.status === "cancelled";
    r.rb.onclick = function () {
      var v = prompt("Max retries: a number, or 'always' for infinite", d.max_retries === -1 ? "always" : String(d.max_retries));
      if (v != null) api("POST", "/api/downloads/" + d.id + "/retries/", { value: v }).then(function (j) { if (j.error) alert(j.error); refresh(); });
    };
    var key = d.status;
    if (r.actKey !== key) {
      r.actKey = key; r.acts.textContent = "";
      var box = el("div", "acts");
      (ACTIONS[d.status] || []).forEach(function (name) {
        if (name === "file") { var a = el("a", null, "download"); a.href = "/api/downloads/" + d.id + "/file/"; box.appendChild(a); return; }
        var b = el("button", "sm", name === "up" ? "\u2191" : name === "down" ? "\u2193" : name);
        b.onclick = function () { act(r.d, name).then(function (j) { if (j && j.error) alert(j.error); refresh(); }); };
        box.appendChild(b);
      });
      r.acts.appendChild(box);
    }
    return r.tr;
  }
  var ORDER = ["running", "retrying", "queued", "paused", "failed", "cancelled", "completed"];
  function renderAll(list) {
    var seen = {}, tbody = $("rows");
    list.sort(function (a, b) { return ORDER.indexOf(a.status) - ORDER.indexOf(b.status) || (a.status === "queued" ? a.id - b.id : b.id - a.id); });
    var frag = document.createDocumentFragment();
    list.forEach(function (d) { seen[d.id] = 1; frag.appendChild(render(d)); });
    Object.keys(rows).forEach(function (id) { if (!seen[id]) delete rows[id]; });
    tbody.textContent = ""; tbody.appendChild(frag);
    $("empty").hidden = list.length > 0;
  }

  // ---- graph ----
  function draw(series, now) {
    var cv = $("graph"), dpr = window.devicePixelRatio || 1, w = cv.clientWidth, h = 120;
    cv.width = w * dpr; cv.height = h * dpr; var g = cv.getContext("2d"); g.scale(dpr, dpr); g.clearRect(0, 0, w, h);
    var css = getComputedStyle(document.documentElement), fg = css.getPropertyValue("--muted"), ac = css.getPropertyValue("--accent"), ln = css.getPropertyValue("--line");
    var span = 300, t0 = now - span, pts = series.filter(function (p) { return p[0] >= t0; });
    var max = Math.max.apply(null, [1].concat(pts.map(function (p) { return p[1]; })));
    g.strokeStyle = ln; g.beginPath(); g.moveTo(0, h - 18.5); g.lineTo(w, h - 18.5); g.stroke();
    g.fillStyle = fg; g.font = "11px system-ui"; g.fillText(bytes(max) + "/s", 4, 12); g.fillText("-5 min", 4, h - 4); g.fillText("now", w - 28, h - 4);
    if (!pts.length) return;
    function X(t) { return (t - t0) / span * w; } function Y(v) { return h - 18 - v / max * (h - 34); }
    g.beginPath(); g.moveTo(X(pts[0][0]), h - 18);
    pts.forEach(function (p) { g.lineTo(X(p[0]), Y(p[1])); });
    g.lineTo(X(pts[pts.length - 1][0]), h - 18); g.closePath(); g.globalAlpha = 0.18; g.fillStyle = ac; g.fill(); g.globalAlpha = 1;
    g.beginPath(); pts.forEach(function (p, i) { i ? g.lineTo(X(p[0]), Y(p[1])) : g.moveTo(X(p[0]), Y(p[1])); }); g.strokeStyle = ac; g.lineWidth = 1.5; g.stroke();
  }

  // ---- polling ----
  var busy = false;
  function refresh() {
    if (busy) return; busy = true;
    return api("GET", "/api/snapshot/").then(function (s) {
      if (!s.downloads) return;
      ["running", "queued", "retrying", "paused", "completed", "failed"].forEach(function (k) { $("n-" + k).textContent = s.counts[k] || 0; });
      $("speed").textContent = bytes(s.speed) + "/s";
      $("trunc").textContent = s.truncated ? "showing newest " + s.downloads.length : "";
      renderAll(s.downloads); draw(s.series, s.now);
    }).catch(function () {}).finally(function () { busy = false; });
  }
  refresh(); setInterval(function () { if (!document.hidden) refresh(); }, 1500);
  window.addEventListener("resize", refresh);

  // ---- purge ----
  function purgeBody(dry) {
    var st = [].slice.call(document.querySelectorAll(".pst:checked")).map(function (c) { return c.value; });
    var b = { statuses: st, delete_files: $("purge-files").checked, dry_run: dry };
    if ($("purge-days").value !== "") b.older_than_days = $("purge-days").value;
    if ($("purge-all")) b.all = $("purge-all").checked;
    return b;
  }
  function summary(r) { return r.entries + " entries, " + r.partials_deleted + " partial files" + (r.files_deleted ? ", " + r.files_deleted + " downloaded files" : "") + ", " + bytes(r.bytes_freed); }
  $("purge-preview").onclick = function () {
    api("POST", "/api/purge/", purgeBody(true)).then(function (r) { $("purge-msg").className = r.error ? "err" : "muted"; $("purge-msg").textContent = r.error || "Would remove " + summary(r); });
  };
  $("purge-go").onclick = function () {
    var b = purgeBody(true);
    api("POST", "/api/purge/", b).then(function (r) {
      if (r.error) { $("purge-msg").className = "err"; $("purge-msg").textContent = r.error; return; }
      if (!r.entries) { $("purge-msg").className = "muted"; $("purge-msg").textContent = "Nothing to purge."; return; }
      if (!confirm("Purge " + summary(r) + "?" + (b.delete_files ? "\n\nDownloaded files will be DELETED from disk." : ""))) return;
      b.dry_run = false;
      api("POST", "/api/purge/", b).then(function (x) { $("purge-msg").className = "ok"; $("purge-msg").textContent = "Purged " + summary(x); refresh(); });
    });
  };
})();
