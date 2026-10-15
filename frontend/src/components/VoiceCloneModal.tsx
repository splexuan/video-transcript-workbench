import { CheckCircle2, LoaderCircle, Mic, Pencil, Play, Trash2, Upload, Wand2 } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

import { api } from '../lib/api'
import { formatBytes } from '../lib/format'
import type { AudioSelection, TtsVoice } from '../types'
import { Modal, ModalHeader } from './Modal'
import { ReferenceAudioEditor } from './ReferenceAudioEditor'
import { VoiceEditModal } from './VoiceEditModal'

const TIPS = [
  '只用一个人说话，别混背景音乐或其他人的声音',
  '选安静环境：底噪和混响都会被一并克隆进去',
  '10 秒左右就够，长了合成会明显变慢',
]

/** 从文件名猜一个音色名：`我的口播录音.m4a` → `我的口播录音`。 */
function nameFromFile(name: string) {
  const base = name.replace(/\.[^.]+$/, '').trim()
  return (base || '我的音色').slice(0, 40)
}

/**
 * 音色克隆：上传一段参考音频，用现成的识别引擎把里面说的话转成文字，回填给用户核对。
 *
 * 参考文本必须与音频逐字一致（对不上克隆出的音色会明显变差），手打既费劲又容易打错，
 * 所以默认自动转写、允许改；识别没装时退回手填，不把这条路堵死。
 *
 * 波形编辑器是这条链路的一半：参考音频越长 ZipVoice 合成越慢（RTF 从 0.47 到 1.9），
 * 而开头那几十秒里的底噪又是最常翻车的地方。选区会同时作用于「自动转写」与
 * 「保存」，所以文字与最终落盘的音频始终是同一段。
 */
export function VoiceCloneModal({
  onClose,
  onCreated,
  onChanged,
  voices,
}: {
  onClose: () => void
  /** 新建成功：顺带把这个音色选中，省得用户再选一次 */
  onCreated: (voiceId: string) => void
  /** 清单变化（新建或删除）后刷新 */
  onChanged: () => void
  /** 已有的自定义音色：管理（编辑 / 删除）入口放在这里，主页面只留一个下拉框 */
  voices: TtsVoice[]
}) {
  const [file, setFile] = useState<File | null>(null)
  const [dragOver, setDragOver] = useState(false)
  const [name, setName] = useState('')
  const [text, setText] = useState('')
  const [preview, setPreview] = useState('')
  const [selection, setSelection] = useState<AudioSelection>({ start: 0, end: 0 })
  const [seconds, setSeconds] = useState(0)
  const [truncated, setTruncated] = useState(false)
  const [busy, setBusy] = useState(false)
  const [removing, setRemoving] = useState('')
  const [editing, setEditing] = useState<TtsVoice | null>(null)
  const [playing, setPlaying] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const objectUrl = useRef('')
  const probe = useRef<HTMLAudioElement>(null)

  const mine = voices.filter((item) => !item.builtin)
  /** 选区是否有效（波形还没解码完时是 0 秒，这时不能拿去裁剪）。 */
  const hasSelection = selection.end - selection.start > 0.01
  /**
   * 选区是否偏离了「从开头到转写长度」这一段，只用来挑提示文案。
   *
   * 注意它**不能**拿来决定要不要把选区发给后端：裁剪后重新转写会让两者长度相等，
   * 一旦据此判定「没裁过」，保存时就会落回整段音频——正是「剪了半天还是完整音频」。
   */
  const windowed = selection.start > 0.01 || (hasSelection && seconds > 0 && Math.abs(selection.end - selection.start - seconds) > 0.01)

  /** 选文件：立刻给试听地址与默认名称，文本区留空等转写（或手填）。 */
  function pick(next: File | null) {
    setError('')
    setNotice('')
    setText('')
    setTruncated(false)
    setSeconds(0)
    setFile(next)
    if (objectUrl.current) {
      URL.revokeObjectURL(objectUrl.current)
      objectUrl.current = ''
    }
    if (!next) {
      setPreview('')
      return
    }
    objectUrl.current = URL.createObjectURL(next)
    setPreview(objectUrl.current)
    setName(nameFromFile(next.name))
  }

  // 卸载时回收 blob URL，否则音频缓冲会一直占着
  useEffect(
    () => () => {
      if (objectUrl.current) URL.revokeObjectURL(objectUrl.current)
    },
    [],
  )

  async function autoTranscribe() {
    if (!file) return
    setBusy(true)
    setError('')
    setNotice('')
    try {
      // 带上选区：转写出来的文字要与最终保存的那一段对齐，裁剪后不必手工删字
      const result = await api.transcribeTtsVoice(file, hasSelection ? selection : undefined)
      setText(result.text)
      setSeconds(result.seconds)
      setTruncated(result.truncated)
      setNotice(
        result.truncated
          ? `这段音频较长，只转写了前 ${Math.round(result.seconds)} 秒（参考音频也只用这一段），请核对文字`
          : windowed
            ? '已按你选的片段转写，请核对一下与音频是否一致'
            : '已自动转写，请核对一下与音频是否一致',
      )
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '转写失败，可以手动填写这段音频里说的话')
    } finally {
      setBusy(false)
    }
  }

  async function save() {
    if (!file) {
      setError('请先选择一段参考音频')
      return
    }
    if (!text.trim()) {
      setError('请填写这段音频里说出的文字，或点「自动转写」让识别引擎帮你填')
      return
    }
    setBusy(true)
    setError('')
    try {
      const created = await api.createTtsVoice({
        name: name.trim() || '我的音色',
        reference_text: text.trim(),
        audio: file,
        // 选区永远跟着走：波形上选的就是要存的，否则「剪了半天保存还是完整音频」
        selection: hasSelection ? selection : undefined,
      })
      onChanged()
      onCreated(created.id)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '保存失败，请稍后重试')
    } finally {
      setBusy(false)
    }
  }

  async function removeVoice(item: TtsVoice) {
    setRemoving(item.id)
    setError('')
    setNotice('')
    try {
      await api.deleteTtsVoice(item.id)
      onChanged()
      setNotice(`音色「${item.name}」已删除`)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '删除失败')
    } finally {
      setRemoving('')
    }
  }

  /** 试听已保存音色的参考音频：克隆质量完全由它决定，听一遍比看波形更有用。 */
  function togglePreview(item: TtsVoice) {
    const player = probe.current
    if (!player) return
    if (playing === item.id) {
      player.pause()
      setPlaying('')
      return
    }
    player.src = item.audio_url ?? ''
    setPlaying(item.id)
    void player.play().catch(() => {
      setPlaying('')
      setError('这段参考音频暂时放不了，可以重新打开弹窗再试')
    })
  }

  return (
    <Modal ariaLabel='音色克隆' onClose={onClose} size='md' dormant={Boolean(editing)}>
      <ModalHeader title='音色克隆' icon={Mic} onClose={onClose} />
      <div className='modal-body tts-clone'>
        <p className='tts-upload-lead'>
          音色是从这段参考音频克隆出来的，所以参考音频的质量直接决定成品质量：
        </p>
        <ul className='tts-tips'>
          {TIPS.map((tip) => (
            <li key={tip}>{tip}</li>
          ))}
        </ul>

        <div className='tts-field'>
          <span>参考音频</span>
          <label
            className={`tts-drop${file ? ' filled' : ''}${dragOver ? ' over' : ''}`}
            onDragOver={(event) => {
              event.preventDefault()
              setDragOver(true)
            }}
            onDragLeave={() => setDragOver(false)}
            onDrop={(event) => {
              event.preventDefault()
              setDragOver(false)
              pick(event.dataTransfer.files?.[0] ?? null)
            }}
          >
            {/*
              * input 不是 hidden 而是透明盖在整块上：这样点击任意位置都能选文件，
              * 键盘 Tab 也能聚焦到它（focus-within 就是它的焦点样式）。
              */}
            <input
              type='file'
              accept='audio/*'
              data-autofocus
              onChange={(event) => pick(event.target.files?.[0] ?? null)}
            />
            {file ? (
              <>
                <span className='tts-drop-icon'>
                  <CheckCircle2 size={18} />
                </span>
                <span className='tts-drop-text'>
                  <strong title={file.name}>{file.name}</strong>
                  <small>{formatBytes(file.size)} · 已选中波形里的一段作为参考</small>
                </span>
                <span className='tts-drop-act'>换一个</span>
              </>
            ) : (
              <>
                <span className='tts-drop-icon'>
                  <Upload size={18} />
                </span>
                <span className='tts-drop-text'>
                  <strong>把参考音频拖到这里，或点击选择文件</strong>
                  <small>支持 wav / mp3 / m4a · 3~10 秒最合适，超过 20 秒只取开头一段</small>
                </span>
              </>
            )}
          </label>
        </div>

        {preview && (
          <div className='tts-preview'>
            <audio className='tts-player' controls preload='metadata' src={preview} />
            <small>{file?.name}</small>
          </div>
        )}

        {/* 选中的片段才是最终保存与被克隆的那一段，所以放在正文里而不是折叠起来 */}
        {preview && (
          <ReferenceAudioEditor src={preview} value={selection} onChange={setSelection} busy={busy} />
        )}

        <label className='tts-field'>
          <span>音色名称</span>
          <input
            type='text'
            value={name}
            maxLength={40}
            placeholder='默认取文件名'
            onChange={(event) => setName(event.target.value)}
          />
        </label>

        <label className='tts-field'>
          <span>音频里说出的文字（必填）</span>
          <textarea
            rows={3}
            value={text}
            placeholder='选好音频后点下面的「自动转写」，或手动逐字填写'
            onChange={(event) => setText(event.target.value)}
          />
          <small>与音频不一致时合成质量会下降，这是必须填的原因。</small>
        </label>

        <button
          className='secondary-button'
          type='button'
          onClick={() => void autoTranscribe()}
          disabled={!file || busy}
        >
          {busy ? <LoaderCircle className='spin' size={15} /> : <Wand2 size={15} />}
          {text ? '重新自动转写' : '自动转写这段音频'}
        </button>

        {mine.length > 0 && (
          <div className='tts-mine'>
            <p className='tts-mine-lead'>
              我的音色（{mine.length}）· 点「编辑」可以裁剪参考音频、换音频或改文本
            </p>
            <ul>
              {mine.map((item) => (
                <li key={item.id}>
                  <span className='tts-mine-name' title={item.reference_text}>
                    {item.name}
                    {item.seconds > 0 && <small>{item.seconds.toFixed(1)}s</small>}
                  </span>
                  <span className='tts-mine-actions'>
                    <button
                      className='icon-button'
                      type='button'
                      aria-label={`试听音色 ${item.name} 的参考音频`}
                      title='试听参考音频'
                      onClick={() => togglePreview(item)}
                    >
                      {playing === item.id ? <LoaderCircle className='spin' size={14} /> : <Play size={14} />}
                    </button>
                    <button
                      className='icon-button'
                      type='button'
                      aria-label={`编辑音色 ${item.name}`}
                      title='编辑参考音频与文本'
                      onClick={() => {
                        setError('')
                        setNotice('')
                        setEditing(item)
                      }}
                    >
                      <Pencil size={14} />
                    </button>
                    <button
                      className='icon-button danger'
                      type='button'
                      disabled={removing === item.id}
                      aria-label={`删除音色 ${item.name}`}
                      title='删除这个音色'
                      onClick={() => void removeVoice(item)}
                    >
                      {removing === item.id ? (
                        <LoaderCircle className='spin' size={14} />
                      ) : (
                        <Trash2 size={14} />
                      )}
                    </button>
                  </span>
                </li>
              ))}
            </ul>
            <audio
              ref={probe}
              preload='none'
              hidden
              onEnded={() => setPlaying('')}
              onPause={() => setPlaying('')}
            />
          </div>
        )}

        {truncated && (
          <p className='form-message warning'>
            这段音频较长，参考只用开头 {Math.round(seconds)} 秒，超出部分的文字请删掉。
          </p>
        )}
        {error && (
          <p className='form-message error' role='alert'>
            {error}
          </p>
        )}
        {notice && (
          <p className='form-message success' role='status'>
            {notice}
          </p>
        )}
      </div>
      <div className='modal-foot'>
        <button
          className='primary-button'
          type='button'
          onClick={() => void save()}
          disabled={busy || !file || !text.trim()}
        >
          {busy ? <LoaderCircle className='spin' size={16} /> : <Upload size={16} />}
          保存音色
        </button>
        <button className='text-button' type='button' onClick={onClose}>
          取消
        </button>
      </div>

      {/* 编辑弹窗叠在这一层之上：新建表单原样留着，关掉它就能接着填 */}
      {editing && (
        <VoiceEditModal
          voice={editing}
          onClose={() => setEditing(null)}
          onChanged={onChanged}
          onRemoved={(voiceId) => {
            setNotice(`音色「${editing.name}」已删除`)
            void voiceId
          }}
          onSaved={(_result, message) => setNotice(message)}
        />
      )}
    </Modal>
  )
}