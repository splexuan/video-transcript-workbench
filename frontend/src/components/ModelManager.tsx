import {
  AlertTriangle,
  AudioLines,
  CheckCircle2,
  Cpu,
  Download,
  HardDrive,
  LoaderCircle,
  RefreshCw,
  Sparkles,
  Trash2,
  Wand2,
  X,
  Zap,
} from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'

import { api } from '../lib/api'
import { RECOGNITION_ENGINES } from '../lib/models'
import { useConfirm } from '../lib/useConfirm'
import { formatBytes } from '../lib/format'
import { LoadingState } from './LoadingState'
import { ProgressBar } from './ProgressBar'
import type { ModelCatalog, RecognitionModel } from '../types'

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
  punctuation: '标点整理引擎',
  tts: '语音合成引擎',
}

const engineIcons: Record<string, typeof Zap> = {
  sensevoice: Zap,
  faster_whisper: Sparkles,
  punctuation: Wand2,
  tts: AudioLines,
}

/** 缺组件时提示用户装什么：直接报模块名（sherpa_onnx）对普通用户没意义。 */
const ENGINE_PACKAGE_NAMES: Record<string, string> = {
  sensevoice: 'sherpa-onnx 运行组件',
  faster_whisper: 'faster-whisper 运行组件',
  punctuation: 'sherpa-onnx 运行组件',
  tts: 'sherpa-onnx 运行组件',
}

/**
 * 模型按「用户什么时候需要它」分区，而不是按引擎罗列。
 *
 * 之前所有模型平铺在一个列表里，顶部的引擎概览又把它们的信息重复了一遍：
 * 用户要自己分辨哪几个属于识别、哪几个属于合成。加了分区之后，
 * 区块标题就能直接把状态、用途、体积说清，模型行只补一个引擎标签（识别区有两个引擎时需要区分）。
 *
 * 顺序即使用顺序：先把文案弄出来，再补标点，最后才轮到配音。
 */
const GROUPS = [
  {
    key: 'recognition',
    label: '识别与转写',
    hint: '把视频里的语音转成文字',
    engines: RECOGNITION_ENGINES,
  },
  {
    key: 'utility',
    label: '辅助处理',
    hint: '给没有标点的字幕补标点',
    engines: ['punctuation'],
  },
  {
    key: 'tts',
    label: '语音合成',
    hint: '把文案读成音频',
    engines: ['tts'],
  },
] as const

/** 一行模型卡片。抽出成组件是因为分组后同样的结构要出现三次。 */
function ModelRow({
  model,
  isDefault,
  showEngine,
  busy,
  onVerify,
  onDownload,
  onCancelInstall,
  onRemove,
}: {
  model: RecognitionModel
  isDefault: boolean
  /** 同一区里有多个引擎时才显示引擎标签，避免每行都挂一个冗余的标签 */
  showEngine: boolean
  busy: boolean
  onVerify: () => void
  onDownload: () => void
  onCancelInstall: () => void
  onRemove: () => void
}) {
  const progress = model.progress
  const installingNow = model.state === 'installing' && progress !== null
  const working = busy

  return (
    <article className="model-row">
      <div className="model-row-head">
        <div className="model-row-title">
          <span className={`model-state ${stateTone(model)}`}>
            {model.state === 'ready' ? <CheckCircle2 size={14} /> : model.state === 'installing' ? <LoaderCircle className="spin" size={14} /> : model.state === 'broken' ? <AlertTriangle size={14} /> : <Cpu size={14} />}
            {stateLabels[model.state]}
          </span>
          <h3>
            {displayModelName(model)}
            {/* 两个标签是两件事：默认用哪个由设置决定，推荐只是出厂建议 */}
            {isDefault && <span className="model-tag default">当前默认</span>}
            {model.recommended && <span className="model-tag">推荐</span>}
          </h3>
        </div>
        <span className="model-size">
          {model.state === 'ready'
            ? formatBytes(model.installed_bytes)
            : `约 ${formatBytes(model.approx_bytes)}`}
        </span>
      </div>

      {showEngine && (
        <span className="model-engine-tag">
          {engineLabels[model.engine] ?? model.engine}
        </span>
      )}

      <p className="model-desc">{model.description}</p>
      <div className="model-meta">
        <span>支持语言：{model.languages}</span>
        {model.note && <span>{model.note}</span>}
      </div>

      {installingNow && progress && (
        <div className="model-progress" role="status" aria-live="polite">
          <ProgressBar value={progress.percent} minPercent={2} label={`${displayModelName(model)} 下载进度`} />
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
          <button className="secondary-button" type="button" disabled={working} onClick={onCancelInstall}>
            <X size={15} /> 取消安装
          </button>
        ) : model.state === 'ready' ? (
          <>
            <button className="secondary-button" type="button" disabled={working} onClick={onVerify}>
              {working ? <LoaderCircle className="spin" size={15} /> : <RefreshCw size={15} />} 校验
            </button>
            <button
              className="icon-button danger model-delete"
              type="button"
              disabled={working}
              onClick={onRemove}
              aria-label={`删除模型 ${model.name}`}
              title="删除模型"
            >
              <Trash2 size={15} />
            </button>
          </>
        ) : (
          <button className="primary-button" type="button" disabled={working} onClick={onDownload}>
            {working ? <LoaderCircle className="spin" size={15} /> : <Download size={15} />}
            {model.state === 'partial' || model.state === 'broken' ? '重新下载' : '下载模型'}
          </button>
        )}
      </div>
    </article>
  )
}

export function ModelManager() {
  const [catalog, setCatalog] = useState<ModelCatalog | null>(null)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [reloadKey, setReloadKey] = useState(0)
  // 新建提取默认用哪个模型：只有设置里有，列表接口不带这个信息。
  // 「推荐」与「当前默认」是两件事——推荐的两个模型里只会有一个是默认的。
  const [defaultModel, setDefaultModel] = useState('')
  const confirm = useConfirm()

  const models = catalog?.models ?? []
  const engines = catalog?.engines ?? []
  const storage = catalog?.storage ?? null
  const installing = (catalog?.active_tasks.length ?? 0) > 0

  const refresh = useCallback(() => setReloadKey((value) => value + 1), [])

  // 首次加载与手动刷新：只在回调里更新状态，避免 effect 内同步 setState。
  useEffect(() => {
    let active = true
    // 设置读不到也不影响模型列表，所以单独兜住失败
    Promise.all([api.models(), api.settings().catch(() => null)])
      .then(([next, settings]) => {
        if (!active) return
        setCatalog(next)
        setDefaultModel(settings?.default_model ?? '')
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
    const confirmed = await confirm({
      title: `删除 ${displayModelName(model)}？`,
      description: '删除后需要重新下载才能继续使用；已提取的文案不受影响。',
      confirmLabel: '删除模型',
      tone: 'danger',
    })
    if (!confirmed) return
    await run(model.id, () => api.deleteModel(model.id), `${model.name} 已删除`)
  }

  return (
    <section className="panel model-manager" aria-labelledby="model-manager-title">
      <div className="section-heading">
        <div>
          <h2 id="model-manager-title">本机模型</h2>
          <p>模型装在本机，装好之后识别与配音都不联网。</p>
        </div>
        <button className="text-button" type="button" onClick={refresh}>
          <RefreshCw size={15} /> 刷新
        </button>
      </div>

      {catalog === null && !error && <LoadingState label="正在检查本机模型…" rows={4} compact />}

      {error && <p className="form-message error" role="alert">{error}</p>}
      {notice && <p className="form-message success" role="status">{notice}</p>}

      {GROUPS.map((group) => {
        const groupModels = models.filter((item) => (group.engines as readonly string[]).includes(item.engine))
        if (!groupModels.length) return null
        const statusOf = (engine: string) => engines.find((item) => item.engine === engine)
        const allReady = group.engines.every((engine) => statusOf(engine)?.ready)
        const missingPackage = group.engines.filter((engine) => !statusOf(engine)?.package_ready)
        const readyCount = groupModels.filter((item) => item.state === 'ready').length
        const usedBytes = groupModels
          .filter((item) => item.state === 'ready')
          .reduce((sum, item) => sum + item.installed_bytes, 0)
        const Icon = engineIcons[group.engines[0]] ?? Cpu

        return (
          <section className="model-group" key={group.key} aria-labelledby={`model-group-${group.key}`}>
            <div className="model-group-head">
              <span className={`model-group-icon ${allReady ? 'ready' : ''}`}>
                <Icon size={17} aria-hidden="true" />
              </span>
              <div className="model-group-title">
                <h3 id={`model-group-${group.key}`}>{group.label}</h3>
                <small>{group.hint}</small>
              </div>
              <div className="model-group-meta">
                <span className="model-size">
                  {readyCount}/{groupModels.length} 个可用
                  {usedBytes > 0 && ` · ${formatBytes(usedBytes)}`}
                </span>
                {missingPackage.length > 0 ? (
                  <span className="model-group-state attention">
                    缺少 {missingPackage.map((engine) => ENGINE_PACKAGE_NAMES[engine] ?? engine).join('、')}
                  </span>
                ) : allReady ? (
                  <span className="model-group-state ready">已就绪</span>
                ) : (
                  <span className="model-group-state">可安装</span>
                )}
              </div>
            </div>

            <div className="model-list">
              {groupModels.map((model) => (
                <ModelRow
                  key={model.id}
                  model={model}
                  isDefault={defaultModel === model.id}
                  // 识别区里有两个引擎，不标的话分不清某个模型属于哪种识别模式
                  showEngine={group.engines.length > 1}
                  busy={busy === model.id}
                  onVerify={() => void run(model.id, () => api.verifyModel(model.id), '校验通过，模型可用')}
                  onDownload={() => void run(model.id, () => api.downloadModel(model.id), `已开始下载 ${model.name}`)}
                  onCancelInstall={() => void run(model.id, () => api.cancelModelInstall(model.id), '正在取消安装')}
                  onRemove={() => void remove(model)}
                />
              ))}
            </div>
          </section>
        )
      })}

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
