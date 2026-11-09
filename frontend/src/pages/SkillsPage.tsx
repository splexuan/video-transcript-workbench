import {
  AlertTriangle,
  BookOpen,
  Bot,
  Check,
  CheckCircle2,
  Clock3,
  Copy,
  Download,
  FileCode2,
  FileJson,
  FileText,
  Files,
  FolderTree,
  HardDrive,
  LoaderCircle,
  PackageOpen,
  RefreshCw,
  ShieldCheck,
} from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

import { EmptyState } from '../components/EmptyState'
import { LoadingState } from '../components/LoadingState'
import { Modal, ModalHeader } from '../components/Modal'
import { PlatformBadge } from '../components/PlatformBadge'
import { SkillMarkdown } from '../components/SkillMarkdown'
import { api } from '../lib/api'
import { formatBytes } from '../lib/format'
import type { Skill, SkillFile, SkillFileContent } from '../types'

/**
 * 技能卡片上的「能力标签」按 id 收在这里。
 *
 * 这些是展示文案，不是运行依赖：后端只从 SKILL.md 读标题与描述，
 * 以后新增技能即使没在这里登记，也能正常显示和下载。
 */
const skillPresets: Record<string, {
  platforms: string[]
  points: string[]
  requirements: string[]
}> = {
  'video-transcript': {
    platforms: ['bilibili', 'douyin', 'kuaishou', 'xiaohongshu', 'wechat'],
    points: [
      '先本机解析，失败才走兜底接口',
      '本地 SenseVoice 转写，音频用完即删',
      '默认生成 txt，同名文件自动加序号',
      '视频号短链自动展开后再解析',
    ],
    requirements: ['Python 3.10+', 'FFmpeg', 'SenseVoice 模型（约 230 MB）', '兜底 Key（视频号必需）'],
  },
}

const MARKDOWN_NAME = 'SKILL.md'

function dateLabel(seconds: number | null) {
  if (!seconds) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: 'numeric',
    day: 'numeric',
  }).format(new Date(seconds * 1000))
}

function FileIcon({ kind }: { kind: SkillFile['kind'] }) {
  const Icon =
    kind === 'python' ? FileCode2 : kind === 'json' ? FileJson : kind === 'markdown' ? BookOpen : FileText
  return <Icon size={15} aria-hidden="true" />
}

export function SkillsPage() {
  const [skills, setSkills] = useState<Skill[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [copied, setCopied] = useState('')
  const [downloadNotice, setDownloadNotice] = useState('')
  const [liveMessage, setLiveMessage] = useState('')
  const [reloadKey, setReloadKey] = useState(0)

  /* ---- 说明弹窗 ---- */
  const [guide, setGuide] = useState<Skill | null>(null)
  const [guideText, setGuideText] = useState('')
  const [guideLoading, setGuideLoading] = useState(false)
  const [guideError, setGuideError] = useState('')

  /* ---- 文件浏览弹窗 ---- */
  const [browser, setBrowser] = useState<{ skill: Skill; path: string } | null>(null)
  const [file, setFile] = useState<SkillFileContent | null>(null)
  const [fileLoading, setFileLoading] = useState(false)
  const [fileError, setFileError] = useState('')

  const copyTimer = useRef<number | undefined>(undefined)
  const noticeTimer = useRef<number | undefined>(undefined)
  const liveTimer = useRef<number | undefined>(undefined)

  useEffect(() => {
    let active = true
    api
      .skills()
      .then((result) => {
        if (active) {
          setSkills(result.items)
          setError('')
        }
      })
      .catch((reason: Error) => {
        if (!active) return
        const message = reason.message || ''
        setError(
          message === 'Not Found'
            ? '当前运行的后端还没有技能接口，请重启程序后再打开本页。'
            : '读取技能包失败，请确认本地服务正在运行，然后重试。',
        )
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => {
      active = false
    }
  }, [reloadKey])

  useEffect(() => {
    if (!guide) return
    let active = true
    api
      .skillFile(guide.id, MARKDOWN_NAME)
      .then((result) => {
        if (active) setGuideText(result.content)
      })
      .catch((reason: Error) => {
        if (active) setGuideError(reason.message || '读取技能说明失败')
      })
      .finally(() => {
        if (active) setGuideLoading(false)
      })
    return () => {
      active = false
    }
  }, [guide])

  useEffect(() => {
    if (!browser) return
    let active = true
    api
      .skillFile(browser.skill.id, browser.path)
      .then((result) => {
        if (active) setFile(result)
      })
      .catch((reason: Error) => {
        if (active) setFileError(reason.message || '读取文件失败')
      })
      .finally(() => {
        if (active) setFileLoading(false)
      })
    return () => {
      active = false
    }
  }, [browser])

  useEffect(() => () => {
    if (copyTimer.current) window.clearTimeout(copyTimer.current)
    if (noticeTimer.current) window.clearTimeout(noticeTimer.current)
    if (liveTimer.current) window.clearTimeout(liveTimer.current)
  }, [])

  /** 读屏反馈：复制按钮的文案会变，但焦点在按钮上时不一定被读出来。 */
  function announce(message: string) {
    setLiveMessage(message)
    if (liveTimer.current) window.clearTimeout(liveTimer.current)
    liveTimer.current = window.setTimeout(() => setLiveMessage(''), 2000)
  }

  /** 下载由浏览器接管，页面本身没有状态变化；给一个会消失的可见反馈。 */
  function announceDownload(skill: Skill) {
    setDownloadNotice(`已开始下载「${skill.title}」技能包，请到浏览器下载列表查看。`)
    if (noticeTimer.current) window.clearTimeout(noticeTimer.current)
    noticeTimer.current = window.setTimeout(() => setDownloadNotice(''), 4000)
  }

  async function copyText(text: string, key: string) {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(key)
      announce('已复制到剪贴板')
      if (copyTimer.current) window.clearTimeout(copyTimer.current)
      copyTimer.current = window.setTimeout(() => setCopied(''), 1800)
    } catch {
      setError('复制失败，请手动选中命令复制。')
    }
  }

  function retry() {
    setLoading(true)
    setError('')
    setReloadKey((value) => value + 1)
  }

  /** 打开说明：加载态和清空旧内容放在事件里做，effect 只负责取数据。 */
  function openGuide(skill: Skill) {
    setGuideText('')
    setGuideError('')
    setGuideLoading(true)
    setGuide(skill)
  }

  function openBrowser(skill: Skill, path = MARKDOWN_NAME) {
    setFile(null)
    setFileError('')
    setFileLoading(true)
    setBrowser({ skill, path })
  }

  function selectBrowserFile(path: string) {
    setFile(null)
    setFileError('')
    setFileLoading(true)
    setBrowser((current) => (current ? { ...current, path } : current))
  }

  const primary = skills[0]
  const primaryPreset = primary ? skillPresets[primary.id] : undefined

  return (
    <div className="page skills-page">
      <header className="page-header">
        <div>
          <h1>技能安装</h1>
          <p>
            把创作工作室里的能力装进你的 Agent：查看说明、预览源码，下载后按步骤安装。
            技能是独立目录，不依赖本程序运行。
          </p>
        </div>
        <span className="local-chip"><ShieldCheck size={15} /> 下载包不含密钥</span>
      </header>

      {error && skills.length > 0 && <p className="form-message error" role="alert">{error}</p>}
      {downloadNotice && <p className="form-message success" role="status">{downloadNotice}</p>}
      <p className="sr-only" role="status" aria-live="polite">{liveMessage}</p>

      {loading && skills.length === 0 ? (
        <LoadingState label="正在准备技能包…" rows={4} />
      ) : error && skills.length === 0 ? (
        <EmptyState
          icon={AlertTriangle}
          title="技能接口暂时不可用"
          description={error}
          action={
            <button className="secondary-button" type="button" onClick={retry}>
              <RefreshCw size={15} /> 重试
            </button>
          }
        />
      ) : skills.length === 0 ? (
        <EmptyState
          icon={PackageOpen}
          title="没有找到可安装的技能包"
          description="请先重启创作工作室再打开本页；如果仍然看不到，再检查程序目录里是否带有 skills 文件夹。开发版对应仓库根目录的 skills/。"
        />
      ) : (
        skills.map((skill) => {
          const preset = skillPresets[skill.id]
          const promptKey = `${skill.id}-agent-prompt`
          return (
            <SkillCard
              key={skill.id}
              skill={skill}
              preset={preset}
              copiedPrompt={copied === promptKey}
              onCopyPrompt={() => void copyText(agentPrompt(skill), promptKey)}
              onDownload={() => announceDownload(skill)}
              onGuide={() => openGuide(skill)}
              onBrowse={() => openBrowser(skill)}
            />
          )
        })
      )}

      {primary && (
        <AgentInstallPrompt
          skill={primary}
          copied={copied}
          onCopy={(text, key) => void copyText(text, key)}
        />
      )}

      {primary && (
        <InstallSteps
          skill={primary}
          requirements={primaryPreset?.requirements ?? ['Python 3.10+']}
          copied={copied}
          onCopy={(text, key) => void copyText(text, key)}
        />
      )}

      {guide && (
        <Modal
          ariaLabel={`${guide.title} 说明`}
          className="skill-guide-modal"
          size="lg"
          onClose={() => setGuide(null)}
        >
          <ModalHeader
            title={`${guide.title} · 说明`}
            icon={BookOpen}
            extra={<span className="skill-modal-file">{MARKDOWN_NAME}</span>}
            onClose={() => setGuide(null)}
          />
          <div className="modal-body skill-guide-scroll">
            {guideLoading ? (
              <LoadingState label="正在读取说明…" rows={6} />
            ) : guideError ? (
              <p className="form-message error" role="alert">{guideError}</p>
            ) : (
              <SkillMarkdown text={guideText} />
            )}
          </div>
          <footer className="modal-foot">
            <button
              className="text-button"
              type="button"
              onClick={() => {
                setGuide(null)
                openBrowser(guide)
              }}
            >
              <FolderTree size={15} /> 浏览文件
            </button>
            <a
              className="primary-button"
              href={api.skillDownloadUrl(guide.id)}
              download={`${guide.id}.zip`}
              onClick={() => announceDownload(guide)}
            >
              <Download size={15} /> 下载技能包
            </a>
          </footer>
        </Modal>
      )}

      {browser && (
        <Modal
          ariaLabel={`${browser.skill.title} 文件`}
          className="skill-files-modal"
          size="lg"
          onClose={() => setBrowser(null)}
        >
          <ModalHeader
            title={`${browser.skill.title} · 文件`}
            icon={FolderTree}
            extra={<span className="skill-modal-file">{browser.skill.files.length} 个文件</span>}
            onClose={() => setBrowser(null)}
          />
          <div className="skill-browser">
            <nav className="skill-file-list" aria-label="技能文件">
              {browser.skill.files.map((item) => (
                <button
                  key={item.path}
                  type="button"
                  className={`skill-file-item${item.path === browser.path ? ' active' : ''}`}
                  title={item.path}
                  aria-current={item.path === browser.path ? 'true' : undefined}
                  onClick={() => selectBrowserFile(item.path)}
                >
                  <FileIcon kind={item.kind} />
                  <span>{item.path}</span>
                  <small>{formatBytes(item.size)}</small>
                </button>
              ))}
            </nav>
            <section className="skill-file-preview" aria-label="文件内容">
              <header>
                <strong>{file?.path ?? browser.path}</strong>
                {fileLoading && <LoaderCircle className="spin" size={14} />}
              </header>
              {fileError ? (
                <p className="form-message error" role="alert">{fileError}</p>
              ) : fileLoading && !file ? (
                <LoadingState label="正在读取文件…" rows={6} compact />
              ) : (
                <pre className="skill-file-code"><code>{file?.content ?? ''}</code></pre>
              )}
              {file?.truncated && (
                <p className="skill-file-note">文件较大，这里只显示开头一部分，完整内容请下载技能包。</p>
              )}
            </section>
          </div>
          <footer className="modal-foot">
            <span className="skill-modal-note">
              <ShieldCheck size={14} /> 不显示 config.json 与运行产物
            </span>
            <a
              className="primary-button"
              href={api.skillDownloadUrl(browser.skill.id)}
              download={`${browser.skill.id}.zip`}
              onClick={() => announceDownload(browser.skill)}
            >
              <Download size={15} /> 下载 ZIP
            </a>
          </footer>
        </Modal>
      )}
    </div>
  )
}

function AgentInstallPrompt({
  skill,
  copied,
  onCopy,
}: {
  skill: Skill
  copied: string
  onCopy: (text: string, key: string) => void
}) {
  const key = `${skill.id}-agent-prompt`
  const prompt = agentPrompt(skill)

  return (
    <section className="skill-agent" aria-label="让 Agent 一键安装">
      <div className="skill-agent-head">
        <div className="skill-agent-title">
          <span className="skill-agent-icon"><Bot size={20} /></span>
          <div>
            <h2>让 Agent 帮你装</h2>
            <p>
              复制下面这段提示词，发给本机的 Claude Code、WorkBuddy 等 Agent；
              它会下载、解压、安装依赖并自检，不用你一条条敲命令。
            </p>
          </div>
        </div>
        <button
          className="primary-button"
          type="button"
          onClick={() => onCopy(prompt, key)}
        >
          {copied === key ? <Check size={15} /> : <Copy size={15} />}
          {copied === key ? '已复制' : '复制给 Agent'}
        </button>
      </div>
      <p className="skill-agent-flow">
        下载 ZIP → 解压到 Agent 的 skills 目录 → 安装依赖 → 自检环境
      </p>
      <details className="skill-agent-details">
        <summary>查看完整安装指令（约 {prompt.length} 字）</summary>
        <pre className="skill-agent-prompt" tabIndex={0} aria-label="Agent 安装指令">
          <code>{prompt}</code>
        </pre>
      </details>
      <p className="skill-agent-note">
        适用于和本程序在同一台电脑上的 Agent。远程 Agent 访问不到 127.0.0.1，
        请先在上面下载 ZIP 再发给它。
      </p>
    </section>
  )
}

function agentPrompt(skill: Skill) {
  const origin = window.location.origin
  // 开发时页面在 5173，/api 由 Vite 代理；Agent 不经过浏览器，直接指向后端端口。
  const base = origin.includes(':5173') ? 'http://127.0.0.1:8765' : origin
  const download = `${base.replace(/\/$/, '')}/api/skills/${encodeURIComponent(skill.id)}/download`

  return `请在这台电脑上安装「${skill.title}」Agent 技能（技能 ID：${skill.id}），安装到你现在使用的 Agent 技能目录。

下载地址（创作工作室本机服务，安装期间请保持程序运行）：
${download}
如果下载返回 404，说明创作工作室后端还是旧版本，先让我重启程序，不要继续安装。

执行步骤：
1. 用 Python 下载并解压到临时目录：
   python -c "import urllib.request; urllib.request.urlretrieve('${download}', '${skill.id}.zip')"
   python -m zipfile -e ${skill.id}.zip .
   解压后应看到 ${skill.id}/SKILL.md。
2. 把整个 ${skill.id} 目录放进你的技能目录：
   - Claude Code：~/.claude/skills/
   - WorkBuddy：~/.workbuddy/skills/
   - 其他 Agent：用它自己的 skills/ 目录
   如果不确定是哪个目录，先问我，不要随便放到系统目录。
3. 进入 ${skill.id} 目录，安装依赖：
   python -m pip install -r requirements.txt
4. 检查环境：
   python scripts/extract.py --check
   缺少 FFmpeg 时，把 ffmpeg / ffprobe 放进 vendor/ffmpeg/，或安装到 PATH。
5. 下载 SenseVoice 模型（只需一次）：
   python scripts/extract.py --install-model
6. 安装完成后再跑一次 --check，把结果发给我；提取功能我随后用真实链接验证。

注意：
- 技能目录里的 config.json 可能含有我的 API Key，不要打印、上传或提交到 git。
- 视频号解析需要兜底 API Key；如果检查提示缺少 Key，先停下来问我，不要自行注册或索要账号密码。
- 转写结束后临时音频会自动删除，不需要保留。
- 安装完成后先读一遍 SKILL.md，按里面的说明执行。`
}

function SkillCard({
  skill,
  preset,
  copiedPrompt,
  onCopyPrompt,
  onDownload,
  onGuide,
  onBrowse,
}: {
  skill: Skill
  preset?: { platforms: string[]; points: string[]; requirements: string[] }
  copiedPrompt: boolean
  onCopyPrompt: () => void
  onDownload: () => void
  onGuide: () => void
  onBrowse: () => void
}) {
  return (
    <article className="skill-card">
      <div className="skill-card-main">
        <div className="skill-card-head">
          <span className="skill-logo"><PackageOpen size={25} /></span>
          <div className="skill-card-title">
            <div className="skill-title-row">
              <h2>{skill.title}</h2>
              {skill.version && <span className="skill-version">v{skill.version}</span>}
              <span className="skill-ready"><CheckCircle2 size={13} /> 可下载</span>
            </div>
            <p className="skill-id">技能 ID：{skill.id}</p>
          </div>
        </div>

        <div className="skill-card-body">
          <p className="skill-description">{skill.description}</p>

          {preset && preset.platforms.length > 0 && (
            <div className="skill-platforms">
              {preset.platforms.map((platform) => <PlatformBadge key={platform} platform={platform} />)}
              <span className="skill-platform-note">以及本地音视频文件</span>
            </div>
          )}

          {preset && preset.points.length > 0 && (
            <ul className="skill-points">
              {preset.points.map((point) => (
                <li key={point}><Check size={14} />{point}</li>
              ))}
            </ul>
          )}
        </div>
      </div>

      <div className="skill-card-side">
        <dl className="skill-facts">
          <div><dt><Files size={14} /> 文件</dt><dd>{skill.files.length} 个</dd></div>
          <div><dt><HardDrive size={14} /> 体积</dt><dd>{formatBytes(skill.total_bytes)}</dd></div>
          <div><dt><Clock3 size={14} /> 更新</dt><dd>{dateLabel(skill.updated_at)}</dd></div>
        </dl>
        <div className="skill-actions">
          <button className="primary-button" type="button" onClick={onCopyPrompt}>
            {copiedPrompt ? <Check size={15} /> : <Bot size={15} />}
            {copiedPrompt ? '已复制，去粘贴给 Agent' : '复制安装指令给 Agent'}
          </button>
          <a
            className="secondary-button"
            href={api.skillDownloadUrl(skill.id)}
            download={`${skill.id}.zip`}
            onClick={onDownload}
          >
            <Download size={15} /> 下载技能包
          </a>
          <div className="skill-action-links">
            <button className="text-button" type="button" onClick={onGuide}>
              <BookOpen size={15} /> 查看说明
            </button>
            <button className="text-button" type="button" onClick={onBrowse}>
              <FolderTree size={15} /> 浏览文件
            </button>
          </div>
        </div>
        <p className="skill-safe">
          <ShieldCheck size={14} />
          只包含源码与说明，不含 config.json、密钥、模型和音视频。
        </p>
      </div>
    </article>
  )
}

function InstallSteps({
  skill,
  requirements,
  copied,
  onCopy,
}: {
  skill: Skill
  requirements: string[]
  copied: string
  onCopy: (text: string, key: string) => void
}) {
  const preset = skillPresets[skill.id]
  const steps = [
    {
      title: '解压到 Agent 的技能目录',
      body: (
        <>
          下载后解压，确保最终结构是 <code>{skill.id}/SKILL.md</code>。Claude Code 常放在
          <code>~/.claude/skills</code>，WorkBuddy 放在 <code>~/.workbuddy/skills</code>。
        </>
      ),
      command: null,
    },
    {
      title: '安装 Python 依赖',
      body: '进入解压后的技能目录执行；pip 不在 PATH 时用 python -m pip。',
      command: `cd ${skill.id}\npython -m pip install -r requirements.txt`,
    },
    {
      title: '检查运行环境',
      body: 'FFmpeg、依赖与识别模型都就绪时返回 ok: true；缺什么按提示补齐。',
      command: 'python scripts/extract.py --check',
    },
    {
      title: '下载识别模型',
      body: '只需下载一次，默认存到 ~/.video-transcript-skill/models。',
      command: 'python scripts/extract.py --install-model',
    },
  ]
  const example = preset
    ? `用 video-transcript 技能提取这个链接的文案：<链接>`
    : `用 ${skill.title} 技能处理这个链接：<链接>`

  return (
    <section className="skill-section" aria-label="安装步骤">
      <div className="section-heading">
        <h2>安装步骤</h2>
        <p>以 {skill.title}（{skill.id}）为例，装一次即可反复使用。</p>
      </div>

      <ol className="skill-steps">
        {steps.map((step, index) => {
          const key = `${skill.id}-step-${index}`
          return (
            <li key={step.title}>
              <span className="skill-step-index" aria-hidden="true">{index + 1}</span>
              <div className="skill-step-body">
                <h3>{step.title}</h3>
                <p>{step.body}</p>
                {step.command && (
                  <div className="skill-command">
                    <pre><code>{step.command}</code></pre>
                    <button
                      className="icon-button"
                      type="button"
                      onClick={() => onCopy(step.command!, key)}
                      aria-label="复制命令"
                      title={copied === key ? '已复制' : '复制命令'}
                    >
                      {copied === key ? <Check size={16} /> : <Copy size={16} />}
                    </button>
                  </div>
                )}
              </div>
            </li>
          )
        })}
      </ol>

      <div className="skill-usage">
        <div>
          <h3>安装好后这样用</h3>
          <p>把链接和这句话一起发给 Agent。视频号第一次使用会需要兜底 Key。</p>
        </div>
        <div className="skill-command">
          <pre><code>{example}</code></pre>
          <button
            className="icon-button"
            type="button"
            onClick={() => onCopy(example, `${skill.id}-example`)}
            aria-label="复制用法"
            title={copied === `${skill.id}-example` ? '已复制' : '复制用法'}
          >
            {copied === `${skill.id}-example` ? <Check size={16} /> : <Copy size={16} />}
          </button>
        </div>
      </div>

      <div className="skill-requirements">
        <span>准备项</span>
        {requirements.map((item) => <span className="skill-requirement" key={item}>{item}</span>)}
      </div>
    </section>
  )
}
