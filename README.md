# 抗裁切水印实验台 · Block-DCT Watermark Bench

一个用于研究/演示**鲁棒数字图像水印**的 Web 实验平台。上传图片与一段短文本水印，系统在
**分块 DCT（离散余弦变换）频域**中把水印与同步标记平铺嵌入；嵌入后的图片即使经过
**JPEG 重压缩**与**中心裁切**，检测端仍能自动定位同步网格、通过重复块多数表决恢复文本，
并给出置信度与**疑似失真区域标注图**。

- 后端：Python 3.11 · FastAPI · OpenCV(headless) · Pillow · NumPy
- 前端：原生 HTML / CSS / JavaScript（无构建、无框架）
- 部署：Dockerfile + docker-compose.yml 一键启动

---

## 一、快速启动

### 方式 A：docker compose（推荐）

```bash
docker compose up -d --build
```

启动后浏览器访问：

> **http://localhost:8000**

停止 / 清理：

```bash
docker compose down            # 停止
docker compose down -v         # 停止并删除任务数据卷
```

### 方式 B：本地 Python 运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
WM_DATA_DIR=./data uvicorn app.main:app --host 0.0.0.0 --port 8000
```

访问 **http://localhost:8000** 。

> OpenCV headless 运行时需要系统库 `libgl1`、`libglib2.0-0`（Dockerfile 已安装）。

---

## 二、使用流程

页面按 **① 嵌入 → ② 施加攻击 → ③ 检测 / 标注** 三步组织：

1. **嵌入水印**：选择本地图片（最短边 ≥ 128px，建议 ≥ 512px 的自然照片），
   输入短文本（≤ 45 字节，约 45 个 ASCII 字符或 15 个汉字），选择强度后点击「嵌入水印」。
   页面显示 PSNR、同步瓦片数、每比特票数等指标，并可**下载无损含水印 PNG**。
2. **施加攻击**：拖动滑块选择 JPEG 质量（20–100）与中心裁切保留比例（30%–100%，
   裁切边界吸附到 8px DCT 网格），可多次叠加。点击「检测受攻击图片」。
3. **检测 / 标注**：
   - 自动执行 **64 种块相移全相关搜索**定位同步网格（不依赖图片边角，只要画面里还存在
     完整瓦片即可），再对所有瓦片相同槽位的载荷块做**跨瓦片多数表决**；
   - 显示恢复文本、总体置信度、同步相关峰、同步块一致率与逐比特置信度；
   - 「失真标注图」用颜色标出健康/受损瓦片、判决错误块、可疑弱块，可**下载 PNG**。

此外，「独立检测」区支持直接上传任意图片（含水印图、JPEG 转发件、截图等）进行盲检测。

### 检测报告可验证归档

检测结果面板内可**勾选一次或多次检测结果**生成规范化 JSON 检测报告（归档）：

- 报告包含：报告 ID / 生成时间 / 生成器版本、嵌入参数（文本、强度、PSNR、瓦片数、
  算法常量快照）、每次检测的恢复文本、置信度、完整指标与当时的攻击参数，以及
  **原图 / 含水印图 / 受攻击图（检测输入快照）/ 失真标注图**的引用（路径 + URL + SHA-256）。
- 服务端为报告及其引用文件计算 **SHA-256 清单**（`manifest.entries`），报告写入
  `reports/<report_id>/report.json`，并向数据目录下**仅追加**的 `archive.log.jsonl`
  追加一条哈希链记录（`previous_entry_sha256` 串接，单条记录自带摘要）。
- 点击「校验清单完整性」会重算全部摘要：全部一致时报告通过；否则返回
  **首个缺失文件或首个摘要不一致项**，并同时报告归档哈希链状态（断链 / 记录被删改）。
- 可一键**下载 JSON 报告**；报告引用文件可通过报告中的 URL 单独下载。
- 报告存放在 `WM_DATA_DIR` 内，随 compose 命名卷持久化，且**不受任务数量上限清理影响**。
- 归档检测输入快照按检测记录单独保存（`detections/det_*.source.png`），
  后续继续叠加攻击不会改变既有报告引用的图片内容。

报告 JSON（节选）：

```json
{
  "schema_version": "1.0",
  "report_id": "rep_8b24aed3390e",
  "job_id": "756b2203d38f",
  "generated_at_iso": "2026-09-23T05:07:46Z",
  "detections": [
    {
      "detection_id": "det_f11ab49c384d",
      "source": "target",
      "detected": true,
      "text": "机密-2026",
      "confidence": 100.0,
      "metrics": { "sync_score": 1.0, "sync_agreement": 1.0 },
      "attack_history": [ { "type": "jpeg", "quality": 65 } ],
      "references": [
        { "role": "attacked", "path": ".../det_xxx.source.png",
          "url": "/api/jobs/.../detections/det_xxx/source",
          "sha256": "f1ac…", "bytes": 119576, "exists": true }
      ]
    }
  ],
  "manifest": {
    "hash_algorithm": "sha-256",
    "entries": [ { "path": "…/original.png", "sha256": "20c4…", "bytes": 203749 } ]
  },
  "archive": { "log_path": "archive.log.jsonl", "seq": 1, "append_only": true }
}
```

> 报告自身的摘要 = SHA-256（报告删除 `manifest` 键后的规范化 JSON：UTF-8、键排序、
> 无空白），因此不存在摘要依赖自身的循环；规范化规则记录在
> `manifest.canonical_form` 中。

### 标注图颜色含义

| 颜色 | 含义 |
| --- | --- |
| 绿色瓦片底纹 | 该瓦片同步标记一致性高（≥ 80%） |
| 黄色瓦片底纹 | 同步标记部分受损（40%–80%） |
| 红色瓦片底纹 | 同步标记大面积失败（< 40%） |
| 绿色小块 | 同步块 / 载荷块判决正确 |
| 红色小块 | 块判决与恢复码流不一致（疑似被压缩/裁切破坏） |
| 黄色小块 | 判决正确但系数差值过小（临界弱块，重点观察区） |
| 蓝色网格线 | 128px 瓦片（同步周期）边界 |

---

## 三、算法说明

### 1. 分块 DCT 频域嵌入

- 图像转 YCrCb，仅在**亮度 Y 通道**处理；按 **8×8** 分块并做正交 DCT-II（与 JPEG 一致）。
- 每个 8×8 块用两对中频系数做**差值调制（spread-spectrum / QIM 风格）**：

  ```
  bit = 1  →  C(3,2) − C(2,3) > +δ      （第二对：C(4,1) − C(1,4)）
  bit = 0  →  差值 < −δ
  ```

  中频系数对 JPEG 量化有天然鲁棒性；差值形式对图像符号变化不敏感。
- `δ` 按强度档位（low / medium / high）给出，并乘以**纹理自适应因子**
  （平坦区域减小、纹理区域增大），兼顾不可见性与鲁棒性；嵌入经 3 轮迭代补偿
  IDCT 舍入与 0–255 裁剪导致的差值回落。

### 2. 同步标记 + 载荷平铺

- 每 **16×16 个块 = 128×128 像素**为一个**瓦片**，全图周期平铺：
  - 瓦片四角 4×4 共 **64 个同步块**，携带一段 **31 bit PN m 序列**（5 级 LFSR，
    自相关旁瓣 ≤ 1），用于确定瓦片网格的块级相位；
  - 其余 **192 个载荷块**，每块 2 bit，共 384 bit；码流
    `魔数 0xAB | 长度 | UTF-8 文本 | 校验和` 在**每个瓦片内重复平铺**。
- 检测端枚举 64 种块相移做归一化相关，取相关峰；允许整体符号翻转。
  因此**中心裁切后无需知道裁掉了多少**，只要画面里残留 ≥ 1 个完整瓦片并满足最少
  同步样本数即可对齐（实验图在保留 30% 边长时仍可恢复）。

### 3. 多数表决与置信度

- 所有瓦片**相同槽位**的载荷块按符号/软差值累加投票（瓦片内码流重复也贡献票数），
  得到软判决比特流；再从最大可能长度向下扫描，魔数与字节校验和同时通过才采信，
  显著降低误报。
- 置信度综合两类指标：
  - **软差值裕量**：平均系数差值相对 JPEG 量化步长的大小；
  - **投票一致率**：跨块投票中多数派占比。
- 逐块比对期望符号：同步块看 PN，载荷块看恢复码流；判决翻转块标红、
  临界弱块（|软差值| < 阈值）标黄，按瓦片聚合健康度。

### 4. 实测鲁棒性（合成测试图，medium 档）

**A. 纹理丰富的自然纹理图**（不可见性 PSNR ≈ 33 dB）

| 场景 | 结果 |
| --- | --- |
| 无攻击 | 置信度 100%，零失真块 |
| JPEG Q90 / Q70 / Q50 / Q40 / Q30 | 文本 100% 恢复 |
| JPEG Q25 / Q20 | 可恢复，置信度约 88% / 47% |
| 中心裁切保留 90% / 75% / 50% / 30% | 文本 100% 恢复 |
| 裁切 75% + JPEG Q60、裁切 50% + JPEG Q50 | 文本 100% 恢复 |
| 无水印图 | 正确判为「未检测到」 |

**B. 极度平滑的低频图**（PSNR ≈ 35.5 dB；JPEG 对平坦区量化最狠，是最坏情形）

| 强度 \ JPEG | Q40 | Q35 | Q30 | Q25 |
| --- | --- | --- | --- | --- |
| low | △（低置信度） | ✘ | ✘ | ✘ |
| medium | ✔ | ✔ | ✔（18%） | ✘（同步仍成功，校验失败） |
| high | ✔ | ✔ | ✔ | ✔（94%） |

> 复现：`python3 tests/test_core.py`。

> 提示：水印鲁棒性与图片内容相关。纹理丰富的照片表现最好；大片纯色/极暗区域、
> 极端低质量（Q < 25）JPEG、缩放/旋转/非 8 像素对齐裁切会明显增加误码——
> 这正是标注图与置信度的意义所在。

---

## 四、HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 前端页面 |
| GET | `/api/health` | 健康检查 |
| POST | `/api/embed` | multipart：`image`(图片)、`text`、`strength`(low/medium/high)，返回任务 ID 与嵌入指标 |
| POST | `/api/attack` | JSON：`{job_id, jpeg_quality?, crop_fraction?}`，生成受攻击待测图 |
| POST | `/api/detect` | multipart：上传 `image` **或**传 `job_id`，返回文本、置信度、指标、检测记录 ID 与标注图 URL |
| GET | `/api/jobs/{id}/{kind}` | 下载 `original` / `watermarked` / `target` / `marked` PNG |
| GET | `/api/jobs/{id}/detections/{det_id}/{kind}` | 下载某次检测的 `source`（检测输入快照）/ `marked`（标注图）PNG |
| POST | `/api/jobs/{id}/reports` | JSON：`{"detection_ids": ["det_…", ...]}`（1–20 条），生成可验证报告并追加归档，返回 `report_id`、`download_url`、`verify_url` |
| GET | `/api/reports/{report_id}` | 读取报告 JSON（内联）；加 `?download=1` 以下载附件 |
| POST | `/api/reports/{report_id}/verify` | 重算 SHA-256 清单；全部一致返回 200，否则返回 409 与**首个** `missing` / `digest_mismatch` 项及归档哈希链状态 |

检测响应（节选）：

```json
{
  "detected": true,
  "text": "机密-2026",
  "metrics": {
    "confidence": 98.7,
    "sync_score": 0.998,
    "sync_agreement": 0.99,
    "tiles_y": 3, "tiles_x": 3,
    "phase": [8, 8],
    "votes_per_bit": 18,
    "marked_blocks": 12,
    "total_blocks": 2304,
    "marked_fraction": 0.0052,
    "bit_confidences": [1.0, 0.98]
  },
  "marked_url": "/api/jobs/.../marked"
}
```

---

## 五、配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `WM_DATA_DIR` | `/data/wmbench` | 任务文件目录（原图/含水印图/攻击图/标注图，PNG）与归档目录（`reports/`、`archive.log.jsonl`） |
| `WM_MAX_JOBS` | `200` | 最多保留任务数，超出自动清理最旧任务（**归档报告不参与清理**） |

上传限制：单文件 ≤ **16MB**，最大边长 **4096px**，最短边 ≥ **128px**。
compose 默认把数据目录持久化到命名卷 `wm-data`；归档报告与仅追加日志
（`$WM_DATA_DIR/reports/`、`$WM_DATA_DIR/archive.log.jsonl`）随之持久化，
升级镜像后仍可下载与校验，Docker Compose 启动方式不变。

### 嵌入参数（在 `app/dctwm.py` 顶部调整）

| 参数 | 含义 |
| --- | --- |
| `STRENGTH_DELTA` | 三档基础嵌入差值 δ（按 JPEG Q50 量化步长标定） |
| `TILE=16` / `SYNC=4` | 瓦片 16 块、同步角 4 块（周期 128px） |
| `SYNC_SCORE_THRESHOLD` | 同步相关峰判决门限（默认 0.42） |
| `WEAK_MARGIN` | 可疑弱块的软差值阈值（默认 14） |
| `PAIR_A / PAIR_B` | 两个调制系数对的位置 |

---

## 六、目录结构

```
.
├── app/
│   ├── dctwm.py          # 核心算法：DCT 嵌入 / PN 同步 / 相移搜索 / 多数表决 / 失真标注
│   ├── archive.py        # 检测报告：规范化 JSON / SHA-256 清单 / 仅追加哈希链归档 / 校验
│   ├── main.py           # FastAPI：任务存储、嵌入/攻击/检测/报告接口、静态资源
│   └── static/
│       ├── index.html    # 单页界面（嵌入 / 攻击 / 检测 / 归档 四区）
│       ├── style.css
│       └── app.js
├── tests/
│   ├── test_core.py      # 核心算法数值验证（DCT/PN/鲁棒性全链路）
│   └── test_reports.py   # 报告归档接口与清单/哈希链校验（篡改、缺失、断链）
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── README.md
```

运行核心算法测试（本地安装依赖后）：

```bash
python3 tests/test_core.py
python3 -m pytest tests/test_reports.py -q    # 归档接口端到端测试
```

数据目录布局（`WM_DATA_DIR`）：

```
<job_id>/
├── meta.json                         # 嵌入参数（独立上传检测时为 upload_only 轻量元数据）
├── original.png / watermarked.png / target.png / marked.png
└── detections/
    ├── det_xxxxxxxxxxxx.json         # 检测记录（文本/置信度/指标/攻击参数快照）
    ├── det_xxxxxxxxxxxx.source.png   # 该次检测输入快照
    └── det_xxxxxxxxxxxx.png          # 该次检测失真标注图
reports/rep_xxxxxxxxxxxx/report.json  # 规范化报告（含 SHA-256 清单）
archive.log.jsonl                     # 仅追加归档记录（哈希链）
```

---

## 七、安全与说明

- 本项目定位于**教学/研究实验台**，无鉴权、任务存于本地卷，请勿直接暴露到公网。
- 上传图片仅用于处理，不会上传到任何第三方；任务按数量自动过期清理。
- 数字水印的鲁棒性是概率性的，标注图与置信度用于辅助判断，不构成版权存证保证。
