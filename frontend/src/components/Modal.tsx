import { X, type LucideIcon } from 'lucide-react'
import { useRef, type ReactNode } from 'react'
import { createPortal } from 'react-dom'

import { useFocusTrap } from '../lib/useFocusTrap'
import { useModalDismiss } from '../lib/useModalDismiss'

/**
 * 弹窗外壳：遮罩 + 对话区 + Esc/滚动锁 + 焦点陷阱。
 *
 * 之前每个弹窗各写一遍 `modal-overlay` / `modal-dialog`，焦点管理只有
 * 作者弹窗靠 `autoFocus` 兜了一下；确认弹窗干脆还是浏览器原生的 `window.confirm`。
 * 统一到这里之后，「弹窗」在全应用只有一种行为。
 *
 * 遮罩用 createPortal 挂到 body 上，而不是留在调用它的页面里：
 * 遮罩是 `position: fixed`，只要祖先里有人带 transform（切页入场动画就是），
 * fixed 就会改成相对那个祖先定位——遮罩会缩成内容区大小、弹窗也跟着偏。
 * 挂到 body 之后，不管页面怎么动，遮罩永远是整屏。
 */
export function Modal({
  ariaLabel,
  onClose,
  role = 'dialog',
  size = 'md',
  closing = false,
  className = '',
  children,
}: {
  ariaLabel: string
  onClose: () => void
  role?: 'dialog' | 'alertdialog'
  size?: 'sm' | 'md' | 'lg'
  /** 退场动画期间为 true：样式据此播淡出，动画结束后父组件再卸载 */
  closing?: boolean
  className?: string
  children: ReactNode
}) {
  const dialogRef = useRef<HTMLElement>(null)
  useModalDismiss(true, onClose)
  useFocusTrap(true, dialogRef)

  return createPortal(
    <div
      className={closing ? 'modal-overlay closing' : 'modal-overlay'}
      onClick={onClose}
      role="presentation"
    >
      <section
        ref={dialogRef}
        className={`modal-dialog modal-${size}${closing ? ' closing' : ''}${className ? ` ${className}` : ''}`}
        role={role}
        aria-modal="true"
        aria-label={ariaLabel}
        onClick={(event) => event.stopPropagation()}
      >
        {children}
      </section>
    </div>,
    document.body,
  )
}

/** 弹窗标题栏：左侧可选图标 + 标题 + 副信息，右侧关闭按钮。 */
export function ModalHeader({
  title,
  icon: Icon,
  extra,
  onClose,
  closeLabel = '关闭',
}: {
  title: ReactNode
  icon?: LucideIcon
  extra?: ReactNode
  onClose: () => void
  closeLabel?: string
}) {
  return (
    <header className="modal-head">
      <div className="modal-title">
        {Icon && <Icon size={18} aria-hidden="true" />}
        <strong>{title}</strong>
        {extra}
      </div>
      <button className="icon-button" type="button" onClick={onClose} aria-label={closeLabel}>
        <X size={17} />
      </button>
    </header>
  )
}
