import {
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
import { FormEvent, useCallback, useEffect, useState } from 'react'

import { api } from '../lib/api'
import { formatBytes } from '../lib/format'
import { shortModelName } from '../lib/labels'
import type { AppSettings, RecognitionModel, UpdatePackage, UpdateProgress, UpdateState } from '../types'

const defaults: AppSettings = { theme: 'system', default_model: '', prefer_subtitle: false, keep_media: false, storage_path: '', fallback_api_key_set: false, auto_check_update: true, ignored_version: '' }

/** 下载与校验都算「进行中」：这期间要持续轮询进度。 */
function isRunning(progress: UpdateProgress | null) {
  return progress?.status === 'pending' || progress?.status === 'running' || progress?.status === 'verifying'
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
  const [models, setModels] = useState<RecognitionModel[]>([])
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState('')
  const [modelsLoading, setModelsLoading] = useState(true)
  const [modelsError, setModelsError] = useState('')
  const [modelsReloadKey, setModelsReloadKey] = useState(0)
  // 兜底解析的 Key 只写不读：输入框留空表示「不动已配置的 Key」
  const [apiKey, setApiKey] = useState('')
  const [apiKeyCleared, setApiKeyCleared] = useState(false)
  // 版本与更新
  const [updates, setUpdates] = useState<UpdateState | null>(null)
  const [checking, setChecking] = useState(false)
  const [notesOpen, setNotesOpen] = useState(false)
  const [updateBusy, setUpdateBusy] = useState('')
  const [updateMessage, setUpdateMessage] = useState('')

  const download = updates?.download ?? null
  const running = isRunning(download)
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

  useEffect(() => {
    api.settings().then(setSettings).catch((reason: Error) => setMessage(reason.message))
  }, [])

  useEffect(() => {
    let active = true
    api.models()
      .then((catalog) => {
        if (active) setModels(catalog.models)
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

  // 首次打开就去问一次版本：后端按设置节流，关掉自动检查时连请求都不会发出去。
  useEffect(() => {
    let active = true
    api.updates()
      .then((state) => {
        if (active) publish(state)
      })
      .catch((reason: Error) => {
        // 本地服务还是旧版本时这里会 404：说出来，别让卡片一直停在「正在读取」
        if (active) setUpdateMessage(`读取版本信息失败：${reason.message}`)
      })
    return () => {
      active = false
    }
  }, [publish])

  // 下载中轮询进度；依赖布尔值，保证定时器在轮询期间保持稳定。
  useEffect(() => {
    if (!running) return
    const timer = window.setInterval(() => {
      void reloadUpdates().catch(() => undefined)
    }, 1200)
    return () => window.clearInterval(timer)
  }, [running, reloadUpdates])

  async function submit(event: FormEvent) {
    event.preventDefault()
    setSaving(true)
    setMessage('')
    // Key 留空表示不改动已配置的值，所以只在「填了」或「点了清除」时才带上
    const payload: Partial<AppSettings> = { ...settings }
    if (apiKeyCleared) payload.fallback_api_key = ''
    else if (apiKey.trim()) payload.fallback_api_key = apiKey.trim()
    else delete payload.fallback_api_key
    try {
      const saved = await api.updateSettings(payload)
      setSettings(saved)
      setApiKey('')
      setApiKeyCleared(false)
      window.dispatchEvent(new CustomEvent('vtw:theme', { detail: saved.theme }))
      setMessage('设置已保存在本机')
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  function toggleClearApiKey() {
    setApiKey('')
    setApiKeyCleared((value) => !value)
  }

  async function checkForUpdates() {
    setChecking(true)
    setUpdateMessage('')
    try {
      const state = await api.checkUpdates()
      publish(state)
      setUpdateMessage(state.error ?? (state.has_update || state.ignored_version ? '' : '已经是最新版本'))
    } catch (reason) {
      setUpdateMessage(reason instanceof Error ? reason.message : '检查更新失败')
    } finally {
      setChecking(false)
    }
  }

  /** 回填时只动这两个字段：设置页可能还有没保存的编辑，不能整份覆盖回去。 */
  async function patchUpdateSetting(payload: Partial<AppSettings>) {
    const saved = await api.updateSettings(payload)
    setSettings((current) => ({ ...current, ignored_version: saved.ignored_version, auto_check_update: saved.auto_check_update }))
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
    const version = updates?.latest?.version
    if (!version) return
    setUpdateBusy('ignore')
    try {
      await patchUpdateSetting({ ignored_version: version })
      setUpdateMessage(`已跳过 v${version}，不再提示`)
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
    setSettings((current) => ({ ...current, auto_check_update: next }))
    setUpdateMessage('')
    try {
      await patchUpdateSetting({ auto_check_update: next })
      if (next) setUpdateMessage('已开启：启动时会自动检查新版本')
    } catch (reason) {
      setSettings((current) => ({ ...current, auto_check_update: !next }))
      setUpdateMessage(reason instanceof Error ? reason.message : '保存失败')
    }
  }

  return (
    <div className="page settings-page">
      <header className="page-header"><div><h1>设置</h1><p>设置新任务的默认识别模型、外观与版本更新。</p></div></header>
      <form onSubmit={submit} className="settings-layout">
        <section className="settings-section panel">
          <div className="settings-heading"><span><MonitorCog size={19} /></span><div><h2>任务偏好</h2><p>新建提取时的默认行为。</p></div></div>
          <div className="setting-row">
            <span><label htmlFor="default-model"><strong>默认识别模型</strong></label><small>没有可用平台字幕时，默认使用这个模型；工作台可临时切换。</small></span>
            <div className="setting-control">
              <select id="default-model" value={settings.default_model} disabled={modelsLoading || Boolean(modelsError)} aria-describedby={modelsError ? 'default-model-error' : undefined} onChange={(event) => setSettings({ ...settings, default_model: event.target.value })}>
                {modelsLoading && <option value={settings.default_model}>正在读取模型…</option>}
                {!modelsLoading && modelsError && <option value={settings.default_model}>{settings.default_model ? shortModelName(settings.default_model) : '暂不可用'}</option>}
                {!modelsLoading && !modelsError && models.map((model) => <option key={model.id} value={model.id} disabled={model.state !== 'ready'}>{shortModelName(model.name)}{model.state === 'ready' ? '' : '（未安装）'}</option>)}
              </select>
              {modelsError && <span className="field-error" id="default-model-error" role="alert">{modelsError}<button className="text-button" type="button" onClick={() => { setModelsLoading(true); setModelsError(''); setModelsReloadKey((value) => value + 1) }}>重试</button></span>}
            </div>
          </div>
          <label className="setting-row" htmlFor="theme"><span><strong>界面主题</strong><small>选「跟随系统」时会随 Windows 深浅色自动切换。</small></span><select id="theme" value={settings.theme} onChange={(event) => setSettings({ ...settings, theme: event.target.value as AppSettings['theme'] })}><option value="system">跟随系统</option><option value="light">浅色</option><option value="dark">深色</option></select></label>
        </section>
        <section className="settings-section panel">
          <div className="settings-heading"><span><CloudDownload size={19} /></span><div><h2>兜底解析</h2><p>本机解析失败时的备用通道。</p></div></div>
          <div className="setting-row">
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
                  placeholder={apiKeyCleared ? '保存后清除' : settings.fallback_api_key_set ? '已配置，留空不改动' : '粘贴 API Key'}
                  value={apiKey}
                  disabled={apiKeyCleared}
                  onChange={(event) => setApiKey(event.target.value)}
                />
                {settings.fallback_api_key_set && (
                  <button className="text-button" type="button" onClick={toggleClearApiKey}>
                    {apiKeyCleared ? '取消' : '清除'}
                  </button>
                )}
              </div>
              <small className="field-hint">
                {apiKeyCleared
                  ? '保存后移除已配置的 Key'
                  : settings.fallback_api_key_set
                    ? '已配置：主链路失败时会自动使用'
                    : '未配置：主链路失败时直接报错'}
              </small>
            </div>
          </div>
        </section>
        <section className="settings-section panel">
          <div className="settings-heading"><span><HardDrive size={19} /></span><div><h2>本地存储</h2><p>当前版本由工作台统一管理文件位置。</p></div></div>
          <div className="storage-summary">
            <ShieldCheck size={20} />
            <span><strong>文案和模型都保存在这台电脑上</strong><small>临时音视频默认在处理后清理，模型位置可在「模型管理」查看。</small></span>
          </div>
          <label className="toggle-row" htmlFor="keep-media">
            <span><strong>保留原始音视频</strong><small>开启后可在编辑页对照收听，但会持续占用磁盘空间。</small></span>
            <input id="keep-media" type="checkbox" checked={settings.keep_media} onChange={(event) => setSettings({ ...settings, keep_media: event.target.checked })} />
            <span className="toggle" aria-hidden="true" />
          </label>
          <div className="settings-actions">
            <span className="save-message" role="status">{message && <><Check size={15} /> {message}</>}</span>
            <button className="primary-button" type="submit" disabled={saving}>{saving ? <LoaderCircle className="spin" size={17} /> : <Check size={17} />}{saving ? '保存中' : '保存设置'}</button>
          </div>
        </section>
        <section className="settings-section panel">
          <div className="settings-heading"><span><Info size={19} /></span><div><h2>关于与更新</h2><p>查看版本、检查并下载新版本。</p></div></div>
          <div className="setting-row">
            <span>
              <strong>当前版本</strong>
              <small>
                {updates ? `v${updates.current_version} · ${updates.packaged ? '打包版（免安装）' : '开发模式'}` : '正在读取版本信息…'}
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
                  <div className="progress-track"><span style={{ width: `${Math.max(download.percent, 2)}%` }} /></div>
                  <div className="update-progress-meta">
                    <span>{download.error ?? download.message}</span>
                    <span>{download.total_bytes > 0 ? `${formatBytes(download.downloaded_bytes)} / ${formatBytes(download.total_bytes)}` : ''}</span>
                  </div>
                </div>
              )}

              <div className="settings-actions">
                <span className="save-message" role="status">{updateMessage}</span>
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
              </div>
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

          {packageFile && (
            <div className="update-package">
              <span>
                <strong>新版本已经下载到本机</strong>
                <small>{packageFile.file_name} · {formatBytes(packageFile.size)}</small>
              </span>
              <div className="update-actions">
                <button className="secondary-button" type="button" onClick={() => void revealPackage()}><FolderOpen size={16} /> 打开下载目录</button>
                <button className="text-button" type="button" disabled={updateBusy === 'remove'} onClick={() => void removePackage()}><Trash2 size={15} /> 删除安装包</button>
              </div>
              <p className="field-hint">
                关闭工作台后，把压缩包解压覆盖程序目录即可完成更新。文案、模型与平台凭据都在数据目录里，替换程序目录不会丢；也可以直接到发布页下载新版。
              </p>
            </div>
          )}

          <label className="toggle-row" htmlFor="auto-check-update">
            <span><strong>启动时自动检查更新</strong><small>打开界面时查一次（超过 12 小时才会真的联网），也可以在「检查更新」里手动查。</small></span>
            <input id="auto-check-update" type="checkbox" checked={settings.auto_check_update} onChange={(event) => void toggleAutoCheck(event.target.checked)} />
            <span className="toggle" aria-hidden="true" />
          </label>
        </section>
      </form>
    </div>
  )
}
