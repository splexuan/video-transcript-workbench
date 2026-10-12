import {
  AudioLines,
  Bot,
  Check,
  CloudDownload,
  Download,
  HardDrive,
  Info,
  LoaderCircle,
  MonitorCog,
  ShieldCheck,
} from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../lib/api'
import { invalidateAiProviders } from '../lib/ai'
import { shortModelName } from '../lib/labels'
import type { AiProviderCatalog, AiProviderType, AppSettings, RecognitionModel, TtsConfig } from '../types'

const defaults: AppSettings = { theme: 'system', default_model: '', prefer_subtitle: false, keep_media: false, storage_path: '', fallback_api_key_set: false, ai_provider: 'deepseek', ai_model: '', ai_proxy: '', tts_model: '', tts_voice: null }

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
  // Codex 代理：null 表示「没在编辑」，显示服务端值；打字期间先留在草稿里
  const [proxyDraft, setProxyDraft] = useState<string | null>(null)
  // 版本号：只用于「关于」里显示当前是哪个版本，不联网
  const [version, setVersion] = useState('')
  // AI 驱动清单：本机引擎要探测登录态（并读一次对话模型），所以进来就重新取一次
  const [aiCatalog, setAiCatalog] = useState<AiProviderCatalog | null>(null)
  const [aiError, setAiError] = useState('')
  const [aiReloadKey, setAiReloadKey] = useState(0)
  // 语音合成：模型与音色清单（音色来自 ZipVoice 的参考音频）
  const [ttsConfig, setTtsConfig] = useState<TtsConfig | null>(null)

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
          // 编辑页/预览弹窗缓存了驱动清单，换过驱动之后必须作废，否则还按旧的选择分流
          invalidateAiProviders()
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
    api.aiProviders()
      .then((catalog) => {
        if (!active) return
        setAiCatalog(catalog)
        setAiError('')
      })
      .catch(() => {
        if (active) setAiError('AI 驱动读取失败，请检查本地服务后重试。')
      })
    return () => {
      active = false
    }
  }, [aiReloadKey])

  // 语音合成的可选模型与音色：模型没装时下拉里仍列出来（标「未安装」），
  // 音色表则完全来自模型规格，这里只读不改
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

  /**
   * 当前选中的驱动。
   *
   * 保存过的驱动可能已经不可用（客户端退登、模型下架），这时按后端返回的 preference 显示，
   * 免得界面上「选中一个选不了的项」，用户一保存又变成另一个驱动。
   */
  const savedAiProvider = aiCatalog?.providers.find((provider) => provider.type === settings.ai_provider)
  const aiProvider = savedAiProvider?.available
    ? savedAiProvider
    : aiCatalog?.providers.find((provider) => provider.type === aiCatalog.preference.provider) ?? null
  /** 设置里选中的语音模型（空串表示「自动」）；用来提示「未安装」。 */
  const ttsSelected = ttsConfig?.models.find((model) => model.id === settings.tts_model) ?? null
  /**
   * 用不了的本机驱动。它们在下拉里是 disabled、选不中，所以原因必须单独列出来，
   * 否则用户只看到「（不可用）」却不知道是缺依赖、没登录还是模型下架。
   */
  const unavailableProviders = (aiCatalog?.providers ?? []).filter((provider) => !provider.available)
  // 保存的模型可能已下架：列表里没有就回到该驱动的默认模型，避免下拉显示空白
  const aiModelValue =
    aiProvider && aiProvider.models.some((model) => model.id === settings.ai_model)
      ? settings.ai_model
      : aiProvider?.default_model ?? ''

  /** 换驱动时把模型一起带回该驱动的默认值，否则会留着上一个驱动的模型名。 */
  function chooseAiProvider(type: AiProviderType) {
    const entry = aiCatalog?.providers.find((provider) => provider.type === type)
    save({ ai_provider: type, ai_model: entry?.default_model ?? '' })
  }

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

  /** 代理地址：失焦时才提交，打字过程中不被自动保存打断。 */
  function commitProxy() {
    if (proxyDraft === null) return
    save({ ai_proxy: proxyDraft.trim() })
    setProxyDraft(null)
  }


  return (
    <div className="page settings-page">
      <header className="page-header">
        <div><h1>设置</h1><p>默认识别模型、AI 总结驱动、外观与本地存储。改动会即时保存，不需要手动提交。</p></div>
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
          <div className="settings-heading"><span><Bot size={19} /></span><div><h2>AI 驱动</h2><p>「AI 总结」交给哪个 AI 来做。</p></div></div>
          <div className="setting-row">
            <span>
              <label htmlFor="ai-provider"><strong>总结使用的 AI</strong></label>
              <small>DeepSeek 会打开网页、用你自己的账号完成；WorkBuddy / Codex 直接复用本机客户端已登录的账号，摘要回到这里展示，不跳转浏览器。</small>
            </span>
            <div className="setting-control">
              <select
                id="ai-provider"
                value={aiProvider?.type ?? settings.ai_provider}
                onChange={(event) => chooseAiProvider(event.target.value as AiProviderType)}
              >
                {!aiCatalog && <option value={settings.ai_provider}>{settings.ai_provider === 'deepseek' ? 'DeepSeek · 网页版' : settings.ai_provider}</option>}
                {(aiCatalog?.providers ?? []).map((provider) => (
                  <option key={provider.type} value={provider.type} disabled={!provider.available}>
                    {provider.name}{provider.available ? '' : '（不可用）'}
                  </option>
                ))}
              </select>
              {aiError && <span className="field-error" role="alert">{aiError}<button className="text-button" type="button" onClick={() => setAiReloadKey((value) => value + 1)}>重试</button></span>}
              {!aiError && aiProvider && <small className="field-hint">{aiProvider.message || aiProvider.note}</small>}
            </div>
          </div>
          {/* 不可用的驱动在下拉里是 disabled 的，选不中，上面的 field-hint 也就只显示当前
              选中那一个。不把原因单独列出来的话，用户只会看到「（不可用）」三个字，
              而「为什么不可用、该怎么办」全在后端返回的 message 里，等于白写了。 */}
          {!aiError && unavailableProviders.length > 0 && (
            <div className="ai-unavailable">
              <strong>这些驱动暂时用不了</strong>
              {unavailableProviders.map((provider) => (
                <small key={provider.type}>
                  <b>{provider.name}</b>：{provider.message || '原因未知'}
                </small>
              ))}
            </div>
          )}
          {/* 只有本机驱动才有模型可选；DeepSeek 的模型在它自己网页里选 */}
          {aiProvider && aiProvider.models.length > 0 && (
            <label className="setting-row" htmlFor="ai-model">
              <span>
                <strong>对话模型</strong>
                <small>本机账号当前开放的对话模型；默认用列表里的第一个。</small>
              </span>
              <select id="ai-model" value={aiModelValue} onChange={(event) => save({ ai_model: event.target.value })}>
                {aiProvider.models.map((model) => <option key={model.id} value={model.id}>{model.label}</option>)}
              </select>
            </label>
          )}
          {/* chatgpt.com 通常要走代理，Codex 单独留一个可选项 */}
          {aiProvider?.type === 'codex' && (
            <label className="setting-row" htmlFor="ai-proxy">
              <span>
                <strong>Codex 代理（可选）</strong>
                <small>本机直连 chatgpt.com 不通时填代理地址；留空则用系统环境变量里的代理设置，只对 Codex 生效。</small>
              </span>
              <input
                id="ai-proxy"
                type="text"
                autoComplete="off"
                spellCheck={false}
                placeholder="http://127.0.0.1:7890"
                value={proxyDraft ?? settings.ai_proxy}
                onChange={(event) => setProxyDraft(event.target.value)}
                onBlur={commitProxy}
                onKeyDown={(event) => { if (event.key === 'Enter') event.currentTarget.blur() }}
              />
            </label>
          )}
        </section>
        <section className="settings-section panel">
          <div className="settings-heading"><span><AudioLines size={19} /></span><div><h2>语音合成</h2><p>「语音合成」页默认用哪个模型和音色。</p></div></div>
          <div className="setting-row">
            <span>
              <label htmlFor="tts-model"><strong>默认语音模型</strong></label>
              <small>把文案读成音频的本地模型（ZipVoice，CPU 推理）。没装的话到「语音合成」页或「模型管理」下载，约 156 MB。</small>
            </span>
            <div className="setting-control">
              <select id="tts-model" value={settings.tts_model} onChange={(event) => save({ tts_model: event.target.value })}>
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
          <div className="settings-heading"><span><Info size={19} /></span><div><h2>关于</h2><p>版本信息与更新方式。</p></div></div>
          <div className="setting-row">
            <span>
              <strong>当前版本</strong>
              <small>{version ? `v${version}` : '正在读取版本信息…'}</small>
            </span>
          </div>
          {/* 程序不再联网检查新版本（发行包由人工分发，私有仓库匿名也查不到），
              所以这里只如实说明「怎么换到新版本」，不做任何入口 */}
          <div className="info-note">
            <Download size={20} />
            <span>
              <strong>更新方式：手动解压覆盖</strong>
              <small>
                拿到新的免安装包后，先退出工作台，再解压覆盖程序目录即可。
                文案、模型与平台凭据都在数据目录里，替换程序不会丢；工作台不会自己联网检查新版本。
              </small>
            </span>
          </div>
        </section>
      </div>
    </div>
  )
}
