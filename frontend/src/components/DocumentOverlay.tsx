import { useCallback, useRef, useState } from 'react'

import { Modal } from './Modal'
import { DocumentEditor } from '../pages/EditorPage'
import { useConfirm } from '../lib/useConfirm'

/**
 * 文案弹窗：打开即进入编辑态。
 *
 * 这里曾经先展示一层只读预览，再由「编辑正文」切进编辑器。实际上点开一条文案
 * 几乎都是要改它，而编辑器本身已经能全文阅读、复制和导出；多一层预览只是多一次点击，
 * 也让复制、导出、AI 总结各维护了两套。现在列表页仍然挂在弹窗后面，关闭就回到原位置。
 */
export function DocumentOverlay({
  documentId,
  onClose,
}: {
  documentId: string
  /** changed 表示这次弹窗里真的落过盘：列表据此决定要不要刷新那一行 */
  onClose: (changed: boolean) => void
}) {
  // 退场期间为 true：样式里据此播 150ms 的淡出 + 上收，然后才真正卸载
  const [closing, setClosing] = useState(false)
  const closingRef = useRef(false)
  // 编辑器报告的未落盘状态：关闭按钮、Esc、点遮罩共用这一个守卫
  const dirtyRef = useRef(false)
  // 只有真正保存过才让列表刷新；打开看一眼再关掉不该触发一次重拉
  const changedRef = useRef(false)
  const confirm = useConfirm()

  /**
   * 关闭前先播一段轻退场再卸载。退出要比进入轻——用户此刻的注意力已经在下一件事上，
   * 弹窗不该用一整套进场动效的镜像把注意力抢回来。
   */
  const beginClose = useCallback(() => {
    if (closingRef.current) return
    closingRef.current = true
    setClosing(true)
    window.setTimeout(() => onClose(changedRef.current), 150)
  }, [onClose])

  // 有未保存的改动时先问一句；平时 0.6 秒自动保存已经兜住，很少会走到这里
  const requestClose = useCallback(() => {
    if (closingRef.current) return
    if (dirtyRef.current) {
      void confirm({
        title: '还有未保存的更改',
        description: '现在关闭会丢掉刚才的修改；识别原文与已经保存过的内容不受影响。',
        confirmLabel: '关闭弹窗',
        cancelLabel: '留下继续编辑',
        tone: 'danger',
      }).then((ok) => {
        if (ok) beginClose()
      })
      return
    }
    beginClose()
  }, [beginClose, confirm])

  return (
    <Modal
      ariaLabel="编辑文案"
      closing={closing}
      onClose={requestClose}
      className="document-overlay is-editing"
    >
      <DocumentEditor
        documentId={documentId}
        onClose={requestClose}
        onDirtyChange={(dirty) => {
          dirtyRef.current = dirty
        }}
        onSaved={() => {
          changedRef.current = true
        }}
      />
    </Modal>
  )
}
