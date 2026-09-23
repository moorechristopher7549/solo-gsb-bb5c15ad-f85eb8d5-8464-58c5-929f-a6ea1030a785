/* 抗裁切水印实验台前端逻辑（原生 JS，无框架） */

const $ = (s) => document.querySelector(s);
const api = async (url, opts = {}) => {
  const res = await fetch(url, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return res.json();
};

const state = {
  jobId: null,
  strength: "medium",
  originalUrl: null,
  watermarkedUrl: null,
  targetUrl: null,
  markedUrl: null,
  detections: [],          // 当前任务页内累积的检测结果（可归档）
  report: null,            // 最近生成的归档报告
};

/* ---------------- 嵌入 ---------------- */

$("#strength-seg").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-value]");
  if (!btn) return;
  state.strength = btn.dataset.value;
  document.querySelectorAll("#strength-seg button").forEach((b) =>
    b.classList.toggle("on", b === btn)
  );
});

$("#embed-text").addEventListener("input", (e) => {
  const bytes = new TextEncoder().encode(e.target.value).length;
  $("#text-counter").textContent =
    `${bytes} 字节` + (bytes > 45 ? "（超出 45 字节容量上限）" : "");
  $("#text-counter").style.color = bytes > 45 ? "var(--bad)" : "";
});

$("#embed-file").addEventListener("change", (e) => {
  const f = e.target.files[0];
  if (!f) return;
  state.originalUrl = URL.createObjectURL(f);
  showImage("original", state.originalUrl);
  switchTab("original");
});

$("#btn-embed").addEventListener("click", async () => {
  const file = $("#embed-file").files[0];
  const text = $("#embed-text").value.trim();
  const status = $("#embed-status");
  if (!file) return fail(status, "请先选择一张图片。");
  if (!text) return fail(status, "请输入水印文本。");

  const fd = new FormData();
  fd.append("image", file);
  fd.append("text", text);
  fd.append("strength", state.strength);

  busy(status, "正在执行分块 DCT 嵌入…");
  try {
    const r = await api("/api/embed", { method: "POST", body: fd });
    state.jobId = r.job_id;
    state.watermarkedUrl = `/api/jobs/${r.job_id}/watermarked?t=${Date.now()}`;
    state.targetUrl = null;
    state.markedUrl = null;
    state.detections = [];
    state.report = null;
    renderDetectList();
    resetReportBox();
    showImage("watermarked", state.watermarkedUrl);
    $("#img-target").removeAttribute("src");
    $("#img-marked").removeAttribute("src");

    $("#m-psnr").textContent = `${r.psnr_db} dB`;
    $("#m-tiles").textContent = `${r.full_tiles} 个`;
    $("#m-votes").textContent = `≈ ${r.votes_per_bit} 票/比特`;
    $("#m-cap").textContent = `${r.payload_bytes} / ${r.capacity_bytes} 字节`;
    $("#m-size").textContent = `${r.width} × ${r.height}`;
    $("#embed-meta").classList.remove("hidden");
    ok(status, "嵌入成功，请查看「含水印图」标签。");

    $("#panel-attack").classList.remove("disabled");
    $("#btn-detect-attacked").disabled = false;
    document.querySelector('.step[data-step="2"]').classList.add("active");
    switchTab("watermarked");
  } catch (err) {
    fail(status, err.message);
  }
});

$("#btn-download-wm").addEventListener("click", () => {
  if (state.jobId) window.open(`/api/jobs/${state.jobId}/watermarked`, "_blank");
});

/* ---------------- 攻击 ---------------- */

$("#jpeg-q").addEventListener("input", (e) => $("#jpeg-val").textContent = e.target.value);
$("#crop-f").addEventListener("input", (e) => $("#crop-val").textContent = `${e.target.value}%`);

$("#btn-attack").addEventListener("click", async () => {
  const status = $("#attack-status");
  if (!state.jobId) return fail(status, "请先完成嵌入。");
  const q = parseInt($("#jpeg-q").value, 10);
  const crop = parseInt($("#crop-f").value, 10) / 100;
  busy(status, "正在施加 JPEG 重压缩 / 中心裁切…");
  try {
    const r = await api("/api/attack", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        job_id: state.jobId,
        jpeg_quality: q,
        crop_fraction: crop === 1 ? null : crop,
      }),
    });
    state.targetUrl = `/api/jobs/${state.jobId}/target?t=${Date.now()}`;
    showImage("target", state.targetUrl);
    switchTab("target");
    $("#attack-history").innerHTML = (r.attack_history || [])
      .map((h) => `<span class="chip">${
        h.type === "jpeg" ? `JPEG Q${h.quality}` : `中心裁切 ${Math.round(h.fraction * 100)}%`
      }</span>`)
      .join("") + `<span class="chip">${r.target_width}×${r.target_height}px</span>`;
    ok(status, "攻击已应用，可继续叠加攻击或直接检测。");
  } catch (err) {
    fail(status, err.message);
  }
});

$("#btn-attack-reset").addEventListener("click", async () => {
  const status = $("#attack-status");
  if (!state.jobId) return;
  busy(status, "重置中…");
  try {
    await api("/api/attack", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: state.jobId, jpeg_quality: 100, crop_fraction: 1 }),
    });
    state.targetUrl = `/api/jobs/${state.jobId}/target?t=${Date.now()}`;
    showImage("target", state.targetUrl);
    $("#attack-history").innerHTML = `<span class="chip">已重置（JPEG Q100、无裁切）</span>`;
    ok(status, "已重置为含水印图。");
  } catch (err) {
    fail(status, err.message);
  }
});

$("#btn-detect-attacked").addEventListener("click", () =>
  runDetect(null, state.jobId, "attack"));

/* ---------------- 独立检测 ---------------- */

$("#btn-detect-upload").addEventListener("click", async () => {
  const file = $("#detect-file").files[0];
  const status = $("#detect-upload-status");
  if (!file) return fail(status, "请先选择待检测图片。");
  await runDetect(file, null, "upload", status);
});

/* ---------------- 检测核心 ---------------- */

async function runDetect(file, jobId, source, statusEl) {
  const status = statusEl || $("#attack-status");
  busy(status, "正在执行 64 相移同步搜索与多数表决…");
  const fd = new FormData();
  if (file) fd.append("image", file);
  if (jobId) fd.append("job_id", jobId);
  try {
    const r = await api("/api/detect", { method: "POST", body: fd });
    renderResult(r, source);
    ok(status, r.detected ? "检测完成，水印已恢复。" : "检测完成，但未能恢复水印。");
  } catch (err) {
    fail(status, err.message);
  }
}

function renderResult(r, source) {
  const panel = $("#result-panel");
  panel.classList.remove("hidden");
  const m = r.metrics;

  const verdict = $("#detect-verdict");
  if (r.detected) {
    verdict.className = "verdict ok";
    verdict.textContent = `✔ 检测到有效水印（${m.confidence}% 置信度）`;
  } else {
    verdict.className = "verdict fail";
    verdict.textContent = `✘ 未检测到可校验的水印：${r.reason || "未知原因"}`;
  }

  $("#detect-text").textContent = r.detected ? r.text : "（未恢复）";

  setBar("overall", m.confidence, `${m.confidence}%`);
  const syncPct = Math.round(Math.min(1, Math.abs(m.sync_score)) * 100);
  setBar("sync", syncPct, m.sync_score.toFixed(3));
  setBar("agree", Math.round(m.sync_agreement * 100), `${(m.sync_agreement * 100).toFixed(1)}%`);

  const grid = $("#bit-grid");
  grid.innerHTML = "";
  (m.bit_confidences || []).forEach((c) => {
    const cell = document.createElement("i");
    const v = Math.round(c * 100);
    cell.style.background = v > 80
      ? "var(--accent-2)" : v > 50 ? "var(--warn)" : "var(--bad)";
    cell.title = `bit confidence ${v}%`;
    grid.appendChild(cell);
  });

  $("#r-tiles").textContent = `${m.tiles_y} × ${m.tiles_x}（共 ${m.tiles_y * m.tiles_x} 瓦）`;
  $("#r-phase").textContent = `(${m.phase[0]}, ${m.phase[1]}) 块`;
  $("#r-blocks").textContent = `${m.sync_blocks} / ${m.payload_blocks}`;
  $("#r-votes").textContent = m.votes_per_bit || "—";
  $("#r-marked").textContent =
    `${m.marked_blocks} / ${m.total_blocks} 块（${(m.marked_fraction * 100).toFixed(1)}%）`;

  if (r.marked_url) {
    state.markedUrl = `${r.marked_url}?t=${Date.now()}`;
    showImage("marked", state.markedUrl);
    document.querySelector('.step[data-step="3"]').classList.add("active");
    switchTab("marked");
  }

  recordDetection(r);
}

/* ---------------- 检测报告归档 ---------------- */

const SOURCE_LABELS = {
  target: "受攻击图",
  watermarked: "含水印图",
  original: "原图",
  uploaded: "上传图",
};

function recordDetection(r) {
  // 跨任务（例如先嵌入后又用独立上传）时清空旧任务的检测记录
  if (!state.jobId || state.jobId !== r.job_id) {
    state.jobId = r.job_id;
    state.detections = [];
    resetReportBox();
  }
  state.detections.unshift({
    detection_id: r.detection_id,
    job_id: r.job_id,
    source: r.source,
    detected: r.detected,
    text: r.text || "",
    confidence: r.metrics.confidence,
    at: r.detected_at_iso || r.detected_at,
    marked_det_url: r.marked_det_url,
    checked: true,
  });
  renderDetectList();
}

function renderDetectList() {
  const box = $("#detect-list");
  box.innerHTML = "";
  if (!state.detections.length) {
    box.innerHTML = `<div class="muted">暂无检测结果，完成检测后可在此选择并归档。</div>`;
  } else {
    state.detections.forEach((d, i) => {
      const label = document.createElement("label");
      label.className = "detect-item";
      const time = String(d.at || "").replace("T", " ").replace("Z", " UTC");
      label.innerHTML = `
        <input type="checkbox" ${d.checked ? "checked" : ""} data-i="${i}" />
        <span class="det-badge ${d.detected ? "ok" : "fail"}">
          ${d.detected ? "✔" : "✘"}
        </span>
        <span class="det-main">
          <b>${escapeHtml(d.detected ? d.text : "未恢复水印")}</b>
          <small>${SOURCE_LABELS[d.source] || d.source} · 置信度 ${d.confidence ?? "—"}% · ${escapeHtml(time)}</small>
        </span>`;
      box.appendChild(label);
    });
  }
  updateArchiveButton();
}

$("#detect-list").addEventListener("change", (e) => {
  const cb = e.target.closest('input[type="checkbox"]');
  if (!cb) return;
  state.detections[+cb.dataset.i].checked = cb.checked;
  updateArchiveButton();
});

function selectedDetections() {
  return state.detections.filter((d) => d.checked);
}

function updateArchiveButton() {
  const n = selectedDetections().length;
  const btn = $("#btn-archive");
  btn.disabled = n === 0 || !state.jobId;
  btn.textContent = n
    ? `归档所选 ${n} 次检测为报告`
    : "归档所选检测为报告";
}

$("#btn-archive").addEventListener("click", async () => {
  const status = $("#archive-status");
  const picked = selectedDetections();
  if (!picked.length || !state.jobId) {
    return fail(status, "请至少勾选一次检测结果。");
  }
  busy(status, "正在生成规范化报告、计算 SHA-256 清单并写入归档…");
  resetReportBox();
  try {
    const r = await api(`/api/jobs/${state.jobId}/reports`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ detection_ids: picked.map((d) => d.detection_id) }),
    });
    state.report = {
      report_id: r.report_id,
      time: r.generated_at_iso,
      entries: r.entries,
      seq: r.archive && r.archive.seq,
    };
    renderReportBox();
    ok(status, `报告已生成并追加到归档记录（#${r.archive.seq}，清单 ${r.entries} 项）。`);
  } catch (err) {
    fail(status, err.message);
  }
});

function renderReportBox() {
  const rep = state.report;
  if (!rep) return;
  $("#report-box").classList.remove("hidden");
  $("#report-id").textContent = rep.report_id;
  $("#report-time").textContent =
    `${String(rep.time).replace("T", " ").replace("Z", " UTC")} · 清单 ${rep.entries} 项 · 归档 #${rep.seq}`;
  const v = $("#report-verify");
  v.className = "verify pending";
  v.textContent = "尚未校验";
  $("#report-verify-detail").textContent = "";
}

function resetReportBox() {
  state.report = null;
  $("#report-box").classList.add("hidden");
  const v = $("#report-verify");
  v.className = "verify pending";
  v.textContent = "尚未校验";
  $("#report-verify-detail").textContent = "";
}

$("#btn-download-report").addEventListener("click", () => {
  if (state.report) {
    window.open(`/api/reports/${state.report.report_id}?download=1`, "_blank");
  }
});

$("#btn-verify-report").addEventListener("click", async () => {
  if (!state.report) return;
  const v = $("#report-verify");
  const detail = $("#report-verify-detail");
  v.className = "verify busy";
  v.textContent = "正在重新计算清单摘要…";
  detail.textContent = "";
  try {
    const res = await fetch(`/api/reports/${state.report.report_id}/verify`,
                            { method: "POST" });
    const r = await res.json();
    if (r.valid && r.archive && r.archive.chain_ok) {
      v.className = "verify ok";
      v.textContent = `✔ 校验通过：${r.entries_total} 项清单全部一致，归档链完整（${r.archive.log_entries} 条记录）`;
      detail.textContent = "";
    } else {
      v.className = "verify fail";
      const parts = [];
      if (r.first_issue) {
        const it = r.first_issue;
        parts.push(`首个异常：${it.path}（${issueLabel(it.kind)}）`);
      }
      if (r.archive && !r.archive.chain_ok && r.archive.issue) {
        parts.push(`归档记录异常：${issueLabel(r.archive.issue.kind)}` +
          (r.archive.issue.seq ? ` #${r.archive.issue.seq}` : ""));
      }
      v.textContent = "✘ 校验未通过";
      detail.textContent = parts.join("；") || "清单或归档链存在不一致。";
    }
  } catch (err) {
    v.className = "verify fail";
    v.textContent = "✘ 校验请求失败";
    detail.textContent = err.message;
  }
});

function issueLabel(kind) {
  return {
    missing: "文件缺失",
    digest_mismatch: "SHA-256 摘要不一致",
    log_missing: "归档日志缺失",
    log_corrupt: "归档日志记录损坏",
    entry_digest_mismatch: "归档记录摘要不一致",
    chain_broken: "哈希链断裂（记录可能被删改）",
    report_not_in_archive: "报告未收录进归档",
    report_digest_mismatch: "归档摘要与当前报告不一致",
  }[kind] || kind;
}

function escapeHtml(s) {
  return String(s == null ? "" : s).replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

$("#btn-download-marked").addEventListener("click", () => {
  if (state.markedUrl) window.open(state.markedUrl.split("?")[0], "_blank");
});

/* ---------------- UI 辅助 ---------------- */

function setBar(name, pct, label) {
  $("#bar-" + name).style.width = `${Math.max(0, Math.min(100, pct))}%`;
  $("#c-" + name).textContent = label;
}

function showImage(kind, url) {
  const img = $("#img-" + kind);
  img.src = url;
  const pane = img.closest(".preview-pane");
  pane.querySelector(".placeholder").style.display = "none";
}

function switchTab(name) {
  document.querySelectorAll(".tab").forEach((t) =>
    t.classList.toggle("on", t.dataset.tab === name));
  document.querySelectorAll(".preview-pane").forEach((p) =>
    p.classList.toggle("on", p.dataset.pane === name));
}

document.querySelectorAll(".preview-tabs .tab").forEach((t) =>
  t.addEventListener("click", () => switchTab(t.dataset.tab)));

function busy(el, msg) { el.className = "status busy"; el.textContent = msg; }
function ok(el, msg) { el.className = "status ok"; el.textContent = "✔ " + msg; }
function fail(el, msg) { el.className = "status error"; el.textContent = "✘ " + msg; }
