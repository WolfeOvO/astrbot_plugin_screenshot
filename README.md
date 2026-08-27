# astrbot_plugin_screenshot

基于 **Playwright Chromium** 的 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 本地网页截图插件。

与依赖第三方截图 API 的同类插件不同，本插件在机器人容器内直接启动 Chromium 渲染网页，
**不依赖任何第三方截图服务**，不会出现第三方 API 挂掉后返回占位图、导致图片发送失败的问题。

## ✨ 特性

- 🖥️ 本地 Playwright Chromium 渲染，零第三方依赖
- 🛡️ SSRF 防护：默认拦截解析到内网/回环/保留地址的 URL，并二次校验 302 跳转后的最终地址
- 📐 自定义格式（png/jpeg/webp）、视窗尺寸、整页截图
- 📜 **整页长图命令 `/截全图`**：滚动整页触发懒加载，整页输出长图
- 🛟 整页保护：64MP 像素预算，超预算自动降倍率（CDP 免重导航）或裁剪，PNG 超限自动转 JPEG
- 🗜️ 截图超过大小限制时自动用 PIL 逐步压缩
- 🔁 共享单个浏览器实例 + 并发信号量，资源占用可控；浏览器崩溃自动重启
- ⏱️ 完善的超时与错误提示（含跨境裸域慢建连的 www 重试）

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

## 🚀 用法

### 视口截图（16:9 视窗，一屏）

```
/截图 <url> [format=png] [width=1920] [height=1080] [full=0] [scale=2]
```

| 参数 | 说明 | 默认 |
|---|---|---|
| `url` | 目标网址（必需，可省略 `https://` 前缀） | — |
| `format` | 图片格式：`png` / `jpeg` / `webp` | `png` |
| `width` | 视窗宽度，100-4096 | `1920` |
| `height` | 视窗高度，100-4096 | `1080` |
| `full` | 是否整页截图：`1`/`true` 开启 | `0` |
| `scale` | 高清倍率 1-3 | `2` |

### 整页长图（滚动截取整个网页）

```
/截全图 <url> [format=png] [width=1920] [scale=2]
```

先滚动整个页面触发懒加载/无限滚动内容，再整页输出长图。参数同上（`height` 为初始视口高度，
不影响输出）。

整页保护策略（自动，无需手动干预）：

- 单图 **64MP 像素预算**：超预算自动从 2x 降为 1x（CDP 免二次导航，实测 25000px 长页 ~2s）
- 降倍率后仍超预算：截取前 N 像素并在元信息中标注
- PNG 超过 15MB：自动转高质量 JPEG
- 页面高度硬上限 500,000px，防御异常页面

示例：

```
/截图 baidu.com
/截图 https://github.com format=jpeg width=1280 height=800
/截全图 example.com
/截全图 news.ycombinator.com format=jpeg width=1280
```

命令别名：`网页截图`、`截图网页`、`webshot`；整页：`整页截图`、`网页长图`、`长截图`、`fullshot`

## ⚙️ 配置

| 配置项 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `timeout` | int | 30 | 页面加载超时（秒） |
| `default_width` | int | 1920 | 默认视窗宽度 |
| `default_height` | int | 1080 | 默认视窗高度 |
| `block_private_network` | bool | true | SSRF 防护开关 |
| `user_agent` | string | 空 | 自定义 UA |
| `sync_to_github` | bool | true | 截图后自动同步源码到 GitHub（需 GITHUB_TOKEN） |

## 📄 License

MIT
