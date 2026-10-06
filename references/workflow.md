# workflow.md — 分层管线的脚本、参数与数据结构

所有脚本都用同一个基准目录：

```python
BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()
```

在新项目里 `export XTRANS_BASE=/path/to/project` 即可；不设则取当前工作目录。
另外两个环境变量：`XTRANS_SRC`（源 PDF，缺省 `$BASE/input.pdf`）、
`XTRANS_DECOR`（品牌装饰字正则，每个项目都要按实测值设）。
OCR 二进制 `ocr_vision` 跟着 skill 走（`Path(__file__).parent / "ocr_vision"`），不用往项目里拷。

---

## 第 0 步：源文件守卫（绝对红线）

### `67_source_guard.py` — 记账 / 复核 / 加锁

红线：**整个过程不可以损坏原文件，不要对原文件进行任何的改动**（见 SKILL.md §零）。
这个脚本是那句话的执行机制。它在流程里的位置有两个端点：**开工第一步**记账，
**收工最后一步**复核。

```bash
python3 67_source_guard.py --record         # 开工第一步：记 SHA-256 基线
python3 67_source_guard.py --record --seal  # 顺手加锁（chmod 444 + macOS uchg）
python3 67_source_guard.py --check          # 收工最后一步：内容变则退出码 1
python3 67_source_guard.py --seal / --unseal  # 单独加锁 / 解锁
python3 67_source_guard.py --check --files a.pdf b.pdf   # 额外素材一并守
```

要守护哪些文件，与 `63_audit_structure.py` 的 `find_source()` 同一口径：
显式 `--files` > `$XTRANS_SRC` > 根目录下唯一的 `*.pdf` > `input.pdf`。
**"源"只能有一个定义**，两处各认一个是最容易出的事故。

产物 `work/verify/source_integrity.json`：

```json
{"recorded_at": "2026-10-06T19:52:13+08:00",
 "files": [{"path": "…pdf", "size": 36036028,
            "mtime": "…", "mode": "0o644", "sha256": "225db755…",
            "recorded_at": "…"}]}
```

**判定口径**（设计取舍见 pitfalls 第 49/50 条）：

| 情况 | 判定 | 理由 |
|---|---|---|
| `sha256` 或 `size` 变了 | **FAIL**，退出码 1 | 这就是红线被踩了，停下来上报 |
| 只有 `mtime` 变了 | WARN | 备份/同步盘/iCloud 都会碰时间戳，不算内容变化 |
| 只有权限位变了 | 只写进说明列 | `--seal` 本来就要 `chmod`，算进判定会满地假警报 |
| 记账时发现基线已存在且哈希不同 | **默认拒绝重建** | 必须显式 `--force`，否则"顺手重建基线"会把误写洗白 |

**为什么 `--seal` 要 `chmod 444` + `uchg` 两道**：实测 `chmod 444` 只挡得住
`saveIncr()`（`Permission denied`），改名和删除照样能做；再加 `uchg` 之后
`mv`/`rm` 也变成 `EPERM`。两者都不影响读取——PyMuPDF 本来就是只读打开。
**顺序不能反**：`uchg` 在位时 `os.chmod` 会 `EPERM`，所以 seal/unseal 都要先解 `uchg`。

---

## L0 侦察

### `10_probe_pdf.py` — 类型判定

```bash
python3 10_probe_pdf.py <input.pdf>
```

报告：页数、页面尺寸（pt）、字体列表与是否嵌入、图片数量与 xref、文字层字符数。
**判据**：文字层字符数 / 页数 < 50 → 视为扫描型；页面尺寸非 A4/Letter（如 720×405）→ 设计稿型。

### `11_embed_fonts.py` — 字体落地

把目标语言字体下载/复制到 `work/fonts/`，并生成 `@font-face` CSS 片段。
字体清单见 `fonts-langs.md`。

### `12_scan_image_text.py` — 图内文字 OCR 侦察

输入 `work/ocr_raw4.json`（整页 4 倍渲染后的 OCR 结果），减去与文字层重合的块，
剩下的就是**烧在位图里的文字**。

输出 `work/ocr_scope4.json`：

```json
{
  "summary": { "ocr_blocks_total": 4650, "on_text_layer": 735, "in_image_blocks": 3915,
               "in_image_chars": 56312, "unique_strings": 2571,
               "by_class_blocks": {"label": 1242, "sentence": 671, "skip_numeric": 2002} },
  "in_image": [ { "page": 1, "text": "AIOZGROWTH", "conf": 1,
                  "bbox": [281.5, 30.3, 439.5, 55.5], "cls": "label" } ]
}
```

**关键**：渲染并 OCR 整页的命令（Windows/Linux 换成 tesseract 同理）

```bash
# 4 倍渲染
python3 - <<'PY'
import pymupdf
d = pymupdf.open("input.pdf")
for i in range(d.page_count):
    d[i].get_pixmap(matrix=pymupdf.Matrix(4, 4), alpha=False).save(f"work/pages4/p{i+1:02d}.png")
PY
# OCR（macOS Vision）
~/.claude/skills/X-Translation/scripts/ocr_vision work/pages4/*.png > work/ocr_raw4.json
```

**zoom 必须 ≥4**。2.2 倍时 5pt 以下文字误识严重（`$OB`/`OpenB` 之类），4 倍可用。

### `13_scan_vertical.py` — 竖排补扫

Vision 整页模式下基本读不出旋转 90° 的纵轴标题。把页面图顺时针转 90° 再 OCR，
旋转文字变成横排即可识别，再映射回原坐标：

```
旋转图归一化 (u', v') → 原图 (u, v) = (v', 1 - u')
原 bbox = (v0', 1-u1', v1', 1-u0')
```

筛"确实竖排"：映射回原图后 `w < 34 且 h > 18 且 h > 1.4w`。
输出 `work/ocr_vertical.json`（全量）与 `work/ocr_vertical_new.json`（去掉主扫描已有项）。
启动图旋转用 `np.rot90(arr, k=-1)`。

### `14_scan_bg.py` — 覆盖法可行性

对每个图内文字块，统计 bbox 内像素的颜色众数占比。

- 众数占比 ≥ 0.6 → 平坦底色，覆盖法无痕
- 0.25 ~ 0.6 → 可覆盖但需留意
- < 0.25 → 背景是图案/渐变，覆盖会留接缝

### `15_probe_strata.py` / `16_probe_smalltext.py` — 分层抽样

按「块高 × OCR 置信度」分层，每层抽 5~10 张对照图，判断该层能不能整体覆盖。
**不要一上来全量翻**——先用样张定策略，能省掉一轮全量返工。

### `17_audit_ocr.py` — OCR 质量抽检

把页面裁剪图与 OCR 文本并排，肉眼核对误识率。OCR 认错的条目必须标 `=` 保留原文。

---

## L1 文字层重建

### `20_extract_units.py` — 抽取文本单元

输出 `work/units.json`（**页码 0 基**）：

```json
{ "p20u03": { "page": 19, "bbox": [x0,y0,x1,y1], "size": 9.5, "font": "RobotoSerif-Regular",
              "lineheight": 11.6, "baseline": 0, "text": "...", "lines": [...] } }
```

### `21_merge_translations.py` — 合并译文

`work/units.json` + 译文 → `work/translations.json`。译文里写 `=` 表示保留原文。

### `22_fix_align.py` — 对齐精判（★ 回填前必做）

```bash
python3 22_fix_align.py --report                       # 只看将要改哪些
python3 22_fix_align.py --apply --log work/align_changes.json
```

抽取给出的 `align` 是**粗判**，必须经这里精判（原因见 SKILL.md 第四节与
`pitfalls.md` 第 26/27 条）。多行取"极差最小轴 + 决定性判据 + 容差随字号缩放"，
单行走"锚点 → 同行中点 → 同行继承 → 页面居中/靠右"四步兜底。

`--apply` 会先备份成 `units.json.bak`。`--log` 写出的改判清单带三轴极差，
便于逐条复核。**不重跑抽取**——`translations.json` 用 id 索引，重抽取会让译文错位。

源 PDF 只用来取页面宽度（判断"页面居中/靠右"）。默认自动找项目根目录下唯一的 `*.pdf`；
找不到就跳过该兜底并打印警告（封面这类页面可能因此漏判）。

### `23_calibrate_baseline.py` — 基线标定

`K = insert_htmlbox 文本框顶端 → 首行基线的距离`，按 (字号, 行高比) 组合实测。
输出 `work/baseline_calib.json`。回填时 `box.y0 = 原文首行基线 - K`，
译文与原文基线才严格对齐。**跳过这一步，正文会整体上下漂移。**

### `24_rebuild_text.py` — 抹除 + 回填

```bash
XTRANS_SRC=input.pdf python3 24_rebuild_text.py work/translations.json work/stage/L1.pdf \
    --report work/l4/rebuild_report.json
```

两个必须记住的细节：

1. 单行单元的行高**多余部分是纯行距空白**。若文本框下方紧贴障碍物（图、页脚），
   排版引擎会把整行缩字（实测被缩到 0.926）。补足行距空白即可 —— 补的是空白，字形不动：

```python
if len(u["lines"]) == 1:
    need = lh_ratio * size
    deficit = need - box.height
    if 0 < deficit <= 0.3 * size:
        box.y1 = min(box.y0 + need, page.rect.height - 2)
```

2. `rebuild_report.json` 里 `auto-shrunk` 必须为 0。不为 0 就是上面那类问题没修完。

---

## L2 图内文字原位覆盖

### `30_build_l2_queue.py` — 生成工作单

从 `work/ocr_scope4.json` 过滤后分批导出 `work/l2_queue/batch_NN.txt`：

```
0001	[sent x1 p7 h51]	Change in Net Income
```

`[...]` 里是元信息（类型 / 出现次数 / 页 / 块高）。gid 段位约定：
`0001+` 主工作单、`9001+` 补扫、`9500+` 微标签 —— **不要复用段位**，否则会静默覆盖。

### `31_build_l2_plan.py` — 合成覆盖计划

工作单 + 译文 + `ocr_scope4.json` + `ocr_vertical_new.json` → `work/l2_queue/overlay_plan_l2.json`。

过滤规则（**必须与 30 一致**，否则译文对不上）：跳过 `skip_numeric`、装饰字、
块高 < `--min-h`（默认 5.0）、纯 CJK、工作单里没有的。

然后重叠去重（`--dup-threshold` 默认 0.55，同位置 OCR 常出多条，留文本更长的），
再定竖排：`w < 14 and h > 18 and len(zh) >= 2` → `rotate: 90`。

```json
{ "source": "work/stage/L1.pdf", "output": "output/xxx.pdf", "zoom": 4,
  "font": "NotoSerifSC-Regular.ttf",
  "defaults": {"pad": 0.7, "size_ratio": 0.78, "size_cap": 1.15, "scale_floor": 0.985},
  "items": [ {"page": 5, "bbox": [341.2,63.8,639.4,81.7], "zh": "科技现占…",
              "align": "left", "rotate": 0} ] }
```

### `32_merge_l2_plan.py` — 增量合并

补充条目按 bbox 重叠去重后并进既有计划，**不重算全局**。
重新全局构建会改变去重结果、丢掉原有条目（实测丢过 70 条）。

### `33_build_micro_plan.py` — L2b 微标签补译（见 SKILL.md 第四节）

```bash
python3 33_build_micro_plan.py --mode queue      # 产出 work/l2_queue/micro_queue.txt
# ← 翻译，写 work/l2_queue/micro_trans.txt（gid / 中文，`=` 表示保留）
python3 33_build_micro_plan.py --mode plan       # 产出 overlay_plan_micro.json
```

### `40_apply_overlay.py` — 原位覆盖（核心）

```bash
python3 40_apply_overlay.py work/l2_queue/overlay_plan_l2.json
```

对每条：采样背景色 → 铺同色矩形 → `insert_htmlbox` 写译文。
字号自适应用**纯算术二分**（`fit_size_arith`），不调排版引擎试算。
`scale < scale_floor` 或 `bg_ratio < 0.25` 的条目会进"需留意"清单。

---

## L2 补扫（残迹驱动）

### `50_build_supplement.py` — 生成补充覆盖候选

```bash
python3 50_build_supplement.py --min-h 3.6
```

残迹扫描（`60_scan_residue.py`）会暴露出两类**系统性**漏项，本脚本把两类合流成补充工作单：

1. `unseen` —— 竖排文字，主 OCR 读不出 → 由 `13_scan_vertical.py` 的旋转扫描补上；
2. `filtered` / 过小 —— 块高 <5pt 被门槛挡掉，但其中 **3.6~5pt 段实际可读**。

用严格判据过一遍（长度、元音占比、字符种类），只留"看着就是正常英文"的，
输出 `work/l2_queue/batch_s1.txt` 供翻译，再用 `32_merge_l2_plan.py` 增量合并进覆盖计划。

**gid 段位 `9001+`**，与主工作单（`0001+`）、微标签（`9500+`）分开，绝不复用。

---

## L3 标题位图

### `41_drop_title_bitmap.py` — 「位图 + 文字层双份」时抹掉位图那层

判据：透明位图（SMask alpha 均值 0.03~0.09）且与 ≥18pt 的文字块重叠 ≥ 50%，
高度比 0.5~2.5。命中后用 `Page.replace_image` 换成**从背景图裁的同位置像素** —— 无损，不动内容流。

```bash
python3 41_drop_title_bitmap.py --src work/stage/L1.pdf --out work/stage/L3.pdf --apply
```

xref=0 的内联图（部分导出器产生）无法替换，会跳过并计数。

### `42_make_cover_art.py` — 生成目标语言美术字

**只有位图、文字层里没有**的标题（最常见的就是封面大标题）必须走这条路，
见 SKILL.md 第四节的 L3 小节。

```bash
python3 42_make_cover_art.py --text "市场状况" --size 2048 878 \
    --tint-pdf input.pdf --tint-xref 14 --out work/cover_art.png
```

- `--size W H` 必须与目标图像素尺寸一致（`43_cover_title.py --list` 会打印出来）。
- `--tint-pdf` / `--tint-xref` 用来从**原封面标题图层**采样色温，让金属色泽与原设计一致。
  取的是**只色温不亮度**——原图逐行均值是暗斑与亮边的混合，直接拿来填色只能是灰调。
- `--font` 默认 `work/fonts/NotoSerifSC-Medium.ttf`。**字重要实测定**：
  汉字笔画密度高于拉丁字母，照搬原图粗细往往偏重。
- 其他旋钮：`--width`（文字占画布宽比）、`--tracking`（字距）、
  `--outline` / `--bevel` / `--cleft`（描边与倒角）。

### `43_cover_title.py` — 原位换图

```bash
python3 43_cover_title.py --list --src input.pdf        # 先看有哪些图层
python3 43_cover_title.py --src work/stage/L1.pdf --out work/stage/L3.pdf \
    --title-size 2048 878 --title-rect 212.9 139.4 507.1 265.6 \
    --art work/cover_art.png
```

- **按「像素尺寸 + 放置矩形」定位，不用 xref**——xref 在管线里会变（实测 14 → 9）。
- 新图沿用原像素尺寸与放置矩形，只换字形像素与蒙版 → 位置/大小/层级全不变。
- **阴影层自动找**（其它带透明蒙版、与标题矩形重叠最大者）并**按新字形重建它的蒙版**；
  底图不用动。`--shadow-rect` 可手动指定。
- 不传 `--art` 则整步直通（只复制 PDF），方便先跑通链路。
- ⚠️ 写图像流的两个坑见 `pitfalls.md` 第 30/31 条：**不能塞 JPEG 字节**、
  **不能手动再 zlib 一次**。传解压后的原始像素，压缩交给 `update_stream`。

---

## L4 验收

L4 分两层：**残迹核验**（`60/61/62`，回答"还有没有漏翻的外文"）和**成果检验**
（`63–69`，回答"做出来这份到底能不能交"）。两层互不替代 —— 残迹扫描只看"还有没有英文"，
对"页数对不对、术语统不统一、数字有没有丢、文字层能不能复制"一概不答。

成果检验按**四层闸门**排：

| 层 | 脚本 | 产出 | 谁判 |
|---|---|---|---|
| **T0 结构断言**（硬闸） | `63_audit_structure.py` | `work/verify/structure.json` | 机器，非零退出即不可交付 |
| **T1 文本完整性**（硬闸） | `64_audit_text.py` | `pairs/terms/numbers/charset/glossary_proposed.json` | 机器报红项，人裁决 |
| **T2 分层抽检**（人判） | `65_sample_review.py` | `sample_plan.json` + `REVIEW.md` + 对照图 | 人，抽样 10% |
| **T3 回译核验**（排序工具） | `66_backtranslate.py` | `backtrans_queue.txt` / `backtrans.json` | 模型回译，只用来排序 |
| **汇总** | `69_qc_summary.py` | `work/verify/QC_REPORT.md` | 只读聚合，天然放最后 |

### `60_scan_residue.py` — 反向残迹扫描

```bash
python3 60_scan_residue.py --pdf output/成品.pdf --ocr work/ocr_raw_v12.json \
        --plan work/l2_queue/overlay_plan_all.json
```

重新渲染 + OCR **成品**，把所有还读得出的外文捞出来，回溯它在源 OCR / 计划里的位置，按成因归类：

| 类 | 含义 |
|---|---|
| `keep` | 有意保留：品牌 / 代码 / 数字刻度 / 页脚装饰字 |
| `textlayer` | 成品文字层本就带这段（专有名词、网址） |
| `covered` | 补丁已应用，但中文比英文短 —— 英文从旁边露出来 |
| `filtered` | 源块被过滤规则挡掉（过小 / 数字 / 装饰 / 标 `=` 保留） |
| `unseen` | 源 OCR 根本没读到该块 → 需要核验真伪 |
| `residual` | 其它 |

`is_english()` 的判据：整串 ASCII 可打印 + 字母占比 ≥ 0.6 + 置信度 ≥ 0.5。
不加置信度闸，中文被误读成西里尔字母的假阳性会翻倍（实测 553 → 251）。

### `61_show_residue.py` — 残迹对照图

把残迹清单渲染成「原图 | 成品」对照图，供肉眼判读。

### `62_verify_residue.py` — 回源核验真伪（★ 必做）

`unseen` 类里混着两种东西：真英文残迹，和 **Vision 把成品自己的中文读成拉丁字母**。
两者文本层面分不开（都可能 conf=1.0）。

解法：对**同一个矩形框**分别从源 PDF 和成品 PDF 裁图（几何完全一致），各自 OCR 后对比：

- 源框读出英文、成品框读出同一英文 → `real`（真残迹）
- 成品框读出英文、源框读不出（空白 / CJK 碎片）→ `noise`（读的是我们写的中文）
- 两边读出不同的英文 → `lookup`（框位串了邻位，需人工看）

```bash
python3 62_verify_residue.py --cls unseen,covered,filtered --min-h 4.5 \
        --pdf output/成品.pdf --residue work/residue/residue.json
```

裁图要点：外扩量随块尺寸走（`max(2pt, 0.18 × 长边)`），四周补 24px 白边 ——
Vision 对贴边文字识别率明显下降。裁片太小会读不出，导致误判。

### `63_audit_structure.py` — T0 结构断言（硬闸）

```bash
python3 63_audit_structure.py --pdf output/xxx_zh-CN.pdf
python3 63_audit_structure.py --pdf output/xxx.pdf --title-size 14 --tol 0.5
```

参数：`--pdf`（成品）、`--src`（源，缺省取 `XTRANS_SRC` 或根目录唯一 `*.pdf`）、
`--title-size`（标题字号阈值，0 = 自动）、`--tol`（尺寸容差，默认 0.5pt）、`--out`。
输出控制台 PASS/FAIL 表 + `work/verify/structure.json`，**FAIL>0 时退出码 1**。

七项断言，任一 FAIL 则退出码为 1（**非零退出即不可交付**）：

| id | 断言 | 判据 | 说明 |
|---|---|---|---|
| `open` | 文件完好 | 能打开且页数 > 0 | 排除写坏的成品 |
| `pages` | 页数 | 与源一致 | 少一页通常意味着某步 `insert_pdf` 出了岔 |
| `size` | 页面尺寸 | 逐页比对，容差 0.5pt | **必须逐页比**——设计稿常有尺寸不同的内页 |
| `rotation` | 旋转 | `/Rotate` 与源一致 | 被静默重置会让某一页横过来 |
| `toc` | 大纲/书签 | 目标条目数 ≥ 源，且书签文字已译 | 源侧本就没有书签时记 **SKIP 而非 FAIL** |
| `titles` | 章节标题完整 | 源侧 ≥ 阈值的标题单元都有非空译文 | 阈值 `max(12.0, 中位字号 × 1.4)` |
| `fonts` | 字体嵌入 | `extract_font` 能取回字节 | 未嵌入 = 到别人机器上字会变 |

三个非显然点写在 docstring 里：尺寸**逐页**比；源侧缺失记 SKIP；纯数字"标题"
（目录页的 `01`–`08`）必须排除，否则满屏误报。

### `64_audit_text.py` — T1 文本完整性（硬闸）

```bash
python3 64_audit_text.py --pdf output/xxx_zh-CN.pdf
python3 64_audit_text.py --pdf output/xxx.pdf --sample-terms 40
```

参数：`--pdf`、`--min-occ`（专名候选的最低出现块数，默认 3）、
`--sample-terms`（抽检表每类最多列几条，默认 30）。
**输出目录固定为 `$BASE/work/verify`，没有 `--out-dir` 参数。**
产出 `{pairs,terms,numbers,charset,glossary_proposed}.json` + `terms_sample.md`。

**配对表的建法**（后面六项检查全建在它上面）：对源 PDF 的每个文本块，
在成品 PDF 的**同一页同一矩形**取文字 —— `page.get_text("text", clip=rect)`。
必须取**与该矩形相交的全部 span 的并集**，不能只取单个 span：中文比英文短，
单 span 匹配会漏掉一大批。

六项检查，**强弱是有差别的**（docstring 里逐项标注）：

| # | 检查 | 强度 | 说明 |
|---|---|---|---|
| ① | 重复源串译法一致 | **硬** | 同一条源串出现多次却译出两种译文 |
| ② | 专有名词保留/翻译策略一致 | **硬** | 同一术语时而保留原文、时而译出 |
| ③ | 括注术语有据 | 仅抽样人判 | 新造译法是否在括注里给了原文 |
| ④ | 数字守恒 | **硬** | 分 `check`/`unit`/`date`/`noise` 四档 |
| ⑤ | **字符集核验** | **硬** | 见下 |
| ⑥ | 冻结术语表逐条核对 | **硬** | 读 `work/glossary.json` |

**⑤ 是这一层的枢纽**。思路是**反推"我们本来打算写哪些字"**：
源文 + 我们自己的译文/计划文本 + `string.printable` + 中英标点 = 应然字符集。
成品里凡是超出这个集合的字符，都是"凭空出现"。

它能抓的是**字形渲染正确、码位却写错**这一类：`insert_htmlbox` 有时把字符的码写成
字体**原始字形 id**，越过该字体 ToUnicode 的覆盖上限 → 抽取端回退成 `chr(code)`。
典型签名是**一串连续码位**（数字 0–9 → `U+7778`–`U+7781`）或**突兀的希腊字母**
（`fl` 连字 → `Κ`）。取证用 `page.get_texttrace()`，它直接给出每个字符的字形 id。

判定要落在「**该字体 ToUnicode 的覆盖上限**」上，不是「像不像乱码」——
`⸺`/`ﬃ` 这类连字在表里**有**条目，属良性。

它同时充当**输入过滤器**：`tgt` 里含坏字符的条目先被剔除，
不参与 ①/②/③（它们的"译文"本身是垃圾，会让后面三项连锁误报），
只由 ⑤ 统一报一次。

```python
bad_chars = {r["char"] for r in charset if not r["benign"]}
clean = [p for p in pairs if not any(c in p["tgt"] for c in bad_chars)]
```

`BENIGN_CHARS` = `U+FB00–U+FB06` 连字 + `U+2E3A/U+2E3B` 破折号连字 ——
这是**字体把相邻字符合并成一个字形**，语义等价、观感一致，只是按原串搜索会失效，
归为 `benign` 只警告不报红。

**④ 数字 token 的四条约束**（不满足会让误报从 4 条涨到 407 条）：

```python
NUMTOK = re.compile(r"(?<![A-Za-z0-9])[0-9][0-9oOlI,.\u00a0]{0,18}")
LOOKALIKE = str.maketrans({"o":"0","O":"0","l":"1","I":"1"})
```

① 必须以**真数字**开头；② 左侧不紧邻字母数字；③ 形近字 `o/O/l/I` 只允许出现在数字串**内部**；
④ 先剥掉尾部表示复数的 `s`。然后再把复合 token 拆成数字段逐段比。

`classify_number()` 把不匹配的项分四档，**只有 `check` 是红项**：

- `unit` —— 币值量级换算（`$97.4B` → `974 亿`）
- `date` —— 日期重排（`Aug 11, 2026` → `2026 年 8 月 11 日`；轴标 `Jan 12` → `1/12`）
- `noise` —— 源侧 OCR 垃圾（带 `?` 或 ≥10 位数字连串）
- `check` —— 真的丢了或错了

### `65_sample_review.py` — T2 分层抽检

```bash
python3 65_sample_review.py --pdf output/xxx_zh-CN.pdf              # 默认抽 10%
python3 65_sample_review.py --pdf output/xxx.pdf --ratio 0.15 --seed 7
```

参数：`--pdf`、`--src`、`--ratio`（默认 0.10）、`--seed`（默认 11，保证可复现）、
`--long`（长句抽样条数，默认 10）、`--long-min`（多少字算长句，默认 60）、`--zoom`（对照图倍率，默认 1.3）。
输出 `work/verify/sample_plan.json` + `REVIEW.md` + `review/cmp_p*.png`。

先**按项目自身分位数**把页分层，再按优先级取前若干层抽 10%，渲染「源 | 成品」对照图，
外加长句抽样，最后产出一份可勾选的待办 `REVIEW.md`。

```python
def stratify(feat):
    """阈值一律取**本项目自身的分位数**，不写死绝对值——不同项目页数、图片密度、
    正文字号差得很远，写死阈值换个项目就把所有页归成一类（实测过）。"""
    q_ch_low, q_ch_hi = quant(chars, 0.25), quant(chars, 0.60)
    q_l2_lo, q_l2_hi = quant([x for x in l2 if x > 0] or [0], 0.25), quant(l2, 0.75)
    q_title = quant(tsz, 0.90)
```

分层顺序即优先级：`cover` → `chapter` → `dense` → `chart` → `text` → `other`。
对照图把源页叠在成品页上方，各带一条彩色标签条（crimson `#FCEBEB` / 绿 `#EAF3DE`）。

### `66_backtranslate.py` — T3 回译核验（两段式）

```bash
python3 66_backtranslate.py --emit       # 导出待回译工作单
#   ← 把 work/verify/backtrans_queue.txt 交给模型回译，结果写 backtrans_done.txt
python3 66_backtranslate.py --check      # 比对并出结论
```

参数：`--min-len`（译文多短就不值得回译，默认 24）、`--max`（工作单最多条数，默认 120）。
它读 `work/verify/pairs.json` 与 `sample_plan.json`（即只处理 T2 抽中的页），
产出 `backtrans_queue.txt` → `backtrans_done.txt` → `backtrans.json`。

两段式的原因：回译要模型做，但判定必须脚本做 —— 否则判定标准会随每次对话漂移。

**它只用来排序，不是判定工具。** 证据：措辞相近度（token 重叠 Jaccard）在**好翻译**上
也只有 28%–46%，因为中文常做近义词替换（`great leap forward` ↔ `huge jump`、
`showing up` ↔ `reflected`）。拿它当阈值会把 14 条好翻译里的 9 条标成漂移。

所以**硬判据只有两条**：**数字不符**、**长度比失衡**（`0.45–2.2` 之外）。
否定极性从硬降为**提示**（`already`、`unmatched` 并非否定；`non-normal` 与 `not-normal` 同义）。

```python
verdict = "flag" if (hard or jac < 0.15) else ("watch" if (jac < 0.35 or hint) else "ok")
```

否定词正则有**两处刻意的修正**：报告里用的是**弯引号**（`aren’t` 不是 `aren't`，只认直引号会全漏）；
`non-` 与 `not-` 是同义的两种构词。

数字归一化**刻意重抄**了 `64_audit_text.py` 的那份，而不是 import ——
skill 脚本会被重命名，兄弟之间 import 会断。

### `69_qc_summary.py` — 验收总表

```bash
python3 69_qc_summary.py --pdf output/xxx_zh-CN.pdf
```

唯一参数 `--pdf`（用来回填成品的页数与体积等元信息）。

只读聚合：把 `63/64/65/66` 的 JSON + `rebuild_report.json` + 覆盖计划 + `residue.json`
汇总成一份 `QC_REPORT.md`，含四层覆盖情况表。**必须排在最后**，它读上面所有产物。

表格单元格要过一遍 `cell()` 净化 —— 源/译文本里带换行是常态，
直接塞进 Markdown 表格会把整张表打断（实测过）：

```python
def cell(v):
    """表格单元格净化：折叠换行与管道符。源/译文本里带换行是常态，
    直接塞进 Markdown 表格会把整张表打断（实测过）。"""
    return re.sub(r"\s+", " ", str(v)).replace("|", "\\|").strip() or "—"
```

### `70_verify_visual.py` — 上下对照

整页对照 + 局部放大，抽查标题页 / 图表页 / 密集图例页。

### `72_lang_probe.py` — 多语言探针

同一句话用多种语言渲染，检查字形、连字、RTL 方向。

---

## 数据流总览

```
input.pdf
   ├─ 00  67_source_guard.py --record ─→ work/verify/source_integrity.json（红线基线）
   │                                    …收工时 --check 复核，内容变即退出码 1
   ├─ L0  work/ocr_raw4.json ─→ ocr_scope4.json ─→ ocr_vertical.json
   │                                          └─→ bg_probe.json / strata 抽样图
   ├─ L1  units.json ─→ translations.json ─→ (22_fix_align 精判) ─→ baseline_calib.json
   │                                                              └─→ stage/L1.pdf
   ├─ L3  stage/L3.pdf            （41_ 抹掉双份位图 / 42_+43_ 换掉纯位图标题）
   ├─ L2  batch_*.txt + trans_*.txt ─→ overlay_plan_l2.json ─┐
   │      micro_queue.txt + micro_trans.txt ─→ overlay_plan_micro.json ─┐
   │                                                    stage/L3.pdf ─→ 40_apply_overlay ─┐
   └─ L4  output/成品.pdf ←───────────────────────────────────────────────────────────┘
          ├─ 残迹核验  residue.json ─→ unseen_verified.json（真伪判定）─→ 61_ 对照图
          └─ 成果检验  work/verify/
                 ├─ T0  63_audit_structure.py ─→ structure.json
                 ├─ T1  64_audit_text.py ─→ pairs.json / terms.json / numbers.json
                 │                          charset.json / glossary_proposed.json
                 │                          （+ work/glossary.json 人工冻结后回读核验）
                 ├─ T2  65_sample_review.py ─→ sample_plan.json / REVIEW.md / review/*.png
                 ├─ T3  66_backtranslate.py ─→ backtrans_queue.txt ─→ backtrans_done.txt
                 │                                               └─→ backtrans.json
                 └─ 汇  69_qc_summary.py ─→ QC_REPORT.md
   └─ 末  67_source_guard.py --check（红线复核；与 63 的门禁并列，任一 FAIL 都不得交付）
```

**术语表的流向**（先挖冲突再冻结）：`64_audit_text.py` 的 ①/② 报出冲突 →
人裁决一次 → 写成 `work/glossary.json` 冻结 → ⑥ 逐条回读核验 →
后续版本只跟这一份对，不再重挖。

