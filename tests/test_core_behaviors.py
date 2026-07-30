import asyncio
import hashlib
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path


def load_plugin_module():
    for name in list(sys.modules):
        if name == "yt_plugin_under_test" or name.startswith("astrbot") or name in {"yt_dlp", "imageio_ffmpeg"}:
            sys.modules.pop(name, None)

    astrbot_api = types.ModuleType("astrbot.api")
    astrbot_api.AstrBotConfig = dict

    class Logger:
        def info(self, *args, **kwargs): pass
        def warning(self, *args, **kwargs): pass
        def error(self, *args, **kwargs): pass
        def debug(self, *args, **kwargs): pass
    astrbot_api.logger = Logger()

    event_mod = types.ModuleType("astrbot.api.event")
    class AstrMessageEvent: pass
    class Filter:
        @staticmethod
        def command(_name):
            def deco(func):
                return func
            return deco
    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.filter = Filter

    comp_mod = types.ModuleType("astrbot.api.message_components")
    class File:
        def __init__(self, name=None, file=None):
            self.name = name
            self.file = file
    class Video:
        def __init__(self, file=None):
            self.file = file
    comp_mod.File = File
    comp_mod.Video = Video

    star_mod = types.ModuleType("astrbot.api.star")
    class Context: pass
    class Star:
        def __init__(self, context=None):
            self.context = context
    def register(*_args, **_kwargs):
        def deco(cls):
            return cls
        return deco
    star_mod.Context = Context
    star_mod.Star = Star
    star_mod.register = register

    sys.modules["astrbot"] = types.ModuleType("astrbot")
    sys.modules["astrbot.api"] = astrbot_api
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.message_components"] = comp_mod
    sys.modules["astrbot.api.star"] = star_mod

    yt_dlp = types.ModuleType("yt_dlp")
    sys.modules["yt_dlp"] = yt_dlp
    imageio_ffmpeg = types.ModuleType("imageio_ffmpeg")
    imageio_ffmpeg.get_ffmpeg_exe = lambda: "ffmpeg"
    sys.modules["imageio_ffmpeg"] = imageio_ffmpeg

    botpy = types.ModuleType("botpy")
    botpy_http = types.ModuleType("botpy.http")
    class Route:
        def __init__(self, method, path, **kwargs):
            self.method = method
            self.path = path
            self.kwargs = kwargs
    botpy_http.Route = Route
    botpy_types = types.ModuleType("botpy.types")
    botpy_types_message = types.ModuleType("botpy.types.message")
    class Media(dict):
        def __init__(self, file_uuid="", file_info="", ttl=0):
            super().__init__(file_uuid=file_uuid, file_info=file_info, ttl=ttl)
            self.file_uuid = file_uuid
            self.file_info = file_info
            self.ttl = ttl
    botpy_types_message.Media = Media
    botpy_types.message = botpy_types_message
    botpy.types = botpy_types
    sys.modules["botpy"] = botpy
    sys.modules["botpy.http"] = botpy_http
    sys.modules["botpy.types"] = botpy_types
    sys.modules["botpy.types.message"] = botpy_types_message

    spec = importlib.util.spec_from_file_location("yt_plugin_under_test", Path(__file__).resolve().parents[1] / "main.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class CoreBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_plugin_module()

    def make_plugin_shell(self):
        plugin = self.mod.YtDlpPlugin.__new__(self.mod.YtDlpPlugin)
        plugin.data_dir = Path(tempfile.mkdtemp())
        plugin.cookie_dir = plugin.data_dir / "cookies"
        plugin.cookie_dir.mkdir(parents=True, exist_ok=True)
        plugin.bilibili_cookies = ""
        plugin.youtube_cookies = ""
        plugin.generic_cookies = ""
        plugin.qq_official_max_size_mb = 200
        plugin.qq_official_video_soft_limit_mb = 30
        plugin.operator_ids = set()
        plugin.allowed_group_openids = set()
        plugin.allowed_platform_instance_ids = set()
        plugin.public_domestic_only = True
        return plugin


    def test_extract_first_url_from_share_text(self):
        text = "这个视频真不错 标题若干 https://www.youtube.com/watch?v=abc123 复制链接"
        self.assertEqual(
            self.mod.YtDlpPlugin._extract_first_url(text),
            "https://www.youtube.com/watch?v=abc123",
        )


    def test_extract_first_url_from_b23_markdown_share_text(self):
        text = "【影   流   之   主-哔哩哔哩】 ++[https://b23.tv/GlngRIZ](https://b23.tv/GlngRIZ)++"
        self.assertEqual(
            self.mod.YtDlpPlugin._extract_first_url(text),
            "https://b23.tv/GlngRIZ",
        )

    def test_extract_first_url_from_markdown_and_unescape(self):
        text = "[标题](https://www.bilibili.com/video/BV1xx/?a=1&amp;b=2)。"
        self.assertEqual(
            self.mod.YtDlpPlugin._extract_first_url(text),
            "https://www.bilibili.com/video/BV1xx/?a=1&b=2",
        )

    def test_normalize_bilibili_url_strips_tracking_query(self):
        raw = "https://www.bilibili.com/video/BV1Cr34zbELk?spm_id_from=333&vd_source=secret"
        self.assertEqual(
            self.mod.YtDlpPlugin._normalize_url(raw),
            "https://www.bilibili.com/video/BV1Cr34zbELk/",
        )


    def test_escaped_netscape_cookie_text_is_written_before_path_probe(self):
        plugin = self.make_plugin_shell()
        plugin.generic_cookies = "# Netscape HTTP Cookie File\n.douyin.com\tTRUE\t/\tFALSE\t0\tttwid\tabc"
        cookiefile, header = plugin._resolve_cookie_for_ytdlp("https://v.douyin.com/abc/")
        self.assertIsNone(header)
        text = Path(cookiefile).read_text()
        self.assertIn(".douyin.com	TRUE", text)

    def test_long_cookie_header_does_not_raise_file_name_too_long(self):
        plugin = self.make_plugin_shell()
        plugin.generic_cookies = "; ".join(f"k{i}=v{i}" for i in range(1200))
        cookiefile, header = plugin._resolve_cookie_for_ytdlp("https://v.douyin.com/abc/")
        self.assertIsNone(cookiefile)
        self.assertIn("k1199=v1199", header)

    def test_cookie_header_config(self):
        plugin = self.make_plugin_shell()
        plugin.bilibili_cookies = "SESSDATA=abc; bili_jct=def"
        cookiefile, header = plugin._resolve_cookie_for_ytdlp("https://www.bilibili.com/video/BV1xx/")
        self.assertIsNone(cookiefile)
        self.assertEqual(header, "SESSDATA=abc; bili_jct=def")

    def test_netscape_cookie_text_is_written_to_private_file(self):
        plugin = self.make_plugin_shell()
        plugin.bilibili_cookies = "# Netscape HTTP Cookie File\n.bilibili.com\tTRUE\t/\tFALSE\t0\tSESSDATA\tabc"
        cookiefile, header = plugin._resolve_cookie_for_ytdlp("https://www.bilibili.com/video/BV1xx/")
        self.assertIsNone(header)
        path = Path(cookiefile)
        self.assertTrue(path.exists())
        self.assertIn("SESSDATA", path.read_text())

    def test_file_hashes(self):
        plugin = self.make_plugin_shell()
        path = plugin.data_dir / "sample.bin"
        data = b"abc" * 100
        path.write_bytes(data)
        md5, sha1, md5_10m = plugin._file_hashes(path)
        self.assertEqual(md5, hashlib.md5(data).hexdigest())
        self.assertEqual(sha1, hashlib.sha1(data).hexdigest())
        self.assertEqual(md5_10m, hashlib.md5(data).hexdigest())

    def test_qq_official_video_over_soft_limit_falls_back_to_file(self):
        plugin = self.make_plugin_shell()
        path = plugin.data_dir / "big.mp4"
        path.write_bytes(b"0" * (31 * 1024 * 1024))
        calls = {}

        async def fake_upload(event, upload_path, *, file_type, file_name):
            calls["file_type"] = file_type
            calls["file_name"] = file_name
            return {"file_info": "mock", "file_uuid": "uuid", "ttl": 300}
        plugin._upload_qq_official_by_chunks = fake_upload
        plugin._qq_official_target = lambda event: ("group", "group_openid")

        async def fake_qq_api_request(event, method, route_path, route_kwargs, payload, **kwargs):
            calls["method"] = method
            calls["route_path"] = route_path
            calls["route_kwargs"] = route_kwargs
            calls["payload"] = payload
            return {"id": "msg"}
        plugin._qq_api_request = fake_qq_api_request
        event = types.SimpleNamespace(
            bot=types.SimpleNamespace(api=types.SimpleNamespace()),
            message_obj=types.SimpleNamespace(message_id="mid"),
        )
        sent_as = asyncio.run(plugin._send_qq_official_media(event, path, title="t", as_file=False))
        self.assertEqual(sent_as, "file")
        self.assertEqual(calls["file_type"], 4)
        self.assertEqual(calls["payload"]["msg_type"], 7)


    def test_chunk_upload_reads_local_file_sequentially_when_api_index_is_one_based(self):
        plugin = self.make_plugin_shell()
        block = 10 * 1024 * 1024
        tail = 6_938_291
        path = plugin.data_dir / "one_based_parts.mp4"
        path.write_bytes(b"a" * block + b"b" * block + b"c" * tail)
        uploaded_sizes = []
        finish_payloads = []

        plugin._qq_official_target = lambda event: ("group", "group_openid")

        async def fake_put(session, url, data, *, retry_timeout, retry_delay):
            uploaded_sizes.append(len(data))

        async def fake_api(event, method, route_path, route_kwargs, payload, **kwargs):
            if route_path.endswith("/upload_prepare"):
                return {
                    "upload_id": "upload_1",
                    "block_size": str(block),
                    # 线上 QQ 可能返回 1-based index；本地读取不能用 index*block_size。
                    "parts": [
                        {"index": 1, "presigned_url": "https://cos/1", "block_size": str(block)},
                        {"index": 2, "presigned_url": "https://cos/2", "block_size": str(block)},
                        {"index": 3, "presigned_url": "https://cos/3", "block_size": str(tail)},
                    ],
                    "upload_config": {"retry_timeout": 1, "retry_delay": 1},
                }
            if route_path.endswith("/upload_part_finish"):
                finish_payloads.append(payload)
                return {}
            if route_path.endswith("/files"):
                return {"file_uuid": "uuid", "file_info": "info", "ttl": 300}
            raise AssertionError(route_path)

        plugin._qq_put_upload_part = fake_put
        plugin._qq_api_request = fake_api
        media = asyncio.run(
            plugin._upload_qq_official_by_chunks(
                types.SimpleNamespace(), path, file_type=4, file_name="video.mp4"
            )
        )
        self.assertEqual(uploaded_sizes, [block, block, tail])
        self.assertEqual([item["part_index"] for item in finish_payloads], [1, 2, 3])
        self.assertEqual(media.file_info, "info")


    def make_event_shell(self, *, sender="user", group="group", platform="default_1", admin=False):
        class Event:
            def get_sender_id(self): return sender
            def get_group_id(self): return group
            def get_platform_id(self): return platform
            def is_admin(self): return admin
            def plain_result(self, text): return text
        return Event()

    def test_public_domestic_mode_blocks_foreign_for_unprivileged_user(self):
        plugin = self.make_plugin_shell()
        event = self.make_event_shell()
        denial = plugin._deny_download_access(
            event,
            "公开下载模式仅支持 B站/抖音/微博等国内平台；YouTube、X/Twitter、Pornhub 等国外或高风险站点仅限管理员、字幕组操作员或白名单群使用。",
        )
        self.assertIn("公开下载模式仅支持", denial)
        self.assertIn("当前群 group_openid：group", denial)

    def test_operator_bypasses_public_domestic_mode(self):
        plugin = self.make_plugin_shell()
        plugin.operator_ids = {"op"}
        event = self.make_event_shell(sender="op")
        self.assertTrue(plugin._has_privileged_download_access(event))

    def test_group_allowlist_bypasses_public_domestic_mode(self):
        plugin = self.make_plugin_shell()
        plugin.allowed_group_openids = {"group"}
        event = self.make_event_shell(group="group")
        self.assertTrue(plugin._has_privileged_download_access(event))

if __name__ == "__main__":
    unittest.main()
