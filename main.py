"""AstrBot 本地网页截图插件

基于 Playwright Chromium 在本地渲染网页并截图发送，不依赖任何第三方截图 API。
"""

import asyncio
import ipaddress
import os
import socket
import tempfile
from typing import Any, AsyncGenerator, Optional
from urllib.parse import urlparse

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr

PLUGIN_NAME = "astrbot_plugin_screenshot"
PLUGIN_VERSION = "v1.0.0"

# 支持的截图格式
SUPPORTED_FORMATS = {"png", "jpeg", "webp"}


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


@register(PLUGIN_NAME, "WolfeOvO", "本地 Playwright 网页截图，不依赖第三方 API", PLUGIN_VERSION)
class LocalScreenshotPlugin(Star):
    """本地网页截图插件

    用法：/截图 <url> [format=png] [width=1920] [height=1080] [full=0]
    """

    MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
    MAX_EDGE = 4096  # 单边最大像素

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.timeout_s = int(config.get("timeout", 30))
        self.default_width = int(config.get("default_width", 1920))
        self.default_height = int(config.get("default_height", 1080))
        self.block_private = bool(config.get("block_private_network", True))
        self.user_agent = str(config.get("user_agent", "") or "").strip()

        self._pw = None
        self._browser = None
        self._browser_lock = asyncio.Lock()
        # 限制并发截图数量，避免同时开太多页面
        self._sem = asyncio.Semaphore(2)
        logger.info(f"[{PLUGIN_NAME}] 初始化完成，超时={self.timeout_s}s，"
                    f"默认尺寸={self.default_width}x{self.default_height}，"
                    f"内网拦截={'开' if self.block_private else '关'}")

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

    def terminate(self):
        """插件卸载/重载时清理浏览器。"""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(self._shutdown_browser())
            else:
                loop.run_until_complete(self._shutdown_browser())
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

    def _parse_args(self, tail: str) -> tuple[dict, list[str]]:
        """解析命令尾部参数。

        第一个 token 是 URL，其余为 key=value 形式参数。
        """
        errors: list[str] = []
        params = {
            "url": None,
            "format": "png",
            "width": self.default_width,
            "height": self.default_height,
            "full": False,
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
            elif key == "full":
                params["full"] = value.lower() in ("1", "true", "yes", "on")
            else:
                errors.append(f"未知参数: {key}")
        return params, errors

    # ------------------------------------------------------------------
    # 截图核心
    # ------------------------------------------------------------------
    async def _render(self, params: dict) -> bytes:
        """渲染网页并返回截图二进制数据。"""
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
        )
        page = await context.new_page()
        try:
            await page.goto(
                params["url"],
                timeout=self.timeout_s * 1000,
                wait_until="load",
            )
            # 给动态内容一点加载时间，超时不致命
            try:
                await page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                pass

            # 跳转后二次校验最终地址，防止 302 到内网
            if self.block_private:
                final_host = urlparse(page.url).hostname
                if final_host and final_host != host:
                    reason = await asyncio.get_event_loop().run_in_executor(
                        None, resolve_and_check, final_host
                    )
                    if reason:
                        raise PermissionError(f"页面跳转后 {reason}")

            shot_kwargs = {
                "type": params["format"],
                "full_page": params["full"],
                "timeout": 20000,
            }
            if params["format"] in ("jpeg", "webp"):
                shot_kwargs["quality"] = 85
            data = await page.screenshot(**shot_kwargs)
            if not data:
                raise RuntimeError("截图返回空数据")
            return data
        finally:
            await context.close()

    @staticmethod
    def _shrink_if_needed(data: bytes, fmt: str, max_size: int) -> bytes:
        """截图过大时用 PIL 逐步缩小尺寸。"""
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
                save_kwargs = {"quality": 80} if fmt in ("jpeg", "webp") else {}
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
    # 命令入口
    # ------------------------------------------------------------------
    @filter.command("截图", alias={"网页截图", "截图网页", "webshot"}, desc="本地网页截图")
    async def handle_screenshot(self, event: AstrMessageEvent, tail: GreedyStr):
        params, errors = self._parse_args(tail or "")
        if errors:
            msg = "⚠️ 参数错误：\n" + "\n".join(f"• {e}" for e in errors)
            msg += "\n\n📖 用法：/截图 <url> [format=png] [width=1920] [height=1080] [full=0]"
            yield event.plain_result(msg)
            return

        url = params["url"]
        host = urlparse(url).hostname

        yield event.plain_result(f"📸 正在本地渲染截图：{host} ...")

        temp_file = None
        try:
            async with self._sem:
                data = await asyncio.wait_for(
                    self._render(params), timeout=self.timeout_s + 30
                )
            data = self._shrink_if_needed(data, params["format"], self.MAX_FILE_SIZE)
            if len(data) > self.MAX_FILE_SIZE:
                yield event.plain_result(
                    f"❌ 截图过大（{len(data) / 1024 / 1024:.1f}MB），"
                    f"超过 {self.MAX_FILE_SIZE // 1024 // 1024}MB 限制，"
                    f"可尝试 full=0 只截视窗区域"
                )
                return

            fd, temp_file = tempfile.mkstemp(suffix=f".{params['format']}")
            with os.fdopen(fd, "wb") as f:
                f.write(data)

            logger.info(f"[{PLUGIN_NAME}] 截图完成: {url} -> {len(data)} bytes")
            yield event.image_result(temp_file)
        except PermissionError as e:
            logger.warning(f"[{PLUGIN_NAME}] SSRF 拦截: {url} - {e}")
            yield event.plain_result(f"🚫 已拦截：{e}")
        except asyncio.TimeoutError:
            yield event.plain_result(
                f"⏱️ 截图超时（{self.timeout_s + 30}s）：{host}，页面可能加载过慢"
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
            if temp_file and os.path.exists(temp_file):
                try:
                    os.unlink(temp_file)
                except OSError:
                    pass
