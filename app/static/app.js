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
