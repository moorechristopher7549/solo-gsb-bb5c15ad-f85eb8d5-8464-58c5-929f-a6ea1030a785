"""抗裁切水印实验台 —— FastAPI 后端。

接口：
  GET  /                 前端页面
  POST /api/embed        上传图片 + 文本 -> 含水印 PNG
  POST /api/attack       对任务图片施加 JPEG 重压缩 / 中心裁切
  POST /api/detect       上传图片或引用任务图片 -> 水印检测结果 + 失真叠加图
  GET  /api/jobs/{id}/{kind}  任务文件下载（original/watermarked/target/marked）
"""

from __future__ import annotations

import io
import json
import time
import uuid
from pathlib import Path

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps
from pydantic import BaseModel

from . import dctwm as wm

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = Path(__import__("os").environ.get("WM_DATA_DIR", "/data/wmbench"))
MAX_JOBS = int(__import__("os").environ.get("WM_MAX_JOBS", "200"))
MAX_UPLOAD_BYTES = 16 * 1024 * 1024
MAX_DIM = 4096
ALLOWED_KINDS = {"original", "watermarked", "target", "marked"}

app = FastAPI(title="抗裁切水印实验台", version="1.0.0")


# ---------------------------------------------------------------- 任务存储

def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _prune_jobs() -> None:
    """超过上限时删除最旧的任务目录。"""
    jobs = sorted(
        (p for p in DATA_DIR.iterdir() if p.is_dir()),
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


# ---------------------------------------------------------------- 请求模型

class AttackRequest(BaseModel):
    job_id: str
    jpeg_quality: int | None = None        # 10~100；为空则不重压缩
    crop_fraction: float | None = None     # 0.2~1.0；为空则不裁切


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


# ---------------------------------------------------------------- 静态前端

@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
