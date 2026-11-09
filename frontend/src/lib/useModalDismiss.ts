import { useEffect, useRef } from 'react'

/**
 * 当前打开的弹窗栈。只有最上面一层响应 Esc，也只有最后一层关闭时才解除滚动锁。
 * 确认弹窗可能叠在编辑/选择弹窗之上，没有这个栈的话按一次 Esc 会把两层一起关掉。
 */
const openModals: symbol[] = []
let locked = false
let previousOverflow = ''

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
    const id = Symbol('modal')
    openModals.push(id)

    function onKey(event: KeyboardEvent) {
      // 只让最上层响应；下层弹窗的监听器仍然挂着，但不能跟着一起关
      if (event.key === 'Escape' && openModals[openModals.length - 1] === id) {
        dismiss.current()
      }
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
    if (!locked) {
      previousOverflow = document.body.style.overflow
      const widthBefore = root.clientWidth
      document.body.style.overflow = 'hidden'
      const shift = root.clientWidth - widthBefore
      if (shift > 0) root.style.setProperty('--scroll-lock-gap', `${shift}px`)
      locked = true
    }

    return () => {
      window.removeEventListener('keydown', onKey)
      const index = openModals.lastIndexOf(id)
      if (index >= 0) openModals.splice(index, 1)
      // 叠层时先关上层：下层还开着就继续保持锁定
      if (openModals.length === 0 && locked) {
        document.body.style.overflow = previousOverflow
        root.style.removeProperty('--scroll-lock-gap')
        locked = false
      }
    }
  }, [open])
}
