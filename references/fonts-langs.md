# fonts-langs.md — 字体清单、多语言配置与下载源

## 一、获取字体（分级降级，断网也能继续）

`09_fetch_fonts.py` 负责把字体弄到 `work/fonts/`，按下面顺序逐级尝试，成功即停：

| 级 | 来源 | 说明 |
|---|---|---|
| L0 | `work/fonts/` 已有静态字体 | 直接跳过 |
| L1 | `~/.cache/x-translation/fonts/` | 一次下载，跨项目复用 |
| L2 | 直连 GitHub → jsDelivr → `$XTRANS_FONT_MIRROR` | 单文件逐个下，连接超时 8s、总超时 600s；失败立刻换下一个源 |
| L3 | macOS 自带字体（宋体 / Times New Roman） | **按组兜底**：中文组缺就补宋体、拉丁组缺就补 Times，只缺哪组换哪组。宋体从 `Songti.ttc` 提取（Black 改用 Bold，因为 Black 只有 8k 字形、遇到生僻字会缺字）；**字形会变**，写 `FONT_SOURCE.json` 并按组告警 |
| L4 | 都没有 | 报错并列出需要手动放置的文件名 |

```bash
python3 scripts/09_fetch_fonts.py             # 自动降级（推荐）
python3 scripts/09_fetch_fonts.py --offline   # 完全不联网，直接走本地/缓存/系统
python3 scripts/09_fetch_fonts.py --status    # 只看当前字体来源
XTRANS_FONT_MIRROR=https://<内网镜像前缀> python3 scripts/09_fetch_fonts.py   # 接内网/自建镜像
```

**为什么这样设计**：字体是回填的前提，但原先只有「直连 GitHub」一条路。外网抖动、
沙箱拦截或 24 MB 的思源宋体下载超时，整条管线就卡在那里。现在任何一个源失败都只是
换下一个，全部失败也会降到系统字体——**流程不中断，但降级一定会在终端和
`work/fonts/FONT_SOURCE.json` 里留痕**，不会让人误以为用的是原字体。

下载源（L2）实测格式：

```
https://raw.githubusercontent.com/google/fonts/main/ofl/<family>/<File>.ttf
https://cdn.jsdelivr.net/gh/google/fonts@main/ofl/<family>/<File>.ttf   # 镜像
```

> 思源宋体来自 `notofonts/noto-cjk` 仓库，24 MB；jsDelivr 对该仓库返回 403，
> 所以这个文件实际只有直连和内网镜像两条路——**L1 缓存对它尤其重要**。

| 用途 | family 路径 | 文件 |
|---|---|---|
| 中文衬线（正文） | `notoserifsc` | `NotoSerifSC-Regular.ttf` / `-Medium` / `-Bold` / `-Black` |
| 中文黑体 | `notosanssc` | `NotoSansSC-Regular.ttf` / `-Medium` / `-Bold` |
| 日文 | `notoserifjp` | `NotoSerifJP-Regular.ttf` … |
| 韩文 | `notoserifkr` | `NotoSerifKR-Regular.ttf` … |
| 拉丁衬线 | `robot serif` → `robotoserif` | `RobotoSerif-Regular.ttf` / `-Medium` / `-Bold` / `-Italic` |
| 拉丁无衬线 | `roboto` | `Roboto-Regular.ttf` … |
| 展示体（标题） | `playfairdisplay` | `PlayfairDisplay-Regular.ttf` / `-Bold` |

字体统一放 `work/fonts/`：`09_fetch_fonts.py` 取文件，`11_embed_fonts.py` 把可变字体实例化成静态 TTF。

## 二、字体选择原则

**选与原文字形气质接近的**，不是选"最好看的"：

| 原文 | 目标字体 |
|---|---|
| 衬线正文（Times / Georgia 类） | Noto Serif SC |
| 无衬线正文（Helvetica / Inter 类） | Noto Sans SC |
| 无衬线标题 | Noto Sans SC Bold / Black |
| 装饰性衬线标题 | Playfair Display + 思源宋体 Bold |

**字重按原字号分档**（在 `24_rebuild_text.py` 里）：

- 字号 ≥ 40pt → Black
- 字号 ≥ 20pt → Bold
- 其余 → Medium（正文）/ Regular

原文里的**拉丁片段**（品牌名、股票代码、单位符号）保留原字体观感，不要用中文字体去排 ——
中文字体的拉丁字形通常很丑，混排会一眼看出是"机器翻的"。

## 三、CSS 注入方式

```python
css = (f'@font-face {{ font-family:"zh"; src:url("{FONTS}/NotoSerifSC-Regular.ttf"); }}'
       "\nbody{margin:0;padding:0;} div{margin:0;padding:0;} p{margin:0;padding:0;}")
page.insert_htmlbox(rect, html, css=css, scale_low=0.3, overlay=True)
```

`margin/padding` 必须清零，否则 `insert_htmlbox` 的排版高度会和预估差一截，
导致字号算小了。

## 四、**子集化是必须的**

`insert_htmlbox` 每页各嵌一份**完整**字体（思源宋体单字重 ~14 MB）。
90 页不子集化 → 成品膨胀到 GB 级。

```python
doc.subset_fonts()
doc.save(out, garbage=4, clean=True, deflate=True, deflate_fonts=True)
```

实测：90 页最终 38.2 MB（源文件 35.7 MB，+7%）。

## 五、多语言实测结论

同一句话用 7 种语言渲染，管线代码**一行都不用改** —— 换语言 = 换字体文件 + 指定书写方向。

| 语言 | 结果 | 说明 |
|---|---|---|
| 中文 / 日文 / 韩文 | ✅ | 各自需要 Noto Serif SC/JP/KR；表意文字无连字问题 |
| 俄文 / 希腊文 | ✅ | Roboto Serif 直接支持西里尔与希腊字母 |
| 阿拉伯文 | ✅ RTL 正常 | 很多工具做不到；`insert_htmlbox` 能正确处理双向文本 |
| 泰文 | ⚠️ | 需显式指定字体，否则依赖系统回退，影响嵌入与可搜索性 |
| 印地语等 | ⚠️ | 同泰文；另需复杂分词换行支持 |

**唯一要额外处理的是泰文/印地语这类需要复杂分词换行的文字。**
字宽差异（汉字 1.0em vs 拉丁 0.5em）对所有非拉丁目标语言都成立，
所以第 11 条坑（窄框压字号）对任何"目标语言比源语言占宽"的组合都适用。

## 六、字体缺失的判断

`insert_htmlbox` 遇到缺字会**静默回退到系统字体**，不报错。
排查方法：渲染后 OCR 或肉眼检查，看是否有字形风格突变或豆腐块（□）。

**本机 LibreOffice 缺方正字库**是常见现象 —— docx 内字体名正确、Word/WPS 打开正常，
但用 LibreOffice 转 PDF 会替换字体。判断问题时先排除这个干扰。
