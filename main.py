import asyncio
import builtins
import json
import os
import re
import shutil
import time
from pathlib import Path

import aiohttp
import imageio_ffmpeg
import yt_dlp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Video
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_yt_dlp"

if not hasattr(builtins, "_ASTRBOT_YTDLP_RUNTIME"):
    builtins._ASTRBOT_YTDLP_RUNTIME = {"generation": 0, "instance": None}


@register(
    "astrbot_plugin_yt_dlp",
    "ハ七",
    "QQ官方Bot单视频下载与本地富媒体上传",
    "4.2.1",
    "",
)
class YtDlpPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config

        self.data_dir = self._resolve_data_dir()
        self.temp_dir = self.data_dir / "temp"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self._cleanup_stale_files()

        proxy = config.get("proxy", {}) or {}
        self.proxy_enabled = bool(proxy.get("enabled", False))
        self.proxy_url = str(proxy.get("url", "") or "").strip()

        download = config.get("download", {}) or {}
        self.max_quality = str(download.get("max_quality", "1080p"))
        self.max_size_mb = max(int(download.get("max_size_mb", 100)), 1)
        # QQ Official Adapter 会把本地文件 Base64 放进 JSON，体积约膨胀 4/3。
        # 使用独立保守上限，避免大文件触发网关 413 后被底层连续重试。
        # QQ Official embeds local media as Base64 in a JSON request. Keep the
        # operational ceiling entirely configurable because different gateways
        # may enforce different body limits; no hidden hard cap lives in code.
        self.qq_official_max_size_mb = max(
            int(download.get("qq_official_max_size_mb", 15)), 1
        )
        self.auto_delete_seconds = max(int(download.get("auto_delete_seconds", 300)), 60)
        self.prefer_h264 = bool(download.get("prefer_h264", True))
        self.max_concurrent = max(int(download.get("max_concurrent", 1)), 1)
        self._download_semaphore = asyncio.Semaphore(self.max_concurrent)

        access = config.get("access_control", {}) or {}
        self.operator_ids = self._parse_values(access.get("operator_openids", ""))
        self.allowed_platform_instance_ids = self._parse_values(
            access.get("allowed_qqofficial_instance_ids", "")
        )

        moderation = config.get("moderation", {}) or {}
        self.moderation_provider_id = str(moderation.get("provider_id", "") or "")
        self.moderation_group_whitelist = self._parse_values(
            moderation.get("group_openid_whitelist", "")
        )

        self.ffmpeg_exe = self._resolve_ffmpeg_exe()
        runtime = builtins._ASTRBOT_YTDLP_RUNTIME
        runtime["generation"] = int(runtime.get("generation", 0)) + 1
        self._runtime_generation = runtime["generation"]
        runtime["instance"] = self
        logger.info(
            "[yt-dlp] QQ官方模式已加载：temp=%s quality=%s max=%dMB operators=%d",
            self.temp_dir,
            self.max_quality,
            self.max_size_mb,
            len(self.operator_ids),
        )

    def _is_current_runtime(self) -> bool:
        runtime = builtins._ASTRBOT_YTDLP_RUNTIME
        return (
            runtime.get("instance") is self
            and runtime.get("generation") == self._runtime_generation
        )

    def _resolve_data_dir(self) -> Path:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            root = Path(get_astrbot_data_path())
        except (ImportError, AttributeError, TypeError):
            root = Path("data").resolve()
        path = root / "plugin_data" / PLUGIN_NAME
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _resolve_ffmpeg_exe(self) -> str:
        ffmpeg = self.config.get("ffmpeg", {}) or {}
        custom = str(ffmpeg.get("custom_path", "") or "").strip()
        if custom:
            if Path(custom).exists():
                return custom
            logger.warning("[yt-dlp] 自定义 FFmpeg 不存在: %s", custom)
        if bool(ffmpeg.get("use_imageio", True)):
            try:
                return imageio_ffmpeg.get_ffmpeg_exe()
            except Exception as exc:
                logger.warning("[yt-dlp] imageio FFmpeg 获取失败: %s", exc)
        system = shutil.which("ffmpeg")
        if system:
            return system
        return "ffmpeg"

    @staticmethod
    def _parse_values(value) -> set[str]:
        if isinstance(value, (list, tuple, set)):
            values = value
        else:
            values = re.split(r"[\s,，;；]+", str(value or ""))
        return {str(item).strip() for item in values if str(item).strip()}

    @staticmethod
    def _safe_filename(value: str) -> str:
        value = re.sub(r'[\\/*?:"<>|\r\n]+', "_", str(value or "video"))
        return value.strip(" ._")[:120] or "video"

    @staticmethod
    def _sender_id(event: AstrMessageEvent) -> str:
        try:
            return str(event.get_sender_id())
        except Exception:
            return str(event.message_obj.sender.user_id)

    @staticmethod
    def _group_id(event: AstrMessageEvent) -> str:
        try:
            return str(event.get_group_id() or "")
        except Exception:
            return str(getattr(event.message_obj, "group_id", "") or "")

    def _is_operator(self, event: AstrMessageEvent) -> bool:
        return bool(event.is_admin() or self._sender_id(event) in self.operator_ids)

    @staticmethod
    def _platform_instance_id(event: AstrMessageEvent) -> str:
        try:
            return str(event.get_platform_id() or "")
        except Exception:
            meta = getattr(event, "platform_meta", None)
            return str(getattr(meta, "id", "") or "")

    def _platform_instance_aliases(self, event: AstrMessageEvent) -> set[str]:
        platform_id = self._platform_instance_id(event)
        aliases = {platform_id} if platform_id else set()
        # AstrBot often names QQ Official instances default_<appid>. Accepting
        # the numeric suffix is convenient but remains an explicit instance
        # whitelist, never a group identifier.
        if platform_id.startswith("default_") and len(platform_id) > 8:
            aliases.add(platform_id[8:])
        return aliases

    def _is_whitelisted_context(self, event: AstrMessageEvent) -> bool:
        group_id = self._group_id(event)
        if group_id and group_id in self.moderation_group_whitelist:
            return True
        return bool(
            self._platform_instance_aliases(event)
            & self.allowed_platform_instance_ids
        )

    @staticmethod
    def _is_qq_official_event(event: AstrMessageEvent) -> bool:
        try:
            return event.get_platform_name() == "qq_official"
        except Exception:
            return False

    def _require_download_access(self, event: AstrMessageEvent):
        if self._is_operator(event) or self._is_whitelisted_context(event):
            return None
        return event.plain_result(
            "该下载功能仅 AstrBot 管理员、字幕组操作员或白名单群可用。\n"
            f"当前用户 OpenID：{self._sender_id(event)}\n"
            f"当前群 group_openid：{self._group_id(event) or '非群聊'}\n"
            f"当前平台实例：{self._platform_instance_id(event) or '未知'}"
        )

    def _ydl_base_options(self) -> dict:
        options = {
            "quiet": True,
            "no_warnings": True,
            "nocheckcertificate": True,
            "noplaylist": True,
            "ffmpeg_location": self.ffmpeg_exe,
        }
        if self.proxy_enabled and self.proxy_url:
            options["proxy"] = self.proxy_url
        return options

    async def _extract_info(self, url: str) -> dict:
        options = self._ydl_base_options()
        options["skip_download"] = True

        def task():
            with yt_dlp.YoutubeDL(options) as ydl:
                return ydl.extract_info(url, download=False)

        info = await asyncio.get_running_loop().run_in_executor(None, task)
        if not isinstance(info, dict) or info.get("_type") == "playlist":
            raise ValueError("仅支持单视频链接，不支持播放列表")
        return info

    @staticmethod
    def _is_domestic_source(info: dict) -> bool:
        text = " ".join(
            str(info.get(key, "") or "").lower()
            for key in ("extractor", "extractor_key", "webpage_url", "original_url")
        )
        domestic = (
            "bilibili", "douyin", "ixigua", "weibo", "xiaohongshu",
            "youku", "iqiyi", "tencentvideo", "acfun", "kuaishou",
        )
        return any(keyword in text for keyword in domestic)

    async def _get_provider_id(self, event: AstrMessageEvent) -> str:
        if self.moderation_provider_id:
            return self.moderation_provider_id
        try:
            return await self.context.get_current_chat_provider_id(
                umo=event.unified_msg_origin
            )
        except Exception:
            return ""

    async def _download_thumbnail(self, url: str, task_id: str) -> Path:
        path = self.temp_dir / f"thumb_{task_id}.jpg"
        timeout = aiohttp.ClientTimeout(total=20, connect=8)
        headers = {"User-Agent": "Mozilla/5.0 (AstrBot yt-dlp moderation)"}
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True, headers=headers) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    raise RuntimeError(f"THUMBNAIL_HTTP_{response.status}")
                raw = await response.read()
        if len(raw) < 500:
            raise RuntimeError("THUMBNAIL_INVALID")
        path.write_bytes(raw)
        return path

    async def _moderate(self, info: dict, event: AstrMessageEvent, task_id: str) -> None:
        if self._is_domestic_source(info):
            logger.info("[yt-dlp] 国内平台免审: %s", info.get("extractor"))
            return
        group_id = self._group_id(event)
        if self._is_whitelisted_context(event):
            logger.warning(
                "[yt-dlp] 上下文命中下载/审核白名单: group=%s platform=%s",
                group_id or "private",
                self._platform_instance_id(event),
            )
            return

        provider_id = await self._get_provider_id(event)
        if not provider_id:
            raise ValueError("没有可用的多模态审核 Provider，拒绝下载")

        title = str(info.get("title", "") or "")[:300]
        description = str(info.get("description", "") or "")[:1000]
        thumbnail = str(info.get("thumbnail", "") or "")
        image_urls = []
        thumb_path = None
        try:
            if thumbnail:
                thumb_path = await self._download_thumbnail(thumbnail, task_id)
                image_urls = [str(thumb_path)]
            prompt = (
                "你是 QQ 官方机器人视频转载安全审核员。结合标题、描述和封面判断。"
                "只返回 SAFE、REJECT、MALICIOUS 之一，不要解释。"
                "政治敏感、色情、血腥暴力、开盒真人信息、违法、恐怖主义、仇恨或无法可靠判断均不得放行。\n"
                f"标题：{title}\n描述：{description}"
            )
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                image_urls=image_urls or None,
                request_max_retries=1,
            )
            result = (response.completion_text or "").strip().upper()
            if "MALICIOUS" in result:
                raise ValueError("视频审核结果：MALICIOUS")
            if result != "SAFE":
                raise ValueError("视频审核结果：REJECT")
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"视频审核失败，默认拒绝：{type(exc).__name__}") from exc
        finally:
            if thumb_path and thumb_path.exists():
                thumb_path.unlink()

    def _format_selector(self) -> str:
        if self.max_quality == "最高画质":
            height_filter = ""
        else:
            try:
                height = int(self.max_quality.lower().replace("p", ""))
            except ValueError:
                height = 1080
            height_filter = f"[height<={height}]"
        if self.prefer_h264:
            video = f"bestvideo{height_filter}[vcodec^=avc1]/bestvideo{height_filter}[ext=mp4]/bestvideo{height_filter}"
        else:
            video = f"bestvideo{height_filter}"
        return f"({video})+bestaudio[ext=m4a]/best[ext=mp4]/best"

    async def _download_video(self, url: str, task_id: str, size_limit_mb: int) -> tuple[Path, dict]:
        output = self.temp_dir / f"{task_id}_%(id)s.%(ext)s"
        options = self._ydl_base_options()
        options.update({
            "outtmpl": str(output),
            "format": self._format_selector(),
            "merge_output_format": "mp4",
            "noplaylist": True,
        })

        def task():
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=True)
                requested = info.get("requested_downloads") or []
                candidates = [item.get("filepath") for item in requested if item.get("filepath")]
                candidates += [info.get("filepath"), ydl.prepare_filename(info)]
                return info, candidates

        info, candidates = await asyncio.get_running_loop().run_in_executor(None, task)
        files = [Path(value) for value in candidates if value and Path(value).exists()]
        if not files:
            files = sorted(self.temp_dir.glob(f"{task_id}_*"), key=lambda path: path.stat().st_mtime, reverse=True)
        if not files:
            raise RuntimeError("下载完成但找不到输出文件")
        final = files[0]
        size_mb = final.stat().st_size / 1024 / 1024
        if size_mb > size_limit_mb:
            raise ValueError(
                f"文件 {size_mb:.1f}MB，超过当前发送模式的安全上限 {size_limit_mb}MB；"
                "已在调用 QQ 上传接口前停止，避免 413 重试风暴"
            )
        return final, info

    async def _cleanup_task(self, task_id: str, delay: int | None = None) -> None:
        await asyncio.sleep(delay or self.auto_delete_seconds)
        for path in self.temp_dir.glob(f"{task_id}_*"):
            try:
                path.unlink()
            except OSError:
                pass

    def _cleanup_stale_files(self) -> None:
        cutoff = time.time() - 24 * 3600
        for path in self.temp_dir.glob("*"):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue

    async def _handle(self, event: AstrMessageEvent, url: str, as_file: bool):
        if not self._is_current_runtime():
            logger.warning("[yt-dlp] 忽略热重载残留插件实例: %s", id(self))
            return
        url = str(url or "").strip()
        if not url.startswith(("http://", "https://")):
            yield event.plain_result("请提供 http/https 单视频链接。")
            return

        task_id = f"{int(time.time() * 1000)}_{hashlib_sha(url)}"
        size_mb = 0.0
        async with self._download_semaphore:
            try:
                info = await self._extract_info(url)
                domestic = self._is_domestic_source(info)
                if not domestic:
                    permission = self._require_download_access(event)
                    if permission:
                        yield permission
                        return
                    await self._moderate(info, event, task_id)
                size_limit = min(
                    self.max_size_mb,
                    self.qq_official_max_size_mb
                    if self._is_qq_official_event(event)
                    else self.max_size_mb,
                )
                estimated = info.get("filesize") or info.get("filesize_approx")
                if estimated and float(estimated) / 1024 / 1024 > size_limit:
                    raise ValueError(
                        f"预计文件 {float(estimated) / 1024 / 1024:.1f}MB，"
                        f"超过当前发送上限 {size_limit}MB，未开始下载"
                    )
                final_path, downloaded_info = await self._download_video(
                    url, task_id, size_limit_mb=size_limit
                )
                title = self._safe_filename(downloaded_info.get("title", "video"))
                size_mb = final_path.stat().st_size / 1024 / 1024
                if as_file:
                    component = File(name=f"{title}{final_path.suffix}", file=str(final_path))
                else:
                    component = Video(file=str(final_path))
                # Send inside the plugin so adapter/upload failures are caught
                # here. Yielding a component defers the failure to RespondStage,
                # where the plugin can no longer produce one final error.
                await event.send(event.chain_result([component]))
                asyncio.create_task(self._cleanup_task(task_id))
            except Exception as exc:
                logger.error("[yt-dlp] 任务失败: %s", exc)
                error_text = str(exc) or type(exc).__name__
                if (
                    self._is_qq_official_event(event)
                    and "无效 markdown content" in error_text
                ):
                    error_text = (
                        f"QQ媒体上传失败（本地文件 {size_mb:.1f}MB，当前配置上限 "
                        f"{self.qq_official_max_size_mb}MB）；适配器在413重试后返回"
                        "“无效 markdown content”。请继续下调 qq_official_max_size_mb"
                    )
                yield event.plain_result(f"❌ 处理失败：{error_text}")
                asyncio.create_task(self._cleanup_task(task_id, delay=1))

    @filter.command("video")
    async def video(self, event: AstrMessageEvent, url: str = ""):
        """下载单视频并作为 QQ 视频消息发送。"""
        async for result in self._handle(event, url, as_file=False):
            yield result

    @filter.command("download")
    async def download(self, event: AstrMessageEvent, url: str = ""):
        """下载单视频并作为 QQ 文件消息发送。"""
        async for result in self._handle(event, url, as_file=True):
            yield result

    async def terminate(self):
        runtime = builtins._ASTRBOT_YTDLP_RUNTIME
        if runtime.get("instance") is self:
            runtime["instance"] = None
        logger.info("[yt-dlp] 插件已停止；临时文件由过期清理兜底")


def hashlib_sha(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()[:10]
