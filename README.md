# 🎬 AstrBot 全能视频下载插件 (Video Downloader)

一个基于 `yt-dlp` 的单视频解析、下载与发送插件。面向 AstrBot v4.26.x，重点兼容 **QQ 官方 Bot**。

## ✨ 功能特色

- **多平台解析**：基于 `yt-dlp`，支持 Bilibili、YouTube、Twitter/X、TikTok 等站点的单视频链接。
- **QQ 官方 Bot 本地富媒体上传**：在 `qq_official` 平台下，插件不再把本地视频交给 AstrBot 适配器做 base64 JSON 上传，而是按 QQ 官方富媒体文档走 **分片上传 → 合并 file_info → 发送 1 条 msg_type=7 富媒体消息**，避免 18MB 级别文件触发网关 413。
- **视频/文件自动选择**：`/video` 下载结果不超过 QQ 官方视频软限制时按视频消息发送；超过软限制但不超过硬限制时按文件卡片发送。
- **B站 URL 清洗与 Cookies**：自动清理 Bilibili `spm_id_from` / `trackid` / `vd_source` 等跟踪参数；支持在配置中粘贴 cookies.txt、Cookie 请求头，或填写 cookies 文件路径。
- **单次结果输出**：成功只发送目标媒体；失败只返回一条错误文本，并标出失败阶段，节省 QQ 官方 Bot 消息频次。
- **yt-dlp 失败自检更新**：解析或下载失败时检查 PyPI 最新稳定版；发现旧版会自动升级，并明确提示重启 AstrBot 后重试。检测有冷却时间，不会因连续失败反复运行 `pip`。

## 指令

```text
/video <单视频URL>     下载并发送。QQ官方下：≤软限制按视频，>软限制按文件。
/download <单视频URL>  下载并按文件发送。
```

## ⚙️ 关键配置

在 AstrBot 插件配置页调整 `_conf_schema.json` 暴露的字段即可。

### yt-dlp 依赖自动更新

- `dependency.auto_update_yt_dlp_on_error`：默认开启。仅在“解析视频信息”或“下载与封装”阶段失败时检查版本；QQ 上传失败不会触发依赖更新。
- `dependency.update_check_interval_minutes`：版本检测冷却时间，默认 `30` 分钟。
- 如果检测到旧版，插件会在 AstrBot 当前 Python 环境中执行等价于 `python -m pip install -U yt-dlp` 的升级。
- **升级后要重启整个 AstrBot 进程**。只热重载插件可能继续使用内存中已导入的旧版 `yt_dlp`，插件不会冒险混合重载新旧子模块。
- 若运行环境只读、权限不足或由系统包管理器托管，自动升级会保留原始下载错误并提示手动升级，不会掩盖真正的失败原因。

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

如果遇到：

```text
ERROR: unable to download video data: HTTP Error 403: Forbidden
```

这表示 `yt-dlp` 已经解析出媒体地址，但源站在实际取视频数据时拒绝了请求；它发生在 QQ 官方 Bot 上传之前，因此通常不是 QQ 上传接口导致。插件会先检查 `yt-dlp` 是否落后：

- 若自动升级成功：重启 AstrBot 后再试；
- 若已经是 PyPI 最新稳定版：优先检查目标站 Cookies/登录态、服务器出口 IP、代理一致性和站点临时风控，稍后重试；
- 只有某一个视频失败时，也可能是该媒体签名地址已失效、地区限制或视频本身受限。

如果遇到 B站：

```text
HTTP Error 412: Precondition Failed
```

建议：

1. 更新 `yt-dlp` 到较新版本；
2. 清洗 URL，仅保留 `https://www.bilibili.com/video/BV.../`；插件会自动做一次清洗；
3. 在 `cookies.bilibili` 配置 B站 cookies；
4. 稍后重试或更换出口网络。

## 访问控制与审核

### 公开国内平台模式

`access_control.public_domestic_only` 默认开启。开启后：

- 普通用户可以下载国内平台，例如 Bilibili、抖音、微博、小红书等；
- YouTube、X/Twitter、Pornhub 等国外或高风险站点，以及未知的非国内站点，只允许 AstrBot 管理员、`operator_openids`、`allowed_group_openids` 或 `allowed_qqofficial_instance_ids` 命中的上下文使用；
- 字幕组操作员、管理员和白名单群不受公开模式限制。

如果关闭 `public_domestic_only`，则所有下载都要求管理员、操作员或白名单上下文。

### 国外视频审核

`moderation.enable_content_review` 默认关闭。关闭时不会调用审核模型。

只有同时满足以下条件时才会在下载国外视频前审核标题、描述和封面：

1. `moderation.enable_content_review = true`
2. `moderation.provider_id` 选择了可用多模态 Provider
3. 当前下载者是管理员、操作员或白名单上下文
4. 视频来源不是国内平台

`provider_id` 留空会被视为未开启审核，不会默认拒绝下载。

## 分享文案自动提取 URL

`/video` 和 `/download` 使用贪婪参数读取整段分享文案，会自动提取第一条 `http://` 或 `https://` 链接。例如下面这些都可以：

```text
/download 这个视频标题 https://www.youtube.com/watch?v=xxxx 复制链接
/video [标题](https://www.bilibili.com/video/BVxxxx/?spm_id_from=...)
```

插件只把提取到的第一个 URL 交给 yt-dlp；Bilibili URL 仍会继续自动清洗跟踪参数。

## 文件转 QQ 官方视频媒体

新增：

```text
/转视频
```

用法：在 QQ 官方群聊中附带一个 mp4 文件/视频，或回复一条 mp4 文件/视频后发送 `/转视频`。插件会把本地附件走 QQ 官方富媒体分片上传，并以 `file_type=2` 视频媒体消息发回当前群。

限制：

- 仅 QQ 官方群聊；
- 仅 AstrBot 管理员、字幕组操作员、白名单群或白名单实例；
- 仅 mp4；
- 超过 `media_relay.max_video_mb` 拒绝，且该值封顶 30MB；
- 不走群文件。适合某些不允许发群文件但允许发视频媒体的群。
