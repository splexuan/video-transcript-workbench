import { useEffect, type RefObject } from 'react'

const FOCUSABLE = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

function focusable(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    (element) => element.getAttribute('aria-hidden') !== 'true',
  )
}

/**
 * 弹窗的焦点陷阱：打开时把焦点移进弹窗、Tab 在弹窗内循环、关闭后还给触发它的元素。
 *
 * 之前弹窗只有 `role="dialog"`，键盘用户 Tab 几下就会跑到遮罩后面的页面上，
 * 读屏软件与视觉焦点对不上。这里补上焦点管理。
 */
export function useFocusTrap(active: boolean, container: RefObject<HTMLElement | null>) {
  useEffect(() => {
    if (!active) return
    const node = container.current
    if (!node) return

    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const initial = node.querySelector<HTMLElement>('[data-autofocus]') ?? focusable(node)[0]
    initial?.focus()

    function onKeyDown(event: KeyboardEvent) {
      if (event.key !== 'Tab') return
      const items = focusable(node as HTMLElement)
      if (items.length === 0) {
        event.preventDefault()
        return
      }
      const first = items[0]
      const last = items[items.length - 1]
      const current = document.activeElement
      if (event.shiftKey && (current === first || !(node as HTMLElement).contains(current))) {
        event.preventDefault()
        last.focus()
        return
      }
      if (!event.shiftKey && (current === last || !(node as HTMLElement).contains(current))) {
        event.preventDefault()
        first.focus()
      }
    }

    node.addEventListener('keydown', onKeyDown)
    return () => {
      node.removeEventListener('keydown', onKeyDown)
      previous?.focus()
    }
  }, [active, container])
}
