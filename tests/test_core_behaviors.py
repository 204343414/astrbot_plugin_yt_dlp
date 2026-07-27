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
        return plugin

    def test_normalize_bilibili_url_strips_tracking_query(self):
        raw = "https://www.bilibili.com/video/BV1Cr34zbELk?spm_id_from=333&vd_source=secret"
        self.assertEqual(
            self.mod.YtDlpPlugin._normalize_url(raw),
            "https://www.bilibili.com/video/BV1Cr34zbELk/",
        )

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


if __name__ == "__main__":
    unittest.main()
