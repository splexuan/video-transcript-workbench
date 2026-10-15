import { LoaderCircle, Play, RotateCcw, Scissors } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'

import type { AudioSelection } from '../types'

/**
 * 参考音频裁剪编辑器：画波形、拖两端手柄选一段、试听这一段。
 *
 * 与 omnivoice-studio 的同名组件同一套思路——**选区只存在于界面**，真正的裁剪
 * 由后端按 (start, end) 完成，所以音色库里存的永远是同一份成品音频，不会出现
 * 「界面上选了一段、库里却是另一段」的错位。
 *
 * 为什么值得做：参考音频越长，ZipVoice 合成越慢（实测 RTF 从 0.47 涨到 1.9），
 * 而开头几十秒里的底噪、背景音乐又是最常见的翻车点。能剪到 3~10 秒且剪得准，
 * 比任何提示文案都管用。
 */

/** 选区的最小长度（秒）：太短克隆出的音色不稳，后端也会拒绝，两边保持一致。 */
export const MIN_SECONDS = 2
const RECOMMENDED: [number, number] = [3, 10]
/** 方向键微调一档的秒数；按住 Shift 走粗调。 */
const FINE_STEP = 0.05
const COARSE_STEP = 0.5
/** 波形振幅占容器高度的比例：留一点余量，峰值顶到边上会显得被削过。 */
const AMPLITUDE = 0.92
/** 选区外的波形要退到背景里去，这个透明度就是「退」的程度。 */
const DIMMED = 0.32

function clamp(value: number, low: number, high: number) {
  return Math.max(low, Math.min(high, value))
}

/**
 * 把 CSS 颜色值加上透明度。
 *
 * canvas 的 fillStyle 不认 color-mix()，只能在运行时算出 rgba，所以先把
 * 主题里的 --primary（#rrggbb 或 rgb()）转出来——这样深浅色主题各画各的，
 * 不用在组件里写死两套颜色。
 */
function withAlpha(color: string, alpha: number): string {
  const value = color.trim()
  const hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(value)
  if (hex) {
    const digits =
      hex[1].length === 3
        ? hex[1]
            .split('')
            .map((char) => char + char)
            .join('')
        : hex[1]
    const packed = Number.parseInt(digits, 16)
    return `rgba(${(packed >> 16) & 255}, ${(packed >> 8) & 255}, ${packed & 255}, ${alpha})`
  }
  const rgb = /^rgba?\(([^)]+)\)$/i.exec(value)
  if (rgb) {
    const [red, green, blue] = rgb[1].split(',').map((part) => part.trim())
    return `rgba(${red}, ${green}, ${blue}, ${alpha})`
  }
  return value
}

/** 多声道混成单声道：只看第一个声道的话，立体声素材会画得一边有、一边空。 */
function monoSamples(buffer: AudioBuffer): Float32Array {
  if (buffer.numberOfChannels === 1) return buffer.getChannelData(0)
  const length = buffer.length
  const mixed = new Float32Array(length)
  for (let channel = 0; channel < buffer.numberOfChannels; channel += 1) {
    const data = buffer.getChannelData(channel)
    for (let index = 0; index < length; index += 1) mixed[index] += data[index] / buffer.numberOfChannels
  }
  return mixed
}

/**
 * 每显示像素取一个峰值，并做一次 3 点平滑。
 *
 * 逐点取 max 出来的包络全是尖刺，看着像噪点；相邻三点加权平均之后才是人耳
 * 感知到的起伏，曲线也连成一片。
 */
function columnPeaks(samples: Float32Array, columns: number): Float32Array {
  const peaks = new Float32Array(columns)
  const perColumn = samples.length / columns
  for (let column = 0; column < columns; column += 1) {
    const from = Math.floor(column * perColumn)
    const to = Math.min(samples.length, Math.max(from + 1, Math.floor((column + 1) * perColumn)))
    let peak = 0
    for (let cursor = from; cursor < to; cursor += 1) {
      const magnitude = Math.abs(samples[cursor])
      if (magnitude > peak) peak = magnitude
    }
    peaks[column] = peak
  }
  for (let column = 0; column < columns; column += 1) {
    const previous = peaks[Math.max(0, column - 1)]
    const next = peaks[Math.min(columns - 1, column + 1)]
    peaks[column] = previous * 0.25 + peaks[column] * 0.5 + next * 0.25
  }
  return peaks
}

/** 取第 p 分位（p 取 0~1）：比平均值抗尖峰，比最大值更能代表「这段音频的常态响度」。 */
function percentile(peaks: Float32Array, p: number): number {
  const sorted = Float32Array.from(peaks).sort()
  const index = clamp(Math.round((sorted.length - 1) * p), 0, sorted.length - 1)
  return sorted[index]
}

type DragMode = 'start' | 'end' | 'move' | 'fresh'

export function ReferenceAudioEditor({
  src,
  value,
  onChange,
  busy = false,
}: {
  /** 音频地址：新建时是本地文件的 blob URL，编辑时是后端的参考音频地址。 */
  src: string
  value: AudioSelection
  onChange: (next: AudioSelection) => void
  busy?: boolean
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const areaRef = useRef<HTMLDivElement>(null)
  const audioRef = useRef<HTMLAudioElement>(null)
  const bufferRef = useRef<AudioBuffer | null>(null)
  /** 峰值按「缓冲 + 列数」缓存：播放头每 250ms 动一次，没必要每次重扫整段音频。 */
  const peaksRef = useRef<{ key: string; peaks: Float32Array } | null>(null)
  const dragRef = useRef<{ mode: DragMode; grabAt: number } | null>(null)
  /** 最新的绘制函数：解码与观察器回调都通过它调用，免得把 paint 变成它们的依赖。 */
  const paintRef = useRef<() => void>(() => undefined)
  const [duration, setDuration] = useState(0)
  const [playhead, setPlayhead] = useState(0)
  const [loadError, setLoadError] = useState('')

  const paint = useCallback(() => {
    const canvas = canvasRef.current
    const area = areaRef.current
    const buffer = bufferRef.current
    if (!canvas || !area || !buffer || !duration) return
    const width = area.clientWidth
    const height = area.clientHeight
    if (width < 2 || height < 2) return

    /*
     * 画布按设备像素比放大，再用 setTransform 把坐标系换回 CSS 像素。
     * 不做这一步的话：缩放不是整数倍时，每个「1px 宽」的波形柱之间会留下
     * 零点几像素的缝，整条波形看上去就是毛的、发虚——这正是之前不流畅的原因。
     */
    const ratio = Math.min(window.devicePixelRatio || 1, 3)
    const pixelWidth = Math.round(width * ratio)
    const pixelHeight = Math.round(height * ratio)
    if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
      canvas.width = pixelWidth
      canvas.height = pixelHeight
    }
    const context = canvas.getContext('2d')
    if (!context) return
    context.setTransform(ratio, 0, 0, ratio, 0, 0)
    context.clearRect(0, 0, width, height)

    const accent = getComputedStyle(area).getPropertyValue('--primary') || '#a9502e'
    const middle = height / 2
    const left = clamp((value.start / duration) * width, 0, width)
    const right = clamp((value.end / duration) * width, 0, width)

    // 选区一层淡底：光靠波形深浅还不够，一眼要能看出「哪段会被留下」
    context.fillStyle = withAlpha(accent, 0.08)
    context.fillRect(left, 0, right - left, height)

    const columns = Math.max(1, Math.round(width))
    const key = `${buffer.length}:${columns}`
    if (peaksRef.current?.key !== key) {
      peaksRef.current = { key, peaks: columnPeaks(monoSamples(buffer), columns) }
    }
    const peaks = peaksRef.current.peaks
    const step = width / columns

    /*
     * 按 98 分位峰值归一化，而不是拿 1.0 当满刻度。
     * 手机录的参考音频轻的只有响的十分之一，直接画会缩成一条平线，什么也看不出来；
     * 用分位数则不管录音音量多大，波形都占满差不多高度。
     */
    const gain = Math.min(1, 0.97 / (percentile(peaks, 0.98) || 1))
    const halfHeight = (index: number) => Math.max(0.5, peaks[index] * gain * middle * AMPLITUDE)

    /** 上下包络围成一个闭合形状填充：比一根根画柱子连续，段与段之间也不会漏缝。 */
    const drawBand = (from: number, to: number, alpha: number) => {
      const startIndex = Math.max(0, Math.min(columns - 1, Math.floor(from)))
      const endIndex = Math.max(startIndex, Math.min(columns - 1, Math.ceil(to) - 1))
      if (endIndex <= startIndex && columns > 1) return
      const gradient = context.createLinearGradient(0, 0, 0, height)
      gradient.addColorStop(0, withAlpha(accent, alpha * 0.5))
      gradient.addColorStop(0.5, withAlpha(accent, alpha))
      gradient.addColorStop(1, withAlpha(accent, alpha * 0.5))
      context.fillStyle = gradient
      context.beginPath()
      context.moveTo(startIndex * step, middle - halfHeight(startIndex))
      for (let index = startIndex + 1; index <= endIndex; index += 1) {
        context.lineTo(index * step, middle - halfHeight(index))
      }
      for (let index = endIndex; index >= startIndex; index -= 1) {
        context.lineTo(index * step, middle + halfHeight(index))
      }
      context.closePath()
      context.fill()
    }

    drawBand(0, left, DIMMED)
    drawBand(left, right, 1)
    drawBand(right, width, DIMMED)

    // 中线压在波形之上：静音段因此不会被误看成「这里没声音」
    context.fillStyle = withAlpha(accent, 0.22)
    context.fillRect(0, middle - 0.5, width, 1)

    // 播放头画进画布：DOM 元素跟 canvas 对不齐会露出一条缝，画在同一坐标系里最稳
    if (playhead > 0) {
      const x = clamp(playhead * width, 0, width - 1.5)
      context.fillStyle = withAlpha(accent, 0.95)
      context.fillRect(x, 0, 1.5, height)
      context.beginPath()
      context.arc(x + 0.75, 4, 3, 0, Math.PI * 2)
      context.fill()
    }
  }, [duration, value, playhead])

  // 换一段音频就重画，并把选区还原成整段：旧的选区属于上一段录音。
  //
  // 这里刻意不把 paint 放进依赖：paint 依赖选区，而选区一变就重解码、
  // 又把选区重置成整段——拖动手柄时会被一次次弹回去。改用 ref 调最新的绘制函数。
  useEffect(() => {
    if (!src) {
      bufferRef.current = null
      peaksRef.current = null
      setDuration(0)
      return
    }
    let cancelled = false
    setLoadError('')
    const context = new AudioContext()
    fetch(src)
      .then((response) => {
        if (!response.ok) throw new Error('bad response')
        return response.arrayBuffer()
      })
      .then((raw) => context.decodeAudioData(raw))
      .then((buffer) => {
        if (cancelled) return
        setDuration(buffer.duration)
        bufferRef.current = buffer
        peaksRef.current = null
        onChange({ start: 0, end: buffer.duration })
        paintRef.current()
      })
      .catch(() => {
        if (!cancelled) setLoadError('这段音频读不出来，无法绘制波形')
      })
    return () => {
      cancelled = true
      void context.close()
    }
  }, [src, onChange])

  // 选区、播放头、主题变化都只影响「怎么画」，不影响「音频解没解码」
  useEffect(() => {
    paintRef.current = paint
    paint()
  }, [paint])

  // 容器宽度变了（窗口缩放、弹窗换档）波形要跟着重画，否则会拉伸失真
  useEffect(() => {
    const area = areaRef.current
    if (!area || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(() => paintRef.current())
    observer.observe(area)
    return () => observer.disconnect()
  }, [])

  // 深浅色主题各有一套 --primary，不重画的话切主题后波形还是上一个主题的颜色
  useEffect(() => {
    if (typeof MutationObserver === 'undefined') return
    const observer = new MutationObserver(() => paintRef.current())
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    return () => observer.disconnect()
  }, [])

  const secondsAt = useCallback(
    (clientX: number) => {
      const rect = areaRef.current?.getBoundingClientRect()
      if (!rect || !duration) return 0
      return clamp((clientX - rect.left) / Math.max(1, rect.width), 0, 1) * duration
    },
    [duration],
  )

  /** 拖拽过程中不能出现零长或反向的选区，否则每次移动都会被后端判成非法。 */
  const applyRange = useCallback(
    (begin: number, finish: number) => {
      if (!duration) return
      let low = clamp(begin, 0, duration)
      let high = clamp(finish, 0, duration)
      if (high < low) [low, high] = [high, low]
      if (high - low < MIN_SECONDS) {
        if (low + MIN_SECONDS <= duration) high = low + MIN_SECONDS
        else low = Math.max(0, high - MIN_SECONDS)
      }
      onChange({ start: low, end: high })
    },
    [duration, onChange],
  )

  function startDrag(event: React.PointerEvent, mode: DragMode) {
    if (!duration || busy) return
    const at = secondsAt(event.clientX)
    event.currentTarget.setPointerCapture(event.pointerId)
    // 「划新选区」的锚点是按下点本身；平移则要记住按下点相对选区起点的偏移
    dragRef.current = { mode, grabAt: mode === 'fresh' ? at : at - value.start }
  }

  function moveDrag(event: React.PointerEvent) {
    const drag = dragRef.current
    if (!drag) return
    const at = secondsAt(event.clientX)
    if (drag.mode === 'start') applyRange(at, value.end)
    else if (drag.mode === 'end') applyRange(value.start, at)
    else if (drag.mode === 'move') {
      // 整体平移：长度不变，超出范围就贴边，绝不把长度改掉
      const width = value.end - value.start
      const low = clamp(at - drag.grabAt, 0, Math.max(0, duration - width))
      onChange({ start: low, end: low + width })
    } else applyRange(drag.grabAt, at)
  }

  function endDrag() {
    dragRef.current = null
  }

  /** 键盘微调：左右方向键挪动整段，Shift 走粗调——鼠标拖不动的精细活靠它。 */
  function onKeyDown(event: React.KeyboardEvent) {
    if (!duration) return
    const step = event.shiftKey ? COARSE_STEP : FINE_STEP
    const width = value.end - value.start
    if (event.key === 'ArrowLeft') {
      const low = clamp(value.start - step, 0, Math.max(0, duration - width))
      onChange({ start: low, end: low + width })
    } else if (event.key === 'ArrowRight') {
      const low = clamp(value.start + step, 0, Math.max(0, duration - width))
      onChange({ start: low, end: low + width })
    } else if (event.key === '[' || event.key === ']') {
      const edge = event.key === '[' ? value.start - step : value.end + step
      event.key === '[' ? applyRange(edge, value.end) : applyRange(value.start, edge)
    } else {
      return
    }
    event.preventDefault()
  }

  function playSelection() {
    const audio = audioRef.current
    if (!audio || !duration) return
    audio.currentTime = value.start
    void audio.play().catch(() => setLoadError('这段音频没法播放，换一个文件试试'))
  }

  function onTimeUpdate() {
    const audio = audioRef.current
    if (!audio || !duration) return
    if (audio.currentTime >= value.end) {
      audio.pause()
      setPlayhead(0)
      return
    }
    setPlayhead(audio.currentTime / duration)
  }

  const selected = Math.max(0, value.end - value.start)
  const left = duration ? (value.start / duration) * 100 : 0
  const right = duration ? (value.end / duration) * 100 : 100
  const [low, high] = RECOMMENDED

  return (
    <div className='tts-wave'>
      <div className='tts-wave-head'>
        <span>参考音频波形</span>
        <small>{duration ? `全长 ${duration.toFixed(2)}s` : '正在读取音频…'}</small>
      </div>
      <div
        className='tts-wave-area'
        ref={areaRef}
        tabIndex={0}
        role='group'
        aria-label='参考音频波形：拖两端选择要保留的片段，左右方向键整体移动选区'
        onKeyDown={onKeyDown}
        onPointerDown={(event) => {
          // 落在手柄上的按下交给手柄自己处理，否则一次拖拽会被算成两次
          if ((event.target as HTMLElement).closest('.tts-wave-handle')) return
          startDrag(event, 'fresh')
        }}
        onPointerMove={moveDrag}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
      >
        <canvas className='tts-wave-canvas' ref={canvasRef} aria-hidden='true' />
        {/* 选区内外深浅与播放头都画在 canvas 里；DOM 只留选区框与两个手柄 */}
        <span className='tts-wave-sel' style={{ left: `${left}%`, width: `${right - left}%` }} aria-hidden='true' />
        <button
          type='button'
          className='tts-wave-handle'
          style={{ left: `${left}%` }}
          aria-label='选区起点'
          disabled={busy}
          onPointerDown={(event) => startDrag(event, 'start')}
          onPointerMove={moveDrag}
          onPointerUp={endDrag}
          onPointerCancel={endDrag}
        />
        <button
          type='button'
          className='tts-wave-handle'
          style={{ left: `${right}%` }}
          aria-label='选区终点'
          disabled={busy}
          onPointerDown={(event) => startDrag(event, 'end')}
          onPointerMove={moveDrag}
          onPointerUp={endDrag}
          onPointerCancel={endDrag}
        />
      </div>
      <div className='tts-wave-foot'>
        <button
          className='secondary-button'
          type='button'
          onClick={playSelection}
          disabled={busy || !duration}
        >
          <Play size={14} />
          试听选区
        </button>
        <button
          className='text-button'
          type='button'
          onClick={() => onChange({ start: 0, end: duration })}
          disabled={busy || !duration}
        >
          <RotateCcw size={14} />
          还原全选
        </button>
        <span className='tts-wave-read'>
          {duration ? `已选 ${selected.toFixed(2)}s · ${value.start.toFixed(2)} – ${value.end.toFixed(2)}` : '—'}
        </span>
      </div>
      {src && (
        <audio ref={audioRef} src={src} preload='metadata' onTimeUpdate={onTimeUpdate} onEnded={() => setPlayhead(0)} hidden />
      )}
      {!loadError && duration > 0 && (
        <p className={selected < low || selected > high ? 'tts-wave-note warning' : 'tts-wave-note ok'}>
          {selected < low
            ? `选区只有 ${selected.toFixed(2)}s，偏短 —— 建议 ${low}–${high} 秒，太短音色可能不稳。`
            : selected > high
              ? `选区 ${selected.toFixed(2)}s，偏长 —— 参考音频越长合成越慢，建议剪到 ${high} 秒以内。`
              : `长度合适（建议 ${low}–${high} 秒）。`}
        </p>
      )}
      {loadError && (
        <p className='form-message error' role='alert'>
          {loadError}
        </p>
      )}
      {busy && (
        <p className='tts-wave-note'>
          <LoaderCircle className='spin' size={13} /> 正在处理，裁剪暂时不可用
        </p>
      )}
      <p className='tts-wave-hint'>
        <Scissors size={13} />
        拖两端手柄选一段；按住选区中间可整体移动，方向键微调（Shift 粗调）
      </p>
    </div>
  )
}