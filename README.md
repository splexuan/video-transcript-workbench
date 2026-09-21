# 文案工作台

一个面向个人创作者的 Windows 本地视频文案提取工作台：把视频链接或本地音视频转成可编辑、可导出的文案。

## 下载使用

到 [Releases](https://github.com/splexuan/video-transcript-workbench/releases/latest) 下载最新的 `video-transcript-workbench-*-win64.zip`，解压后双击目录里的 `文案工作台.exe`。

- **免安装**：压缩包内已包含 Python 运行时与 FFmpeg，不需要另外装环境；也不需要管理员权限
- **首次使用**：到「模型管理」页下载一个语音识别模型（数百 MB 到 1 GB，取决于所选模型，仅需一次）；要提取平台视频的话，再去「平台连接」配置对应平台的登录态
- **隐私**：音视频、Cookie、文案全部留在本机，不上传任何服务器
- 运行环境：Windows 10 / 11 64 位

想从源码构建或参与开发，看下面的「开发启动」。

## 支持的能力

- 本地音视频上传、FFmpeg 音轨转换和 SenseVoice 本地识别。
- B站字幕优先：先走平台字幕接口（含未登录也能拿到的 AI 字幕），拿不到再下载音轨、用所选模型识别；配置访问凭据后还能读取会员与登录可见视频的字幕。
- 持久化单机任务队列、进度、取消和失败信息。
- 批量链接提取：一次粘贴最多 50 条链接或分享文案，提交前检查重复/不支持项；任务页按批次汇总进度，并支持暂停、继续、取消与失败项重试。
- 文案分段校对，以及 TXT、SRT、VTT、JSON 导出。
- 提取结果一并保留来源信息：标题、作者、时长、作品介绍与封面图（封面会另存到本机，平台给的临时地址过期后仍能显示）。
- 原始音视频默认在处理完成后清理；可在编辑页或「设置」里开启保留，保留后能在编辑页直接对照收听。
- 独立识别模型管理：多源断点续传下载、校验与删除。
- 首页直接选识别模型（SenseVoice Small / faster-whisper small / medium），并可开关「优先使用平台字幕」。
- 精准时间轴模式：faster-whisper 句级时间戳，可直接生成 SRT。
- 识别只用 CPU，不依赖 CUDA / cuDNN，装完即用、没有显卡门槛：「极速文本」把 30 秒音频块并行识别（块数随逻辑核自动伸缩），「精准时间轴」用满全部逻辑核。
- 中文转写统一输出简体：Whisper 系列常输出繁体，落库前会做一次繁转简（只映射字形，不动用词和标点）。
- 抖音链接：导入一次 Cookie 后，粘贴公开作品链接即可提取文案（抖音没有可读取的字幕，走音轨识别）。
- 小红书链接：公开视频**不需要登录**——程序用移动端 UA 读取分享页拿视频地址（桌面 UA 会被要求登录）；命中风控时自动改用本机浏览器打开页面取数据；配置 Cookie 或扫码登录还能覆盖登录可见内容。
- 快手链接：粘贴分享链接或分享文案即可提取，**不需要登录、也不需要 Cookie**——程序用移动端 UA 读取平台分享页，页面里直接内嵌视频地址。yt-dlp 没有快手 extractor、站内数据接口又带签名风控，所以这条链路走的是平台自己的分享页（详见下文说明）。
- **兜底解析**：本机解析失败时（多为平台风控），配了第三方聚合解析接口的 Key 就自动改用它取无水印直链继续识别；没配则保持原样报错，不会多发任何请求。网关地址已内置 `https://api-new.ifphp.com`，去 <https://api-new.ifphp.com/> 注册账号拿到 Key 填进「设置」即可（详见「兜底解析接口」）。
- **AI 总结**：编辑页与文案预览弹窗（工作台 / 文案库 / 任务队列共用）都有一键入口，把文案与总结指令带到 DeepSeek —— 正常情况下用 URL 参数**直接填进它的输入框**，文案过长（超 URL 长度上限）时改为复制到剪贴板并提示粘贴。总结在 DeepSeek 侧完成，本机不做任何 AI 处理，也不保存第三方返回结果。
- 视频号链接：本机没有可用的解析方案，粘贴分享链接后由兜底解析接口提取（需要先在「设置」页填好它的 API Key）；未配置时会明确提示去配置，而不是当成「暂不支持」。

## 开发启动

后端需要 **Python 3.11 或 3.12**（3.13 起装不上「精准时间轴」的推理引擎，见「已知环境约束」）：

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m uvicorn app.main:app --reload --port 8765
```

前端（热更新，改完不用重新构建）：

```powershell
cd frontend
npm install
npm run dev
```

打开 <http://localhost:5173> 使用：Vite 开发服务器带热更新，`/api` 已代理到 `http://127.0.0.1:8765`。后端的 `--reload` 会在 `.py` 文件变化时自动重启。

注意 `--reload` 只监听 `.py` 文件：改了 `VTW_*` 环境变量或新装了依赖，仍然需要重启进程。

依赖默认走国内镜像（`backend\pip.ini`：清华源为主，阿里、腾讯为备），无需额外配置。

> 若在受限环境（如自动化沙箱）中安装，需要先清空 `PYTHONPATH`，否则注入的 `sitecustomize.py` 会拦截 pip 的删除操作导致安装中断：
> `.\.venv\Scripts\python -m pip install -e ".[dev]"` 前先执行 `$env:PYTHONPATH=""`。

需要「精准时间轴」模式时，额外安装可选引擎：

```powershell
cd backend
.\.venv\Scripts\python -m pip install -e ".[accurate]"
```

前端：

```powershell
cd frontend
npm install
npm run dev
```

打开 `http://127.0.0.1:5173`。

日常使用直接双击根目录的 `start.bat`。启动器会等待本地服务就绪后自动打开浏览器；关闭启动窗口或按 `Ctrl+C` 即可停止服务。

## 打包成 exe

**一键构建**：双击根目录的 `build.bat`，它会依次更新代码（`git pull --ff-only`）、构建前端、检查打包依赖、关闭正在运行的旧版本、再跑 PyInstaller。只想要前端产物就用 `build.bat frontend`；从命令行或自动化脚本里调用时可先设 `VTW_NO_PAUSE=1` 跳过结束时的等待。

手动分步执行：

```powershell
cd frontend
npm run build
cd ..\backend
.\.venv\Scripts\python -m pip install -e ".[dev,accurate]"
.\.venv\Scripts\python -m PyInstaller 文案工作台.spec --noconfirm
```

打包前请确认 `.[dev,accurate]` 装好：`accurate` 里的 faster-whisper / ctranslate2 是「精准时间轴」的引擎，缺了打包会提示「产物里不会有对应能力」（这是提示而非报错，容易漏看）。

产物在 `backend\dist\文案工作台\`，双击其中的 `文案工作台.exe` 即可运行，整个目录拷给别人就能用（首次使用仍需下载识别模型）。

打包版**不会弹出终端窗口**，而是常驻**系统托盘**：

- 托盘菜单：**打开界面**（左键单击也是它）、**打开日志目录**、**退出**。关掉浏览器页面不影响服务。
- 重复双击只会把已运行实例的界面打开，不会起第二个进程、第二个托盘图标。
- 没有控制台，运行日志写在 `<数据目录>\logs\app.log`（超过 2 MB 自动留一代备份），排查问题从这里看。
- 要边看实时输出边调试，用 `文案工作台.exe --no-tray`（前台运行），或开发时跑 `python run.py --no-tray`。
- 开发用的 `start.bat` 仍是有窗口的前台模式（`launcher.py`），方便看日志与 Ctrl+C 停止。

> 在受限环境（如自动化沙箱）里打包前先清空 `PYTHONPATH`：注入的 `sitecustomize.py` 会拦截 PyInstaller 清理旧 `dist` 目录，报 `AttributeError: 'NoneType' object has no attribute 'strip'`。
> ```powershell
> $env:PYTHONPATH=""
> ```

- **自带 FFmpeg**：打包时会把 `ffmpeg.exe` 与 `ffprobe.exe` 一起打进产物，用户不需要另外安装。来源优先取 `backend\vendor\ffmpeg\`（想固定版本就放这里，该目录不入库），没有就回退到打包机 `PATH` 上的同名工具，两者都没有时打包会给出提示。
- **运行时查找顺序**：`VTW_FFMPEG_DIR` 环境变量指定的目录 → 打包资源目录 → exe 同目录 → `PATH`。所以用户也可以自己把 `ffmpeg.exe` 放到 exe 旁边或加进 `PATH`。
- **授权提醒**：随包分发 FFmpeg 前请确认其授权。gyan.dev 的 essentials 构建是 GPL；需要 LGPL 时，把 LGPL 构建的 `ffmpeg.exe`/`ffprobe.exe` 放进 `backend\vendor\ffmpeg\` 再打包即可。
- 显卡加速的 CUDA 运行库（约 1.3 GB）不打进产物，第一次用显卡时由程序按需下载。
- 应用图标在 `assets\`：`icon.svg` 是矢量源文件，`icon.ico`（打包用）与 `icon.png` 由 `python assets\build_icon.py` 导出（需要 Pillow）；前端页签用的是同一份图形的 `frontend\public\favicon.svg`，改图标时两处一起同步。

## 识别模型

模型统一安装在 `%LOCALAPPDATA%\VideoTranscriptWorkbench\models\<模型 id>`，可以在「模型管理」页下载、校验和删除。模型只能由工作台从官方与镜像源下载，不读取本机其它位置的模型文件，识别也只使用这个目录。

模型管理相关接口：`GET /api/models` 查看全部模型状态，`POST /api/models/{id}/download` 下载，`DELETE /api/models/{id}` 删除。

## 平台访问授权

抖音的凭据是**必需**的（没有它无法解析）；B站与小红书是**可选**的（公开内容不配置也能解析，配置后能读会员与登录可见内容）；**快手不需要凭据**。凭据都在「平台连接」页完成：

1. **一键获取访问权限（推荐）**：点一下，程序用本机已安装的 Chrome 或 Edge 打开平台首页，浏览器自行生成游客 Cookie，关键字段齐全会立刻保存并关闭窗口（最长等 3 分钟）。全程**无需登录、无需任何操作**。
2. **浏览器登录（可选）**：只有登录后才能观看的内容，点「用浏览器登录」，在弹出的窗口里扫码即可。
3. **手动粘贴（兜底）**：没有 Chrome/Edge 时，按 `F12` → Network → 复制请求头里的 `Cookie` 整行粘贴进来。

程序不读取浏览器的 Cookie 数据库（Chrome/Edge 127+ 已加密，且运行时被占用），而是用专用资料目录启动独立浏览器实例，通过 DevTools 协议取回凭据。该资料目录会保留，登录态与游客身份下次可直接复用。

授权信息用 Windows DPAPI 加密保存在 `%LOCALAPPDATA%\VideoTranscriptWorkbench\credentials\`，只有当前 Windows 用户能解密；接口只返回配置状态，不回传内容。任务执行时才解密到临时文件，任务结束立即删除。凭据失效时任务会给出提示，重新获取即可。B站的凭据同时作用于链接解析和站内字幕接口，因此会员视频的字幕也能读到。

平台凭据接口：`GET /api/credentials` 查看状态，`PUT /api/credentials/{platform}` 手动导入，`DELETE /api/credentials/{platform}` 清除，`POST / GET / DELETE /api/credentials/{platform}/login` 控制浏览器助手（`?mode=guest|login`）。

## 兜底解析接口

B站、抖音、快手、小红书都是**本机解析**，平台风控或页面结构变化时可能整条链路失败；**视频号没有本机解析方案，只能走这个接口**。这时如果配了第三方解析接口的 Key，工作台会改用它取直链继续识别。

**第一步：拿 Key** —— 打开 <https://api-new.ifphp.com/> 注册账号，在站内领取自己的 API Key，然后填到工作台的「设置 → 兜底解析」里。网关地址已经内置于程序（`https://api-new.ifphp.com`），**不需要配置任何环境变量**，源码运行与打包版行为一致。

要换成自己的网关时，用环境变量覆盖即可（该变量为空或只有空白时仍回落内置地址）：

```powershell
$env:VTW_FALLBACK_API_BASE = "https://你的网关地址"
```

未配置 Key 时兜底链路不参与：其它平台的行为与没有这个功能时完全一致，**视频号**则会明确提示去「设置」页填写。网关需要兼容下面这套约定（本项目按 BugPk-Api 风格的响应实现，其它同类网关只要能对上字段也能用）：

| 用途 | 路径 | 说明 |
| --- | --- | --- |
| 短视频解析聚合 | `GET /api/svparse?url=<作品链接>` | 各平台通用的聚合端点 |
| B站 | `GET /api/bilibili?url=...` | 聚合端点对 B站 可能返回 502，因此优先试专属端点 |
| 抖音 | `GET /api/dyjx?url=...` | |
| 快手 | `GET /api/ksjx?url=...` | |
| 视频号 | `GET /api/wxsph?url=...` | |

鉴权用 `X-API-Key` 请求头（也支持 `?key=` 查询参数）。工作台按平台选专属端点，失败再退回聚合端点。响应里的直链字段各端点写法不同（作者字段有 `auther`、`author.name` 两种写法，备选直链在 `video_backup[]`），程序按别名兜底取值。

**接入行为**

- Key 在「设置 → 兜底解析」里填写：与本机 Cookie 同一套 Windows DPAPI 加密保存，设置接口只回传「是否已配置」，不回传明文；输入框留空表示不改动，点「清除」才移除。
- **其它平台只在主链路失败后才使用**：没配 Key 时不发任何额外请求，错误提示与接入前完全一致。**视频号例外**，它只有这一条链路，没配 Key 时直接提示去「设置」页填写。
- 三条失败路径都会兜：解析阶段失败 → 用兜底接口的标题 / 作者 / 封面建文案，并直接下它的直链；解析成功但下载失败（风控是概率性的）→ 仍用主链路的元信息，只把音视频来源换成兜底直链；**抖音没配 Cookie**（原本直接报「需要配置凭据」）→ 也用兜底试一次。
- 兜底也没成时，任务报错会把两边的原因都带上，并保留原错误码（例如仍是 `COOKIE_REQUIRED`，便于前端继续引导配凭据）。
- 网关已内置 `https://api-new.ifphp.com`：开箱可用，源码运行与打包版都走这个地址，不需要任何环境变量；接口方换域名时用 `VTW_FALLBACK_API_BASE` 覆盖即可（该变量为空或只有空白时，仍回落内置地址）。

> 提醒：兜底网关是第三方服务，直链由对方返回、可用性不受本项目控制，使用前请自行确认合规性与稳定性；返回的直链带时效签名，只能现取现用。

## 已知环境约束

- `ctranslate2` 限定在 `>=4.4,<4.6`：更新版本在部分 Windows/CPU 环境下加载模型会触发访问违例。这条约束带来两个跟着走的限制：① 该版本只发到 `cp312`，所以后端运行在 **Python 3.11/3.12**（`requires-python` 已写上界）；② 它导入时会 `import pkg_resources`，而 setuptools 81 起不再提供，因此 `accurate` 里额外钉了 `setuptools<81`。
- 精准识别的 VAD 预处理默认关闭（依赖 `onnxruntime`，该库在同类环境下可能加载即崩溃）；设置 `VTW_WHISPER_VAD=true` 可开启，程序会先在子进程探测可用性。
- 精准识别只在 CPU 上跑，精度默认 `int8`（可用 `VTW_WHISPER_COMPUTE_TYPE` 改成 `float32` 等）。`VTW_WHISPER_BEAM_SIZE` 默认 5，设为 1 可再快约一倍（同一段音频 30 秒 → 13 秒），代价是解码质量略降。

## 原则

- 媒体、Cookie、转写文本和任务记录默认不离开本机。
- 只做「提取 → 校对 → 导出」这条链路；AI 总结由跳转到 DeepSeek 完成，本机不做 AI 推理、也不保存第三方结果。
- 平台适配器与 ASR 引擎保持隔离，模型与引擎互不耦合，可独立替换和更新。
