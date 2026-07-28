import asyncio
import builtins
import hashlib
import html
import os
import random
import re
import shutil
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

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
    "4.3.2",
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
        # QQ 官方富媒体文档：视频软限制 30MB，富媒体硬限制 200MB。
        # 旧版 AstrBot qq_official 适配器会把本地文件 base64 塞进 JSON，
        # 容易在 10~30MB 级别触发网关 413；本插件在 qq_official 下改走
        # 官方分片上传，最终仍只发送一条 msg_type=7 富媒体消息。
        self.qq_official_max_size_mb = min(
            max(int(download.get("qq_official_max_size_mb", 200)), 1), 200
        )
        self.qq_official_video_soft_limit_mb = min(
            max(int(download.get("qq_official_video_soft_limit_mb", 30)), 1),
            self.qq_official_max_size_mb,
        )
        self.auto_delete_seconds = max(int(download.get("auto_delete_seconds", 300)), 60)
        self.prefer_h264 = bool(download.get("prefer_h264", True))
        self.max_concurrent = max(int(download.get("max_concurrent", 1)), 1)
        self._download_semaphore = asyncio.Semaphore(self.max_concurrent)

        cookies = config.get("cookies", {}) or {}
        self.bilibili_cookies = str(cookies.get("bilibili", "") or "").strip()
        self.youtube_cookies = str(cookies.get("youtube", "") or "").strip()
        self.generic_cookies = str(cookies.get("generic", "") or "").strip()
        self.cookie_dir = self.data_dir / "cookies"
        self.cookie_dir.mkdir(parents=True, exist_ok=True)

        access = config.get("access_control", {}) or {}
        self.operator_ids = self._parse_values(access.get("operator_openids", ""))
        self.allowed_platform_instance_ids = self._parse_values(
            access.get("allowed_qqofficial_instance_ids", "")
        )

        moderation = config.get("moderation", {}) or {}
        self.moderation_provider_id = str(moderation.get("provider_id", "") or "")
        # Backward compatible group allowlist parsing.  Older versions exposed
        # the QQ Official group_openid allowlist under ``moderation`` because it
        # also skipped foreign-video moderation.  Keep honoring that key, but
        # the canonical home is now access_control.allowed_group_openids so the
        # permission surface is not hidden inside the moderation section.
        self.allowed_group_openids = set()
        for value in (
            access.get("allowed_group_openids", ""),
            access.get("allowed_qqofficial_group_openids", ""),
            moderation.get("group_openid_whitelist", ""),
            config.get("group_openid_whitelist", ""),
            config.get("group_whitelist", ""),
        ):
            self.allowed_group_openids |= self._parse_values(value)
        self.moderation_group_whitelist = self.allowed_group_openids

        self.ffmpeg_exe = self._resolve_ffmpeg_exe()
        runtime = builtins._ASTRBOT_YTDLP_RUNTIME
        runtime["generation"] = int(runtime.get("generation", 0)) + 1
        self._runtime_generation = runtime["generation"]
        runtime["instance"] = self
        logger.info(
            "[yt-dlp] 已加载：temp=%s quality=%s generic_max=%dMB qq_max=%dMB qq_video_soft=%dMB operators=%d allowed_groups=%d allowed_instances=%d",
            self.temp_dir,
            self.max_quality,
            self.max_size_mb,
            self.qq_official_max_size_mb,
            self.qq_official_video_soft_limit_mb,
            len(self.operator_ids),
            len(self.allowed_group_openids),
            len(self.allowed_platform_instance_ids),
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
        if group_id and group_id in self.allowed_group_openids:
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
            f"当前平台实例：{self._platform_instance_id(event) or '未知'}\n"
            "如需放行本群，请把当前 group_openid 填入 access_control.allowed_group_openids。"
        )

    @staticmethod
    def _normalize_url(url: str) -> str:
        url = html.unescape(str(url or "").strip().strip("<>"))
        if not url:
            return url
        try:
            parts = urlsplit(url)
        except ValueError:
            return url
        host = parts.netloc.lower()
        if host.endswith("bilibili.com"):
            match = re.search(r"/video/((?:BV[0-9A-Za-z]+)|(?:av\d+))", parts.path)
            if match:
                # spm_id_from / trackid / vd_source 等查询参数对 yt-dlp 无益，
                # 反而会污染日志或触发外壳转义问题；B 站视频统一规范成 BV/av 短 URL。
                return urlunsplit((parts.scheme or "https", parts.netloc, f"/video/{match.group(1)}/", "", ""))
        return url

    @staticmethod
    def _cookie_key_for_url(url: str) -> str:
        host = ""
        try:
            host = urlsplit(url).netloc.lower()
        except ValueError:
            pass
        if "bilibili" in host or "b23.tv" in host:
            return "bilibili"
        if "youtube" in host or "youtu.be" in host:
            return "youtube"
        return "generic"

    def _cookie_config_for_url(self, url: str) -> str:
        key = self._cookie_key_for_url(url)
        if key == "bilibili":
            return self.bilibili_cookies or self.generic_cookies
        if key == "youtube":
            return self.youtube_cookies or self.generic_cookies
        return self.generic_cookies

    def _resolve_cookie_for_ytdlp(self, url: str) -> tuple[str | None, str | None]:
        value = self._cookie_config_for_url(url)
        if not value:
            return None, None

        expanded = Path(os.path.expanduser(value))
        if expanded.exists():
            return str(expanded), None

        lowered = value.lower().lstrip()
        key = self._cookie_key_for_url(url)
        # 支持 WebUI 大文本框直接粘贴 Netscape cookies.txt。
        if "\n" in value or lowered.startswith("# netscape") or "\t" in value:
            cookie_path = self.cookie_dir / f"{key}.cookies.txt"
            cookie_path.write_text(value.rstrip() + "\n", encoding="utf-8")
            try:
                cookie_path.chmod(0o600)
            except OSError:
                pass
            return str(cookie_path), None

        # 也兼容直接粘贴浏览器 Request Header 里的 Cookie: a=b; c=d。
        if lowered.startswith("cookie:"):
            value = value.split(":", 1)[1].strip()
        if ";" in value and "=" in value:
            return None, value

        raise ValueError(
            f"{key} cookies 配置既不是已存在的文件路径，也不像 cookies.txt 或 Cookie 请求头"
        )

    def _ydl_base_options(self, url: str | None = None) -> dict:
        options = {
            "quiet": True,
            "no_warnings": True,
            "nocheckcertificate": True,
            "noplaylist": True,
            "ffmpeg_location": self.ffmpeg_exe,
            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        }
        if self.proxy_enabled and self.proxy_url:
            options["proxy"] = self.proxy_url
        if url:
            cookiefile, cookie_header = self._resolve_cookie_for_ytdlp(url)
            if cookiefile:
                options["cookiefile"] = cookiefile
            if cookie_header:
                options["http_headers"]["Cookie"] = cookie_header
        return options

    async def _extract_info(self, url: str) -> dict:
        options = self._ydl_base_options(url)
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
        options = self._ydl_base_options(url)
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

    @staticmethod
    def _file_hashes(path: Path) -> tuple[str, str, str]:
        md5 = hashlib.md5()  # noqa: S324 - protocol requires MD5 checksum
        sha1 = hashlib.sha1()  # noqa: S324 - protocol requires SHA1 checksum
        md5_10m = hashlib.md5()  # noqa: S324 - protocol requires MD5 checksum
        remaining_10m = 10_002_432
        with path.open("rb") as file_obj:
            while True:
                chunk = file_obj.read(1024 * 1024)
                if not chunk:
                    break
                md5.update(chunk)
                sha1.update(chunk)
                if remaining_10m > 0:
                    head = chunk[:remaining_10m]
                    md5_10m.update(head)
                    remaining_10m -= len(head)
        return md5.hexdigest(), sha1.hexdigest(), md5_10m.hexdigest()

    @staticmethod
    def _qq_official_target(event: AstrMessageEvent) -> tuple[str, str]:
        source = getattr(event.message_obj, "raw_message", None)
        group_openid = str(getattr(source, "group_openid", "") or "")
        if group_openid:
            return "group", group_openid
        author = getattr(source, "author", None)
        openid = str(
            getattr(author, "user_openid", "")
            or getattr(source, "openid", "")
            or getattr(source, "user_openid", "")
            or ""
        )
        if openid:
            return "c2c", openid
        raise RuntimeError("无法识别 QQ 官方消息目标：缺少 group_openid / user_openid")

    async def _qq_api_request(
        self,
        event: AstrMessageEvent,
        method: str,
        path: str,
        route_kwargs: dict,
        payload: dict,
        *,
        allow_none: bool = False,
    ):
        from botpy.http import Route

        route = Route(method, path, **route_kwargs)
        result = await event.bot.api._http.request(route, json=payload)
        if result is None and not allow_none:
            raise RuntimeError(f"QQ API 返回空响应: {method} {path}")
        return result

    async def _qq_put_upload_part(
        self,
        session: aiohttp.ClientSession,
        url: str,
        data: bytes,
        *,
        retry_timeout: int,
        retry_delay: int,
    ) -> None:
        deadline = time.monotonic() + max(retry_timeout, 1)
        attempt = 0
        last_error = ""
        while True:
            attempt += 1
            try:
                async with session.put(
                    url,
                    data=data,
                    headers={"Content-Type": "application/octet-stream"},
                ) as response:
                    if response.status in (200, 201, 204):
                        return
                    body = await response.text()
                    last_error = f"HTTP {response.status}: {body[:300]}"
            except Exception as exc:  # noqa: BLE001 - keep final stage error explicit
                last_error = f"{type(exc).__name__}: {exc}"
            if time.monotonic() >= deadline:
                raise RuntimeError(f"分片 PUT 多次失败（attempt={attempt}）：{last_error}")
            await asyncio.sleep(max(retry_delay, 1))

    async def _upload_qq_official_by_chunks(
        self,
        event: AstrMessageEvent,
        path: Path,
        *,
        file_type: int,
        file_name: str,
    ):
        from botpy.types.message import Media

        target_kind, target_id = self._qq_official_target(event)
        file_size = path.stat().st_size
        md5, sha1, md5_10m = self._file_hashes(path)

        if target_kind == "group":
            prepare_path = "/v2/groups/{group_id}/upload_prepare"
            finish_path = "/v2/groups/{group_id}/upload_part_finish"
            files_path = "/v2/groups/{group_openid}/files"
            prepare_kwargs = {"group_id": target_id}
            finish_kwargs = {"group_id": target_id}
            files_kwargs = {"group_openid": target_id}
        else:
            prepare_path = "/v2/users/{user_id}/upload_prepare"
            finish_path = "/v2/users/{user_id}/upload_part_finish"
            files_path = "/v2/users/{user_openid}/files"
            prepare_kwargs = {"user_id": target_id}
            finish_kwargs = {"user_id": target_id}
            files_kwargs = {"user_openid": target_id}

        prepare_payload = {
            "file_type": file_type,
            "file_size": str(file_size),
            "file_name": file_name,
            "md5": md5,
            "sha1": sha1,
            "md5_10m": md5_10m,
        }
        prepare = await self._qq_api_request(
            event, "POST", prepare_path, prepare_kwargs, prepare_payload
        )
        if not isinstance(prepare, dict):
            raise RuntimeError(f"预上传响应不是 dict: {prepare}")

        upload_id = str(prepare.get("upload_id") or "")
        if not upload_id:
            raise RuntimeError(f"预上传响应缺少 upload_id: {prepare}")
        try:
            block_size = int(prepare.get("block_size") or 5 * 1024 * 1024)
        except (TypeError, ValueError):
            block_size = 5 * 1024 * 1024
        parts = prepare.get("parts") or []
        if not isinstance(parts, list) or not parts:
            raise RuntimeError(f"预上传响应缺少 parts: {prepare}")

        upload_config = prepare.get("upload_config") or {}
        try:
            retry_timeout = int(upload_config.get("retry_timeout") or 300)
        except (AttributeError, TypeError, ValueError):
            retry_timeout = 300
        try:
            retry_delay = int(upload_config.get("retry_delay") or 1)
        except (AttributeError, TypeError, ValueError):
            retry_delay = 1

        timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=retry_timeout)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            with path.open("rb") as file_obj:
                uploaded_bytes = 0
                # 官方文档写 UploadPart.index 从 0 开始，但线上返回可能与
                # 文档不完全一致（例如从 1 开始）。读取本地文件时不要用
                # index * block_size 反推偏移，而按 parts 顺序连续读取；
                # part_index 仍按服务端返回值原样回传给 upload_part_finish。
                for order, part in enumerate(
                    sorted(parts, key=lambda item: int(item.get("index", 0)))
                ):
                    try:
                        index = int(part.get("index", order))
                    except (TypeError, ValueError):
                        index = order
                    presigned_url = str(part.get("presigned_url") or "")
                    if not presigned_url:
                        raise RuntimeError(f"分片 {index} 缺少 presigned_url")
                    try:
                        part_size = int(part.get("block_size") or block_size)
                    except (TypeError, ValueError):
                        part_size = block_size
                    remaining = file_size - uploaded_bytes
                    if remaining <= 0:
                        logger.warning(
                            "[yt-dlp] QQ预上传返回多余分片: index=%s uploaded=%d size=%d",
                            index,
                            uploaded_bytes,
                            file_size,
                        )
                        break
                    offset = uploaded_bytes
                    file_obj.seek(offset)
                    data = file_obj.read(min(part_size, remaining))
                    if not data:
                        raise RuntimeError(
                            f"分片 {index} 读取为空，offset={offset}, "
                            f"part_size={part_size}, uploaded={uploaded_bytes}, "
                            f"file_size={file_size}"
                        )
                    await self._qq_put_upload_part(
                        session,
                        presigned_url,
                        data,
                        retry_timeout=retry_timeout,
                        retry_delay=retry_delay,
                    )
                    part_md5 = hashlib.md5(data).hexdigest()  # noqa: S324
                    await self._qq_api_request(
                        event,
                        "POST",
                        finish_path,
                        finish_kwargs,
                        {
                            "upload_id": upload_id,
                            "part_index": index,
                            "block_size": str(len(data)),
                            "md5": part_md5,
                        },
                        allow_none=True,
                    )
                    uploaded_bytes += len(data)
                if uploaded_bytes != file_size:
                    raise RuntimeError(
                        f"分片上传本地读取字节数不一致：uploaded={uploaded_bytes}, file_size={file_size}"
                    )

        complete_payload = {
            "file_type": file_type,
            "srv_send_msg": False,
            "file_name": file_name,
            "upload_id": upload_id,
        }
        complete = await self._qq_api_request(
            event, "POST", files_path, files_kwargs, complete_payload
        )
        if not isinstance(complete, dict):
            raise RuntimeError(f"上传合并响应不是 dict: {complete}")
        if not complete.get("file_info"):
            raise RuntimeError(f"上传合并响应缺少 file_info: {complete}")
        return Media(
            file_uuid=complete.get("file_uuid", ""),
            file_info=complete["file_info"],
            ttl=complete.get("ttl", 0),
        )

    async def _send_qq_official_media(
        self,
        event: AstrMessageEvent,
        path: Path,
        *,
        title: str,
        as_file: bool,
    ) -> str:
        size_mb = path.stat().st_size / 1024 / 1024
        if size_mb > self.qq_official_max_size_mb:
            raise ValueError(
                f"文件 {size_mb:.1f}MB，超过 QQ 官方硬上限 {self.qq_official_max_size_mb}MB"
            )

        # 1=图片，2=视频，3=语音，4=文件。视频超过软限制时主动按文件发，
        # 避免先失败/降级导致多占一次消息或出现二义性。
        file_type = 4 if as_file or size_mb > self.qq_official_video_soft_limit_mb else 2
        suffix = path.suffix or ".mp4"
        file_name = self._safe_filename(title) + suffix
        media = await self._upload_qq_official_by_chunks(
            event, path, file_type=file_type, file_name=file_name
        )

        target_kind, target_id = self._qq_official_target(event)
        payload = {
            "msg_type": 7,
            "msg_id": event.message_obj.message_id,
            "msg_seq": random.randint(1, 10000),
            "media": media,
        }
        if target_kind == "group":
            result = await self._qq_api_request(
                event,
                "POST",
                "/v2/groups/{group_openid}/messages",
                {"group_openid": target_id},
                payload,
            )
        else:
            result = await self._qq_api_request(
                event,
                "POST",
                "/v2/users/{openid}/messages",
                {"openid": target_id},
                payload,
            )
        if result is None:
            raise RuntimeError("QQ 发送富媒体消息返回空响应")
        try:
            event._has_send_oper = True
        except Exception:
            pass
        return "file" if file_type == 4 else "video"

    async def _handle(self, event: AstrMessageEvent, url: str, as_file: bool):
        if not self._is_current_runtime():
            logger.warning("[yt-dlp] 忽略热重载残留插件实例: %s", id(self))
            return
        url = self._normalize_url(url)
        if not url.startswith(("http://", "https://")):
            yield event.plain_result("请提供 http/https 单视频链接。")
            return

        task_id = f"{int(time.time() * 1000)}_{hashlib_sha(url)}"
        size_mb = 0.0
        stage = "初始化"
        async with self._download_semaphore:
            try:
                stage = "解析视频信息"
                info = await self._extract_info(url)

                stage = "权限与内容审核"
                domestic = self._is_domestic_source(info)
                if not domestic:
                    permission = self._require_download_access(event)
                    if permission:
                        yield permission
                        return
                    await self._moderate(info, event, task_id)

                qq_official = self._is_qq_official_event(event)
                size_limit = (
                    self.qq_official_max_size_mb if qq_official else self.max_size_mb
                )

                stage = "大小预估"
                estimated = info.get("filesize") or info.get("filesize_approx")
                if estimated and float(estimated) / 1024 / 1024 > size_limit:
                    raise ValueError(
                        f"预计文件 {float(estimated) / 1024 / 1024:.1f}MB，"
                        f"超过当前发送上限 {size_limit}MB，未开始下载"
                    )

                stage = "下载与封装"
                final_path, downloaded_info = await self._download_video(
                    url, task_id, size_limit_mb=size_limit
                )
                title = self._safe_filename(downloaded_info.get("title", "video"))
                size_mb = final_path.stat().st_size / 1024 / 1024

                if qq_official:
                    stage = "QQ官方分片上传与发送"
                    sent_as = await self._send_qq_official_media(
                        event, final_path, title=title, as_file=as_file
                    )
                    logger.info(
                        "[yt-dlp] QQ官方发送成功: mode=%s size=%.1fMB title=%s",
                        sent_as,
                        size_mb,
                        title,
                    )
                else:
                    stage = "平台适配器发送"
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
                logger.error("[yt-dlp] 任务失败(stage=%s): %s", stage, exc)
                error_text = str(exc) or type(exc).__name__
                if "HTTP Error 412" in error_text and (
                    "BiliBili" in error_text or "bilibili" in url.lower()
                ):
                    error_text = (
                        "B站风控返回 412 Precondition Failed；建议更新 yt-dlp，"
                        "在插件 cookies.bilibili 中配置 B站 cookies.txt 或 Cookie 请求头，"
                        "并稍后重试。原始错误：" + error_text
                    )
                elif (
                    self._is_qq_official_event(event)
                    and "无效 markdown content" in error_text
                ):
                    error_text = (
                        f"QQ媒体上传失败（本地文件 {size_mb:.1f}MB，当前配置上限 "
                        f"{self.qq_official_max_size_mb}MB）；适配器在413重试后返回"
                        "“无效 markdown content”。本版本应已绕过适配器 base64 上传；"
                        "若仍出现，请确认插件已重载到最新代码"
                    )
                if "阶段失败" not in error_text:
                    error_text = f"{stage}阶段失败：{error_text}"
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
