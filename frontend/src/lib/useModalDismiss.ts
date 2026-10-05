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
    const root = document.documentElement
    /*
     * 锁滚动会让滚动条消失、视口变宽，背景页和居中的弹窗都会平移一下，
     * 所以要补上这段宽度。这里改成「锁上之后再量」：
     * 量的是「视口实际变宽了多少」，不是「滚动条有多宽」——两者并不总是相等。
     * 样式表里根元素用了 `scrollbar-gutter: stable`，槽位恒定预留时视口压根不会变宽，
     * 量出来就是 0，自然也不用补偿；浏览器若没兑现这个预留，量出来就是真实差值。
     * 读 clientWidth 会触发一次同步布局，但每个弹窗只发生一次，可以忽略。
     */
    const widthBefore = root.clientWidth
    document.body.style.overflow = 'hidden'
    const shift = root.clientWidth - widthBefore
    if (shift > 0) root.style.setProperty('--scroll-lock-gap', `${shift}px`)
    return () => {
      window.removeEventListener('keydown', onKey)
      document.body.style.overflow = ''
      root.style.removeProperty('--scroll-lock-gap')
    }
  }, [open])
}
