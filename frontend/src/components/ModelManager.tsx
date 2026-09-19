import {
  AlertTriangle,
  CheckCircle2,
  Cpu,
  Download,
  HardDrive,
  LoaderCircle,
  RefreshCw,
  Sparkles,
  Trash2,
  X,
  Zap,
} from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'

import { api } from '../lib/api'
import { LoadingState } from './LoadingState'
import type { ModelCatalog, RecognitionModel } from '../types'

function formatBytes(value: number | null | undefined) {
  if (!value || value <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let size = value
  let index = 0
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024
    index += 1
  }
  return `${index === 0 ? Math.round(size) : size.toFixed(1)} ${units[index]}`
}

const stateLabels: Record<RecognitionModel['state'], string> = {
  ready: '可用',
  missing: '未安装',
  partial: '待补全',
  broken: '文件异常',
  installing: '安装中',
}

function stateTone(model: RecognitionModel) {
  if (model.state === 'ready') return 'ready'
  if (model.state === 'installing') return 'installing'
  if (model.state === 'missing') return 'missing'
  return 'attention'
}

function displayModelName(model: RecognitionModel) {
  return model.recommended ? model.name.replace(/[（(]推荐[）)]/g, '').trim() : model.name
}

const engineLabels: Record<string, string> = {
  sensevoice: '极速文本引擎',
  faster_whisper: '精准时间轴引擎',
}

const engineIcons: Record<string, typeof Zap> = {
  sensevoice: Zap,
  faster_whisper: Sparkles,
}

export function ModelManager() {
  const [catalog, setCatalog] = useState<ModelCatalog | null>(null)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [reloadKey, setReloadKey] = useState(0)

  const models = catalog?.models ?? []
  const engines = catalog?.engines ?? []
  const storage = catalog?.storage ?? null
  const installing = (catalog?.active_tasks.length ?? 0) > 0

  const refresh = useCallback(() => setReloadKey((value) => value + 1), [])

  // 首次加载与手动刷新：只在回调里更新状态，避免 effect 内同步 setState。
  useEffect(() => {
    let active = true
    api.models()
      .then((next) => {
        if (!active) return
        setCatalog(next)
        setError('')
      })
      .catch((reason: Error) => {
        if (active) setError(reason.message || '读取模型状态失败')
      })
    return () => {
      active = false
    }
  }, [reloadKey])

  // 有安装任务时轮询进度；依赖布尔值，保证定时器在轮询期间保持稳定。
  useEffect(() => {
    if (!installing) return
    const timer = window.setInterval(refresh, 1200)
    return () => window.clearInterval(timer)
  }, [installing, refresh])

  async function run(id: string, action: () => Promise<unknown>, done: string) {
    setBusy(id)
    setError('')
    setNotice('')
    try {
      await action()
      setNotice(done)
      refresh()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '操作失败')
    } finally {
      setBusy('')
    }
  }

  async function remove(model: RecognitionModel) {
    if (!window.confirm(`删除 ${model.name}？删除后需要重新下载才能继续使用。`)) return
    await run(model.id, () => api.deleteModel(model.id), `${model.name} 已删除`)
  }

  return (
    <section className="panel model-manager" aria-labelledby="model-manager-title">
      <div className="section-heading">
        <div>
          <h2 id="model-manager-title">识别模型</h2>
          <p>模型装在本机，装好之后不联网也能识别。</p>
        </div>
        <button className="text-button" type="button" onClick={refresh}>
          <RefreshCw size={15} /> 刷新
        </button>
      </div>

      {catalog === null && !error && <LoadingState label="正在检查本机模型…" rows={4} compact />}

      <div className="engine-summary">
        {engines.map((engine) => {
          const label = engineLabels[engine.engine] ?? '识别引擎'
          const Icon = engineIcons[engine.engine] ?? Cpu
          return (
            <div className="engine-pill" key={engine.engine}>
              <span className={`engine-icon ${engine.ready ? 'ready' : ''}`}><Icon size={16} /></span>
              <div>
                <strong>{label}</strong>
                <small>
                  {engine.ready
                    ? '已就绪'
                    : !engine.package_ready
                      ? '缺少运行组件'
                      : '安装下方任一模型后可用'}
                </small>
              </div>
            </div>
          )
        })}
      </div>

      {error && <p className="form-message error" role="alert">{error}</p>}
      {notice && <p className="form-message success" role="status">{notice}</p>}

      <div className="model-list">
        {models.map((model) => {
          const progress = model.progress
          const installingNow = model.state === 'installing' && progress !== null
          const working = busy === model.id
          return (
            <article className="model-row" key={model.id}>
              <div className="model-row-head">
                <div className="model-row-title">
                  <span className={`model-state ${stateTone(model)}`}>
                    {model.state === 'ready' ? <CheckCircle2 size={14} /> : model.state === 'installing' ? <LoaderCircle className="spin" size={14} /> : model.state === 'broken' ? <AlertTriangle size={14} /> : <Cpu size={14} />}
                    {stateLabels[model.state]}
                  </span>
                  <h3>
                    {displayModelName(model)}
                    {model.recommended && <span className="model-tag">推荐</span>}
                  </h3>
                </div>
                <span className="model-size">
                  {model.state === 'ready'
                    ? formatBytes(model.installed_bytes)
                    : `约 ${formatBytes(model.approx_bytes)}`}
                </span>
              </div>

              <p className="model-desc">{model.description}</p>
              <div className="model-meta">
                <span>支持语言：{model.languages}</span>
                {model.note && <span>{model.note}</span>}
              </div>

              {installingNow && progress && (
                <div className="model-progress" role="status" aria-live="polite">
                  <div className="progress-track">
                    <span style={{ width: `${Math.max(progress.percent, 2)}%` }} />
                  </div>
                  <div className="model-progress-meta">
                    <span>{progress.message}</span>
                    <span>
                      {progress.total_bytes > 0
                        ? `${formatBytes(progress.downloaded_bytes)} / ${formatBytes(progress.total_bytes)}`
                        : `${progress.completed_files}/${progress.total_files} 个文件`}
                    </span>
                  </div>
                </div>
              )}

              <div className="model-actions">
                {installingNow ? (
                  <button
                    className="secondary-button"
                    type="button"
                    disabled={working}
                    onClick={() => void run(model.id, () => api.cancelModelInstall(model.id), '正在取消安装')}
                  >
                    <X size={15} /> 取消安装
                  </button>
                ) : model.state === 'ready' ? (
                  <>
                    <button
                      className="secondary-button"
                      type="button"
                      disabled={working}
                      onClick={() => void run(model.id, () => api.verifyModel(model.id), '校验通过，模型可用')}
                    >
                      {working ? <LoaderCircle className="spin" size={15} /> : <RefreshCw size={15} />} 校验
                    </button>
                    <button
                      className="icon-button danger model-delete"
                      type="button"
                      disabled={working}
                      onClick={() => void remove(model)}
                      aria-label={`删除模型 ${model.name}`}
                      title="删除模型"
                    >
                      <Trash2 size={15} />
                    </button>
                  </>
                ) : (
                  <button
                    className="primary-button"
                    type="button"
                    disabled={working}
                    onClick={() => void run(model.id, () => api.downloadModel(model.id), `已开始下载 ${model.name}`)}
                  >
                    {working ? <LoaderCircle className="spin" size={15} /> : <Download size={15} />}
                    {model.state === 'partial' || model.state === 'broken' ? '重新下载' : '下载模型'}
                  </button>
                )}
              </div>
            </article>
          )
        })}
      </div>

      {storage && (
        <footer className="model-storage">
          <span><HardDrive size={14} /> 模型目录：<code>{storage.models_root}</code></span>
          <span>已占用 {formatBytes(storage.total_bytes)}</span>
          {storage.disk_free_bytes !== null && <span>磁盘剩余 {formatBytes(storage.disk_free_bytes)}</span>}
        </footer>
      )}
    </section>
  )
}
