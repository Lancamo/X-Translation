# fonts-langs.md — 字体清单、多语言配置与下载源

## 一、下载源

实测可达（沙箱白名单内）：

```
https://raw.githubusercontent.com/google/fonts/main/ofl/<family>/<File>.ttf
```

| 用途 | family 路径 | 文件 |
|---|---|---|
| 中文衬线（正文） | `notoserifsc` | `NotoSerifSC-Regular.ttf` / `-Medium` / `-Bold` / `-Black` |
| 中文黑体 | `notosanssc` | `NotoSansSC-Regular.ttf` / `-Medium` / `-Bold` |
| 日文 | `notoserifjp` | `NotoSerifJP-Regular.ttf` … |
| 韩文 | `notoserifkr` | `NotoSerifKR-Regular.ttf` … |
| 拉丁衬线 | `robot serif` → `robotoserif` | `RobotoSerif-Regular.ttf` / `-Medium` / `-Bold` / `-Italic` |
| 拉丁无衬线 | `roboto` | `Roboto-Regular.ttf` … |
| 展示体（标题） | `playfairdisplay` | `PlayfairDisplay-Regular.ttf` / `-Bold` |

字体统一放 `work/fonts/`，由 `11_embed_fonts.py` 落地。

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
