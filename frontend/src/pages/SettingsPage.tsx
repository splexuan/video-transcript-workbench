import { Check, CloudDownload, HardDrive, LoaderCircle, MonitorCog, ShieldCheck } from 'lucide-react'
import { FormEvent, useEffect, useState } from 'react'

import { api } from '../lib/api'
import { shortModelName } from '../lib/labels'
import type { AppSettings, RecognitionModel } from '../types'

const defaults: AppSettings = { theme: 'system', default_model: '', prefer_subtitle: false, keep_media: false, storage_path: '', fallback_api_key_set: false }

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

  return (
    <div className="page settings-page">
      <header className="page-header"><div><h1>设置</h1><p>设置新任务的默认识别模型和工作台外观。</p></div></header>
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
              <small>自建解析和 yt-dlp 都拿不到视频时（多为平台风控），改用第三方聚合接口取无水印直链继续识别。留空则只用本机解析；Key 加密保存在本机，不会回传到界面。</small>
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
      </form>
    </div>
  )
}
