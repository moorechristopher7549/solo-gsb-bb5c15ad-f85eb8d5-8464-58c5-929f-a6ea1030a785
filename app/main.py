"""抗裁切水印实验台 —— FastAPI 后端。

接口：
  GET  /                 前端页面
  POST /api/embed        上传图片 + 文本 -> 含水印 PNG
  POST /api/attack       对任务图片施加 JPEG 重压缩 / 中心裁切
  POST /api/detect       上传图片或引用任务图片 -> 水印检测结果 + 失真叠加图
  GET  /api/jobs/{id}/{kind}  任务文件下载（original/watermarked/target/marked）
  POST /api/jobs/{id}/reports   选择检测结果 -> 生成规范化 JSON 报告 + SHA-256 清单归档
  GET  /api/reports/{report_id}         下载归档报告 JSON
  POST /api/reports/{report_id}/verify  重算清单，报告首个缺失/摘要不一致项
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps
from pydantic import BaseModel, Field

from . import dctwm as wm

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = Path(__import__("os").environ.get("WM_DATA_DIR", "/data/wmbench"))
MAX_JOBS = int(__import__("os").environ.get("WM_MAX_JOBS", "200"))
MAX_UPLOAD_BYTES = 16 * 1024 * 1024
MAX_DIM = 4096
ALLOWED_KINDS = {"original", "watermarked", "target", "marked"}

# 报告归档：{DATA_DIR}/_reports/{report_id}/{report.json, files/*.png}
# 仅追加归档记录：{DATA_DIR}/_reports/archive.jsonl（每行一条 JSON，只增不改）
REPORTS_DIR = DATA_DIR / "_reports"
ARCHIVE_LOG = REPORTS_DIR / "archive.jsonl"
REPORT_FILE_KINDS = ("original", "watermarked", "target", "marked")
REPORT_SCHEMA = "wm-bench-report/1"
MAX_REPORT_DETECTIONS = 100
_ID_RE = re.compile(r"[0-9a-f]{12}")

app = FastAPI(title="抗裁切水印实验台", version="1.0.0")


# ---------------------------------------------------------------- 任务存储

def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _prune_jobs() -> None:
    """超过上限时删除最旧的任务目录（归档目录 _reports 等下划线开头目录永久保留）。"""
    jobs = sorted(
        (p for p in DATA_DIR.iterdir() if p.is_dir() and not p.name.startswith("_")),
        key=lambda p: p.stat().st_mtime,
    )
    for p in jobs[:-MAX_JOBS] if len(jobs) > MAX_JOBS else []:
        for f in p.iterdir():
            f.unlink()
        p.rmdir()


def _job_dir(job_id: str) -> Path:
    d = DATA_DIR / job_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_meta(job_id: str, meta: dict) -> None:
    (_job_dir(job_id) / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _load_meta(job_id: str) -> dict:
    p = DATA_DIR / job_id / "meta.json"
    if not p.exists():
        raise HTTPException(status_code=404, detail="任务不存在或已过期。")
    return json.loads(p.read_text(encoding="utf-8"))


def _resolve_image(file: UploadFile | None, job_id: str | None,
                   prefer: str = "watermarked") -> np.ndarray:
    """从上传文件或已有任务中读取一张 RGB 图。"""
    if file is not None and file.filename:
        data = file.file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="图片不能超过 16MB。")
        try:
            im = Image.open(io.BytesIO(data))
            im = ImageOps.exif_transpose(im).convert("RGB")
        except Exception:
            raise HTTPException(status_code=400, detail="无法解析上传的图片。")
        return _check_dimensions(np.array(im))

    if not job_id:
        raise HTTPException(status_code=400, detail="请提供图片文件或已有任务 ID。")
    meta = _load_meta(job_id)
    kind = prefer if (DATA_DIR / job_id / f"{prefer}.png").exists() else "original"
    im = Image.open(DATA_DIR / job_id / f"{kind}.png").convert("RGB")
    return np.array(im)


def _check_dimensions(rgb: np.ndarray) -> np.ndarray:
    h, w = rgb.shape[:2]
    if max(h, w) > MAX_DIM:
        raise HTTPException(
            status_code=400,
            detail=f"图片尺寸 {w}x{h} 超过 {MAX_DIM}px 上限，请先缩小。",
        )
    if min(h, w) < wm.TILE * wm.BLK:
        raise HTTPException(
            status_code=400,
            detail=f"图片最短边至少需要 {wm.TILE * wm.BLK}px（当前 {min(h, w)}px）。",
        )
    return rgb


# ---------------------------------------------------------------- 报告归档

def _check_id(value: str, what: str = "ID") -> str:
    """任务/报告 ID 必须是本服务生成的 12 位十六进制串（防路径穿越）。"""
    if not value or not _ID_RE.fullmatch(value):
        raise HTTPException(status_code=404, detail=f"{what}不存在。")
    return value


def _iso_utc(ts: float | None = None) -> str:
    dt = datetime.fromtimestamp(time.time() if ts is None else ts, tz=timezone.utc)
    return dt.isoformat(timespec="milliseconds")


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _canonical_bytes(obj: dict) -> bytes:
    """规范化 JSON 序列化：键排序、UTF-8、两空格缩进，保证同一内容字节级一致。"""
    return (json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _append_archive(record: dict) -> dict:
    """向仅追加归档日志写入一条记录（只 append，不修改历史行），返回带序号的记录。"""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    seq = 1
    if ARCHIVE_LOG.exists():
        with ARCHIVE_LOG.open("r", encoding="utf-8") as f:
            seq += sum(1 for line in f if line.strip())
    entry = {"seq": seq, **record}
    with ARCHIVE_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    return entry


def _find_archive_record(report_id: str) -> dict | None:
    """在归档日志中查找某报告的最后一条记录。"""
    if not ARCHIVE_LOG.exists():
        return None
    found = None
    with ARCHIVE_LOG.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("report_id") == report_id:
                found = rec
    return found


def _safe_join(base: Path, rel: str) -> Path | None:
    """拼接归档内相对路径；越出基目录时返回 None。"""
    p = (base / rel).resolve()
    try:
        p.relative_to(base.resolve())
    except ValueError:
        return None
    return p


# ---------------------------------------------------------------- 请求模型

class AttackRequest(BaseModel):
    job_id: str
    jpeg_quality: int | None = None        # 10~100；为空则不重压缩
    crop_fraction: float | None = None     # 0.2~1.0；为空则不裁切


class DetectionInput(BaseModel):
    """一次检测结果的客户端回传（服务端仅取下列字段做规范化归档）。"""
    source: str = "attack"                 # attack=受攻击图检测 / upload=上传图检测
    detected: bool
    text: str | None = None
    reason: str | None = None
    confidence: float | None = None
    metrics: dict = Field(default_factory=dict)
    marked_url: str | None = None
    recorded_at: str | None = None         # 客户端记录时间（ISO 8601）


class ReportRequest(BaseModel):
    detections: list[DetectionInput]       # 至少一条


# ---------------------------------------------------------------- 接口

@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/embed")
async def embed(
    image: UploadFile = File(...),
    text: str = Form(...),
    strength: str = Form("medium"),
):
    _ensure_data_dir()
    text = text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="水印文本不能为空。")
    data = await image.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="图片不能超过 16MB。")
    try:
        pil = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    except Exception:
        raise HTTPException(status_code=400, detail="无法解析上传的图片。")
    rgb = _check_dimensions(np.array(pil))

    try:
        res = wm.embed_watermark(rgb, text, strength=strength)
    except wm.WatermarkError as e:
        raise HTTPException(status_code=400, detail=str(e))

    job_id = uuid.uuid4().hex[:12]
    d = _job_dir(job_id)
    Image.fromarray(rgb).save(d / "original.png", format="PNG", optimize=True)
    Image.fromarray(res.image).save(d / "watermarked.png", format="PNG", optimize=True)
    meta = {
        "id": job_id,
        "created_at": time.time(),
        "text": text,
        "strength": strength,
        "width": int(rgb.shape[1]),
        "height": int(rgb.shape[0]),
        "psnr_db": res.psnr_db,
        "full_tiles": res.full_tiles,
        "payload_bytes": res.payload_bytes,
        "capacity_bytes": res.capacity_bytes,
        "votes_per_bit": res.votes_per_bit,
    }
    _save_meta(job_id, meta)
    _prune_jobs()
    return {"job_id": job_id, **meta}


@app.post("/api/attack")
def attack(req: AttackRequest):
    _ensure_data_dir()
    meta = _load_meta(req.job_id)
    d = DATA_DIR / req.job_id
    source = d / "watermarked.png"
    if not source.exists():
        raise HTTPException(status_code=404, detail="该任务没有含水印图。")
    rgb = np.array(Image.open(source).convert("RGB"))

    history = meta.get("attack_history", [])
    if req.crop_fraction is not None:
        rgb = wm.center_crop(rgb, req.crop_fraction)
        history.append({"type": "center_crop", "fraction": round(float(req.crop_fraction), 3)})
    if req.jpeg_quality is not None:
        rgb = wm.jpeg_compress(rgb, int(req.jpeg_quality))
        history.append({"type": "jpeg", "quality": int(req.jpeg_quality)})

    Image.fromarray(rgb).save(d / "target.png", format="PNG", optimize=True)
    meta["attack_history"] = history
    meta["target_width"] = int(rgb.shape[1])
    meta["target_height"] = int(rgb.shape[0])
    _save_meta(req.job_id, meta)
    return {
        "job_id": req.job_id,
        "target_width": meta["target_width"],
        "target_height": meta["target_height"],
        "attack_history": history,
    }


@app.post("/api/detect")
async def detect(
    image: UploadFile | None = File(None),
    job_id: str | None = Form(None),
):
    _ensure_data_dir()
    rgb = _resolve_image(image, job_id, prefer="target")
    try:
        res = wm.detect_watermark(rgb)
    except wm.WatermarkError as e:
        raise HTTPException(status_code=400, detail=str(e))

    marked_url = None
    if res.overlay is not None:
        if job_id and (DATA_DIR / job_id).exists():
            d = DATA_DIR / job_id
        else:
            job_id = uuid.uuid4().hex[:12]
            d = _job_dir(job_id)
        Image.fromarray(res.overlay).save(d / "marked.png", format="PNG", optimize=True)
        marked_url = f"/api/jobs/{job_id}/marked"

    return JSONResponse({
        "job_id": job_id,
        "detected": res.detected,
        "text": res.text,
        "reason": res.reason,
        "metrics": {
            "confidence": res.confidence,
            "sync_score": res.sync_score,
            "sync_agreement": res.sync_agreement,
            "payload_bytes": res.payload_bytes,
            "payload_blocks": res.payload_blocks,
            "sync_blocks": res.sync_blocks,
            "tiles_y": res.tiles_y,
            "tiles_x": res.tiles_x,
            "phase": list(res.phase),
            "votes_per_bit": res.votes_per_bit,
            "marked_blocks": res.marked_blocks,
            "total_blocks": res.total_blocks,
            "marked_fraction": res.marked_fraction,
            "bit_confidences": res.bit_confidences,
        },
        "marked_url": marked_url,
    })


@app.get("/api/jobs/{job_id}/{kind}")
def get_job_file(job_id: str, kind: str):
    if kind not in ALLOWED_KINDS:
        raise HTTPException(status_code=404, detail="未知文件类型。")
    p = DATA_DIR / job_id / f"{kind}.png"
    if not p.exists():
        raise HTTPException(status_code=404, detail="文件不存在。")
    download = kind in ("watermarked", "marked", "original", "target")
    return FileResponse(
        p, media_type="image/png",
        filename=f"{kind}.png" if download else None,
    )


# ---------------------------------------------------------------- 报告归档接口

@app.post("/api/jobs/{job_id}/reports", status_code=201)
def create_report(job_id: str, req: ReportRequest):
    """把一次或多次检测结果归档为规范化 JSON 报告。

    - 快照任务当前的原图/含水印图/受攻击图/标注图到报告目录，计算 SHA-256 清单；
    - 报告本体（规范化 JSON 字节）的 SHA-256 写入仅追加归档记录 archive.jsonl；
    - 之后任务被清理也不影响报告校验。
    """
    _ensure_data_dir()
    _check_id(job_id, "任务")
    meta = _load_meta(job_id)
    if not req.detections:
        raise HTTPException(status_code=400, detail="请至少选择一次检测结果。")
    if len(req.detections) > MAX_REPORT_DETECTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"单份报告最多包含 {MAX_REPORT_DETECTIONS} 条检测结果。",
        )

    job_dir = DATA_DIR / job_id
    report_id = uuid.uuid4().hex[:12]
    rdir = REPORTS_DIR / report_id
    files_dir = rdir / "files"
    files_dir.mkdir(parents=True)

    # 1) 快照引用文件并计算清单
    references: dict[str, str] = {}
    manifest: list[dict] = []
    for kind in REPORT_FILE_KINDS:
        src = job_dir / f"{kind}.png"
        if not src.exists():
            continue
        dst = files_dir / f"{kind}.png"
        shutil.copy2(src, dst)
        rel = f"files/{kind}.png"
        references[kind] = rel
        manifest.append({
            "name": rel,
            "sha256": _sha256_file(dst),
            "bytes": dst.stat().st_size,
        })

    # 2) 规范化报告文档
    generated_at = _iso_utc()
    detections = [
        {
            "seq": i,
            "source": str(d.source)[:32],
            "recorded_at": d.recorded_at,
            "detected": d.detected,
            "text": d.text,
            "reason": d.reason,
            "confidence": d.confidence,
            "metrics": d.metrics,
            "marked_url": d.marked_url,
        }
        for i, d in enumerate(req.detections, 1)
    ]
    report = {
        "schema": REPORT_SCHEMA,
        "report_id": report_id,
        "job_id": job_id,
        "generated_at": generated_at,
        "generator": {"name": "wm-bench", "version": app.version},
        "parameters": {
            "strength": meta.get("strength"),
            "embedded_text": meta.get("text"),
            "psnr_db": meta.get("psnr_db"),
            "image_width": meta.get("width"),
            "image_height": meta.get("height"),
            "full_tiles": meta.get("full_tiles"),
            "votes_per_bit": meta.get("votes_per_bit"),
            "payload_bytes": meta.get("payload_bytes"),
            "capacity_bytes": meta.get("capacity_bytes"),
            "attack_history": meta.get("attack_history", []),
            "target_width": meta.get("target_width"),
            "target_height": meta.get("target_height"),
            "job_created_at": _iso_utc(meta["created_at"]) if meta.get("created_at") else None,
        },
        "detections": detections,
        "references": references,
        "manifest": manifest,
    }
    blob = _canonical_bytes(report)
    (rdir / "report.json").write_bytes(blob)
    report_sha256 = hashlib.sha256(blob).hexdigest()

    # 3) 写入仅追加归档记录（报告本体摘要的信任锚点）
    record = _append_archive({
        "report_id": report_id,
        "job_id": job_id,
        "archived_at": generated_at,
        "report_sha256": report_sha256,
        "detections": len(detections),
        "files": len(manifest),
    })

    return {
        "report_id": report_id,
        "job_id": job_id,
        "generated_at": generated_at,
        "report_sha256": report_sha256,
        "detections": len(detections),
        "manifest": manifest,
        "archive_seq": record["seq"],
        "download_url": f"/api/reports/{report_id}",
        "verify_url": f"/api/reports/{report_id}/verify",
    }


@app.get("/api/reports/{report_id}")
def get_report(report_id: str):
    """下载归档报告（规范化 JSON，含引用文件清单）。"""
    _check_id(report_id, "报告")
    p = REPORTS_DIR / report_id / "report.json"
    if not p.exists():
        raise HTTPException(status_code=404, detail="报告不存在。")
    return FileResponse(
        p, media_type="application/json",
        filename=f"wm-report-{report_id}.json",
    )


@app.post("/api/reports/{report_id}/verify")
def verify_report(report_id: str):
    """重新计算报告及其引用文件的 SHA-256，报告首个缺失或摘要不一致项。

    校验链：归档记录（report_sha256）-> report.json -> manifest 中各引用文件。
    """
    _check_id(report_id, "报告")
    rdir = REPORTS_DIR / report_id
    if not rdir.is_dir():
        raise HTTPException(status_code=404, detail="报告不存在。")

    entries: list[dict] = []
    record = _find_archive_record(report_id)
    if record is None:
        entries.append({
            "name": "archive_record", "status": "missing",
            "expected": None, "actual": None,
        })
    else:
        # 1) 报告本体：与仅追加归档记录中的摘要比对
        report_path = rdir / "report.json"
        expected_report = record.get("report_sha256")
        if not report_path.exists():
            entries.append({
                "name": "report.json", "status": "missing",
                "expected": expected_report, "actual": None,
            })
        else:
            actual = _sha256_file(report_path)
            entries.append({
                "name": "report.json",
                "status": "ok" if actual == expected_report else "digest_mismatch",
                "expected": expected_report,
                "actual": actual,
            })

        # 2) 清单引用的文件：与报告内 manifest 逐项比对
        try:
            report = json.loads(report_path.read_text(encoding="utf-8")) \
                if report_path.exists() else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            report = {}
        for m in report.get("manifest", []):
            name = str(m.get("name", ""))
            expected = m.get("sha256")
            fp = _safe_join(rdir, name)
            if fp is None or not fp.is_file():
                entries.append({
                    "name": name, "status": "missing",
                    "expected": expected, "actual": None,
                })
                continue
            actual = _sha256_file(fp)
            entries.append({
                "name": name,
                "status": "ok" if actual == expected else "digest_mismatch",
                "expected": expected,
                "actual": actual,
            })

    first_failure = next((e for e in entries if e["status"] != "ok"), None)
    ok_count = sum(1 for e in entries if e["status"] == "ok")
    return {
        "report_id": report_id,
        "ok": first_failure is None,
        "checked": len(entries),
        "ok_count": ok_count,
        "first_failure": first_failure,
        "entries": entries,
        "archive_seq": record.get("seq") if record else None,
        "verified_at": _iso_utc(),
    }


# ---------------------------------------------------------------- 静态前端

@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
