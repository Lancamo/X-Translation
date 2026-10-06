# X-Translation

**保版面的 PDF 翻译技能（Claude Code / WorkBuddy Skill）**——把 PDF 逐段译成目标语言（英→中为主，可扩展任意语言互转），而版面、图表、字体风格、文字位置**全部不动**。

适用于：用户说"翻译这个 PDF""保留原版面""图里的字也要翻""不要重排/不要改变图表"；以及扫描件、设计稿型（画册/研报/白皮书）PDF。

## 它解决什么问题

市面上的 PDF 翻译工具（pdf2zh、BabelDOC、Google Docs 等）普遍做法是**丢弃原内容流重排**——译文放在段落框里重新排版。X-Translation 的差异化路线是**严格原位**：

- **文字层原位回填**：逐单元删除原文、同位置写入译文，还原标题居中/靠左/靠右、字重与颜色
- **图内文字原位覆盖**：OCR 定位图表内文字，用背景补丁 + 同位置 HTML 覆盖，不重画图表
- **封面位图标题原位换字**：封面美术字做成位图替换，保留金属字等特效
- **微标签补译**：轴标签、图例等小字号元素单独队列处理

外加一套**T0–T3 成果检验框架**——质量不靠"翻得好"，靠检验层兜底：

| 层 | 内容 |
|---|---|
| **T0 结构断言**（硬闸） | 页数/尺寸/旋转/书签/字体嵌入/文件完好，FAIL>0 不许交付 |
| **T1 文本完整性** | 术语一致性、数字守恒、专名保留、字符集取证（抓"能看不能复制"的损坏） |
| **T2 风险倾斜抽检** | 高风险页强制进池 + 均匀抽样，渲染对照图 + 人判清单 |
| **T3 回译核验** | 语义漂移排序，只用「数字不符/长度比失衡」两条硬判据 |
| **三维视觉回归** | 块级掩膜 SSIM + 掩膜外 AE + 图层矩形 IoU，并表不并分 |
| **反向残迹扫描** | 成品里是否还有漏翻外文、回源核验 |

还有一条**绝对红线**：全程不改源 PDF 一个字节——SHA-256 基线 + macOS 物理加锁（`uchg`）机制化执行，交付前核验源文件完整性。

## 环境要求

- **macOS**（源文件加锁用 `chflags`；图内文字 OCR 用自带的 `ocr_vision` 工具，编译自 `scripts/ocr_vision.swift`，调用 Apple Vision 框架）
- **Python 3.11+**，`pip install pymupdf`（建议 ≥1.28；PyMuPDF 为 AGPL-3.0，自用无影响，对外提供服务需注意授权）
- **中英文字体文件**（放在项目 `work/fonts/`，如 Noto Serif SC + Roboto Serif，见 `references/fonts-langs.md`）

## 安装

```bash
git clone https://github.com/Lancamo/X-Translation.git
# Claude Code：把整个目录放到 ~/.claude/skills/X-Translation
# WorkBuddy：放到 ~/.workbuddy/skills/（或建立软链）
```

## 使用方式

技能加载后，对话里说"翻译这个 PDF，保留原版面"即可触发。典型流程：

```
侦察 → 字体嵌入 → 文字层提取分诊 → 逐条翻译（工作单模式，带术语表）
→ L1 文字层回填 / L2 图内覆盖 / L3 封面换字 / L2b 微标签
→ T0–T3 检验 + 视觉回归 + 残迹扫描 → QC_REPORT.md 验收总表
```

所有中间产物在项目 `work/`，成品在 `output/`，逐版本保留。

## 文档

| 文件 | 内容 |
|---|---|
| `SKILL.md` | 技能主文档：红线、分层流程、实测坑表 |
| `references/workflow.md` | 端到端流程与每步脚本参数 |
| `references/pitfalls.md` | 48 条实测坑（ToUnicode 字形码越界、bfrange 解析串线、连字抑制等） |
| `references/pdf-types.md` | PDF 类型分诊（文字层/扫描件/混合） |
| `references/fonts-langs.md` | 字体选择与语言扩展 |

## 已知限制

- 源文件红线守卫的物理加锁（`uchg`）仅 macOS 可用
- 图内文字 OCR 依赖 Apple Vision，仅限 macOS；OCR 误读需人工标注"保留原文"
- 避头尾：MuPDF 断行器在图内标签处偶发行首标点（记录为已知小项）
- "重建 ToUnicode"根治方案尚在验证中，当前以拉丁字体路由止血

## License

MIT
