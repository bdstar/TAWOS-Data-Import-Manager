(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
  const num = (v) => (v === null || v === undefined ? "–" : Number(v).toLocaleString());
  const bytes = (b) => {
    if (b === null || b === undefined) return "–";
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0, x = b;
    while (x >= 1024 && i < u.length - 1) { x /= 1024; i++; }
    return `${x.toFixed(i ? 2 : 0)} ${u[i]}`;
  };
  const dur = (s) => {
    if (s === null || s === undefined) return "–";
    s = Math.round(s);
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    return h ? `${h}h ${m}m ${sec}s` : m ? `${m}m ${sec}s` : `${sec}s`;
  };
  const ts = (iso) => (iso ? new Date(iso).toLocaleString() : "–");

  const STATUS_CLASS = {
    "Not Started": "text-bg-light border",
    "Processing": "text-bg-primary",
    "Completed": "text-bg-success",
    "Completed with Errors": "text-bg-warning",
    "Failed": "text-bg-danger",
    "Cancelled": "text-bg-secondary",
    "Interrupted": "text-bg-dark",
  };
  const JOB_STATUS_CLASS = {
    queued: "text-bg-info", running: "text-bg-primary", cancelling: "text-bg-warning",
    completed: "text-bg-success", completed_with_errors: "text-bg-warning",
    failed: "text-bg-danger", cancelled: "text-bg-secondary", interrupted: "text-bg-dark",
  };
  const ACTIVE = new Set(["queued", "running", "cancelling"]);

  let lastStatus = null;
  let pollTimer = null;
  let modalTable = null;
  const importModal = new bootstrap.Modal($("#importModal"));
  const jobModal = new bootstrap.Modal($("#jobModal"));

  async function api(method, url, body) {
    const opts = { method, headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(url, opts);
    let data = null;
    try { data = await res.json(); } catch (_) { /* empty */ }
    if (!res.ok) {
      const d = data && data.detail;
      let msg = typeof d === "string" ? d : (d && d.message) || res.statusText;
      if (d && d.errors) msg += "\n" + d.errors.join("\n");
      throw new Error(msg);
    }
    return data;
  }

  function toast(msg, kind = "primary") {
    const el = document.createElement("div");
    el.className = `toast align-items-center text-bg-${kind} border-0`;
    el.innerHTML = `<div class="d-flex"><div class="toast-body err-msg">${esc(msg)}</div>
      <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button></div>`;
    $("#toasts").appendChild(el);
    const t = new bootstrap.Toast(el, { delay: 8000 });
    t.show();
    el.addEventListener("hidden.bs.toast", () => el.remove());
  }

  // ---------------------------------------------------------------- rendering
  function renderHeader(s) {
    const db = s.database;
    if (db.connected) {
      $("#db-status").innerHTML = `<span class="badge text-bg-success"><i class="bi bi-check-circle"></i> Connected</span>
        <code class="ms-2">${esc(db.database)}</code>`;
      $("#db-detail").textContent = `${db.user}@${db.host}:${db.port} · ${db.server_version || ""}` +
        (db.max_allowed_packet ? ` · max_allowed_packet ${bytes(db.max_allowed_packet)}` : "");
    } else {
      $("#db-status").innerHTML = `<span class="badge text-bg-danger"><i class="bi bi-x-circle"></i> Not connected</span>`;
      $("#db-detail").textContent = db.error || "";
    }

    const src = s.source;
    const fs = $("#file-status");
    if (src.readable) { fs.className = "badge text-bg-success"; fs.textContent = "Valid & readable"; }
    else { fs.className = "badge text-bg-danger"; fs.textContent = src.error || "Unavailable"; }
    $("#file-path").textContent = src.path;
    $("#file-size").textContent = src.size !== null ? bytes(src.size) : "";

    const ix = src.index;
    const wrap = $("#index-progress-wrap");
    let html = "";
    if (ix.state === "ready") {
      const h = ix.header || {};
      html = `<i class="bi bi-check2-circle text-success"></i> Source index ready` +
        (ix.build_seconds ? ` (built in ${ix.build_seconds}s)` : "") +
        (h.source_server_version ? ` · dumped from MySQL ${esc(h.source_server_version)}` : "") +
        (h.source_database ? ` · source database <code>${esc(h.source_database)}</code>` : "");
      wrap.classList.add("d-none");
    } else if (ix.state === "building") {
      html = `<span class="spinner-border spinner-border-sm text-primary"></span> Indexing SQL dump… ` +
        `${ix.percent ?? 0}% (${bytes(ix.progress_bytes)} of ${bytes(ix.total_bytes)}, ${dur(ix.elapsed)})`;
      wrap.classList.remove("d-none");
      $("#index-progress").style.width = `${ix.percent || 0}%`;
    } else if (ix.state === "error") {
      html = `<i class="bi bi-exclamation-triangle text-danger"></i> Index error: ${esc(ix.error)}`;
      wrap.classList.add("d-none");
    } else {
      html = `<span class="text-muted">Source index not built.</span>`;
      wrap.classList.add("d-none");
    }
    $("#index-status").innerHTML = html;

    const sm = s.summary;
    $("#sum-tables").textContent = `${sm.tables_found} / ${sm.tables_expected}`;
    $("#sum-completed").textContent = sm.completed;
    $("#sum-errors").textContent = sm.completed_with_errors + sm.failed_or_stopped;
    $("#sum-running").textContent = sm.running;
    $("#sum-existing").textContent = num(sm.existing_records_total);
    $("#sum-source").textContent = sm.source_records_total === null ? "…" : num(sm.source_records_total);

    $("#order-hint").innerHTML = s.import_order.length
      ? `<i class="bi bi-diagram-3"></i> Import order derived from foreign keys: ` +
        s.import_order.map((t) => `<code>${esc(t)}</code>`).join(" → ")
      : "Import order unavailable (database not connected).";
  }

  function progressCell(t) {
    const j = t.job;
    if (!j) return `<div class="progress" style="height: 18px"><div class="progress-bar" style="width:0%">0%</div></div>
      <div class="progress-text">Not started</div>`;
    const active = ACTIVE.has(j.status);
    const pct = j.percent;
    const indeterminate = active && (pct === null || pct === undefined);
    const barClass = {
      completed: "bg-success", completed_with_errors: "bg-warning", failed: "bg-danger",
      cancelled: "bg-secondary", interrupted: "bg-dark",
    }[j.status] || "";
    const anim = active ? "progress-bar-striped progress-bar-animated" : "";
    const width = indeterminate ? 100 : (pct || 0);
    const label = indeterminate ? "…" : `${(pct || 0).toFixed(1)}%`;
    const processed = (j.ordinal ?? j.checkpoint_ordinal ?? j.processed) || 0;
    let text = "";
    if (active) {
      text = `<strong>${esc(j.stage || j.status)}</strong> · ${num(processed)}` +
        (j.total_rows ? ` of ${num(j.total_rows)}` : "") + ` records processed · ` +
        `${num(j.inserted)} inserted · ${num(j.skipped)} skipped · ${num(j.failed)} failed · ${dur(j.duration_seconds)}`;
    } else {
      text = `${num(j.processed)} processed · ${num(j.inserted)} inserted` +
        (j.updated ? ` · ${num(j.updated)} updated` : "") +
        ` · ${num(j.skipped)} skipped · ${num(j.failed)} failed · ${dur(j.duration_seconds)}`;
    }
    if (j.error) text += `<div class="text-danger text-truncate" title="${esc(j.error)}">${esc(j.error)}</div>`;
    return `<div class="progress ${indeterminate ? "indeterminate" : ""}" style="height: 18px">
        <div class="progress-bar ${barClass} ${anim}" style="width:${width}%">${label}</div></div>
      <div class="progress-text">${text}</div>`;
  }

  function renderTables(s) {
    const q = $("#search").value.trim().toLowerCase();
    const rows = s.tables
      .filter((t) => !q || t.name.includes(q))
      .sort((a, b) => (a.import_order ?? 99) - (b.import_order ?? 99) || a.name.localeCompare(b.name))
      .map((t) => {
        const j = t.job;
        const label = t.status_label || "Not Started";
        const deps = t.parents.length ? `<div class="deps">needs: ${t.parents.map(esc).join(", ")}</div>` : "";
        const existing = t.counting && t.existing_records === null
          ? `<span class="spinner-border spinner-border-sm"></span>` : num(t.existing_records);
        let action;
        if (!t.exists) {
          action = `<span class="badge text-bg-danger">Missing in DB</span>`;
        } else if (t.running && j) {
          action = `<button class="btn btn-sm btn-primary" disabled><span class="spinner-border spinner-border-sm"></span> Running</button>
            <button class="btn btn-sm btn-outline-danger ms-1" data-cancel="${esc(j.id)}" ${j.status === "cancelling" ? "disabled" : ""}>
            <i class="bi bi-stop-fill"></i> ${j.status === "cancelling" ? "Cancelling…" : "Cancel"}</button>`;
        } else {
          const blocked = t.blocked_reason;
          action = `<span title="${esc(blocked || "")}"><button class="btn btn-sm btn-success" data-insert="${esc(t.name)}"
            ${blocked || !s.database.connected ? "disabled" : ""}><i class="bi bi-box-arrow-in-down"></i> Data Insert</button></span>`;
          if (j && j.resumable) {
            action += ` <button class="btn btn-sm btn-outline-primary" data-resume="${esc(j.id)}" ${blocked ? "disabled" : ""}
              title="Continue from the last committed batch"><i class="bi bi-arrow-repeat"></i> Resume</button>`;
          }
        }
        return `<tr>
          <td class="text-muted">${t.import_order ?? ""}</td>
          <td><code>${esc(t.name)}</code>${deps}</td>
          <td class="text-end">${existing}</td>
          <td class="text-end">${t.source_records === null ? '<span class="text-muted">…</span>' : num(t.source_records)}</td>
          <td><span class="badge status-badge ${STATUS_CLASS[label] || "text-bg-light"}">${esc(label)}</span></td>
          <td>${progressCell(t)}</td>
          <td class="text-end">${j ? num(j.inserted) : "–"}</td>
          <td class="text-end text-nowrap">${action}</td>
        </tr>`;
      });
    $("#tables-body").innerHTML = rows.join("") ||
      `<tr><td colspan="8" class="text-center text-muted py-4">No tables match.</td></tr>`;
  }

  async function renderJobs() {
    const jobs = await api("GET", "/api/jobs?limit=50");
    if (!jobs.length) {
      $("#jobs-body").innerHTML = `<tr><td colspan="12" class="text-center text-muted py-3">No jobs yet.</td></tr>`;
      return;
    }
    $("#jobs-body").innerHTML = jobs.map((j) => {
      const cp = j.checkpoint_offset !== null && j.checkpoint_offset !== undefined
        ? `byte ${num(j.checkpoint_offset)}${j.checkpoint_line ? `, line ${num(j.checkpoint_line)}` : ""}, row ${num(j.checkpoint_row)}`
        : "–";
      const actions = [`<button class="btn btn-sm btn-outline-secondary" data-job="${esc(j.id)}"><i class="bi bi-info-circle"></i> Details</button>`];
      if (ACTIVE.has(j.status)) actions.push(`<button class="btn btn-sm btn-outline-danger" data-cancel="${esc(j.id)}">Cancel</button>`);
      if (j.resumable) actions.push(`<button class="btn btn-sm btn-outline-primary" data-resume="${esc(j.id)}">Resume</button>`);
      return `<tr>
        <td><code>${esc(j.id)}</code>${j.resumed_from ? `<div class="deps">resumes ${esc(j.resumed_from)}</div>` : ""}</td>
        <td><code>${esc(j.table_name)}</code></td>
        <td><span class="badge ${JOB_STATUS_CLASS[j.status] || "text-bg-light"}">${esc(j.status)}</span></td>
        <td>${ts(j.started_at)}</td><td>${ts(j.finished_at)}</td>
        <td class="text-end">${dur(j.duration_seconds)}</td>
        <td class="text-end">${num(j.processed)}</td><td class="text-end">${num(j.inserted)}</td>
        <td class="text-end">${num(j.skipped)}</td>
        <td class="text-end ${j.failed ? "text-danger fw-bold" : ""}">${num(j.failed)}</td>
        <td>${cp}</td>
        <td class="text-end text-nowrap">${actions.join(" ")}</td>
      </tr>`;
    }).join("");
  }

  async function refresh() {
    try {
      const s = await api("GET", "/api/status");
      lastStatus = s;
      renderHeader(s);
      renderTables(s);
      await renderJobs();
      const busy = s.summary.running > 0 || s.source.index.state === "building" ||
        s.tables.some((t) => t.counting);
      schedule(busy ? 1000 : 5000);
    } catch (e) {
      $("#db-status").innerHTML = `<span class="badge text-bg-danger">Server unreachable</span>`;
      schedule(5000);
    }
  }

  function schedule(ms) {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(refresh, ms);
  }

  // ---------------------------------------------------------------- actions
  async function openImport(table) {
    modalTable = table;
    $("#im-table").textContent = table;
    $("#im-policy").value = window.TAWOS_DEFAULTS.duplicate_policy;
    $("#im-errors").value = window.TAWOS_DEFAULTS.error_policy;
    $("#im-batch").value = window.TAWOS_DEFAULTS.batch_size;
    importModal.show();
    await runPrecheck();
  }

  async function runPrecheck() {
    const box = $("#im-precheck");
    box.innerHTML = `<div class="text-muted"><span class="spinner-border spinner-border-sm"></span> Running checks…</div>`;
    $("#im-start").disabled = true;
    try {
      const r = await api("GET", `/api/tables/${encodeURIComponent(modalTable)}/precheck?duplicate_policy=${$("#im-policy").value}`);
      const info = r.info || {};
      let html = `<ul class="list-unstyled small mb-2">
        <li><strong>Source rows:</strong> ${num(info.source_rows)} (${bytes(info.source_bytes)}) from dump table <code>${esc(info.dump_table_name || "?")}</code></li>
        <li><strong>Prerequisite tables:</strong> ${(info.parents || []).map((p) => `<code>${esc(p)}</code>`).join(", ") || "none"}</li>`;
      if (info.disk) html += `<li><strong>Disk:</strong> ${bytes(info.disk.free_bytes)} free on database volume, ~${bytes(info.disk.estimated_required_bytes)} estimated</li>`;
      html += `</ul>`;
      if (r.errors.length) html += `<div class="alert alert-danger small mb-2"><strong>Blocking issues</strong><ul class="mb-0">${r.errors.map((e) => `<li>${esc(e)}</li>`).join("")}</ul></div>`;
      if (r.warnings.length) html += `<div class="alert alert-warning small mb-2"><strong>Warnings</strong><ul class="mb-0">${r.warnings.map((e) => `<li>${esc(e)}</li>`).join("")}</ul></div>`;
      if (r.ok) html += `<div class="alert alert-success small mb-0 py-2"><i class="bi bi-check-circle"></i> Ready to import.</div>`;
      box.innerHTML = html;
      $("#im-start").disabled = !r.ok;
    } catch (e) {
      box.innerHTML = `<div class="alert alert-danger small err-msg">${esc(e.message)}</div>`;
    }
  }

  async function startImport() {
    $("#im-start").disabled = true;
    try {
      const job = await api("POST", `/api/tables/${encodeURIComponent(modalTable)}/import`, {
        duplicate_policy: $("#im-policy").value,
        error_policy: $("#im-errors").value,
        batch_size: parseInt($("#im-batch").value, 10) || undefined,
      });
      importModal.hide();
      toast(`Import of ${modalTable} started (job ${job.id})`, "success");
      refresh();
    } catch (e) {
      toast(e.message, "danger");
      $("#im-start").disabled = false;
    }
  }

  async function cancelJob(id) {
    if (!confirm("Cancel this import? The current batch will finish or roll back; you can resume later.")) return;
    try { await api("POST", `/api/jobs/${id}/cancel`); toast("Cancellation requested", "warning"); refresh(); }
    catch (e) { toast(e.message, "danger"); }
  }

  async function resumeJob(id) {
    try { const j = await api("POST", `/api/jobs/${id}/resume`); toast(`Resumed as job ${j.id}`, "success"); refresh(); }
    catch (e) { toast(e.message, "danger"); }
  }

  async function showJob(id) {
    $("#jm-id").textContent = id;
    $("#jm-body").innerHTML = `<span class="spinner-border spinner-border-sm"></span>`;
    jobModal.show();
    try {
      const [j, e] = await Promise.all([api("GET", `/api/jobs/${id}`), api("GET", `/api/jobs/${id}/errors?limit=500`)]);
      const kv = [
        ["Table", `<code>${esc(j.table_name)}</code>`], ["Status", esc(j.status)], ["Stage", esc(j.stage)],
        ["Duplicate policy", esc(j.duplicate_policy)], ["Error policy", esc(j.error_policy)], ["Batch size", num(j.batch_size)],
        ["Created", ts(j.created_at)], ["Started", ts(j.started_at)], ["Finished", ts(j.finished_at)], ["Duration", dur(j.duration_seconds)],
        ["Source rows (table)", num(j.total_rows)], ["Processed", num(j.processed)], ["Inserted", num(j.inserted)],
        ["Updated", num(j.updated)], ["Skipped", num(j.skipped)], ["Failed", num(j.failed)],
        ["Batches committed", num(j.batches_committed)],
        ["Last checkpoint", j.checkpoint_offset !== null ? `byte ${num(j.checkpoint_offset)}, line ${num(j.checkpoint_line)}, row ${num(j.checkpoint_row)}, table row #${num(j.checkpoint_ordinal)}` : "–"],
        ["Resumed from", j.resumed_from ? `<code>${esc(j.resumed_from)}</code>` : "–"],
      ];
      let html = `<div class="row">${kv.map(([k, v]) => `<div class="col-md-4 col-lg-3 mb-2"><div class="small text-muted">${k}</div><div>${v}</div></div>`).join("")}</div>`;
      if (j.error) html += `<div class="alert alert-danger err-msg">${esc(j.error)}</div>`;
      if (j.warnings && j.warnings.length) html += `<div class="alert alert-warning small"><ul class="mb-0">${j.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>`;
      html += `<h6 class="mt-3">Row-level failures (${num(e.total_stored)} stored${j.failed > e.total_stored ? `, ${num(j.failed)} total; full list in <code>${esc(e.full_log)}</code>` : ""})</h6>`;
      if (e.errors.length) {
        html += `<div class="table-responsive"><table class="table table-sm small"><thead><tr><th>Stage</th><th>Kind</th><th>SQL line</th><th>Row #</th><th>Key</th><th>Message</th></tr></thead><tbody>` +
          e.errors.map((x) => `<tr><td>${esc(x.stage)}</td><td>${esc(x.kind)}</td><td>${num(x.line)}</td><td>${num(x.row_in_statement)}</td>
            <td><code>${esc(x.row_key)}</code></td><td class="err-msg">${esc(x.message)}</td></tr>`).join("") + `</tbody></table></div>`;
      } else {
        html += `<p class="text-muted small">No row-level failures.</p>`;
      }
      $("#jm-body").innerHTML = html;
    } catch (err) {
      $("#jm-body").innerHTML = `<div class="alert alert-danger">${esc(err.message)}</div>`;
    }
  }

  document.addEventListener("click", (ev) => {
    const b = ev.target.closest("button");
    if (!b) return;
    if (b.dataset.insert) openImport(b.dataset.insert);
    else if (b.dataset.cancel) cancelJob(b.dataset.cancel);
    else if (b.dataset.resume) resumeJob(b.dataset.resume);
    else if (b.dataset.job) showJob(b.dataset.job);
  });
  $("#im-start").addEventListener("click", startImport);
  $("#im-policy").addEventListener("change", runPrecheck);
  $("#search").addEventListener("input", () => lastStatus && renderTables(lastStatus));
  $("#btn-refresh").addEventListener("click", async () => {
    try { await api("POST", "/api/counts/refresh"); toast("Refreshing record counts…"); refresh(); }
    catch (e) { toast(e.message, "danger"); }
  });
  $("#btn-reindex").addEventListener("click", async () => {
    if (!confirm("Re-scan the whole SQL dump to rebuild the source index?")) return;
    try { await api("POST", "/api/index/rebuild"); toast("Rebuilding source index…"); refresh(); }
    catch (e) { toast(e.message, "danger"); }
  });

  refresh();
})();
