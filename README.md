# 🎬 AstrBot 全能视频下载插件 (Video Downloader)

一个基于 `yt-dlp` 的单视频解析、下载与发送插件。面向 AstrBot v4.26.x，重点兼容 **QQ 官方 Bot**。

## ✨ 功能特色

- **多平台解析**：基于 `yt-dlp`，支持 Bilibili、YouTube、Twitter/X、TikTok 等站点的单视频链接。
- **QQ 官方 Bot 本地富媒体上传**：在 `qq_official` 平台下，插件不再把本地视频交给 AstrBot 适配器做 base64 JSON 上传，而是按 QQ 官方富媒体文档走 **分片上传 → 合并 file_info → 发送 1 条 msg_type=7 富媒体消息**，避免 18MB 级别文件触发网关 413。
- **视频/文件自动选择**：`/video` 下载结果不超过 QQ 官方视频软限制时按视频消息发送；超过软限制但不超过硬限制时按文件卡片发送。
- **B站 URL 清洗与 Cookies**：自动清理 Bilibili `spm_id_from` / `trackid` / `vd_source` 等跟踪参数；支持在配置中粘贴 cookies.txt、Cookie 请求头，或填写 cookies 文件路径。
- **单次结果输出**：成功只发送目标媒体；失败只返回一条错误文本，并标出失败阶段，节省 QQ 官方 Bot 消息频次。

## 指令

```text
/video <单视频URL>     下载并发送。QQ官方下：≤软限制按视频，>软限制按文件。
/download <单视频URL>  下载并按文件发送。
```

## ⚙️ 关键配置

在 AstrBot 插件配置页调整 `_conf_schema.json` 暴露的字段即可。

### 下载与 QQ 官方限制

- `download.max_quality`：最高画质，默认 `1080p`。
- `download.max_size_mb`：非 QQ 官方平台的下载上限，默认 `100`。
- `download.qq_official_max_size_mb`：QQ 官方富媒体硬上限，默认 `200`，并在代码中封顶 200。
- `download.qq_official_video_soft_limit_mb`：QQ 官方视频软限制，默认 `30`。
  - `/video` 结果 `<= 30MB`：按视频消息发。
  - `/video` 结果 `> 30MB 且 <= 200MB`：按文件卡片发。
  - `> 200MB`：拒绝，不上传。

> 说明：这里的“分片上传”只是上传链路分块，最终 QQ 里仍然只出现一条富媒体消息，不会刷屏。

### Cookies

`cookies.bilibili` / `cookies.youtube` / `cookies.generic` 支持三种写法：

1. 服务器上的 cookies.txt 文件路径：

```text
/data/cookies/bilibili.txt
```

2. 直接粘贴 Netscape cookies.txt 全文：

```text
# Netscape HTTP Cookie File
.bilibili.com	TRUE	/	FALSE	0	SESSDATA	xxxx
```

3. 直接粘贴浏览器请求头里的 Cookie：

```text
SESSDATA=xxxx; bili_jct=yyyy; DedeUserID=zzzz
```

Bilibili 412 通常是风控/登录态/指纹问题；配置 cookies 能提高成功率，但不能保证绕过所有风控。

## QQ 官方上传流程

插件在 `qq_official` 下对本地文件使用官方推荐流程：

1. `POST /v2/groups/{group_id}/upload_prepare` 或 `POST /v2/users/{user_id}/upload_prepare`
2. 按返回的 `block_size` 把文件分片，逐片 `PUT` 到 `presigned_url`
3. 每片完成后调用 `upload_part_finish`
4. 全部完成后调用 `/files` 合并，拿到 `file_info`
5. 发送 `msg_type=7`，`media.file_info = file_info`

插件不会再让 AstrBot 当前 qq_official 适配器读取本地视频并转 base64，因此可避开 base64 JSON 请求体过大的 413 问题。

## 排错

失败消息会包含阶段，例如：

- `解析视频信息阶段失败`：通常是 yt-dlp、URL、cookies 或站点风控问题。
- `下载与封装阶段失败`：通常是格式、FFmpeg、磁盘或大小限制问题。
- `QQ官方分片上传与发送阶段失败`：通常是 QQ 上传接口、机器人权限、群/用户 OpenID、富媒体格式或容量限制问题。

如果遇到 B站：

```text
HTTP Error 412: Precondition Failed
```

建议：

1. 更新 `yt-dlp` 到较新版本；
2. 清洗 URL，仅保留 `https://www.bilibili.com/video/BV.../`；插件会自动做一次清洗；
3. 在 `cookies.bilibili` 配置 B站 cookies；
4. 稍后重试或更换出口网络。
