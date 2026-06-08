import asyncio
import logging
import os
import time
import yt_dlp
import glob
import re
import subprocess
import sys  # <--- 新增这行，用于调用当前环境的pip
import imageio_ffmpeg
import shutil
import zipfile
import socket
import threading
import json
import requests
from http.server import SimpleHTTPRequestHandler, HTTPServer
from astrbot.api.all import *
from astrbot.api.message_components import Video, Plain, File

@register("yt_dlp_plugin", "YourName", "全能视频下载助手", "3.5.0-MaxQuality")
class YtDlpPlugin(Star):
    def __init__(self, context: Context, config: dict, *args, **kwargs):
        super().__init__(context)
        self.logger = logging.getLogger("astrbot_plugin_yt_dlp")
        self.logger.info("🔥 加载最高画质版 (v3.5)...")
        self.config = config
        
        self.plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self.temp_dir = os.path.join(self.plugin_dir, "temp")
        if not os.path.exists(self.temp_dir):
            os.makedirs(self.temp_dir)
            
        self.ffmpeg_exe = self._resolve_ffmpeg_exe()
            
        self.proxy_enabled = self.config.get("proxy", {}).get("enabled", False)
        self.proxy_url = self.config.get("proxy", {}).get("url", "")
        # 默认改为最高画质
        self.max_quality = self.config.get("download", {}).get("max_quality", "最高画质")
        self.max_size_mb = self.config.get("download", {}).get("max_size_mb", 100)
        self.delete_seconds = self.config.get("download", {}).get("auto_delete_seconds", 60)
        self.prefer_h264 = self.config.get("download", {}).get("prefer_h264", True)
        
        # 审核配置
        mod_config = self.config.get("moderation", {})
        self.enable_moderation = mod_config.get("enabled", False)
        self.moderation_provider_id = mod_config.get("provider_id", "")
        
        # 数据目录
        self.data_dir = os.path.join(self.plugin_dir, "data")
        if not os.path.exists(self.data_dir):
            os.makedirs(self.data_dir)
        
        self.blocked_videos_file = os.path.join(self.data_dir, "blocked_videos.json")
        self.banned_users_file = os.path.join(self.data_dir, "banned_users.json")
        self.blocked_videos = self._load_blocked_videos()
        self.banned_users = self._load_banned_users()
        
        self.server_port = 0
        self.server_ip = self._get_local_ip()
        self._start_http_server()
        self.logger.info(f"文件服务器: http://{self.server_ip}:{self.server_port}")
        self.logger.info(f"画质设置: {self.max_quality} | H.264优先: {self.prefer_h264}")
        self.logger.info(f"内容审核: {self.enable_moderation} | LLM提供商: {self.moderation_provider_id or '当前使用'}")

    def _resolve_ffmpeg_exe(self):
        ffmpeg_config = self.config.get("ffmpeg", {})
        custom_path = ffmpeg_config.get("custom_path", "")

        if custom_path:
            if os.path.exists(custom_path):
                return custom_path
            self.logger.warning(f"自定义 FFmpeg 路径不存在: {custom_path}")

        if ffmpeg_config.get("use_builtin", True):
            try:
                return imageio_ffmpeg.get_ffmpeg_exe()
            except Exception as e:
                self.logger.warning(f"内置 FFmpeg 获取失败: {e}")

        if ffmpeg_config.get("use_system_path", True):
            system_ffmpeg = shutil.which("ffmpeg")
            if system_ffmpeg:
                return system_ffmpeg

        return "ffmpeg"

    def _get_local_ip(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except:
            return "127.0.0.1"

    def _start_http_server(self):
        class TempDirHandler(SimpleHTTPRequestHandler):
            def __init__(handler_self, *args, **kwargs):
                super().__init__(*args, directory=self.temp_dir, **kwargs)
            def log_message(self, format, *args):
                pass

        def run_server():
            server = HTTPServer(('0.0.0.0', 0), TempDirHandler)
            self.server_port = server.server_port
            server.serve_forever()

        t = threading.Thread(target=run_server, daemon=True)
        t.start()
        time.sleep(0.5)

    def _sanitize_filename(self, name: str) -> str:
        if not name:
            return "video"
        name = re.sub(r'[\/\\*?:\"<>|]', '_', name)
        return name.replace('\n', ' ').replace('\r', '')[:100].strip()

    def _format_size(self, size_bytes):
        if size_bytes is None:
            return "未知"
        if size_bytes < 1024:
            return f"{size_bytes} B"
        elif size_bytes < 1024**2:
            return f"{size_bytes/1024:.2f} KB"
        elif size_bytes < 1024**3:
            return f"{size_bytes/1024**2:.2f} MB"
        else:
            return f"{size_bytes/1024**3:.2f} GB"

    def _load_blocked_videos(self):
        if os.path.exists(self.blocked_videos_file):
            try:
                with open(self.blocked_videos_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return set(data.get('videos', []))
            except:
                return set()
        return set()

    def _save_blocked_videos(self):
        try:
            with open(self.blocked_videos_file, 'w', encoding='utf-8') as f:
                json.dump({'videos': list(self.blocked_videos)}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.logger.error(f"保存黑名单失败: {e}")

    def _load_banned_users(self):
        if os.path.exists(self.banned_users_file):
            try:
                with open(self.banned_users_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return data.get('users', {})
            except:
                return {}
        return {}

    def _save_banned_users(self):
        try:
            with open(self.banned_users_file, 'w', encoding='utf-8') as f:
                json.dump({'users': self.banned_users}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.logger.error(f"保存用户封禁失败: {e}")

    def _get_sender_id(self, event):
        try:
            if hasattr(event, 'message_obj'):
                msg = event.message_obj
                if getattr(msg, 'user_id', None):
                    return str(msg.user_id)
            return str(event.session_id)
        except:
            return str(event.session_id)

    def _ban_user(self, user_id, hours=24):
        ban_until = time.time() + hours * 3600
        self.banned_users[user_id] = ban_until
        self._save_banned_users()
        self.logger.info(f"用户 {user_id} 被封禁 {hours} 小时")

    def _is_user_banned(self, user_id):
        if user_id in self.banned_users:
            if time.time() < self.banned_users[user_id]:
                return True
            else:
                del self.banned_users[user_id]
                self._save_banned_users()
        return False

    def _add_blocked_video(self, video_key):
        self.blocked_videos.add(video_key)
        self._save_blocked_videos()
        self.logger.info(f"视频 {video_key} 已加入永久黑名单")

    async def _get_full_video_info(self, url):
        opts = {
            "quiet": True, "no_warnings": True, "nocheckcertificate": True
        }
        if self.proxy_enabled:
            opts["proxy"] = self.proxy_url
        try:
            info = await asyncio.get_running_loop().run_in_executor(
                None, lambda: yt_dlp.YoutubeDL(opts).extract_info(url, download=False))
            return info
        except Exception as e:
            self.logger.error(f"Full info error: {e}")
            return None

    def _is_foreign_video(self, info):
        extractor = str(info.get('extractor', '')).lower()
        if 'bilibili' in extractor:
            return False
        return True

    async def _audit_content(self, info, event):
        if not self.enable_moderation:
            return False
        title = info.get('title', '')
        description = info.get('description', '') or ''
        
        if not self._is_foreign_video(info):
            return False
        
        # LLM 审核标题
        prompt = f"""你是一个内容审核助手。请判断以下视频是否涉及政治敏感 (如习近平、中共等)、色情、暴力、反动内容。
只回复 '安全' 或 '危险' 两个字。
如果内容模糊不清、涉及政治敏感或不确定，默认回复 '危险'。保守优先，宁可误杀不可放过。

标题: {title}
描述: {description}"""
        
        try:
            # 参考其他插件的写法：优先指定 provider_id，否则 fallback 到当前使用
            provider = self.context.get_provider_by_id(self.moderation_provider_id) or self.context.get_using_provider(umo=event.unified_msg_origin)
            if not provider:
                self.logger.warning("未找到可用 LLM 提供商，跳过审核")
                return False
            
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider.meta().id if hasattr(provider, 'meta') else None,
                prompt=prompt
            )
            response_text = llm_resp.completion_text.strip().lower()
            if '危险' in response_text or 'danger' in response_text or '不安全' in response_text:
                self.logger.info(f"LLM 标题审核判定危险: {title}")
                return True
        except Exception as e:
            self.logger.error(f"LLM 标题审核失败: {e}")
            return True  # 保守，失败则阻挡
        
        # 封面图片审核 (如果有缩略图)
        thumbnail = info.get('thumbnail')
        if thumbnail:
            try:
                thumb_path = os.path.join(self.temp_dir, f"thumb_audit_{int(time.time())}.jpg")
                resp = requests.get(thumbnail, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
                if resp.status_code == 200:
                    with open(thumb_path, 'wb') as f:
                        f.write(resp.content)
                    
                    # 尝试用 image_urls 传递图片进行 vision 审核
                    image_prompt = """分析这张视频封面图片，判断是否包含色情、暴力、政治敏感、反动内容。
只回复 '安全' 或 '危险' 两个字。
如果模糊不清或有敏感元素，默认 '危险'。保守优先。"""
                    
                    try:
                        llm_resp_img = await self.context.llm_generate(
                            chat_provider_id=provider.meta().id if hasattr(provider, 'meta') else None,
                            prompt=image_prompt,
                            image_urls=[thumb_path]
                        )
                        img_response = llm_resp_img.completion_text.strip().lower()
                        if '危险' in img_response or 'danger' in img_response:
                            self.logger.info(f"LLM 封面审核判定危险: {title}")
                            # 清理缩略图
                            if os.path.exists(thumb_path):
                                os.remove(thumb_path)
                            return True
                    except Exception as img_e:
                        self.logger.warning(f"封面 vision 审核失败 (可能提供商不支持图片): {img_e}")
                        # 如果 vision 失败，只依赖标题
                    
                    if os.path.exists(thumb_path):
                        os.remove(thumb_path)
            except Exception as e:
                self.logger.error(f"封面下载或审核失败: {e}")
        
        return False

    async def _try_update_ytdlp(self):
        self.logger.info("正在尝试自动更新 yt-dlp...")
        def _run_update():
            try:
                # 使用当前python解释器调用pip更新
                cmd = [sys.executable, "-m", "pip", "install", "-U", "yt-dlp"]
                res = subprocess.run(cmd, capture_output=True, text=True)
                # 检查输出中是否有更新成功的关键词
                if "Successfully installed" in res.stdout:
                    return True, res.stdout
                elif "Requirement already satisfied" in res.stdout:
                    return False, "Already latest"
                return False, res.stderr
            except Exception as e:
                return False, str(e)
        
        return await asyncio.get_running_loop().run_in_executor(None, _run_update)
    async def _manual_merge(self, v, a, out):
        cmd = [self.ffmpeg_exe, "-i", v, "-i", a, "-c:v", "copy", "-c:a", "copy", "-y", out]
        def _run():
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            return subprocess.run(cmd, capture_output=True, text=True, startupinfo=startupinfo)
        
        res = await asyncio.get_running_loop().run_in_executor(None, _run)
        if res.returncode != 0:
            cmd_re = [self.ffmpeg_exe, "-i", v, "-i", a, "-c:v", "copy", "-c:a", "aac", "-y", out]
            res = await asyncio.get_running_loop().run_in_executor(None, lambda: subprocess.run(cmd_re, capture_output=True))
            if res.returncode != 0:
                raise Exception("合并失败")

    async def _get_video_info_safe(self, url):
        # extract_flat=True 加快列表解析速度
        opts = {
            "quiet": True, "no_warnings": True, "nocheckcertificate": True,
            "extract_flat": "in_playlist" 
        }
        if self.proxy_enabled:
            opts["proxy"] = self.proxy_url
        try:
            info = await asyncio.get_running_loop().run_in_executor(
                None, lambda: yt_dlp.YoutubeDL(opts).extract_info(url, download=False))
            
            # 判断是否为列表
            if info.get('_type') == 'playlist':
                return {
                    'is_playlist': True,
                    'title': info.get('title', 'Playlist'),
                    'count': info.get('playlist_count', len(info.get('entries', []))),
                    'entries': info.get('entries', [])
                }
            
            sz = info.get('filesize') or info.get('filesize_approx')
            return {'is_playlist': False, 'title': info.get('title', ''), 'filesize': sz}
        except Exception as e:
            self.logger.error(f"Info error: {e}")
            return None

    async def _download_stream(self, url, fmt, tmpl):
        opts = {
            "outtmpl": tmpl,
            "format": fmt,
            "noplaylist": True,
            "quiet": True,
            "ffmpeg_location": self.ffmpeg_exe
        }
        if self.proxy_enabled:
            opts["proxy"] = self.proxy_url
        def _task():
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return ydl.prepare_filename(info), info
        return await asyncio.get_running_loop().run_in_executor(None, _task)

    async def _core_download_handler(self, event: AstrMessageEvent, url: str, method: str, ctype: str):
        if not url:
            return
        
        # 1. 检查是否包含确认参数
        confirmed = False
        if "--y" in url:
            url = url.replace("--y", "").replace("  ", " ").strip()
            confirmed = True
            
        yield event.plain_result(f"⏳ 正在解析资源信息...")
        info = await self._get_video_info_safe(url)
        
        if not info:
            yield event.plain_result(f"❌ 无法解析链接，请检查网络或链接有效性。")
            return

        # ==================== 内容审核检查 ====================
        if self.enable_moderation:
            sender_id = self._get_sender_id(event)
            if self._is_user_banned(sender_id):
                yield event.plain_result("网络不太好，过一会再来吧。")
                return
            
            # 获取完整信息用于审核 (单视频需要)
            full_info = await self._get_full_video_info(url)
            if full_info and not full_info.get('_type') == 'playlist':
                video_key = f"{full_info.get('extractor', 'unknown')}_{full_info.get('id', str(hash(url))[-10:])}"
                if video_key in self.blocked_videos:
                    self._ban_user(sender_id)
                    yield event.plain_result("网络不太好，过一会再来吧。")
                    return
                
                is_bad = await self._audit_content(full_info, event)
                if is_bad:
                    self._add_blocked_video(video_key)
                    self._ban_user(sender_id)
                    yield event.plain_result("网络不太好，过一会再来吧。")
                    return

        ts = int(time.time())
        final_password = None # 用于最后提示密码

        # ==================== 播放列表逻辑 ====================
        if info.get('is_playlist'):
            count = info['count']
            title = info['title']
            
            # 交互确认机制
            if not confirmed:
                yield event.plain_result(
                    f"📂 检测到播放列表:【{title}】\n"
                    f"🔢 包含视频数: {count} 个\n\n"
                    f"⚠️ 为防止炸服，请确认是否下载并打包（加密）？\n"
                    f"✅ 确认下载请回复:\n/download {url} --y"
                )
                return

            if count > 30: # 阈值可自己改
                yield event.plain_result(f"❌ 视频数量 ({count}) 超过单次限制 (30)。")
                return

            yield event.plain_result(f"📦 开始下载播放列表 ({count}个)... 请耐心等待。")
            
            playlist_folder = os.path.join(self.temp_dir, f"pl_{ts}")
            if not os.path.exists(playlist_folder):
                os.makedirs(playlist_folder)

            # 下载列表
            playlist_tmpl = f"{playlist_folder}/%(playlist_index)s_%(title)s.%(ext)s"
            # 列表建议限制画质以减小体积
            fmt_v = "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
            
            opts = {
                "outtmpl": playlist_tmpl,
                "format": fmt_v,
                "quiet": True,
                "ignoreerrors": True,
                "noplaylist": False,
                "ffmpeg_location": self.ffmpeg_exe,
            }
            if self.proxy_enabled: opts["proxy"] = self.proxy_url

            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, lambda: yt_dlp.YoutubeDL(opts).download([url]))
            except Exception as e:
                yield event.plain_result(f"⚠️ 下载部分出错: {e}")

            files = glob.glob(os.path.join(playlist_folder, "*"))
            if not files:
                yield event.plain_result("❌ 列表下载失败，无文件。")
                shutil.rmtree(playlist_folder)
                return

            # ========== 加密打包逻辑 ==========
            yield event.plain_result(f"🔐 正在加密打包 {len(files)} 个文件 (密码: 123456)...")
            
            # 尝试导入 pyzipper，如果没有则自动安装
            try:
                import pyzipper
            except ImportError:
                self.logger.info("未找到 pyzipper，正在自动安装...")
                yield event.plain_result("⚙️ 首次运行正在安装加密依赖库...")
                await asyncio.get_running_loop().run_in_executor(
                    None, lambda: subprocess.run([sys.executable, "-m", "pip", "install", "pyzipper"], capture_output=True)
                )
                import pyzipper # 安装后再次导入

            # 文件名加上 pwd 提示
            zip_name = f"Playlist_{self._sanitize_filename(title)}_Pwd123456.zip"
            zip_path = os.path.join(self.temp_dir, zip_name)
            
            def _do_encrypted_zip():
                # 使用 AES 加密
                with pyzipper.AESZipFile(zip_path, 'w', compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES) as zf:
                    zf.setpassword(b"123456") # 设置二进制密码
                    for f in files:
                        zf.write(f, os.path.basename(f))
            
            await asyncio.get_running_loop().run_in_executor(None, _do_encrypted_zip)
            
            shutil.rmtree(playlist_folder) # 清理源文件
            final_path = zip_path
            video_title_real = f"Playlist_{title}"
            method = "file" # 强制转为文件发送
            final_password = "123456"

        # ==================== 单视频逻辑 ====================
        else:
            yield event.plain_result(f"📹 {info['title'][:30]}...\n⏳ 开始下载...")
            
            v_tmpl = f"{self.temp_dir}/v_{ts}_%(id)s.%(ext)s"
            a_tmpl = f"{self.temp_dir}/a_{ts}_%(id)s.%(ext)s"
            
            # 画质逻辑
            limit = self.max_quality
            prefer_h264 = self.prefer_h264
            if limit == "最高画质":
                fmt_v = "bestvideo[vcodec^=avc1]/bestvideo[ext=mp4]/bestvideo" if prefer_h264 else "bestvideo"
            else:
                height = int(limit.replace('p', ''))
                fmt_v = f"bestvideo[height<={height}][vcodec^=avc1]/bestvideo[height<={height}]" if prefer_h264 else f"bestvideo[height<={height}]"
            fmt_a = "bestaudio[ext=m4a]/bestaudio"

            try:
                if ctype == "audio_only":
                    final_path, a_info = await self._download_stream(url, fmt_a, a_tmpl)
                    video_title_real = a_info.get('title', 'audio')
                    temp_files = [final_path]
                else:
                    v_path, v_info = await self._download_stream(url, fmt_v, v_tmpl)
                    video_title_real = v_info.get('title', 'video')
                    a_path, a_info = await self._download_stream(url, fmt_a, a_tmpl)
                    
                    yield event.plain_result(f"⚙️ 合并中...")
                    out_path = os.path.join(self.temp_dir, f"final_{ts}.mp4")
                    await self._manual_merge(v_path, a_path, out_path)
                    final_path = out_path
                    temp_files = [v_path, a_path]
            except Exception as e:
                # 自动检测 yt-dlp 更新逻辑
                err_str = str(e).lower()
                yield event.plain_result(f"❌ 错误: {e}")
                updated, log = await self._try_update_ytdlp()
                if updated:
                    yield event.plain_result(f"✅ 核心组件已自动更新，请重启机器人后重试。")
                return

        # ==================== 统一上传逻辑 ====================
        if not final_path or not os.path.exists(final_path):
            yield event.plain_result("❌ 文件生成失败。")
            return

        fsize_mb = os.path.getsize(final_path) / (1024 * 1024)
        
        # 播放列表通常允许更大一点的体积 (500MB)，单视频跟随配置
        max_limit = 500 if info.get('is_playlist') else self.max_size_mb
        
        # 构造密码提示文本
        pwd_hint = f"\n🔐 **解压密码: {final_password}**" if final_password else ""

        if fsize_mb > max_limit:
            fname_disk = os.path.basename(final_path)
            furl = f"http://{self.server_ip}:{self.server_port}/{fname_disk}"
            yield event.plain_result(
                f"⚠️ 文件过大 ({fsize_mb:.1f}MB)，无法直接发送。\n"
                f"🔗 直链下载: {furl}\n"
                f"{pwd_hint}\n"
                f"⏳ 有效期 {self.delete_seconds} 秒"
            )
        else:
            fname_disk = os.path.basename(final_path)
            furl = f"http://{self.server_ip}:{self.server_port}/{fname_disk}"
            safe_title = self._sanitize_filename(video_title_real)
            ext = os.path.splitext(final_path)[1]
            display_name = f"{safe_title}{ext}"
            
            # 如果是加密包，强制在文件名里也写上密码，防止用户忘
            if final_password and "Pwd" not in display_name:
                display_name = f"Pwd{final_password}_{display_name}"

            if method == "file":
                yield event.plain_result(f"⬆️ 正在上传 ({fsize_mb:.1f}MB)...{pwd_hint}")
                tid = None
                is_group = False
                if hasattr(event, 'message_obj'):
                    msg = event.message_obj
                    if getattr(msg, 'group_id', None):
                        is_group = True
                        tid = msg.group_id
                    elif getattr(msg, 'user_id', None):
                        tid = msg.user_id
                if not tid: tid = event.session_id
                
                if tid:
                    act = "upload_group_file" if is_group else "upload_private_file"
                    key = "group_id" if is_group else "user_id"
                    try:
                        await event.bot.call_action(act, **{key: int(tid), "file": furl, "name": display_name})
                    except Exception as upload_err:
                        yield event.plain_result(f"❌ 上传超时或失败: {upload_err}\n🔗 请使用直链: {furl}{pwd_hint}")
                else:
                    yield event.plain_result(f"🔗 直链: {furl}{pwd_hint}")
            else:
                yield event.chain_result([Video(file=furl, url=furl)])

        # 清理任务
        async def _clean():
            wait_time = 120 if info.get('is_playlist') else self.delete_seconds + 30
            await asyncio.sleep(wait_time)
            if os.path.exists(final_path):
                os.remove(final_path)
            if 'temp_files' in locals():
                for f in temp_files:
                    if os.path.exists(f): os.remove(f)
        asyncio.create_task(_clean())

    @command("download")
    async def cmd_download_file(self, event: AstrMessageEvent, url: str = ""):
        raw = event.message_str
        # 兼容多种格式: /download, download, 或直接url
        full_url = url
        for prefix in ["/download ", "download "]:
            if prefix in raw:
                full_url = raw.split(prefix, 1)[1].strip()
                break
        # 如果url参数里没有--y，但原始消息有，补上
        if "--y" not in full_url and "--y" in raw:
            full_url = full_url + " --y"
        async for res in self._core_download_handler(event, full_url, "file", "merged"):
            yield res

    @command("video")
    async def cmd_download_video(self, event: AstrMessageEvent, url: str = ""):
        raw = event.message_str
        full_url = url
        for prefix in ["/video ", "video "]:
            if prefix in raw:
                full_url = raw.split(prefix, 1)[1].strip()
                break
        if "--y" not in full_url and "--y" in raw:
            full_url = full_url + " --y"
        async for res in self._core_download_handler(event, full_url, "video", "merged"):
            yield res
    @command("直链")
    async def cmd_get_direct_url(self, event: AstrMessageEvent, url: str = ""):
        """提取视频直链，不下载"""
        raw = event.message_str
        full_url = url
        for prefix in ["/直链 ", "直链 "]:
            if prefix in raw:
                full_url = raw.split(prefix, 1)[1].strip()
                break
        if not full_url:
            yield event.plain_result("❌ 请提供视频链接，例如: /直链 https://www.youtube.com/watch?v=xxx")
            return

        yield event.plain_result("⏳ 正在解析直链，请稍候...")

        opts = {
            "quiet": True,
            "no_warnings": True,
            "nocheckcertificate": True,
            "noplaylist": True,
            "skip_download": True,
        }
        if self.proxy_enabled:
            opts["proxy"] = self.proxy_url

        try:
            def _extract():
                with yt_dlp.YoutubeDL(opts) as ydl:
                    return ydl.extract_info(full_url, download=False)

            info = await asyncio.get_running_loop().run_in_executor(None, _extract)
        except Exception as e:
            yield event.plain_result(f"❌ 解析失败: {e}")
            return

        if not info:
            yield event.plain_result("❌ 无法获取视频信息。")
            return

        title = info.get("title", "未知标题")
        duration = info.get("duration")
        dur_str = f"{int(duration)//60}:{int(duration)%60:02d}" if duration else "未知"

        # 如果有直接可用的 url（合并格式）
        direct_url = info.get("url")

        # 收集所有 format 中的直链
        formats = info.get("formats", [])

        # 找最佳合并流（同时含视频和音频的格式）
        best_combined = None
        for f in formats:
            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")
            if vcodec != "none" and acodec != "none":
                best_combined = f

        # 找最佳纯视频流
        best_video = None
        for f in formats:
            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")
            if vcodec != "none" and acodec == "none":
                best_video = f

        # 找最佳纯音频流
        best_audio = None
        for f in formats:
            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")
            if vcodec == "none" and acodec != "none":
                best_audio = f

        lines = []
        lines.append(f"🎬 标题: {title}")
        lines.append(f"⏱ 时长: {dur_str}")
        lines.append("")

        if best_combined and best_combined.get("url"):
            res_h = best_combined.get("height", "?")
            res_w = best_combined.get("width", "?")
            ext = best_combined.get("ext", "?")
            fsize = self._format_size(best_combined.get("filesize") or best_combined.get("filesize_approx"))
            lines.append(f"✅ 最佳合并流 ({res_w}x{res_h}, {ext}, {fsize}):")
            lines.append(best_combined["url"])
        elif direct_url:
            lines.append(f"✅ 直链:")
            lines.append(direct_url)
        else:
            lines.append("⚠️ 无合并流直链")

        lines.append("")

        if best_video and best_video.get("url"):
            res_h = best_video.get("height", "?")
            res_w = best_video.get("width", "?")
            ext = best_video.get("ext", "?")
            vcodec = best_video.get("vcodec", "?")
            fsize = self._format_size(best_video.get("filesize") or best_video.get("filesize_approx"))
            lines.append(f"🎥 最佳视频流 ({res_w}x{res_h}, {vcodec}, {ext}, {fsize}):")
            lines.append(best_video["url"])
        else:
            lines.append("⚠️ 无单独视频流直链")

        lines.append("")

        if best_audio and best_audio.get("url"):
            acodec = best_audio.get("acodec", "?")
            ext = best_audio.get("ext", "?")
            fsize = self._format_size(best_audio.get("filesize") or best_audio.get("filesize_approx"))
            lines.append(f"🎵 最佳音频流 ({acodec}, {ext}, {fsize}):")
            lines.append(best_audio["url"])
        else:
            lines.append("⚠️ 无单独音频流直链")

        lines.append("")
        lines.append("⚠️ 直链有时效性，请尽快使用。")

        yield event.plain_result("\n".join(lines))
