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
4. **报告归档（可验证）**：在「检测报告归档」区勾选同一任务下的一次或多次检测结果，
   点击「生成归档报告」——服务端生成包含**原图 / 含水印图 / 受攻击图引用、恢复文本、
   置信度、嵌入与攻击参数、生成时间**的规范化 JSON 报告，为报告及其引用文件计算
   **SHA-256 清单**并写入**仅追加归档记录**；之后可随时**下载报告 JSON** 或
   点击「校验完整性」重算清单，逐项确认未被篡改。

此外，「独立检测」区支持直接上传任意图片（含水印图、JPEG 转发件、截图等）进行盲检测。

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
| POST | `/api/detect` | multipart：上传 `image` **或**传 `job_id`，返回文本、置信度、指标与标注图 URL |
| GET | `/api/jobs/{id}/{kind}` | 下载 `original` / `watermarked` / `target` / `marked` PNG |
| POST | `/api/jobs/{id}/reports` | JSON：`{detections: [...]}`，生成规范化检测报告并归档（201） |
| GET | `/api/reports/{report_id}` | 下载归档报告 JSON（含 SHA-256 清单） |
| POST | `/api/reports/{report_id}/verify` | 重算清单，返回首个缺失 / 摘要不一致项 |

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

### 检测报告归档（可验证）

`POST /api/jobs/{id}/reports` 接收前端勾选的一次或多次检测结果（`/api/detect` 的响应
加上 `source` / `recorded_at` 字段），完成三件事：

1. **快照引用文件**：把任务当前的 `original / watermarked / target / marked` PNG 复制到
   `{WM_DATA_DIR}/_reports/{report_id}/files/`，之后任务被自动清理也不影响归档；
2. **生成规范化报告** `report.json`：键排序、UTF-8、固定缩进，内容字节级确定，包含
   报告 ID、生成时间（ISO 8601 UTC）、嵌入/攻击参数、检测结果列表、引用文件相对路径
   及其 **SHA-256 清单**（`manifest`）；
3. **写入仅追加归档记录**：向 `{WM_DATA_DIR}/_reports/archive.jsonl` 追加一行
   （只增不改），记录递增序号 `seq` 与报告本体的 `report_sha256`——这是校验报告
   未被篡改的信任锚点。

`POST /api/reports/{report_id}/verify` 按「归档记录 → report.json → 清单文件」的顺序
重算 SHA-256 并逐项比对，响应中的 `first_failure` 给出**首个**缺失（`missing`）或
摘要不一致（`digest_mismatch`）的条目，`entries` 列出每项状态：

```json
{
  "ok": false,
  "checked": 5,
  "ok_count": 4,
  "first_failure": {"name": "files/target.png", "status": "digest_mismatch",
                    "expected": "b1498e…", "actual": "c27a11…"},
  "archive_seq": 1
}
```

> 归档目录 `_reports`（下划线开头）不参与任务数量清理，永久保留在数据卷中。

---

## 五、配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `WM_DATA_DIR` | `/data/wmbench` | 任务文件目录（原图/含水印图/攻击图/标注图，PNG）；其下 `_reports/` 为报告归档 |
| `WM_MAX_JOBS` | `200` | 最多保留任务数，超出自动清理最旧任务（归档不受影响） |

上传限制：单文件 ≤ **16MB**，最大边长 **4096px**，最短边 ≥ **128px**。
compose 默认把数据目录持久化到命名卷 `wm-data`。

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
│   ├── main.py           # FastAPI：任务存储、嵌入/攻击/检测接口、报告归档与校验、静态资源
│   └── static/
│       ├── index.html    # 单页界面（嵌入 / 攻击 / 检测 / 归档 四区）
│       ├── style.css
│       └── app.js
├── tests/
│   └── test_core.py      # 核心算法数值验证（DCT/PN/鲁棒性全链路）
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── README.md
```

数据卷内布局（`WM_DATA_DIR`）：

```
{WM_DATA_DIR}/
├── {job_id}/             # 临时任务：original/watermarked/target/marked.png + meta.json
└── _reports/             # 永久归档（不参与任务清理）
    ├── archive.jsonl     # 仅追加归档记录（每行一条：seq、report_id、report_sha256…）
    └── {report_id}/
        ├── report.json   # 规范化 JSON 报告（含引用文件 SHA-256 清单）
        └── files/        # 引用文件快照（original/watermarked/target/marked.png）
```

运行核心算法测试（本地安装依赖后）：

```bash
python3 tests/test_core.py
```

---

## 七、安全与说明

- 本项目定位于**教学/研究实验台**，无鉴权、任务存于本地卷，请勿直接暴露到公网。
- 上传图片仅用于处理，不会上传到任何第三方；任务按数量自动过期清理。
- 数字水印的鲁棒性是概率性的，标注图与置信度用于辅助判断，不构成版权存证保证。
