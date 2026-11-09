import { Check, CheckCircle2, LoaderCircle, Mic, RefreshCw, Trash2, Upload } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

import { api } from '../lib/api'
import { formatBytes } from '../lib/format'
import { useConfirm } from '../lib/useConfirm'
import type { AudioSelection, TtsVoice, TtsVoiceEditResult } from '../types'
import { Modal, ModalHeader } from './Modal'
import { ReferenceAudioEditor } from './ReferenceAudioEditor'

/**
 * 编辑一个已有的自定义音色：换参考音频、裁剪、重命名、改参考文本、删除。
 *
 * 与 omnivoice-studio 的做法一致，几个要点直接沿用：
 *  - 选区只存在于界面，真正裁剪由后端按 (start, end) 完成；
 *  - 参考文本按 手填 → 转写 → **沿用旧值** 的顺序定，转写失败时把原因如实回传，
 *    不让用户以为文字已经换过了；
 *  - 换音频是不可撤销的，所以按钮上写清楚。
 */
export function VoiceEditModal({
  voice,
  onClose,
  onChanged,
  onRemoved,
  onSaved,
}: {
  voice: TtsVoice
  onClose: () => void
  /** 音频或文本变了之后刷新清单（音色可能被改名或被删） */
  onChanged: () => void
  onRemoved: (voiceId: string) => void
  /** 保存成功：把新音色回传，用于提示用户 */
  onSaved: (result: TtsVoiceEditResult, message: string) => void
}) {
  const [name, setName] = useState(voice.name)
  const [text, setText] = useState(voice.reference_text)
  const [file, setFile] = useState<File | null>(null)
  const [dragOver, setDragOver] = useState(false)
  const [selection, setSelection] = useState<AudioSelection>({ start: 0, end: voice.seconds })
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const objectUrl = useRef('')
  const confirm = useConfirm()

  const source = file ? objectUrl.current : voice.audio_url

  // 选中新文件后立刻给出预览地址；波形与试听都以它为准
  function pick(next: File | null) {
    if (objectUrl.current) {
      URL.revokeObjectURL(objectUrl.current)
      objectUrl.current = ''
    }
    if (!next) {
      setFile(null)
      setSelection({ start: 0, end: voice.seconds })
      return
    }
    objectUrl.current = URL.createObjectURL(next)
    setFile(next)
    // 旧选区是按上一个音频算的，对新文件多半越界；置成无效值，
    // 等波形解码完由编辑器回填真实全长（期间点转写会退回整段，不会转错一段）
    setSelection({ start: 0, end: 0 })
    setNotice('已选好新音频：先在波形上选一段，再点「重新转写」把文字对上新音频，最后点「保存修改」')
  }

  useEffect(
    () => () => {
      if (objectUrl.current) URL.revokeObjectURL(objectUrl.current)
    },
    [],
  )

  /** 结果提示要区分「文本是新的」与「文本还是旧的」——后者不说明就会以为白改了。 */
  function describe(result: TtsVoiceEditResult, action: string) {
    if (result.text_source === 'transcribed') return `${action}，参考文本已重新转写，请核对`
    if (result.text_source === 'kept') return `${action}；没能重新转写${result.note ? `：${result.note}` : ''}，参考文本还是原来的`
    return `${action}，参考文本按你填写的内容保存`
  }

  function fail(reason: unknown, fallback: string) {
    const message = reason instanceof Error ? reason.message : fallback
    setError(message)
    return message
  }

  async function save() {
    const label = name.trim() || voice.name
    if (!text.trim()) {
      setError('参考文本不能为空：它必须与音频逐字一致')
      return
    }
    setBusy('save')
    setError('')
    setNotice('')
    try {
      if (file) {
        // 换了音频：名字与参考文本一起带过去，音频在选区内落盘。
        // 选区还是无效值（波形没解码完）就不传，由后端按「取开头」处理，
        // 总比把一个越界的选区发过去报「选区太短」要好。
        const result = await api.replaceTtsVoiceAudio({
          id: voice.id,
          audio: file,
          name: label,
          reference_text: text.trim(),
          selection: transcribableRange(),
        })
        onChanged()
        onSaved(result, describe(result, `参考音频已替换（${result.voice.seconds.toFixed(2)} 秒）`))
        onClose()
        return
      }

      let result: TtsVoiceEditResult | null = null
      // 只改文本也算一次保存：裁剪接口收 start/end，整段传回去即可，
      // 否则「只调文字」这种最常见的改动会被静默丢掉。
      const rangeChanged =
        Math.abs(selection.end - selection.start - voice.seconds) > 0.01 || selection.start > 0.01
      const textChanged = text.trim() !== voice.reference_text
      if (rangeChanged || textChanged) {
        // 只有「波形还没解码完」（选区仍是 0 秒）才落回整段；合法选区哪怕正好等于
        // 最小长度也得原样发过去，否则剪到 2 秒这种边界值会被悄悄还原成整段。
        const range = selection.end > 0.01 ? selection : { start: 0, end: voice.seconds }
        result = await api.trimTtsVoice({
          id: voice.id,
          selection: range,
          reference_text: text.trim(),
        })
      }
      if (label !== voice.name) {
        const renamed = await api.renameTtsVoice(voice.id, label)
        result = result ?? renamed
      }
      onChanged()
      if (result) {
        onSaved(result, describe(result, `已保存（${result.voice.seconds.toFixed(2)} 秒）`))
      } else {
        onSaved(
          { voice: { ...voice, name: label }, text_source: 'typed', note: '' },
          '没有需要保存的改动',
        )
      }
      onClose()
    } catch (reason) {
      fail(reason, '保存失败，请稍后重试')
    } finally {
      setBusy('')
    }
  }

  /** 当前能转写的范围：换了新文件就转新文件的选区，否则转已存音频的选区。 */
  function transcribableRange(): AudioSelection | undefined {
    return selection.end - selection.start > 0.01 ? selection : undefined
  }

  async function retranscribe() {
    setBusy('asr')
    setError('')
    setNotice('')
    try {
      const range = transcribableRange()
      /*
       * 转写的是「界面上选中的那一段」，而且**不写盘**：写盘的话，
       * 用户只是想看看这段被裁成什么样，音色却已经被改掉了。
       * 换了新音频时转写本地文件即可，这时它还没落盘，只能走上传那条接口。
       */
      const result = file
        ? await api.transcribeTtsVoice(file, range)
        : await api.previewTtsVoiceTranscript(voice.id, range)
      setText(result.text)
      setNotice(
        result.truncated
          ? `这段比较长，只转写了前 ${result.seconds.toFixed(2)} 秒，核对一下文字`
          : file
            ? '已按新音频的选区转写，核对后点「保存修改」才会替换'
            : '已按选区转写，核对后点「保存修改」才会生效',
      )
    } catch (reason) {
      fail(reason, '转写失败，可以直接修改上面的文字')
    } finally {
      setBusy('')
    }
  }

  async function remove() {
    const confirmed = await confirm({
      title: `删除音色「${voice.name}」？`,
      description: '这个音色的参考音频会一并删除；已经合成的音频不受影响，但以后需要重新克隆才能再使用它。',
      confirmLabel: '删除音色',
      tone: 'danger',
    })
    if (!confirmed) return
    setBusy('remove')
    setError('')
    try {
      await api.deleteTtsVoice(voice.id)
      onChanged()
      onRemoved(voice.id)
      onClose()
    } catch (reason) {
      fail(reason, '删除失败，请稍后重试')
    } finally {
      setBusy('')
    }
  }

  return (
    <Modal ariaLabel={`编辑音色 ${voice.name}`} onClose={onClose} size='lg' layer='top'>
      <ModalHeader title={`编辑音色 · ${voice.name}`} icon={Mic} onClose={onClose} />
      <div className='modal-body tts-clone'>
        <label className='tts-field'>
          <span>音色名称</span>
          <input
            type='text'
            value={name}
            maxLength={40}
            disabled={Boolean(busy)}
            onChange={(event) => setName(event.target.value)}
          />
        </label>

        <ReferenceAudioEditor
          src={source ?? ''}
          value={selection}
          onChange={setSelection}
          busy={Boolean(busy)}
        />

        <div className='tts-field'>
          <span>换一段参考音频（可选）</span>
          <label
            className={`tts-drop compact${file ? ' filled' : ''}${dragOver ? ' over' : ''}`}
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
            <input
              type='file'
              accept='audio/*'
              disabled={Boolean(busy)}
              onChange={(event) => pick(event.target.files?.[0] ?? null)}
            />
            <span className='tts-drop-icon'>
              {file ? <CheckCircle2 size={16} /> : <Upload size={16} />}
            </span>
            <span className='tts-drop-text'>
              <strong>{file ? file.name : '拖进来或点击选择新音频'}</strong>
              <small>
                {file
                  ? `${formatBytes(file.size)} · 保存后覆盖原音频，且不可撤销`
                  : `当前用的是这个音色已保存的参考音频（${voice.seconds.toFixed(2)} 秒）`}
              </small>
            </span>
          </label>
        </div>

        <label className='tts-field'>
          <span>音频里说出的文字（必填）</span>
          <textarea
            rows={3}
            value={text}
            disabled={Boolean(busy)}
            onChange={(event) => setText(event.target.value)}
          />
          <small>裁剪或替换音频后文字要对上这一段，否则克隆出的音色会明显变差。</small>
        </label>

        <div className='tts-edit-actions'>
          <button
            className='secondary-button'
            type='button'
            disabled={Boolean(busy)}
            title={
              file
                ? '按新音频的选区重新转写一遍（换完音频最该做的一件事）'
                : '按当前选区重新转写一遍'
            }
            onClick={() => void retranscribe()}
          >
            {busy === 'asr' ? <LoaderCircle className='spin' size={15} /> : <RefreshCw size={15} />}
            重新转写这段音频
          </button>
          <button
            className='icon-button danger'
            type='button'
            disabled={Boolean(busy)}
            aria-label='删除这个音色'
            title='删除这个音色'
            onClick={() => void remove()}
          >
            {busy === 'remove' ? <LoaderCircle className='spin' size={14} /> : <Trash2 size={14} />}
          </button>
        </div>

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
          disabled={Boolean(busy) || !text.trim()}
          onClick={() => void save()}
        >
          {busy === 'save' ? <LoaderCircle className='spin' size={16} /> : <Check size={16} />}
          保存修改
        </button>
        <button className='text-button' type='button' onClick={onClose}>
          取消
        </button>
      </div>
    </Modal>
  )
}