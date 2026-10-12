import { LoaderCircle, Mic, Trash2, Upload, Wand2 } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

import { api } from '../lib/api'
import type { TtsVoice } from '../types'
import { Modal, ModalHeader } from './Modal'

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
  /** 已有的自定义音色：管理（删除）入口放在这里，主页面只留一个下拉框 */
  voices: TtsVoice[]
}) {
  const [file, setFile] = useState<File | null>(null)
  const [name, setName] = useState('')
  const [text, setText] = useState('')
  const [preview, setPreview] = useState('')
  const [seconds, setSeconds] = useState(0)
  const [truncated, setTruncated] = useState(false)
  const [busy, setBusy] = useState(false)
  const [removing, setRemoving] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const objectUrl = useRef('')

  const mine = voices.filter((item) => !item.builtin)

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
      const result = await api.transcribeTtsVoice(file)
      setText(result.text)
      setSeconds(result.seconds)
      setTruncated(result.truncated)
      setNotice(
        result.truncated
          ? `这段音频较长，只转写了前 ${Math.round(result.seconds)} 秒（参考音频也只用这一段），请核对文字`
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

  return (
    <Modal ariaLabel='音色克隆' onClose={onClose} size='md'>
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

        <label className='tts-field'>
          <span>参考音频</span>
          <input
            type='file'
            accept='audio/*'
            data-autofocus
            onChange={(event) => pick(event.target.files?.[0] ?? null)}
          />
          <small>支持 wav / mp3 / m4a，超过 20 秒只取开头一段。</small>
        </label>

        {preview && (
          <div className='tts-preview'>
            <audio className='tts-player' controls preload='metadata' src={preview} />
            <small>{file?.name}</small>
          </div>
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
            <p className='tts-mine-lead'>我的音色（{mine.length}）</p>
            <ul>
              {mine.map((item) => (
                <li key={item.id}>
                  <span title={item.reference_text}>{item.name}</span>
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
                </li>
              ))}
            </ul>
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
    </Modal>
  )
}