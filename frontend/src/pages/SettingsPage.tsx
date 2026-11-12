import {
  AudioLines,
  Check,
  CloudDownload,
  Download,
  ExternalLink,
  FolderOpen,
  HardDrive,
  Info,
  LoaderCircle,
  MonitorCog,
  RefreshCw,
  ShieldCheck,
  Trash2,
  X,
} from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import { ProgressBar } from '../components/ProgressBar'
import { api } from '../lib/api'
import { formatBytes } from '../lib/format'
import { shortModelName } from '../lib/labels'
import { recognitionModels } from '../lib/models'
import type {
  AppSettings,
  RecognitionModel,
  TtsConfig,
  UpdatePackage,
  UpdateProgress,
  UpdateState,
} from '../types'

const defaults: AppSettings = { theme: 'system', default_model: '', prefer_subtitle: false, keep_media: false, storage_path: '', fallback_api_key_set: false, tts_model: '', tts_voice: null, auto_check_update: true, ignored_version: '' }

/** 下载与校验都算「进行中」：这期间要持续轮询进度。 */
function isRunning(progress: UpdateProgress | null) {
  return progress?.status === 'pending' || progress?.status === 'running' || progress?.status === 'verifying' || progress?.status === 'cancelling'
}

/** 后端给的是秒级时间戳；界面上只说「什么时候查的」。 */
function checkedLabel(value: number) {
  return new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(new Date(value * 1000))
}

function publishedLabel(value: string) {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return `发布于 ${new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium' }).format(date)}`
}

/** 没有「发现新版本」块时的状态行；跳过的那一版要明说，不能写成「已经是最新」。 */
function updateStatusText(state: UpdateState | null) {
  if (!state) return ''
  if (state.error) return state.error
  if (state.ignored_version && state.latest?.version === state.ignored_version) {
    return `已跳过 v${state.ignored_version} 的更新提示。`
  }
  return state.checked_at ? '当前已经是最新版本。' : '还没有检查过版本。'
}

export function SettingsPage() {
  const [settings, setSettings] = useState(defaults)
  // 最新设置的同步副本：自动保存是「改动即提交」，回调里读 state 会拿到旧值
  const settingsRef = useRef(settings)
  // 保存请求串行化：前一个响应没回来就发下一个，服务端返回的顺序不保证，
  // 串起来之后「改了驱动又改模型」不会互相覆盖
  const queueRef = useRef<Promise<void>>(Promise.resolve())
  const [saving, setSaving] = useState(false)
  const [savedOnce, setSavedOnce] = useState(false)
  const [saveError, setSaveError] = useState('')
  const [models, setModels] = useState<RecognitionModel[]>([])
  const [modelsLoading, setModelsLoading] = useState(true)
  const [modelsError, setModelsError] = useState('')
  const [modelsReloadKey, setModelsReloadKey] = useState(0)
  // 兜底解析的 Key 只写不读：输入框留空表示「不动已配置的 Key」，填了并失焦/回车才提交
  const [apiKey, setApiKey] = useState('')
  // 版本号：只用于「关于」里显示当前是哪个版本，不联网
  const [version, setVersion] = useState('')
  // 语音合成：模型与音色清单（音色来自 ZipVoice 的参考音频）
  const [ttsConfig, setTtsConfig] = useState<TtsConfig | null>(null)
  // 版本与更新
  const [updates, setUpdates] = useState<UpdateState | null>(null)
  const [checking, setChecking] = useState(false)
  const [notesOpen, setNotesOpen] = useState(false)
  const [updateBusy, setUpdateBusy] = useState('')
  const [updateMessage, setUpdateMessage] = useState('')
  const [installMessage, setInstallMessage] = useState('')

  const download = updates?.download ?? null
  const running = isRunning(download)
  const install = updates?.install ?? null
  const installing = install?.status === 'pending' || install?.status === 'extracting' || install?.status === 'launching'
  // 下好的包以服务端的目录扫描为准：重开程序之后它还在
  const packageFile: UpdatePackage | null = updates?.package ?? null
  // 没有「发现新版本」块时的状态行；没有可说的内容时整行不渲染
  const statusText = updateStatusText(updates)

  /** 状态一变就广播给侧边栏：版本号与新版本提示在两个位置展示，不该各查一次。 */
  const publish = useCallback((state: UpdateState) => {
    setUpdates(state)
    window.dispatchEvent(new CustomEvent('vtw:updates', { detail: state }))
  }, [])

  const reloadUpdates = useCallback(async () => {
    publish(await api.updates())
  }, [publish])

  /** 本地与同步副本一起写，避免两处口径不一致。 */
  function applySettings(next: AppSettings) {
    settingsRef.current = next
    setSettings(next)
  }

  /**
   * 自动保存：改动即提交，不再需要「保存设置」按钮。
   *
   * 本地先乐观更新（下拉立刻反映选择），服务端返回后只取回它知道而前端不知道的
   * `fallback_api_key_set`；其余字段以本地为准——排队中的后续改动不能被这次响应顶回去。
   */
  function save(patch: Partial<AppSettings>) {
    applySettings({ ...settingsRef.current, ...patch } as AppSettings)
    setSaveError('')
    queueRef.current = queueRef.current
      .then(async () => {
        setSaving(true)
        try {
          const saved = await api.updateSettings(patch)
          const merged = { ...settingsRef.current, fallback_api_key_set: saved.fallback_api_key_set }
          // Key 只写不读：提交完就不在本地留着明文
          delete merged.fallback_api_key
          applySettings(merged)
          setSavedOnce(true)
          if ('theme' in patch) {
            window.dispatchEvent(new CustomEvent('vtw:theme', { detail: saved.theme }))
          }
        } catch (reason) {
          setSaveError(reason instanceof Error ? reason.message : '保存失败，请稍后重试')
        } finally {
          setSaving(false)
        }
      })
      // 队列本身不能断：一次失败之后后续改动仍然要能提交
      .catch(() => undefined)
  }

  // 左栏那个日/月按钮改的也是「界面主题」：两边是同一个设置，
  // 所以在侧栏切完之后回到这一页，下拉要跟着走，不能显示旧值。
  // 侧栏自己会保存，这里只同步本地，不重复提交。
  useEffect(() => {
    function sync(event: Event) {
      const detail = (event as CustomEvent<AppSettings['theme']>).detail
      if (detail) {
        settingsRef.current = { ...settingsRef.current, theme: detail }
        setSettings(settingsRef.current)
      }
    }
    window.addEventListener('vtw:theme', sync)
    return () => window.removeEventListener('vtw:theme', sync)
  }, [])

  useEffect(() => {
    api
      .settings()
      .then((loaded) => {
        settingsRef.current = loaded
        setSettings(loaded)
      })
      .catch((reason: Error) => setSaveError(`设置读取失败：${reason.message}`))
  }, [])

  useEffect(() => {
    let active = true
    api
      .health()
      .then((info) => {
        if (active) setVersion(info.version)
      })
      .catch(() => undefined)
    return () => {
      active = false
    }
  }, [])

  useEffect(() => {
    let active = true
    api
      .ttsConfig()
      .then((next) => {
        if (active) setTtsConfig(next)
      })
      .catch(() => undefined)
    return () => {
      active = false
    }
  }, [])

  // 首次打开就去问一次版本：后端按设置节流，关掉自动检查时连请求都不会发出去。
  useEffect(() => {
    let active = true
    api
      .updates()
      .then((state) => {
        if (active) publish(state)
      })
      .catch((reason: Error) => {
        if (active) setUpdateMessage(`读取版本信息失败：${reason.message}`)
      })
    return () => {
      active = false
    }
  }, [publish])

  // 下载或解压替换进行中都要持续轮询；替换开始后服务会退出，轮询失败就停在最后一条进度上。
  useEffect(() => {
    if (!running && !installing) return
    const timer = window.setInterval(() => {
      void reloadUpdates().catch(() => undefined)
    }, 1200)
    return () => window.clearInterval(timer)
  }, [running, installing, reloadUpdates])

  useEffect(() => {
    let active = true
    api.models()
      .then((catalog) => {
        // 只列识别模型：标点恢复与语音合成也在同一个目录里，选它们没法转写
        if (active) setModels(recognitionModels(catalog.models))
      })
      .catch(() => {
        if (active) setModelsError('模型列表读取失败，请检查本地服务后重试。')
      })
      .finally(() => {
        if (active) setModelsLoading(false)
      })
    return () => {
      active = false
    }
  }, [modelsReloadKey])

  /**
   * 当前选中的语音模型（空串表示「自动」）；用来提示「未安装」。
   */
  const ttsSelected = ttsConfig?.models.find((model) => model.id === settings.tts_model) ?? null
  // 保存的语音模型可能已下架（例如被移除的 int8 规格）：列表里没有就回到「自动」，
  // 否则下拉框会显示成空白，看起来像设置丢了。
  const ttsModelValue = ttsConfig?.models.some((model) => model.id === settings.tts_model)
    ? settings.tts_model
    : ''
  /**
   * 存过的默认识别模型现在不在可选项里（模型被移除或不再展示）时，保留一个占位项。
   * 不做回落覆盖：直接改掉用户的设置会让他们以为模型还在，只提示重新选择。
   */
  const staleDefaultModel =
    settings.default_model && !models.some((model) => model.id === settings.default_model)
      ? settings.default_model
      : ''

  /** 提交兜底解析的 Key；留空表示不改动，要清除用旁边的「清除」。 */
  function commitApiKey() {
    const value = apiKey.trim()
    if (!value) return
    save({ fallback_api_key: value })
    setApiKey('')
  }

  function clearApiKey() {
    setApiKey('')
    save({ fallback_api_key: '' })
  }

  async function checkForUpdates() {
    setChecking(true)
    setUpdateMessage('')
    try {
      const state = await api.checkUpdates()
      publish(state)
      // 结果（最新 / 已跳过 / 错误）已经由 statusText 统一说明，这里不再重复一遍。
      setUpdateMessage('')
    } catch (reason) {
      setUpdateMessage(reason instanceof Error ? reason.message : '检查更新失败')
    } finally {
      setChecking(false)
    }
  }

  /** 只回填版本相关的两个字段：设置页可能还有没保存的编辑，不能整份覆盖回去。 */
  async function patchUpdateSetting(payload: Partial<AppSettings>) {
    const saved = await api.updateSettings(payload)
    applySettings({
      ...settingsRef.current,
      auto_check_update: saved.auto_check_update,
      ignored_version: saved.ignored_version,
    })
    await reloadUpdates()
    return saved
  }

  async function startDownload() {
    setUpdateBusy('download')
    setUpdateMessage('')
    try {
      await api.downloadUpdate()
      await reloadUpdates()
    } catch (reason) {
      setUpdateMessage(reason instanceof Error ? reason.message : '下载失败')
    } finally {
      setUpdateBusy('')
    }
  }

  async function cancelDownload() {
    setUpdateBusy('cancel')
    setUpdateMessage('')
    try {
      await api.cancelUpdateDownload()
      await reloadUpdates()
    } catch (reason) {
      setUpdateMessage(reason instanceof Error ? reason.message : '取消失败')
    } finally {
      setUpdateBusy('')
    }
  }

  /**
   * 自动替换：后端解压后会请求进程退出，脚本在进程结束后备份旧目录、换上新目录、
   * 再启动新版本；失败会自动回滚。这里只负责发起，页面随服务退出而断开。
   */
  async function startInstall() {
    if (!packageFile || installing) return
    const confirmed = window.confirm(
      `将退出当前程序，自动替换为 v${packageFile.version} 后重新启动。正在识别的任务会被中断。继续吗？`,
    )
    if (!confirmed) return
    setUpdateBusy('install')
    setInstallMessage('')
    try {
      await api.installUpdate()
      await reloadUpdates()
    } catch (reason) {
      setInstallMessage(reason instanceof Error ? reason.message : '启动自动更新失败')
    } finally {
      setUpdateBusy('')
    }
  }

  async function revealPackage() {
    try {
      const result = await api.revealUpdatePackage()
      if (!result.opened) setUpdateMessage(`请手动打开这个目录：${result.path}`)
    } catch (reason) {
      setUpdateMessage(reason instanceof Error ? reason.message : '打开目录失败')
    }
  }

  async function removePackage() {
    if (!window.confirm('删除已下载的安装包？下次要更新时需要重新下载。')) return
    setUpdateBusy('remove')
    setUpdateMessage('')
    try {
      await api.deleteUpdatePackage()
      await reloadUpdates()
    } catch (reason) {
      setUpdateMessage(reason instanceof Error ? reason.message : '删除失败')
    } finally {
      setUpdateBusy('')
    }
  }

  async function ignoreVersion() {
    const ignored = updates?.latest?.version
    if (!ignored) return
    setUpdateBusy('ignore')
    try {
      await patchUpdateSetting({ ignored_version: ignored })
      setUpdateMessage(`已跳过 v${ignored}，不再提示`)
    } catch (reason) {
      setUpdateMessage(reason instanceof Error ? reason.message : '跳过失败')
    } finally {
      setUpdateBusy('')
    }
  }

  async function restoreVersion() {
    setUpdateBusy('ignore')
    try {
      await patchUpdateSetting({ ignored_version: '' })
      setUpdateMessage('已恢复版本提示')
    } catch (reason) {
      setUpdateMessage(reason instanceof Error ? reason.message : '恢复失败')
    } finally {
      setUpdateBusy('')
    }
  }

  async function toggleAutoCheck(next: boolean) {
    applySettings({ ...settingsRef.current, auto_check_update: next })
    setUpdateMessage('')
    try {
      await patchUpdateSetting({ auto_check_update: next })
      if (next) setUpdateMessage('已开启：启动时会自动检查新版本')
    } catch (reason) {
      applySettings({ ...settingsRef.current, auto_check_update: !next })
      setUpdateMessage(reason instanceof Error ? reason.message : '保存失败')
    }
  }


  return (
    <div className="page settings-page">
      <header className="page-header">
        <div><h1>设置</h1><p>默认识别模型、语音合成、外观与本地存储。改动会即时保存，不需要手动提交。</p></div>
        {/* 状态放在页头：下面每个区块都是自动保存的，没有一个「提交」按钮可以挂提示 */}
        {(saving || savedOnce) && (
          <span className={saving ? 'autosave-status unsaved' : 'autosave-status'} role="status">
            {saving ? <><LoaderCircle className="spin" size={14} /> 保存中…</> : <><Check size={14} /> 已自动保存</>}
          </span>
        )}
      </header>
      {saveError && <p className="form-message error settings-save-error" role="alert">{saveError}</p>}
      <div className="settings-layout">
        <section className="settings-section panel">
          <div className="settings-heading"><span><MonitorCog size={19} /></span><div><h2>任务偏好</h2><p>新建提取时的默认行为。</p></div></div>
          <div className="setting-row">
            <span><label htmlFor="default-model"><strong>默认识别模型</strong></label><small>没有可用平台字幕时，默认使用这个模型；工作台可临时切换。</small></span>
            <div className="setting-control">
              <select id="default-model" value={settings.default_model} disabled={modelsLoading || Boolean(modelsError)} aria-describedby={modelsError ? 'default-model-error' : undefined} onChange={(event) => save({ default_model: event.target.value })}>
                {modelsLoading && <option value={settings.default_model}>正在读取模型…</option>}
                {!modelsLoading && modelsError && <option value={settings.default_model}>{settings.default_model ? shortModelName(settings.default_model) : '暂不可用'}</option>}
                {!modelsLoading && !modelsError && staleDefaultModel && <option value={staleDefaultModel}>{shortModelName(staleDefaultModel)}（当前不可用，请重新选择）</option>}
                {!modelsLoading && !modelsError && models.map((model) => <option key={model.id} value={model.id} disabled={model.state !== 'ready'}>{shortModelName(model.name)}{model.state === 'ready' ? '' : '（未安装）'}</option>)}
              </select>
              {modelsError && <span className="field-error" id="default-model-error" role="alert">{modelsError}<button className="text-button" type="button" onClick={() => { setModelsLoading(true); setModelsError(''); setModelsReloadKey((value) => value + 1) }}>重试</button></span>}
            </div>
          </div>
          <label className="setting-row" htmlFor="theme">
            <span>
              <strong>界面主题</strong>
              {/* 左栏页脚那个日/月按钮改的是同一个设置，这里说清楚，
                  免得在两边看到不一致时以为是两件事 */}
              <small>选「跟随系统」时会随 Windows 深浅色自动切换；左下角的主题按钮是同一个设置，点了会切到浅色或深色。</small>
            </span>
            <select id="theme" value={settings.theme} onChange={(event) => save({ theme: event.target.value as AppSettings['theme'] })}><option value="system">跟随系统</option><option value="light">浅色</option><option value="dark">深色</option></select>
          </label>
        </section>
        <section className="settings-section panel">
          <div className="settings-heading"><span><AudioLines size={19} /></span><div><h2>语音合成</h2><p>「语音合成」页默认用哪个模型和音色。</p></div></div>
          <div className="setting-row">
            <span>
              <label htmlFor="tts-model"><strong>默认语音模型</strong></label>
              <small>把文案读成音频的本地模型（ZipVoice，CPU 推理）。没装的话到「语音合成」页或「模型管理」下载，约 506 MB。</small>
            </span>
            <div className="setting-control">
              <select id="tts-model" value={ttsModelValue} onChange={(event) => save({ tts_model: event.target.value })}>
                <option value="">自动（用已安装的那个）</option>
                {(ttsConfig?.models ?? []).map((model) => (
                  <option key={model.id} value={model.id} disabled={!model.ready}>
                    {model.name}{model.ready ? '' : '（未安装）'}
                  </option>
                ))}
              </select>
              {ttsConfig?.models.length === 0 && <small className="field-hint">暂时读不到语音模型清单</small>}
              {ttsSelected && !ttsSelected.ready && (
                <small className="field-hint">未安装：<Link to="/tts">去「语音合成」页下载</Link></small>
              )}
            </div>
          </div>
          {ttsConfig && ttsConfig.voices.length > 0 && (
            <label className="setting-row" htmlFor="tts-voice">
              <span>
                <strong>默认音色</strong>
                <small>音色来自一段参考音频（ZipVoice 零样本克隆）。内置音色随模型发布；用自己的声音，去「语音合成」页上传参考音频。语速在那里调。</small>
              </span>
              <select
                id="tts-voice"
                value={settings.tts_voice ?? ttsConfig.voice}
                onChange={(event) => save({ tts_voice: event.target.value })}
              >
                <optgroup label="内置音色">
                  {ttsConfig.voices.filter((item) => item.builtin).map((item) => (
                    <option key={item.id} value={item.id}>{item.name}</option>
                  ))}
                </optgroup>
                {ttsConfig.voices.some((item) => !item.builtin) && (
                  <optgroup label="我的音色">
                    {ttsConfig.voices.filter((item) => !item.builtin).map((item) => (
                      <option key={item.id} value={item.id}>{item.name}</option>
                    ))}
                  </optgroup>
                )}
              </select>
            </label>
          )}
        </section>
        <section className="settings-section panel">
          <div className="settings-heading"><span><CloudDownload size={19} /></span><div><h2>兜底解析</h2><p>本机解析失败时的备用通道。</p></div></div>
          <div className="setting-row setting-row-wide">
            <span>
              <label htmlFor="fallback-api-key"><strong>兜底解析接口 API Key</strong></label>
              <small>自建解析和 yt-dlp 都拿不到视频时（多为平台风控），改用第三方聚合接口取无水印直链继续识别；视频号只能走这条通道，必须配置。还没有 Key 可以到 <a href="https://api-new.ifphp.com/" target="_blank" rel="noreferrer">api-new.ifphp.com</a> 注册账号获取；留空则只用本机解析，Key 加密保存在本机、不会回传到界面。</small>
            </span>
            <div className="setting-control">
              <div className="api-key-field">
                <input
                  id="fallback-api-key"
                  type="password"
                  autoComplete="off"
                  spellCheck={false}
                  placeholder={settings.fallback_api_key_set ? '已配置，留空不改动' : '粘贴 API Key'}
                  value={apiKey}
                  onChange={(event) => setApiKey(event.target.value)}
                  // 只写不读的字段不适合逐字保存：填完失焦（或回车）才提交
                  onBlur={commitApiKey}
                  onKeyDown={(event) => { if (event.key === 'Enter') event.currentTarget.blur() }}
                />
                {settings.fallback_api_key_set && (
                  <button className="text-button" type="button" onClick={clearApiKey}>
                    清除
                  </button>
                )}
              </div>
              <small className="field-hint">
                {settings.fallback_api_key_set
                  ? '已配置：主链路失败时会自动使用；填入新 Key 后离开输入框即保存'
                  : '未配置：主链路失败时直接报错；Key 加密保存在本机'}
              </small>
            </div>
          </div>
        </section>
        <section className="settings-section panel">
          <div className="settings-heading"><span><HardDrive size={19} /></span><div><h2>本地存储</h2><p>当前版本由工作台统一管理文件位置。</p></div></div>
          <div className="info-note ok">
            <ShieldCheck size={20} />
            <span><strong>文案和模型都保存在这台电脑上</strong><small>临时音视频默认在处理后清理，模型位置可在「模型管理」查看。</small></span>
          </div>
          <label className="toggle-row" htmlFor="keep-media">
            <span><strong>保留原始音视频</strong><small>开启后可在编辑页对照收听，但会持续占用磁盘空间。</small></span>
            <input id="keep-media" type="checkbox" checked={settings.keep_media} onChange={(event) => save({ keep_media: event.target.checked })} />
            <span className="toggle" aria-hidden="true" />
          </label>
        </section>
        <section className="settings-section panel">
          <div className="settings-heading"><span><Info size={19} /></span><div><h2>关于与更新</h2><p>查看版本、检查并下载新版本。</p></div></div>
          <div className="setting-row">
            <span>
              <strong>当前版本</strong>
              <small>
                {updates ? `v${updates.current_version} · ${updates.packaged ? '打包版（免安装）' : '开发模式'}` : version ? `v${version}` : '正在读取版本信息…'}
                {updates?.checked_at ? ` · 上次检查 ${checkedLabel(updates.checked_at)}` : ''}
              </small>
            </span>
            <div className="setting-control">
              <button className="secondary-button" type="button" disabled={checking} onClick={() => void checkForUpdates()}>
                {checking ? <LoaderCircle className="spin" size={16} /> : <RefreshCw size={16} />}
                {checking ? '检查中' : '检查更新'}
              </button>
            </div>
          </div>

          {updates?.has_update && updates.latest ? (
            <div className="update-block">
              <div className="update-head">
                <strong>发现新版本 v{updates.latest.version}</strong>
                <small>{publishedLabel(updates.latest.published_at)}</small>
              </div>
              {updates.latest.notes && (
                <div className="update-notes">
                  <button className="text-button" type="button" onClick={() => setNotesOpen((value) => !value)}>
                    {notesOpen ? '收起更新说明' : '查看更新说明'}
                  </button>
                  {notesOpen && <pre className="release-notes">{updates.latest.notes}</pre>}
                </div>
              )}

              {(running || download?.status === 'ready' || download?.status === 'failed') && download && (
                <div className="update-progress" role="status" aria-live="polite">
                  <ProgressBar value={download.percent} minPercent={2} label="更新包下载进度" />
                  <div className="update-progress-meta">
                    <span>{download.error ?? download.message}</span>
                    <span>{download.total_bytes > 0 ? `${formatBytes(download.downloaded_bytes)} / ${formatBytes(download.total_bytes)}` : ''}</span>
                  </div>
                </div>
              )}

              <div className="update-actions">
                {running ? (
                  <button className="secondary-button" type="button" disabled={updateBusy === 'cancel'} onClick={() => void cancelDownload()}>
                    <X size={16} /> 取消下载
                  </button>
                ) : (
                  <button className="primary-button" type="button" disabled={updateBusy === 'download'} onClick={() => void startDownload()}>
                    {updateBusy === 'download' ? <LoaderCircle className="spin" size={16} /> : <Download size={16} />}
                    {download?.status === 'failed' ? '重新下载' : '下载新版'}
                  </button>
                )}
                {updates.latest.page_url && (
                  <a className="text-button" href={updates.latest.page_url} target="_blank" rel="noreferrer"><ExternalLink size={15} /> 前往发布页</a>
                )}
                <button className="text-button" type="button" disabled={updateBusy === 'ignore'} onClick={() => void ignoreVersion()}>跳过此版本</button>
              </div>
              {updateMessage && <p className="update-status" role="status">{updateMessage}</p>}
            </div>
          ) : (
            (statusText || updateMessage || updates?.ignored_version) && (
              <p className="update-status" role="status">
                {statusText}
                {updates?.ignored_version && (
                  <>
                    {statusText ? ' ' : ''}
                    <button className="text-button" type="button" disabled={updateBusy === 'ignore'} onClick={() => void restoreVersion()}>恢复提示</button>
                  </>
                )}
                {updateMessage ? `${statusText ? ' ' : ''}${updateMessage}` : ''}
              </p>
            )
          )}

          {updates?.last_install && updates.last_install.status !== 'succeeded' && (
            <p className={updates.last_install.status === 'failed' ? 'update-status error' : 'update-status'} role="status">
              {updates.last_install.status === 'running'
                ? `正在完成 v${updates.last_install.version} 的自动更新…`
                : `上次自动更新失败：${updates.last_install.message}`}
              {updates.last_install.status === 'failed' && (
                <small className="update-log-path">日志：{updates.last_install.log_path}</small>
              )}
            </p>
          )}

          {packageFile && (
            <div className="update-package">
              <span>
                <strong>新版本已经下载到本机</strong>
                <small>{packageFile.file_name} · {formatBytes(packageFile.size)}</small>
              </span>

              {install && (installing || install.status === 'failed') && (
                <div className="update-progress" role="status" aria-live="polite">
                  <ProgressBar value={install.percent} minPercent={2} label="更新安装进度" />
                  <div className="update-progress-meta">
                    <span>{install.error ?? install.message}</span>
                    <span>{installing ? `${install.percent}%` : ''}</span>
                  </div>
                </div>
              )}

              <div className="update-actions">
                {!installing && updates?.install_supported && (
                  <button className="primary-button" type="button" disabled={updateBusy === 'install'} onClick={() => void startInstall()}>
                    {updateBusy === 'install' ? <LoaderCircle className="spin" size={16} /> : <RefreshCw size={16} />}
                    {updateBusy === 'install' ? '正在准备' : '立即更新并重启'}
                  </button>
                )}
                {installing && (
                  <button className="primary-button" type="button" disabled>
                    <LoaderCircle className="spin" size={16} /> 正在准备更新
                  </button>
                )}
                <button className="secondary-button" type="button" disabled={installing} onClick={() => void revealPackage()}><FolderOpen size={16} /> 打开下载目录</button>
                <button className="text-button" type="button" disabled={updateBusy === 'remove' || installing} onClick={() => void removePackage()}><Trash2 size={15} /> 删除安装包</button>
              </div>

              {updates?.install_supported ? (
                <p className="field-hint">
                  点击后程序会自动退出，把当前程序目录备份一份、替换成新版本并重新启动；新版本启动失败会自动回滚。
                  正在识别的任务会被中断，建议先等任务结束。
                </p>
              ) : (
                <p className="field-hint">
                  {updates?.install_reason ?? '当前程序目录不能自动替换。'}
                  也可以手动解压：关闭工作台后把压缩包解压到新的程序目录（新版目录名是「创作工作室」）。
                  文案、模型与平台凭据都在数据目录里，换程序目录不会丢；旧版「文案工作台」的数据会被直接沿用。
                </p>
              )}
              {installMessage && <p className="update-status error" role="alert">{installMessage}</p>}
            </div>
          )}

          <label className="toggle-row" htmlFor="auto-check-update">
            <span><strong>启动时自动检查更新</strong><small>打开界面时查一次（超过 12 小时才会真的联网），也可以在「检查更新」里手动查。</small></span>
            <input id="auto-check-update" type="checkbox" checked={settings.auto_check_update} onChange={(event) => void toggleAutoCheck(event.target.checked)} />
            <span className="toggle" aria-hidden="true" />
          </label>
        </section>
      </div>
    </div>
  )
}
