"""AstrBot 本地网页截图插件

基于 Playwright Chromium 在本地渲染网页并截图发送，不依赖任何第三方截图 API。
支持高清渲染（device_scale_factor=2）、发图后追加元数据消息、源码自动同步 GitHub。
v2.2.0 新增 /截全图 命令：整页滚动长图，自动触发懒加载，超长页面按像素预算
自动降倍率（CDP 免重导航）或裁剪，避免 Chromium 截图超时/内存爆炸。
"""

import asyncio
import base64
import ipaddress
import os
import socket
import subprocess
import tempfile
import time
from typing import Optional
from urllib.parse import urlparse

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr

PLUGIN_NAME = "astrbot_plugin_screenshot"
PLUGIN_VERSION = "v2.2.0"

# 支持的截图格式
SUPPORTED_FORMATS = {"png", "jpeg", "webp"}

# 完整导航超时（毫秒），用于 www 子域 / 非裸域。实测跨境下裸域（如 baidu.com）
# 的 TCP/TLS 建连可到 22s+，甚至 >90s 直接卡死（DNS 解析到大陆 CDN 慢节点），
# 而 www 子域走国际节点通常 <1s。
NAV_TIMEOUT_MS = 90_000

# 裸域（apex，如 baidu.com）首次导航的短超时（毫秒）。跨境下裸域常卡在 TCP/TLS
# 建连，15s 足够判断「该裸域境外不通」，随即切 www 重试，避免把 90s 全耗在
# 裸域上导致外层 120s 总超时（即用户看到的「截图超时(120s)」）。
BARE_NAV_TIMEOUT_MS = 15_000

# 高清渲染倍率（Retina）。2 = 逻辑像素翻倍，文字/图片更清晰，文件更大。
SCALE_FACTOR = 2

# 整页截图单图像素预算。实测（容器内 Chromium）：
# - 128MP（2560x50000 @2x）full_page 截图 30s 超时直接失败
# - 102MP（1280x80000 @1x）可成功但逼近 PIL 解压炸弹警告线（89MP）
# 64MP 为安全上限：2x 下约 2560x25000，1x 下约 1920x33333。
FULLPAGE_PIXEL_BUDGET = 64_000_000

# 整页高度硬上限（CSS px），防御异常页面（如 body 高度 1e6 的坏页面）
FULLPAGE_MAX_HEIGHT_CSS = 500_000

# 页面总高度测量 JS（documentElement 与 body 取最大，兼容怪异页面）
PAGE_HEIGHT_JS = (
    "() => Math.max(document.documentElement.scrollHeight, "
    "document.body ? document.body.scrollHeight : 0)"
)


def _is_blocked_ip(ip_str: str) -> bool:
    """判断 IP 是否属于内网/回环/链路本地等禁止访问的地址段。"""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # 无法解析的一律拒绝
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def resolve_and_check(host: str, port: int = 443) -> Optional[str]:
    """解析域名并检查是否指向内网地址。

    Returns:
        None 表示通过；否则返回拒绝原因。
    """
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        return f"域名解析失败: {e}"
    if not infos:
        return "域名解析结果为空"
    for info in infos:
        addr = info[4][0]
        if _is_blocked_ip(addr):
            return f"目标地址 {addr} 属于内网/保留地址，已拦截"
    return None


@register(PLUGIN_NAME, "WolfeOvO", "本地 Playwright 网页截图（支持整页长图），不依赖第三方 API", PLUGIN_VERSION)
class LocalScreenshotPlugin(Star):
    """本地网页截图插件

    用法：/截图 <url> [format=png] [width=1920] [height=1080] [full=0] [scale=2]
          /截全图 <url> [format=png] [width=1920] [scale=2]   （整页滚动长图）
    """

    MAX_FILE_SIZE = 15 * 1024 * 1024  # 15MB（高清后文件更大，放宽限制）
    MAX_EDGE = 8192  # 单边最大像素（含 scale 后）

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.timeout_s = int(config.get("timeout", 30))
        self.default_width = int(config.get("default_width", 1920))
        self.default_height = int(config.get("default_height", 1080))
        self.block_private = bool(config.get("block_private_network", True))
        self.user_agent = str(config.get("user_agent", "") or "").strip()
        self.sync_github = bool(config.get("sync_to_github", True))

        self._pw = None
        self._browser = None
        self._browser_lock = asyncio.Lock()
        # 限制并发截图数量，避免同时开太多页面
        self._sem = asyncio.Semaphore(2)
        logger.info(f"[{PLUGIN_NAME}] 初始化完成 {PLUGIN_VERSION}，超时={self.timeout_s}s，"
                    f"默认尺寸={self.default_width}x{self.default_height}，"
                    f"高清倍率={SCALE_FACTOR}，"
                    f"整页像素预算={FULLPAGE_PIXEL_BUDGET // 1_000_000}MP，"
                    f"内网拦截={'开' if self.block_private else '关'}，"
                    f"GitHub同步={'开' if self.sync_github else '关'}")

    # ------------------------------------------------------------------
    # 浏览器生命周期
    # ------------------------------------------------------------------
    async def _get_browser(self):
        """获取（必要时启动）共享的 Chromium 实例。"""
        async with self._browser_lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            from playwright.async_api import async_playwright

            if self._pw is None:
                self._pw = await async_playwright().start()
            logger.info(f"[{PLUGIN_NAME}] 正在启动 Chromium headless ...")
            self._browser = await self._pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--hide-scrollbars",
                    "--mute-audio",
                ],
            )
            logger.info(f"[{PLUGIN_NAME}] Chromium 启动成功")
            return self._browser

    async def _shutdown_browser(self):
        async with self._browser_lock:
            try:
                if self._browser is not None:
                    await self._browser.close()
            except Exception:
                pass
            self._browser = None
            try:
                if self._pw is not None:
                    await self._pw.stop()
            except Exception:
                pass
            self._pw = None

    async def terminate(self):
        """插件卸载/重载时清理浏览器。基类 Star.terminate 是 async，AstrBot 会 await。"""
        try:
            await self._shutdown_browser()
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 清理浏览器失败: {e}")

    # ------------------------------------------------------------------
    # 参数解析
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_url(raw: str) -> Optional[str]:
        raw = raw.strip()
        if not raw:
            return None
        if not raw.startswith(("http://", "https://")):
            raw = "https://" + raw
        parsed = urlparse(raw)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return None
        return raw

    def _parse_args(self, tail: str, default_full: bool = False) -> tuple[dict, list[str]]:
        """解析命令尾部参数。

        第一个 token 是 URL，其余为 key=value 形式参数。
        default_full：整页模式（/截全图）下 full 默认为 True。
        """
        errors: list[str] = []
        params = {
            "url": None,
            "format": "png",
            "width": self.default_width,
            "height": self.default_height,
            "full": default_full,
            "scale": SCALE_FACTOR,
        }
        tokens = tail.split()
        if not tokens:
            errors.append("缺少 URL 参数")
            return params, errors

        url = self._normalize_url(tokens[0])
        if url is None:
            errors.append(f"无效的 URL: {tokens[0]}")
        params["url"] = url

        for tok in tokens[1:]:
            if "=" not in tok:
                errors.append(f"无法识别的参数: {tok}（应为 key=value 形式）")
                continue
            key, _, value = tok.partition("=")
            key = key.strip().lower()
            value = value.strip()
            if key == "format":
                value = value.lower()
                if value in SUPPORTED_FORMATS:
                    params["format"] = value
                else:
                    errors.append(f"不支持的格式: {value}（可选 png/jpeg/webp）")
            elif key in ("width", "height"):
                try:
                    v = int(value)
                    if not (100 <= v <= self.MAX_EDGE):
                        errors.append(f"{key} 超出范围 100-{self.MAX_EDGE}: {v}")
                    else:
                        params[key] = v
                except ValueError:
                    errors.append(f"{key} 必须是整数: {value}")
            elif key == "scale":
                try:
                    v = int(value)
                    if not (1 <= v <= 3):
                        errors.append(f"scale 超出范围 1-3: {v}")
                    else:
                        params["scale"] = v
                except ValueError:
                    errors.append(f"scale 必须是整数: {value}")
            elif key == "full":
                params["full"] = value.lower() in ("1", "true", "yes", "on")
            else:
                errors.append(f"未知参数: {key}")
        return params, errors

    # ------------------------------------------------------------------
    # 截图核心
    # ------------------------------------------------------------------
    async def _navigate(self, page, url: str):
        """导航到目标 URL。裸域（如 baidu.com）跨境建连极慢甚至卡死，
        先用短超时探测，失败立即补 www. 重试（www 走国际节点通常快得多）。"""
        from urllib.parse import urlparse as _up
        parsed = _up(url)
        host = parsed.hostname or ""
        bare = bool(host) and not host.startswith("www.")
        # 裸域首跳用短超时，避免把 90s 全耗在慢建连上
        first_timeout = BARE_NAV_TIMEOUT_MS if bare else NAV_TIMEOUT_MS
        try:
            await page.goto(url, timeout=first_timeout, wait_until="domcontentloaded")
            return None
        except Exception as e:
            err = str(e)
            if bare and "Timeout" in err:
                new_url = url.replace(f"//{host}", f"//www.{host}", 1)
                logger.warning(f"[{PLUGIN_NAME}] 裸域 {host} 建连过慢，改用 {new_url}")
                await page.goto(new_url, timeout=NAV_TIMEOUT_MS,
                                wait_until="domcontentloaded")
                return new_url
            raise

    @staticmethod
    async def _scroll_for_lazy(page, max_steps: int = 80, step_ms: int = 120):
        """滚动整个页面以触发懒加载图片/无限滚动内容，完成后回到顶部。

        - 每步滚动一个视口高度，step_ms 毫秒间隔让懒加载触发
        - 页面高度连续 3 步不变且已到底则提前结束
        - max_steps 硬上限防止无限滚动页面把命令卡死（80 步约 10s）
        - behavior:'instant' 绕过 CSS scroll-behavior:smooth 的慢滚动
        """
        try:
            await page.evaluate(
                """async (cfg) => {
                    const H = () => Math.max(
                        document.documentElement.scrollHeight,
                        document.body ? document.body.scrollHeight : 0);
                    const vh = window.innerHeight || 800;
                    let last = -1, stable = 0;
                    for (let i = 0; i < cfg.max_steps; i++) {
                        const h0 = H();
                        const y = Math.min((i + 1) * vh, h0);
                        window.scrollTo({top: y, behavior: 'instant'});
                        await new Promise(r => setTimeout(r, cfg.step_ms));
                        const h = H();
                        if (h === last) {
                            stable++;
                            if (stable >= 3 && y >= h - vh) break;
                        } else {
                            stable = 0;
                        }
                        last = h;
                    }
                    window.scrollTo({top: 0, behavior: 'instant'});
                    await new Promise(r => setTimeout(r, 200));
                }""",
                {"max_steps": max_steps, "step_ms": step_ms},
            )
        except Exception as e:
            # 滚动失败不致命（个别页面禁 JS 滚动），继续用当前状态截图
            logger.debug(f"[{PLUGIN_NAME}] 懒加载滚动跳过: {e}")

    async def _cdp_fullpage_capture(self, page, context, width: int, viewport_h: int,
                                    clip_h: int, fmt: str) -> bytes:
        """超像素预算时的兜底整页截图：CDP 临时把 deviceScaleFactor 降为 1，
        再用原生 Page.captureScreenshot(captureBeyondViewport) 截取。

        优点：不用重新导航（跨境站点二次加载可能又花 90s），实测 1280x25000
        约 2.3s。DSF 不影响 CSS 布局，仅影响输出设备像素。
        """
        client = await context.new_cdp_session(page)
        try:
            await client.send("Emulation.setDeviceMetricsOverride", {
                "width": width,
                "height": viewport_h,
                "deviceScaleFactor": 1,
                "mobile": False,
            })
            kwargs = {
                "format": fmt,
                "captureBeyondViewport": True,
                "clip": {"x": 0, "y": 0, "width": width, "height": clip_h, "scale": 1},
            }
            if fmt in ("jpeg", "webp"):
                kwargs["quality"] = 95
            res = await asyncio.wait_for(
                client.send("Page.captureScreenshot", kwargs), timeout=60
            )
            data = base64.b64decode(res["data"])
            if not data:
                raise RuntimeError("CDP 截图返回空数据")
            return data
        finally:
            try:
                await client.send("Emulation.clearDeviceMetricsOverride")
            except Exception:
                pass
            try:
                await client.detach()
            except Exception:
                pass

    @staticmethod
    def _real_dims(data: bytes) -> Optional[tuple[int, int]]:
        """用 PIL 读取截图的真实像素尺寸（同时验证图片完整性）。"""
        try:
            import io

            from PIL import Image

            with Image.open(io.BytesIO(data)) as img:
                return img.size
        except Exception:
            return None

    @staticmethod
    def _to_jpeg(data: bytes) -> Optional[bytes]:
        """PNG 转高质量 JPEG（q90）。整页长图超大小限制时兜底用。"""
        try:
            import io

            from PIL import Image

            img = Image.open(io.BytesIO(data))
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=90)
            return buf.getvalue()
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] PNG 转 JPEG 失败: {e}")
            return None

    async def _render(self, params: dict) -> tuple[bytes, dict]:
        """渲染页面并返回 (截图二进制数据, 元信息 dict)。

        高清渲染（device_scale_factor）+ domcontentloaded + 追踪域名拦截。
        裸域先用 15s 短超时探测，失败自动补 www 重试（90s），规避跨境建连慢。
        整页模式（full=1）：先滚动全页触发懒加载，再按像素预算选择路径：
        - 常规：Playwright full_page 截图（保留高清倍率）
        - 超预算：CDP 降 1x + 原生 captureScreenshot（免二次导航）
        - 仍超预算：裁剪到预算内并在元信息标注
        """
        host = urlparse(params["url"]).hostname
        if self.block_private:
            reason = await asyncio.get_event_loop().run_in_executor(
                None, resolve_and_check, host
            )
            if reason:
                raise PermissionError(reason)

        browser = await self._get_browser()
        context = await browser.new_context(
            viewport={"width": params["width"], "height": params["height"]},
            locale="zh-CN",
            user_agent=self.user_agent or None,
            device_scale_factor=params["scale"],
        )

        # ---------- 拦截常见追踪请求 ----------
        BLOCK_HOSTS = {
            "hm.baidu.com",
            "tongji.baidu.com",
            "dup.baidustatic.com",
            "als.baidu.com",
            "bdimg.share.baidu.com",
        }

        async def _block_route(route):
            url = route.request.url
            if any(h in url for h in BLOCK_HOSTS):
                await route.abort()
            else:
                await route.continue_()

        await context.route("**/*", _block_route)
        # -----------------------------------------

        context.set_default_timeout(NAV_TIMEOUT_MS)

        page = await context.new_page()
        try:
            t0 = time.time()
            await self._navigate(page, params["url"])
            # 等待页面关键元素出现，以确保渲染完成（不致命）
            try:
                await page.wait_for_selector(
                    "input#kw, input[name=wd], input[type=search]",
                    timeout=15000,
                )
            except Exception:
                pass

            # 二次跳转后再次检查内网拦截
            if self.block_private:
                final_host = urlparse(page.url).hostname
                if final_host and final_host != host:
                    reason = await asyncio.get_event_loop().run_in_executor(
                        None, resolve_and_check, final_host
                    )
                    if reason:
                        raise PermissionError(f"页面跳转后 {reason}")

            notes: list[str] = []
            height_css: Optional[int] = None

            if params["full"]:
                # ---------- 整页模式 ----------
                await self._scroll_for_lazy(page)
                try:
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                height_css = int(await page.evaluate(PAGE_HEIGHT_JS) or 0)
                height_css = min(max(height_css, params["height"]),
                                 FULLPAGE_MAX_HEIGHT_CSS)
                need_px = params["width"] * height_css * params["scale"] ** 2
                if need_px <= FULLPAGE_PIXEL_BUDGET:
                    # 常规路径：保留高清倍率
                    shot_kwargs = {
                        "type": params["format"],
                        "full_page": True,
                        "timeout": 45_000,
                    }
                    if params["format"] in ("jpeg", "webp"):
                        shot_kwargs["quality"] = 95
                    data = await page.screenshot(**shot_kwargs)
                    eff_scale = params["scale"]
                else:
                    # 超像素预算：降 1x（CDP 免重导航），仍超则裁剪
                    eff_scale = 1
                    if params["scale"] > 1:
                        notes.append(
                            f"页面高 {height_css}px，{params['scale']}x 像素总量超出"
                            f"{FULLPAGE_PIXEL_BUDGET // 1_000_000}MP 预算，已自动降为 1x")
                    cap_h = FULLPAGE_PIXEL_BUDGET // params["width"]
                    clip_h = min(height_css, cap_h)
                    if clip_h < height_css:
                        notes.append(f"页面过长（{height_css}px），已截取前 {clip_h}px")
                    data = await self._cdp_fullpage_capture(
                        page, context, params["width"], params["height"],
                        clip_h, params["format"])
            else:
                # ---------- 视口模式 ----------
                shot_kwargs = {
                    "type": params["format"],
                    "full_page": False,
                    "timeout": 30_000,
                }
                if params["format"] in ("jpeg", "webp"):
                    shot_kwargs["quality"] = 95
                data = await page.screenshot(**shot_kwargs)
                eff_scale = params["scale"]

            if not data:
                raise RuntimeError("截图返回空数据")

            elapsed = time.time() - t0
            # 读取真实输出尺寸（同时校验图片完整性）
            dims = self._real_dims(data)
            if dims:
                width_px, height_px = dims
            elif params["full"] and height_css:
                width_px = params["width"] * eff_scale
                height_px = height_css * eff_scale
            else:
                width_px = params["width"] * eff_scale
                height_px = params["height"] * eff_scale

            meta = {
                "url": params["url"],
                "final_url": page.url,
                "format": params["format"],
                "width": width_px,
                "height": height_px,
                "scale": eff_scale,
                "full": params["full"],
                "page_height": height_css if params["full"] else None,
                "bytes": len(data),
                "elapsed": round(elapsed, 1),
                "notes": notes,
            }
            return data, meta
        finally:
            await context.close()

    @staticmethod
    def _shrink_if_needed(data: bytes, fmt: str, max_size: int) -> bytes:
        """截图过大时用 PIL 逐步缩小尺寸（仅在超限时触发，默认不压缩）。"""
        if len(data) <= max_size or fmt not in ("png", "jpeg", "webp"):
            return data
        try:
            import io

            from PIL import Image

            img = Image.open(io.BytesIO(data))
            for _ in range(4):
                w, h = img.size
                img = img.resize((w * 3 // 4, h * 3 // 4))
                buf = io.BytesIO()
                save_fmt = "JPEG" if fmt == "jpeg" else fmt.upper()
                save_kwargs = {"quality": 90} if fmt in ("jpeg", "webp") else {}
                if save_fmt == "JPEG" and img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                img.save(buf, format=save_fmt, **save_kwargs)
                data = buf.getvalue()
                if len(data) <= max_size:
                    break
            return data
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 压缩截图失败: {e}")
            return data

    # ------------------------------------------------------------------
    # GitHub 同步
    # ------------------------------------------------------------------
    def _sync_to_github(self) -> str:
        """把插件源码同步推送到 GitHub 仓库。

        插件目录在容器内即 /AstrBot/data/plugins/astrbot_plugin_screenshot，
        宿主机侧挂载可见。使用环境变量 GITHUB_TOKEN（个人 PAT）以 WolfeOvO 身份认证推送。
        Returns: 状态字符串。
        """
        import os as _os
        plugin_dir = _os.path.dirname(_os.path.abspath(__file__))
        token = _os.environ.get("GITHUB_TOKEN", "")
        if not token:
            return "⚠️ 未配置 GITHUB_TOKEN，跳过同步"
        repo = "WolfeOvO/astrbot_plugin_screenshot"
        remote = f"https://WolfeOvO:{token}@github.com/{repo}.git"
        try:
            # 在插件目录初始化/复用 git 仓库
            if not _os.path.exists(_os.path.join(plugin_dir, ".git")):
                subprocess.run(["git", "init", "-q"], cwd=plugin_dir, check=True)
                subprocess.run(["git", "remote", "add", "origin", remote],
                               cwd=plugin_dir, check=True)
            else:
                # 更新 remote URL（含 token，避免凭据缺失）
                subprocess.run(["git", "remote", "set-url", "origin", remote],
                               cwd=plugin_dir, check=True)
            # 配置身份（仅本仓库）
            subprocess.run(["git", "config", "user.email", "265155059+WolfeOvO@users.noreply.github.com"],
                           cwd=plugin_dir, check=True)
            subprocess.run(["git", "config", "user.name", "WolfeOvO"],
                           cwd=plugin_dir, check=True)
            # 拉取远端（避免 non-fast-forward）
            subprocess.run(["git", "fetch", "-q", "origin", "main"],
                           cwd=plugin_dir, capture_output=True)
            subprocess.run(["git", "reset", "-q", "--mixed", "origin/main"],
                           cwd=plugin_dir, capture_output=True)
            # 只添加插件自身文件，不递归父目录
            for f in ("main.py", "metadata.yaml", "_conf_schema.json",
                      "requirements.txt", "README.md"):
                fp = _os.path.join(plugin_dir, f)
                if _os.path.exists(fp):
                    subprocess.run(["git", "add", "-f", f], cwd=plugin_dir, check=True)
            # 若有变更则提交并推送
            r = subprocess.run(["git", "diff", "--cached", "--quiet"],
                               cwd=plugin_dir)
            if r.returncode == 0:
                return "✅ 源码已是最新，无需推送"
            msg = f"chore: sync {PLUGIN_VERSION} @ {time.strftime('%Y-%m-%d %H:%M')}"
            subprocess.run(["git", "commit", "-q", "-m", msg],
                           cwd=plugin_dir, check=True)
            pr = subprocess.run(["git", "push", "-q", "origin", "HEAD:main"],
                                cwd=plugin_dir, capture_output=True, text=True)
            if pr.returncode != 0:
                return f"⚠️ 推送失败: {pr.stderr[:120]}"
            return f"✅ 已同步 {PLUGIN_VERSION} 到 GitHub"
        except Exception as e:
            return f"⚠️ 同步异常: {str(e)[:120]}"

    # ------------------------------------------------------------------
    # 命令入口（共用管线）
    # ------------------------------------------------------------------
    async def _run(self, event: AstrMessageEvent, tail: str, full_mode: bool):
        """/截图 与 /截全图 的共用执行管线。"""
        params, errors = self._parse_args(tail, default_full=full_mode)
        if errors:
            usage = ("/截全图 <url> [format=png] [width=1920] [scale=2]"
                     if full_mode else
                     "/截图 <url> [format=png] [width=1920] [height=1080] [full=0] [scale=2]")
            msg = "⚠️ 参数错误：\n" + "\n".join(f"• {e}" for e in errors)
            msg += "\n\n📖 用法：" + usage
            yield event.plain_result(msg)
            return

        url = params["url"]
        host = urlparse(url).hostname

        if params["full"]:
            yield event.plain_result(
                f"📸 正在本地渲染整页长图：{host} ...\n"
                f"⏳ 会先滚动加载全部内容，超长页面自动降倍率/裁剪，请稍候")
        else:
            yield event.plain_result(f"📸 正在本地高清渲染截图：{host} ...")

        # 外层总超时：整页含滚动懒加载（~10s）+ 导航 + 截图，给 180s
        outer_timeout = 180 if params["full"] else 120
        temp_file = None
        deleted = False
        try:
            async with self._sem:
                data, meta = await asyncio.wait_for(
                    self._render(params), timeout=outer_timeout
                )
            # 整页 PNG 超限兜底：转高质量 JPEG（网页长图几乎不需要透明通道）
            if (params["full"] and params["format"] == "png"
                    and len(data) > self.MAX_FILE_SIZE):
                jpeg = self._to_jpeg(data)
                if jpeg and len(jpeg) < len(data):
                    data = jpeg
                    params["format"] = "jpeg"
                    meta["format"] = "jpeg"
                    meta.setdefault("notes", []).append(
                        "PNG 超过大小限制，已自动转为 JPEG")
            data = self._shrink_if_needed(data, params["format"], self.MAX_FILE_SIZE)
            if len(data) > self.MAX_FILE_SIZE:
                hint = "，可尝试 format=jpeg / scale=1"
                if params["full"]:
                    hint += " 或减小 width"
                else:
                    hint += " 或 full=0"
                yield event.plain_result(
                    f"❌ 截图过大（{len(data) / 1024 / 1024:.1f}MB），"
                    f"超过 {self.MAX_FILE_SIZE // 1024 // 1024}MB 限制{hint}"
                )
                return

            meta["bytes"] = len(data)
            fd, temp_file = tempfile.mkstemp(suffix=f".{params['format']}")
            with os.fdopen(fd, "wb") as f:
                f.write(data)

            logger.info(f"[{PLUGIN_NAME}] 截图完成: {url} -> {len(data)} bytes "
                        f"full={params['full']}")
            # 发送图片
            yield event.image_result(temp_file)
            # 发完即删本地临时文件
            try:
                os.unlink(temp_file)
                deleted = True
                logger.info(f"[{PLUGIN_NAME}] 已发送并删除临时文件: {temp_file}")
            except OSError as e:
                logger.warning(f"[{PLUGIN_NAME}] 删除临时文件失败: {e}")

            # 发送元数据消息
            title = "📊 整页截图信息" if params["full"] else "📊 截图信息"
            meta_lines = [
                title,
                f"• 原始 URL：{meta['url']}",
                f"• 落地 URL：{meta['final_url']}",
                f"• 格式：{meta['format'].upper()}　倍率：{meta['scale']}x",
                f"• 分辨率：{meta['width']}×{meta['height']}",
            ]
            if params["full"] and meta.get("page_height"):
                meta_lines.append(f"• 页面高度：{meta['page_height']}px")
            meta_lines.append(f"• 大小：{meta['bytes']/1024:.1f} KB")
            meta_lines.append(f"• 渲染耗时：{meta['elapsed']}s")
            for note in meta.get("notes", []):
                meta_lines.append(f"• ℹ️ {note}")

            # GitHub 同步（后台静默执行，结果不再展示在元信息里）
            if self.sync_github:
                try:
                    await asyncio.get_event_loop().run_in_executor(
                        None, self._sync_to_github
                    )
                except Exception:
                    pass
            yield event.plain_result("\n".join(meta_lines))
        except PermissionError as e:
            logger.warning(f"[{PLUGIN_NAME}] SSRF 拦截: {url} - {e}")
            yield event.plain_result(f"🚫 已拦截：{e}")
        except asyncio.TimeoutError:
            yield event.plain_result(
                f"⏱️ 截图超时（{outer_timeout}s）：{host}，跨境站点建连过慢或页面无响应"
            )
        except Exception as e:
            err = str(e)
            logger.error(f"[{PLUGIN_NAME}] 截图失败: {url} - {err}")
            # 浏览器崩溃时重置实例，下次自动重启
            if "browser" in err.lower() or "closed" in err.lower():
                await self._shutdown_browser()
            if "net::ERR" in err or "NS_ERROR" in err:
                yield event.plain_result(f"🌐 页面无法访问：{host}\n{err[:200]}")
            else:
                yield event.plain_result(f"❌ 截图失败：{err[:200]}")
        finally:
            if temp_file and not deleted and os.path.exists(temp_file):
                try:
                    os.unlink(temp_file)
                except OSError:
                    pass

    @filter.command("截图", alias={"网页截图", "截图网页", "webshot"}, desc="本地网页截图（视口 16:9）")
    async def handle_screenshot(self, event: AstrMessageEvent, tail: GreedyStr):
        async for result in self._run(event, tail or "", full_mode=False):
            yield result

    @filter.command("截全图", alias={"整页截图", "网页长图", "长截图", "fullshot"},
                    desc="整页滚动长图截图（自动触发懒加载）")
    async def handle_fullshot(self, event: AstrMessageEvent, tail: GreedyStr):
        async for result in self._run(event, tail or "", full_mode=True):
            yield result
