---
name: video-transcript
description: 从视频链接提取口播文案，默认生成 txt 文件。用户发来 B站、抖音、快手、小红书、视频号链接（或本地音视频文件）并希望得到文案、字幕、逐字稿或转写时使用。完全独立运行：本机解析优先，失败自动走兜底解析接口；下载音轨后用本地 SenseVoice 识别，音频用完即删，不上传、不写库。
---

# 视频文案提取

把视频链接变成可阅读的口播文案。整个流程在本机完成，不需要任何宿主程序或云服务。

这是一个**独立技能**：目录内自带全部业务代码，拷到任何机器上，装好 Python 依赖和 FFmpeg 就能运行。

## 准备

要求 Python 3.10+ 和 FFmpeg；FFprobe 只在本机音视频文件的标题/时长探测时用到，不影响链接提取。

```bash
cd <技能目录>
python -m pip install -r requirements.txt
```

`pip` 不在 PATH 时用 `python -m pip`；Windows 上也可以写 `py -3 -m pip ...`。

FFmpeg 三种放法任选其一：装到系统 PATH；把 `ffmpeg.exe`、`ffprobe.exe` 放进 `vendor/ffmpeg/`；或用 `ffmpeg_dir` / `VTS_FFMPEG_DIR` 指定目录。

Windows 上官网 / gyan.dev 可能很慢，可以直接从 npm 国内镜像取（Git Bash）：

```bash
cd <技能目录>
mkdir -p vendor/ffmpeg
tmp=$(mktemp -d)
curl -sL --max-time 180 "https://registry.npmmirror.com/@ffmpeg-installer/win32-x64/-/win32-x64-4.1.0.tgz" -o "$tmp/ffmpeg.tgz"
curl -sL --max-time 180 "https://registry.npmmirror.com/@ffprobe-installer/win32-x64/-/win32-x64-5.1.0.tgz" -o "$tmp/ffprobe.tgz"
tar -xzf "$tmp/ffmpeg.tgz" -C "$tmp" && cp "$tmp/package/ffmpeg.exe" vendor/ffmpeg/ && rm -rf "$tmp/package"
tar -xzf "$tmp/ffprobe.tgz" -C "$tmp" && cp "$tmp/package/ffprobe.exe" vendor/ffmpeg/
rm -rf "$tmp"
```

`5.1.0` 是当前可用的 ffprobe npm 版本（`5.2.0` 的包不存在）；以后版本变化时，先查 `https://registry.npmmirror.com/@ffprobe-installer/win32-x64` 的 `dist-tags`。

首次使用检查环境：

```bash
python scripts/extract.py --check
```

下载识别模型（SenseVoice，约 230 MB，只需一次）：

```bash
python scripts/extract.py --install-model
# 等价写法：python scripts/extract.py --install-model sensevoice-small
```

模型默认存到 `~/.video-transcript-skill/models/`，可用 `models_dir` 或 `VTS_MODELS_DIR` 修改。

## 配置兜底解析（可选）

本机解析失败时才会用到兜底接口。复制配置模板：

```bash
cp config.example.json config.json
```

在 `config.json` 里填写：

```json
{
  "fallback_api_key": "你的 API Key",
  "output_dir": "./output",
  "cookies": {
    "douyin": "D:/cookies/douyin.txt"
  }
}
```

也可以全部用环境变量：

| 变量 | 作用 |
| --- | --- |
| `VTS_FALLBACK_API_KEY` | 兜底解析 API Key |
| `VTS_FALLBACK_BASE` | 兜底网关地址，默认 `https://api-new.ifphp.com` |
| `VTS_MODELS_DIR` | 模型目录 |
| `VTS_FFMPEG_DIR` | FFmpeg 所在目录 |
| `VTS_COOKIE_FILE` | 全局 Netscape cookies.txt |
| `VTS_OUTPUT_DIR` | 默认的文案输出目录 |
| `VTS_CONFIG_FILE` | 指定另一个 config.json |

没有兜底 Key 也能用：B站、快手、小红书的公开作品通常可以本机解析；视频号必须有 Key；抖音在需要登录态时也需要 Cookie 或 Key。

`config.json` 里是明文 API Key 和 Cookie 路径：不要提交到仓库、不要贴到对话里。本仓库已用 `.gitignore` 忽略它；检查配置时只确认字段是否存在，不要 `cat` 整个文件。

### 没有兜底 Key？按这几步注册

兜底接口由 **BugPk-Api** 提供，默认网关是 `https://api-new.ifphp.com`。站点标注为公益免费，视频号/短视频解析接口在接口市场里也是免费（以页面实际显示为准）。

> Agent 注意：用户说“没有 Key / 不知道怎么弄”时，直接把下面几步发给他，不要只回一句“配置 fallback_api_key”。不要索要账号密码。

1. 打开注册页：<https://api-new.ifphp.com/auth/register>
   打不开就先打开 <https://api-new.ifphp.com>，点右上角「登录 / 注册」→「创建账号」。
2. 填写用户名、邮箱；点「获取邮箱验证码」，按提示完成滑块/人机验证，填入邮箱收到的 6 位验证码；设置密码并确认（至少 8 位，字母、数字、符号混合）；勾选隐私政策，点「注册」。
3. 登录后，左侧菜单点「密钥管理」→「新增密钥」。名称随便填；密钥类型优先选「全站接口」；如果只能选单个接口，至少把「微信视频号解析」「短视频解析聚合」选上，B站/抖音/快手也走兜底时再把对应接口一起选上。
4. 创建后复制完整密钥（列表里可点小眼睛查看），发给 Agent。只需要 API Key，不要发账号密码。
5. Agent 收到后写入 `config.json` 的 `fallback_api_key`；也可以设置环境变量 `VTS_FALLBACK_API_KEY`。

接口市场：<https://api-new.ifphp.com/apilist/>（可看到「微信视频号解析」「短视频解析聚合」等接口）。

如果不想注册，视频号也可以直接让用户发本地视频/音频文件，用本地 SenseVoice 转写。

可以直接发给用户的话术：

> 视频号解析需要一个兜底 API Key，注册是免费的：
> 1. 打开 https://api-new.ifphp.com/auth/register
> 2. 用邮箱注册并登录
> 3. 左侧「密钥管理」→「新增密钥」，复制完整 Key 发给我
>
> 只需要发 Key，不要发账号密码。如果不想注册，也可以把视频/音频文件发给我，我本地转写。

## 运行

Agent 执行顺序：

1. 先跑 `--check`；`ok` 不为 `true` 时按 `hint` 补齐依赖、FFmpeg 或模型。
2. 首次使用先跑 `--install-model`，模型只需装一次。
3. 再执行下面的提取命令；成功时默认生成 txt。
4. 把 `output_path` 和文案返回给用户；失败时按错误码处理，不要编造文案。

```bash
python scripts/extract.py "<链接或本地文件路径>"
```

默认会做两件事：

1. 在**当前目录**的 `output/` 下生成 `<视频标题>.txt`，同名时自动加 `-2`、`-3`，不覆盖已有文件；
2. 把「标题 + 元信息 + 正文」打印到 stdout，方便直接转述。

换个落盘位置，用 `-o`，或在配置里设 `output_dir` / 环境变量 `VTS_OUTPUT_DIR`：

```bash
python scripts/extract.py "<链接>" -o "./文案/我的文案.txt"
python scripts/extract.py "<链接>" -o "./文案/"          # 按视频标题命名
python scripts/extract.py "<链接>" --no-output           # 不写文件，只打印
```

常用参数：

- `-o, --output PATH`：指定输出文件或目录。路径没有扩展名时自动补 `.txt`；给一个目录则按视频标题命名。
- `--no-output`：不生成文件，只打印。此时默认 JSON；要纯文本加 `--format text`。
- `--format text|json`：显式指定格式。默认落 `.txt` 时走 text；指定 `json` 时默认文件也变成 `.json`。
- `--plain`：text 输出和 txt 文件只保留正文，不带标题、作者、来源等头部信息。
- `--segments`：JSON 中附带 `segments`（`start_ms` / `end_ms` / `text`）。
- `--platform bilibili|douyin|kuaishou|xiaohongshu|wechat`：手动指定平台。
- `--cookie-file D:/cookies/xxx.txt`：本次使用指定 Cookie。
- `--keep-audio`：保留临时音轨，仅用于排查；正常不要加。

成功时 JSON / 文本结果里会包含 `output_path`，文本输出的头部也会有一行「文件：」。

## 结果怎么给用户

- 每次成功提取都会生成 txt，默认在 `./output/<标题>.txt`。把 `output_path`（文本输出里的「文件：」行，或 JSON 字段，或 stderr 的「文案已保存」）告诉用户。
- 只想看 JSON、但用户要的是 txt：不要用 `--format json`，它会把默认文件也写成 `.json`。默认命令的 stderr 里已经有「文案已保存：...txt」；需要结构化字段时用 `--no-output --format json`，或另用 `-o result.json` 存一份 JSON。
- `--plain` 时 stdout 只有正文；文件路径从 stderr 的「文案已保存」读取。
- 成功时把 `title`、`uploader`、`duration_seconds`、`transcript` 整理后发给用户；正文原样转述，不要自行润色或补全。
- `resolved_by` 为 `fallback` 时说明这次用了兜底接口，不影响文案内容。
- 失败时退出码非 0，stdout 是 `{"ok": false, "code": "...", "message": "..."}`。把 `message` 的意思转告用户，并按下面的表给下一步建议。不要编造文案。

## 失败码与处理

| code | 含义 | 建议 |
| --- | --- | --- |
| `DEPENDENCY_MISSING` | Python 依赖没装 | `python -m pip install -r requirements.txt` |
| `MODEL_ERROR` | 识别模型缺失/不完整 | `--install-model` |
| `MEDIA_TOOL_ERROR` | FFmpeg 缺失或转码失败 | 检查 PATH 或 `vendor/ffmpeg/` |
| `COOKIE_REQUIRED` / `COOKIE_INVALID` | 平台需要登录态 | 导出 Netscape cookies.txt 并在配置里按平台指定 |
| `FALLBACK_KEY_MISSING` | 视频号或本机解析失败，且没有兜底 Key | 按上面的「没有兜底 Key？」引导注册；视频号也可以让用户直接发本地视频/音频 |
| `FALLBACK_ERROR` | 兜底接口拒绝/不可用 | 视频号先确认链接已展开为 `channels.weixin.qq.com/...`；再检查 Key 额度、作品是否失效，稍后重试 |
| `UNSUPPORTED_SOURCE` | 平台不支持或作品是图文 | 换公开视频链接，或改用本地文件 |
| `NO_SPEECH` | 没有识别到人声 | 确认视频有口播；纯音乐/环境音无法转写 |
| `PROCESSING_ERROR` | 其他处理失败 | 加 `--verbose` 重跑一次，查看 stderr 末尾 |

## 内部流程

1. 从输入中取出链接，识别平台。
2. 本机解析：B站/抖音用 yt-dlp；快手、小红书用移动端分享页；视频号直接用兜底接口。
3. 本机解析失败且配置了 Key → 兜底解析接口。
4. 下载体积最小的可用音轨；下载失败再尝试兜底直链。
5. FFmpeg 转 16 kHz 单声道 WAV。
6. SenseVoice 本地识别，繁简统一。
7. 删除临时目录里的视频与音频。

技能不上传音视频、不写任何数据库、不保留音频；只按默认设置（或 `-o`）落一份 txt 文案文件。重复提取同一条链接不会留下记录。

## 平台说明

- **B站**：yt-dlp 解析；无字幕需求，直接识别音轨。
- **抖音**：yt-dlp 解析；风控较严，必要时配置 Cookie 或兜底 Key。
- **快手**：解析移动端分享页，无需登录。
- **小红书**：解析移动端分享页；遇到风控时配置 Cookie，仍失败则走兜底。
- **视频号**：没有本机解析方案，必须配置兜底 Key。`https://weixin.qq.com/sph/...` 分享短链会自动跟随 301 换成 `channels.weixin.qq.com/finder-preview/pages/sph?id=...` 后再交给兜底接口；也可以直接传展开后的地址。
