# astrbot_plugin_screenshot

基于 **Playwright Chromium** 的 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 本地网页截图插件。

与依赖第三方截图 API 的同类插件不同，本插件在机器人容器内直接启动 Chromium 渲染网页，
**不依赖任何第三方截图服务**，不会出现第三方 API 挂掉后返回占位图、导致图片发送失败的问题。

- 支持视口截图（一屏）与整页长图（滚动截取整个网页）
- 高清倍率渲染（详见下文「倍率是什么」），默认输出即为无损 PNG
- 超大小限制时按「无损优先链」自动兜底，常规情况全链路像素级无损

## ✨ 特性

- 🖥️ 本地 Playwright Chromium 渲染，零第三方依赖
- 🛡️ SSRF 防护：默认拦截解析到内网/回环/保留地址的 URL，并二次校验 302 跳转后的最终地址
- 📐 自定义格式（png/jpeg/webp）、视窗尺寸、整页截图
- 📜 **整页长图命令 `/截全图`**：滚动整页触发懒加载，整页输出长图
- 🛟 整页保护：64MP 像素预算，超预算自动降倍率（CDP 免重导航）或裁剪
- 🔒 无损优先兜底链：PNG 超 15MB → 无损 WebP → JPEG q95（4:4:4）→ LANCZOS 缩小
- 🔁 共享单个浏览器实例 + 并发信号量，资源占用可控；浏览器崩溃自动重启
- ⏱️ 完善的超时与错误提示（含跨境裸域慢建连的 www 重试）
- 📊 截图后追加元数据消息（URL、格式、倍率、分辨率、大小、耗时、自动处理说明）

## 📦 安装

### 前置条件

容器内需要安装 Playwright 及 Chromium 浏览器。以官方镜像为基础构建：

```dockerfile
FROM soulter/astrbot:latest

RUN python -m pip install --no-cache-dir "playwright>=1.40.0" "Pillow>=11.0"

# 安装 Chromium headless shell 及系统依赖
RUN python -m playwright install --with-deps chromium-headless-shell \
    || python -m playwright install chromium-headless-shell
```

中文字体建议一并安装（否则中文网页会显示方块）：

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-wqy-zenhei fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*
```

### 安装插件

将本仓库放入 AstrBot 插件目录（`data/plugins/astrbot_plugin_screenshot/`），
然后在面板重载或重启容器。新插件目录首次加载需要完整重载/重启，热重载只对已加载插件生效。

## 🚀 命令与参数完整说明

插件提供两个命令，参数均为 `key=value` 形式、顺序任意、可省略（省略时用默认值）。

### `/截图` — 视口截图（截一屏）

```
/截图 <url> [format=png] [width=1920] [height=1080] [full=0] [scale=2]
```

别名：`网页截图`、`截图网页`、`webshot`

按指定视窗尺寸加载网页，截取**当前视口一屏**（不滚动），适合截首屏/指定画面。

### `/截全图` — 整页长图（截整个网页）

```
/截全图 <url> [format=png] [width=1920] [scale=2]
```

别名：`整页截图`、`网页长图`、`长截图`、`fullshot`

先自动**滚动整个页面**触发懒加载/无限滚动内容，再输出**从头到尾的完整长图**。
（`height` 参数在此模式下只是初始视口高度，不影响输出；等价于 `/截图 <url> full=1`。）

### 参数逐项说明（两命令通用）

| 参数 | 必填 | 取值 | 默认 | 说明 |
|---|---|---|---|---|
| `url` | ✅ | 网址 | — | 目标网址。可省略 `https://` 前缀（如 `baidu.com` 自动补全为 `https://baidu.com`）；裸域建连慢时会自动补 `www.` 重试 |
| `format` | ❌ | `png` / `jpeg` / `webp` | `png` | 输出图片格式。`png` 无损；`jpeg`/`webp` 以质量 95 输出（有损但体积小）。PNG 超 15MB 时自动转无损 WebP（见下文） |
| `width` | ❌ | 整数 100–8192 | `1920` | 视窗宽度（CSS 逻辑像素，不含倍率）。倍率会让实际输出像素 = width × scale |
| `height` | ❌ | 整数 100–8192 | `1080` | 视窗高度。**仅视口模式生效**；整页模式输出高度由页面实际高度决定 |
| `full` | ❌ | `1`/`true`/`yes`/`on` | `0` | 是否整页截图。`/截图 full=1` 等价于 `/截全图`；`/截全图` 恒为整页 |
| `scale` | ❌ | 整数 1–3 | `2` | 高清渲染倍率，详见下文专章 |

### 示例

```
/截图 baidu.com
/截图 https://github.com format=jpeg width=1280 height=800
/截图 news.site scale=3
/截全图 example.com
/截全图 news.ycombinator.com format=jpeg width=1280
/截全图 long-article.site scale=1
```

## 🔍 「倍率」是什么？（scale 参数详解）

截图完成后元信息里的「倍率：2x」就是 `scale` 参数的**实际生效值**。

### 原理：重新渲染，不是放大图片

倍率对应 Playwright 的 `device_scale_factor`（设备像素比），即让 Chromium **以 N 倍的设备像素密度重新渲染页面**——和手机 Retina 屏的原理一致：

```
width=1920 height=1080 scale=2  →  实际输出 3840×2160 像素
width=1920 height=1080 scale=3  →  实际输出 5760×3240 像素
```

关键点：**不是**先截一张 1920×1080 再拉伸放大（那会模糊），而是浏览器直接按 3840×2160 的分辨率排版、栅格化每个文字与矢量图形。因此文字、图标、图表边缘是真清晰，放大看不会有锯齿或发虚。

### 倍率是无损的吗？

**渲染与格式层面：是的。**

| 环节 | 是否无损 | 说明 |
|---|---|---|
| 倍率渲染（scale） | ✅ 无损 | 高密度重新栅格化，无插值放大；更高倍率=更多原始像素信息 |
| PNG 输出（默认） | ✅ 无损 | PNG 是无损编码，Chromium 截的每个像素原样保存 |
| 整页超限 → 无损 WebP | ✅ 无损 | `lossless=True`，像素级无损，仅体积变小（网页类内容约为 PNG 的 40–70%），透明通道也保留。注意：WebP 单边上限 16383px，超长整页图会自动跳过此步 |
| `format=jpeg` / `format=webp` | ⚠️ 高质量有损 | 固定 quality=95，视觉上几乎不可分辨，体积显著更小 |
| 整页 WebP 仍超限 → JPEG | ⚠️ 近无损 | q95 + 4:4:4（关闭色度抽样），仅在极端超长页时触发 |
| 最后兜底缩尺寸 | ⚠️ 有损 | 仅前述全部手段仍超 15MB 时才触发，LANCZOS 高质量重采样 |

也就是说：**默认参数（png + scale=2）下，从渲染到落盘全链路像素级无损**；只有你主动选 jpeg/webp，或页面极端超长触发兜底时，才会引入（高质量的）有损压缩。

### 倍率什么时候会「自动降级」

整页模式有 **64MP 像素预算**（实测 Chromium full_page 截图超 128MP 会直接超时）。
当 `宽 × 页高 × scale²` 超预算时，自动把倍率降为 1x（通过 CDP 原生接口完成，**无需重新加载页面**），元信息中会标注：

```
• ℹ️ 页面高 40000px，2x 像素总量超出64MP 预算，已自动降为 1x
```

降级后仍超预算则截取前段并在元信息标注截取高度。视口模式不受预算影响，始终按指定倍率输出。

### 怎么选倍率

| 场景 | 建议 |
|---|---|
| 日常截图、发群看 | `scale=2`（默认）：清晰度和体积的最佳平衡 |
| 要放大细看小字、截代码/图表 | `scale=3`：最清晰，文件约为 2x 的 2.25 倍 |
| 超长整页、追求速度/体积 | `scale=1`：输出即视窗原始像素，最快最小 |
| 图片太大发送失败 | 先试 `scale=1`；整页可再减小 `width` |

## 🛟 自动保护策略（无需手动干预）

| 情况 | 自动处理 |
|---|---|
| 整页像素总量 > 64MP | 自动降 1x（CDP 免二次导航，实测 25000px 长页约 2s） |
| 降 1x 后页面仍超预算 | 截取前 N 像素，元信息标注 |
| 页面高度异常（>500,000px） | 高度封顶，防御坏页面 |
| PNG > 15MB | 转无损 WebP（像素级无损）；仍超才转 JPEG q95 |
| 转 JPEG 后仍 > 15MB | LANCZOS 逐步缩小至限额内 |
| 裸域（如 `baidu.com`）跨境建连慢 | 15s 短超时探测，失败立即补 `www.` 重试 |
| 页面存在懒加载/无限滚动 | 整页模式先滚动全页再测量高度，确保增量内容入图 |

## 📊 元数据消息说明

每次截图后会追加一条信息消息：

```
📊 截图信息
• 原始 URL：https://baidu.com
• 落地 URL：https://www.baidu.com/     ← 经 302/301 跳转后的最终地址
• 格式：PNG　倍率：2x                   ← 倍率为实际生效值（可能因预算自动降为 1x）
• 分辨率：3840×2160                     ← 实际输出像素（已含倍率）
• 页面高度：12000px                     ← 仅整页模式显示
• 大小：1234.5 KB
• 渲染耗时：3.2s
• ℹ️ ……                                ← 自动降级/裁剪/转格式的说明（如有）
```

## ⚠️ 错误提示一览

| 提示 | 含义 / 建议 |
|---|---|
| `⚠️ 参数错误` + 用法 | 参数名拼错、缺 URL、数值超范围等，按提示修正 |
| `🚫 已拦截：目标地址 x.x.x.x 属于内网/保留地址` | SSRF 防护生效。确需截内网页面时在配置中关闭 `block_private_network` |
| `⏱️ 截图超时（120s/180s）` | 跨境站点建连过慢或页面无响应；可稍后重试或换 `www.` 前缀 |
| `🌐 页面无法访问` | 站点返回网络错误（DNS 失败/拒绝连接等），检查 URL 是否可公开访问 |
| `❌ 截图失败：...` | 其他异常，附前 200 字符错误信息 |
| `❌ 截图过大（xxMB）` | 所有自动兜底后仍超 15MB，按提示调 `scale=1` / 减小 `width` / `format=jpeg` |

## ⚙️ 配置（面板中修改）

| 配置项 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `timeout` | int | 30 | 页面加载超时（秒）。整体命令超时约为「此值 + 90s」 |
| `default_width` | int | 1920 | 默认视窗宽度（100–8192） |
| `default_height` | int | 1080 | 默认视窗高度（100–8192） |
| `block_private_network` | bool | true | SSRF 防护开关：拦截内网/回环/保留地址，并校验跳转后地址 |
| `user_agent` | string | 空 | 自定义 UA，留空用 Chromium 默认 |
| `sync_to_github` | bool | true | 截图后自动同步源码到 GitHub（需 `GITHUB_TOKEN` 环境变量，见下） |

### GitHub 源码同步（可选）

作者自用功能：开启后每次截图会把插件源码 push 回仓库。需要：

1. 宿主机 gh 凭据中读取 PAT，写入 `/opt/astrbot/.github_token.env`（chmod 600）：
   ```
   GITHUB_TOKEN=ghp_xxxx
   ```
2. `compose.yml` 的 astrbot 服务挂载 `env_file: [.github_token.env]` 后重建容器。

普通用户直接关掉 `sync_to_github` 即可，不影响任何截图功能。

## 📄 License

MIT
