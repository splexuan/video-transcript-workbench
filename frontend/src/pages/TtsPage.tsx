import {
  AudioLines,
  Check,
  Download,
  LoaderCircle,
  Mic,
  RefreshCw,
  Square,
  Trash2,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import { VoiceCloneModal } from '../components/VoiceCloneModal'
import { api } from '../lib/api'
import type { TtsConfig, TtsJob, TtsVoice } from '../types'

const ACTIVE: TtsJob['status'][] = ['pending', 'running', 'cancelling']

/** 把秒数说成人话：合成等待与音频时长都用它。 */
function secondsLabel(value: number) {
  const total = Math.max(0, Math.round(value))
  if (total < 60) return `${total} 秒`
  const minutes = Math.floor(total / 60)
  const rest = total % 60
  return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分钟`
}

function sizeLabel(bytes: number) {
  return bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(1)} GB` : `${Math.round(bytes / 1024 ** 2)} MB`
}

const statusLabels: Record<TtsJob['status'], string> = {
  pending: '排队中',
  running: '合成中',
  cancelling: '正在取消',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
}

export function TtsPage() {
  const [config, setConfig] = useState<TtsConfig | null>(null)
  const [configError, setConfigError] = useState('')
  const [reloadKey, setReloadKey] = useState(0)
  const [voices, setVoices] = useState<TtsVoice[]>([])
  const [text, setText] = useState('')
  const [voice, setVoice] = useState<string | null>(null)
  const [speed, setSpeed] = useState(1)
  const [job, setJob] = useState<TtsJob | null>(null)
  const [history, setHistory] = useState<TtsJob[]>([])
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [installing, setInstalling] = useState<{ percent: number; message: string } | null>(null)
  const mounted = useRef(true)

  /* ---- 音色克隆（弹窗） ---- */
  const [cloneOpen, setCloneOpen] = useState(false)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  /** 音色清单与配置一起刷新：上传/删除音色后要立刻反映到下拉里。 */
  const applyConfig = useCallback((next: TtsConfig) => {
    setConfig(next)
    setConfigError('')
    setVoices(next.voices)
    // 首次进入用设置里的默认音色；用户已经选过就尊重他的选择
    setVoice((current) => {
      if (current && next.voices.some((item) => item.id === current)) return current
      return next.voice
    })
  }, [])

  useEffect(() => {
    let active = true
    api
      .ttsConfig()
      .then((next) => {
        if (active) applyConfig(next)
      })
      .catch((reason: Error) => {
        if (active) setConfigError(reason.message)
      })
    return () => {
      active = false
    }
  }, [reloadKey, applyConfig])

  // 历史列表只是辅助信息：取不到不打断主流程，用 key 触发重新拉取
  const [historyKey, setHistoryKey] = useState(0)
  const refreshHistory = useCallback(() => setHistoryKey((value) => value + 1), [])

  useEffect(() => {
    let active = true
    api
      .ttsJobs()
      .then((items) => {
        if (active) setHistory(items)
      })
      .catch(() => undefined)
    return () => {
      active = false
    }
  }, [historyKey])

  const running = job !== null && ACTIVE.includes(job.status)
  const jobId = job?.id ?? ''

  // 合成要几分钟，进度靠轮询（1.5 秒一次，与模型安装页同一节奏）
  useEffect(() => {
    if (!running || !jobId) return
    const timer = window.setInterval(() => {
      api
        .ttsJob(jobId)
        .then((next) => {
          if (!mounted.current) return
          setJob(next)
          if (!ACTIVE.includes(next.status)) refreshHistory()
        })
        .catch(() => undefined)
    }, 1500)
    return () => window.clearInterval(timer)
  }, [running, jobId, refreshHistory])

  const charCount = text.trim().length
  const estimateSeconds = useMemo(() => {
    if (!config || !charCount) return 0
    const audio = charCount / config.chars_per_second
    // 后端会把句子摊给 parallel_lanes 个实例并行合成，等待时间跟着摊薄
    const lanes = Math.max(1, config.parallel_lanes)
    return (audio * config.realtime_factor) / lanes + 2
  }, [config, charCount])

  async function submit() {
    const body = text.trim()
    if (!body) {
      setError('请先输入要合成的文本')
      return
    }
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const created = await api.createTtsJob({ text: body, voice: voice ?? undefined, speed })
      setJob(created)
      refreshHistory()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '提交失败，请稍后重试')
    } finally {
      setBusy(false)
    }
  }

  async function cancel() {
    if (!job) return
    try {
      setJob(await api.cancelTtsJob(job.id))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '取消失败')
    }
  }

  async function remove(item: TtsJob) {
    try {
      await api.deleteTtsJob(item.id)
      if (job?.id === item.id) setJob(null)
      refreshHistory()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '删除失败')
    }
  }

  /** 音色清单变了（新建或删除）：重新拉配置，让下拉与弹窗同步。 */
  const refreshVoices = useCallback(async () => {
    const next = await api.ttsConfig()
    if (!mounted.current) return null
    applyConfig(next)
    return next
  }, [applyConfig])

  /** 弹窗里保存成功：刷新音色清单，并直接把新音色选中，省得用户再选一次。 */
  async function handleVoiceCreated(voiceId: string) {
    const next = await refreshVoices()
    if (!next) return
    setCloneOpen(false)
    setVoice(voiceId)
    const created = next.voices.find((item) => item.id === voiceId)
    setNotice(`音色「${created?.name ?? '新音色'}」已保存，可以直接合成了`)
  }

  /** 模型没装时的下载入口：直接在这里装，不用来回切页。 */
  async function downloadModel() {
    const model = config?.model
    if (!model) return
    setError('')
    setNotice('')
    setInstalling({ percent: 0, message: '正在准备下载' })
    try {
      await api.downloadModel(model.id)
      for (;;) {
        await new Promise((resolve) => window.setTimeout(resolve, 3000))
        if (!mounted.current) return
        const catalog = await api.models()
        const target = catalog.models.find((item) => item.id === model.id)
        if (!target) break
        setInstalling({
          percent: target.progress?.percent ?? 0,
          message: target.progress?.message || target.message,
        })
        if (target.state !== 'installing') {
          setInstalling(null)
          if (target.state === 'ready') {
            setReloadKey((value) => value + 1)
            setNotice('语音模型已安装，可以开始合成了')
          } else {
            setError(target.message)
          }
          break
        }
      }
    } catch (reason) {
      setInstalling(null)
      setError(reason instanceof Error ? reason.message : '模型下载失败')
    }
  }

  function downloadAudio(item: TtsJob, format: 'wav' | 'mp3') {
    // 用临时链接触发下载；直接改 location 会被 react-hooks 规则拦下
    const anchor = window.document.createElement('a')
    anchor.href = api.ttsAudioUrl(item.id, format, true)
    anchor.download = ''
    anchor.click()
  }

  const builtinVoices = useMemo(() => voices.filter((item) => item.builtin), [voices])
  const customVoices = useMemo(() => voices.filter((item) => !item.builtin), [voices])
  const selectedVoice = useMemo(
    () => voices.find((item) => item.id === voice) ?? null,
    [voices, voice],
  )

  /** 模型 id → 可读名。清单里找不到（装完又删了）就退回 id 本身，不要显示成空白。 */
  const modelLabel = useCallback(
    (id: string) => config?.models.find((item) => item.id === id)?.name ?? id,
    [config],
  )

  return (
    <div className="page tts-page">
      <header className="page-header">
        <div>
          <h1>语音合成</h1>
          <p>把文案读成音频：本地 ZipVoice 模型、CPU 推理，文本不出本机。音色由参考音频克隆而来。</p>
        </div>
      </header>

      {configError && (
        <p className="form-message error" role="alert">
          {configError}
          <button className="text-button" type="button" onClick={() => setReloadKey((value) => value + 1)}>重试</button>
        </p>
      )}

      {/* 装好之后也要一直看得见当前用的是哪个模型：合成质量与速度都随它变，
          而且换引擎版本后历史记录的音色不再可比，必须能从结果反推当时用的什么。 */}
      {config && config.ready && (
        <section className="panel tts-model-bar">
          <span className="tts-model-label">当前模型</span>
          {config.models.filter((item) => item.ready).length > 1 ? (
            <select
              className="tts-model-select"
              aria-label="选择语音模型"
              value={config.model?.id ?? ''}
              onChange={async (event) => {
                const next = event.target.value
                try {
                  await api.updateSettings({ tts_model: next })
                  setNotice(`已切换到「${config.models.find((item) => item.id === next)?.name ?? next}」`)
                  setReloadKey((value) => value + 1)
                } catch (reason) {
                  setError(reason instanceof Error ? reason.message : '切换模型失败')
                }
              }}
            >
              {config.models.filter((item) => item.ready).map((item) => (
                <option key={item.id} value={item.id}>{item.name}</option>
              ))}
            </select>
          ) : (
            <strong>{config.model?.name}</strong>
          )}
          <small className="tts-model-note">
            {config.models.filter((item) => item.ready).length > 1
              ? 'fp32 人声细节更干净，但每开一个并行实例约占双倍内存'
              : '只装了 int8 版；想要更干净的人声可在「设置 → 语音合成」里装 fp32 版'}
          </small>
        </section>
      )}

      {config && !config.ready && (
        <section className="panel tts-setup">
          <div className="section-heading">
            <div>
              <h2>还差一个语音模型</h2>
              <p>{config.message}</p>
            </div>
          </div>
          {installing ? (
            <div className="tts-progress">
              <div className="tts-progress-bar"><span style={{ width: `${installing.percent}%` }} /></div>
              <span className="tts-progress-text">{installing.percent}% · {installing.message}</span>
            </div>
          ) : (
            <div className="tts-setup-actions">
              <button className="primary-button" type="button" onClick={() => void downloadModel()}>
                <Download size={16} /> 下载语音模型
                {config.model ? `（约 ${sizeLabel(config.model.approx_bytes)}）` : ''}
              </button>
              <small>模型由本机下载到数据目录，也可在<Link to="/models">模型管理</Link>里统一管理。</small>
            </div>
          )}
        </section>
      )}

      <div className="tts-layout">
        <section className="panel tts-input-panel">
          <div className="section-heading">
            <div>
              <h2>要读的文案</h2>
              <p>粘贴或输入文本，最多 {config?.max_chars ?? 2000} 字。</p>
            </div>
          </div>
          <textarea
            className="tts-textarea"
            value={text}
            onChange={(event) => setText(event.target.value)}
            placeholder="把口播稿、文章或任意要朗读的文本贴进来。emoji 与多余空行会自动清理。"
            aria-label="要合成的文本"
          />
          <div className="tts-meta">
            <span>{charCount} 字</span>
            {charCount > 0 && estimateSeconds > 0 && <span>预计合成约 {secondsLabel(estimateSeconds)}</span>}
            {config && charCount > config.max_chars && (
              <span className="tts-warn">超出上限，请分段合成</span>
            )}
          </div>

          <div className="tts-controls">
            <label className="tts-control" htmlFor="tts-voice">
              <span>音色</span>
              <select
                id="tts-voice"
                value={voice ?? ''}
                onChange={(event) => setVoice(event.target.value)}
              >
                {builtinVoices.length > 0 && (
                  <optgroup label="内置音色">
                    {builtinVoices.map((item) => (
                      <option key={item.id} value={item.id}>{item.name}</option>
                    ))}
                  </optgroup>
                )}
                {customVoices.length > 0 && (
                  <optgroup label="我的音色">
                    {customVoices.map((item) => (
                      <option key={item.id} value={item.id}>{item.name}</option>
                    ))}
                  </optgroup>
                )}
              </select>
            </label>
            <label className="tts-control" htmlFor="tts-speed">
              <span>语速 <strong>{speed.toFixed(2)}×</strong></span>
              <input
                id="tts-speed"
                type="range"
                min={0.6}
                max={1.6}
                step={0.05}
                value={speed}
                onChange={(event) => setSpeed(Number(event.target.value))}
              />
            </label>
          </div>

          {selectedVoice && (
            <details className="tts-voice-detail">
              <summary>这段参考音频里说的是</summary>
              <p>{selectedVoice.reference_text}</p>
            </details>
          )}

          <div className="tts-actions">
            <button
              className="primary-button"
              type="button"
              onClick={() => void submit()}
              disabled={busy || running || !config?.ready || charCount === 0 || charCount > (config?.max_chars ?? 0)}
            >
              {busy || running ? <LoaderCircle className="spin" size={16} /> : <AudioLines size={16} />}
              {running ? '正在合成' : '开始合成'}
            </button>
            {running && (
              <button className="secondary-button" type="button" onClick={() => void cancel()}>
                <Square size={15} /> 取消
              </button>
            )}
            <button
              className="secondary-button"
              type="button"
              onClick={() => setCloneOpen(true)}
            >
              <Mic size={15} /> 音色克隆
            </button>
          </div>

          {error && <p className="form-message error" role="alert">{error}</p>}
          {notice && <p className="form-message success" role="status">{notice}</p>}
        </section>

        <aside className="tts-aside">
          <section className="panel">
            <div className="section-heading">
              <div>
                <h2>本次结果</h2>
                <p>合成完成后可在这里试听与导出。</p>
              </div>
            </div>
            {job ? (
              <div className="tts-result">
                <div className="tts-result-head">
                  <strong>{statusLabels[job.status]}</strong>
                  <small>{job.message}</small>
                </div>
                {ACTIVE.includes(job.status) && (
                  <div className="tts-progress">
                    <div className="tts-progress-bar"><span style={{ width: `${job.percent}%` }} /></div>
                    <span className="tts-progress-text">
                      {job.percent}%
                      {job.sentences_total > 0 ? ` · ${job.sentences_done}/${job.sentences_total} 句` : ''}
                    </span>
                  </div>
                )}
                {job.status === 'completed' && (
                  <>
                    {/* 音频直接来自后端文件，不用把整段音频塞进内存 */}
                    <audio className="tts-player" controls preload="metadata" src={api.ttsAudioUrl(job.id)} />
                    <div className="tts-result-meta">
                      <span>时长 {secondsLabel(job.duration)}</span>
                      <span>耗时 {secondsLabel(job.elapsed)}</span>
                      <span>{job.chars} 字</span>
                    </div>
                    <div className="tts-result-actions">
                      <button className="secondary-button" type="button" onClick={() => downloadAudio(job, 'wav')}>
                        <Download size={15} /> 下载 WAV
                      </button>
                      <button className="secondary-button" type="button" onClick={() => downloadAudio(job, 'mp3')}>
                        <Download size={15} /> 下载 MP3
                      </button>
                    </div>
                  </>
                )}
                {job.status === 'failed' && job.error && (
                  <p className="form-message error" role="alert">{job.error}</p>
                )}
              </div>
            ) : (
              <div className="compact-empty">
                <span>还没有合成结果</span>
                <p>左边输入文案，点「开始合成」，这里会显示进度与播放器。</p>
              </div>
            )}
          </section>

          <section className="panel">
            <div className="section-heading">
              <div>
                <h2>最近合成</h2>
                <p>只保留最近 10 条，重新合成更省磁盘。</p>
              </div>
              <button className="text-button" type="button" onClick={refreshHistory}>
                <RefreshCw size={14} /> 刷新
              </button>
            </div>
            {history.length === 0 ? (
              <p className="compact-empty">暂无记录</p>
            ) : (
              <ul className="tts-history">
                {history.map((item) => (
                  <li key={item.id} className={job?.id === item.id ? 'active' : ''}>
                    <button className="tts-history-main" type="button" onClick={() => setJob(item)}>
                      <span className="tts-history-title">
                        {statusLabels[item.status]} · {item.chars} 字
                        {item.duration > 0 ? ` · ${secondsLabel(item.duration)}` : ''}
                      </span>
                      <span className="tts-history-sub">
                        {/* 换过模型后音色不再可比，所以连模型一起标出来 */}
                        {modelLabel(item.model_id)} · 音色 {voices.find((v) => v.id === item.voice)?.name ?? item.voice} ·{' '}
                        {item.speed.toFixed(2)}×
                        {item.status === 'completed' && <Check size={12} />}
                      </span>
                    </button>
                    {!ACTIVE.includes(item.status) && (
                      <button
                        className="icon-button"
                        type="button"
                        title="删除这条合成"
                        aria-label="删除这条合成"
                        onClick={() => void remove(item)}
                      >
                        <Trash2 size={14} />
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </section>
        </aside>
      </div>

      {cloneOpen && (
        <VoiceCloneModal
          onClose={() => setCloneOpen(false)}
          onCreated={(voiceId) => void handleVoiceCreated(voiceId)}
          onChanged={() => void refreshVoices()}
          voices={voices}
        />
      )}
    </div>
  )
}