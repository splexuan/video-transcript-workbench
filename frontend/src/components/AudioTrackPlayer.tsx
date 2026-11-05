import { Download, Pause, Play } from 'lucide-react'
import { useCallback, useEffect, useRef, useState, type PointerEvent, type RefObject } from 'react'

/**
 * 原始音轨播放器：把浏览器自带的 36px 控件换成一条好点的波形进度条。
 *
 * 自带控件的问题在「操作」而不只是外观：进度条只有几像素高，想拖到某一句
 * 经常点空；也没有「点击轨道任意位置跳转」这种明确的命中区。这里整块轨道
 * （默认 64px 高）都是可点的，按下后还能继续拖动，方向键与 Home/End 也能用。
 *
 * 波形来自本机音频的峰值：先读音频、再按轨道宽度分桶取每段最大振幅。
 * 音频是提取时统一转出的 16kHz 单声道 WAV，所以这里直接用 8kHz 离线上下文
 * 解码，既够画包络，也把长音频的内存占用压下来。太长的音频（>30 分钟）或
 * 读取失败时退化成普通进度条，交互完全不变。
 */

/** 超过这个时长就不再解码波形：再长，解码后的浮点数据会明显占内存。 */
const MAX_WAVE_SECONDS = 30 * 60
/** 服务端会带 Content-Length；超过这个体积也不解码（正常 WAV 30 分钟约 58MB）。 */
const MAX_WAVE_BYTES = 64 * 1024 * 1024
/** 解码采样率：只为画包络，不需要原始 16kHz 的分辨率。 */
const WAVE_SAMPLE_RATE = 8000

type WaveState = 'idle' | 'loading' | 'ready' | 'flat'

function clock(seconds: number) {
  if (!Number.isFinite(seconds) || seconds < 0) return '0:00'
  const total = Math.floor(seconds)
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const remain = total % 60
  return hours
    ? `${hours}:${minutes.toString().padStart(2, '0')}:${remain.toString().padStart(2, '0')}`
    : `${minutes}:${remain.toString().padStart(2, '0')}`
}

/** 每个桶取抽样范围内的最大振幅；抽样上限避免长音频在渲染线程上跑几十万次。 */
function computePeaks(buffer: AudioBuffer, buckets: number): number[] {
  const samples = buffer.getChannelData(0)
  if (samples.length === 0) return []
  const step = samples.length / buckets
  const peaks = new Array<number>(buckets)
  for (let index = 0; index < buckets; index += 1) {
    const start = Math.floor(index * step)
    const end = Math.min(samples.length, Math.floor((index + 1) * step))
    const stride = Math.max(1, Math.floor((end - start) / 96))
    let peak = 0
    for (let cursor = start; cursor < end; cursor += stride) {
      const value = Math.abs(samples[cursor])
      if (value > peak) peak = value
    }
    peaks[index] = peak
  }
  return peaks
}

export function AudioTrackPlayer({
  src,
  audioRef,
  onTimeUpdate,
  onScrubStart,
  onScrubEnd,
  downloadName = '原始音轨.wav',
  showDownload = true,
}: {
  src: string
  audioRef: RefObject<HTMLAudioElement | null>
  onTimeUpdate?: () => void
  /** 用户开始拖/点进度条：调用方据此暂停「跟随当前句滚动」 */
  onScrubStart?: () => void
  /** 松手或键盘调整结束：调用方在此期间继续抑制自动滚动 */
  onScrubEnd?: () => void
  /** 下载链接的文件名；调用方已有自己的下载按钮时可关掉这个图标 */
  downloadName?: string
  showDownload?: boolean
}) {
  const trackRef = useRef<HTMLDivElement>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const draggingRef = useRef(false)
  const [duration, setDuration] = useState(0)
  const [current, setCurrent] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [peaks, setPeaks] = useState<number[] | null>(null)
  const [waveState, setWaveState] = useState<WaveState>('idle')
  // 已经拿到元数据的源：换 src 后要等新音频的 loadedmetadata，不能拿旧时长去解码
  const [loadedSrc, setLoadedSrc] = useState('')

  // 播放时 timeupdate 约每秒 4 次，播放头会一跳一跳；用 rAF 补到每帧
  useEffect(() => {
    if (!playing) return
    let frame = 0
    const tick = () => {
      const audio = audioRef.current
      if (audio) setCurrent(audio.currentTime)
      frame = requestAnimationFrame(tick)
    }
    frame = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(frame)
  }, [playing, audioRef])

  /**
   * 解码波形：等到 loadedmetadata 拿到时长再决定要不要读。
   * 不阻塞播放——读不到就保持普通进度条，用户照样能拖。
   */
  useEffect(() => {
    if (!duration || loadedSrc !== src || peaks) return
    // 过长不解码：waveState 留在 idle，界面上就是一条普通轨道
    if (duration > MAX_WAVE_SECONDS) return

    const controller = new AbortController()
    let cancelled = false

    async function load() {
      setWaveState('loading')
      try {
        const response = await fetch(src, { signal: controller.signal })
        if (!response.ok) throw new Error(`音频读取失败（${response.status}）`)
        const size = Number(response.headers.get('content-length') ?? 0)
        if (size > MAX_WAVE_BYTES) {
          setWaveState('flat')
          return
        }
        const bytes = await response.arrayBuffer()
        if (cancelled) return

        const OfflineContext =
          window.OfflineAudioContext ??
          (window as unknown as { webkitOfflineAudioContext?: typeof OfflineAudioContext })
            .webkitOfflineAudioContext
        if (!OfflineContext) {
          setWaveState('flat')
          return
        }
        const buffer = await new OfflineContext(1, 1, WAVE_SAMPLE_RATE).decodeAudioData(bytes)
        if (cancelled) return

        const width = trackRef.current?.clientWidth ?? 240
        const buckets = Math.max(40, Math.min(260, Math.round(width / 3)))
        const next = computePeaks(buffer, buckets)
        if (next.length === 0) {
          setWaveState('flat')
          return
        }
        setPeaks(next)
        setWaveState('ready')
      } catch {
        // AbortError（换文案 / 卸载）与解码失败都走同一条退路，不打扰用户
        if (!cancelled) setWaveState('flat')
      }
    }

    void load()
    return () => {
      cancelled = true
      controller.abort()
    }
  }, [duration, loadedSrc, peaks, src])

  // 画波形；宽度变化与主题切换时重画
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas || !peaks) return
    const values = peaks

    function draw() {
      if (!canvas) return
      const width = canvas.clientWidth
      const height = canvas.clientHeight
      if (width <= 0 || height <= 0) return
      const ratio = Math.min(2, window.devicePixelRatio || 1)
      canvas.width = Math.round(width * ratio)
      canvas.height = Math.round(height * ratio)
      const context = canvas.getContext('2d')
      if (!context) return
      context.setTransform(ratio, 0, 0, ratio, 0, 0)
      context.clearRect(0, 0, width, height)

      const muted = getComputedStyle(canvas).getPropertyValue('--text-muted').trim()
      context.fillStyle = muted || '#706356'
      context.globalAlpha = 0.42
      const middle = height / 2
      const amplitude = Math.max(2, middle - 7)
      const bar = Math.max(1, (width / values.length) * 0.62)
      for (let index = 0; index < values.length; index += 1) {
        const x = (index + 0.5) * (width / values.length)
        const half = Math.max(0.75, values[index] * amplitude)
        context.fillRect(Math.round(x - bar / 2), middle - half, bar, half * 2)
      }
    }

    draw()
    const resizes = new ResizeObserver(draw)
    resizes.observe(canvas)
    const themes = new MutationObserver(draw)
    themes.observe(window.document.documentElement, {
      attributes: true,
      attributeFilter: ['data-theme'],
    })
    return () => {
      resizes.disconnect()
      themes.disconnect()
    }
  }, [peaks])

  const percentage = duration ? Math.min(100, Math.max(0, (current / duration) * 100)) : 0

  const seekToX = useCallback(
    (clientX: number) => {
      const track = trackRef.current
      const audio = audioRef.current
      if (!track || !audio || !duration) return
      const rect = track.getBoundingClientRect()
      const ratio = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width))
      const next = ratio * duration
      audio.currentTime = next
      setCurrent(next)
      onTimeUpdate?.()
    },
    [audioRef, duration, onTimeUpdate],
  )

  function togglePlay() {
    const audio = audioRef.current
    if (!audio) return
    if (audio.paused) void audio.play().catch(() => undefined)
    else audio.pause()
  }

  function onPointerDown(event: PointerEvent<HTMLDivElement>) {
    event.preventDefault()
    draggingRef.current = true
    onScrubStart?.()
    event.currentTarget.setPointerCapture(event.pointerId)
    seekToX(event.clientX)
  }

  function onPointerMove(event: PointerEvent<HTMLDivElement>) {
    if (draggingRef.current) seekToX(event.clientX)
  }

  function onPointerUp(event: PointerEvent<HTMLDivElement>) {
    if (!draggingRef.current) return
    draggingRef.current = false
    onScrubEnd?.()
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId)
    }
  }

  return (
    <div className="audio-track-player">
      <div className="audio-track-head">
        <button
          className="audio-play"
          type="button"
          onClick={togglePlay}
          aria-label={playing ? '暂停' : '播放'}
          title={playing ? '暂停' : '播放'}
        >
          {playing ? <Pause size={15} /> : <Play size={15} />}
        </button>
        <span className="audio-time">
          <b>{clock(current)}</b> / {clock(duration)}
        </span>
        {showDownload && (
          <a className="audio-download" href={src} download={downloadName} title="下载音频" aria-label="下载音频">
            <Download size={14} />
          </a>
        )}
      </div>
      <div
        ref={trackRef}
        className={`audio-track is-${waveState}`}
        role="slider"
        tabIndex={0}
        title="点击或拖动跳转；← / → 快退快进，Shift 加大步长"
        aria-label="播放进度"
        aria-valuemin={0}
        aria-valuemax={Math.round(duration)}
        aria-valuenow={Math.round(current)}
        aria-valuetext={`${clock(current)} / ${clock(duration)}`}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onLostPointerCapture={onPointerUp}
        onKeyDown={(event) => {
          const step = event.shiftKey ? 15 : 5
          const audio = audioRef.current
          if (!audio) return
          if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
            onScrubStart?.()
            const delta = event.key === 'ArrowLeft' ? -step : step
            const next = Math.min(duration, Math.max(0, audio.currentTime + delta))
            audio.currentTime = next
            setCurrent(next)
            onTimeUpdate?.()
            onScrubEnd?.()
            event.preventDefault()
          } else if (event.key === 'Home' || event.key === 'End') {
            onScrubStart?.()
            const next = event.key === 'Home' ? 0 : duration
            audio.currentTime = next
            setCurrent(next)
            onTimeUpdate?.()
            onScrubEnd?.()
            event.preventDefault()
          } else if (event.key === ' ' || event.key === 'Enter') {
            togglePlay()
            event.preventDefault()
          }
        }}
      >
        <canvas ref={canvasRef} aria-hidden="true" />
        {waveState !== 'ready' && <span className="audio-track-flat" aria-hidden="true" />}
        <span className="audio-track-played" style={{ width: `${percentage}%` }} aria-hidden="true" />
        <span className="audio-track-rest" style={{ left: `${percentage}%` }} aria-hidden="true" />
        <span className="audio-track-cursor" style={{ left: `${percentage}%` }} aria-hidden="true" />
      </div>
      <audio
        ref={audioRef}
        className="audio-element"
        src={src}
        preload="metadata"
        onLoadedMetadata={() => {
          const audio = audioRef.current
          if (audio && Number.isFinite(audio.duration)) setDuration(audio.duration)
          setLoadedSrc(src)
        }}
        onDurationChange={() => {
          const audio = audioRef.current
          if (audio && Number.isFinite(audio.duration)) setDuration(audio.duration)
        }}
        onTimeUpdate={() => {
          const audio = audioRef.current
          if (!audio) return
          setCurrent(audio.currentTime)
          onTimeUpdate?.()
        }}
        onSeeked={() => {
          const audio = audioRef.current
          if (audio) setCurrent(audio.currentTime)
        }}
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onEnded={() => setPlaying(false)}
      />
    </div>
  )
}
