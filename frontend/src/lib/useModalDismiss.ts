import { useEffect, useRef } from 'react'

/**
 * 弹窗的两件公共事务：Esc 关闭 + 弹窗期间锁住背景滚动。
 *
 * 锁滚动后滚动条会消失、视口宽出约 15px，背景页和居中的弹窗都会跟着平移一下，所以顺手量出
 * 这段宽度写进 `--scroll-lock-gap`，由样式表补成内边距抵消掉（必须在设 overflow 之前量）。
 *
 * 回调存在 ref 里，effect 只跟 `open` 走：父组件每次重渲染（作者搜索每敲一个键、任务轮询）
 * 都不会把滚动锁拆掉再装一遍。
 */
export function useModalDismiss(open: boolean, onDismiss: () => void) {
  const dismiss = useRef(onDismiss)

  useEffect(() => {
    dismiss.current = onDismiss
  })

  useEffect(() => {
    if (!open) return
    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape') dismiss.current()
    }
    window.addEventListener('keydown', onKey)
    const gap = window.innerWidth - document.documentElement.clientWidth
    const root = document.documentElement
    document.body.style.overflow = 'hidden'
    if (gap > 0) root.style.setProperty('--scroll-lock-gap', `${gap}px`)
    return () => {
      window.removeEventListener('keydown', onKey)
      document.body.style.overflow = ''
      root.style.removeProperty('--scroll-lock-gap')
    }
  }, [open])
}
