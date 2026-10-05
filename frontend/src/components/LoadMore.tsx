import { ChevronDown, LoaderCircle } from 'lucide-react'
import { useEffect, useRef } from 'react'

/**
 * 分页列表的收尾：滚到底自动加载下一页，按钮保留作为兜底——
 * 键盘、读屏用户需要一个能聚焦的入口，观察器不可用时也不至于卡死。
 *
 * 哨兵提前 240px 触发，滚动过程中不会先看到一片空白再等加载。
 * 加载中不再观察：本页渲染完、`loading` 落回 false 后会重新观察一次，
 * 因此内容没填满一屏时会接着往下取，直到游标用尽（父级据此卸载本组件）。
 */
export function LoadMore({
  label,
  loading,
  onLoad,
}: {
  label: string
  /** 有请求在飞：此时既不该观察，也不该让按钮可点 */
  loading: boolean
  onLoad: () => void
}) {
  const sentinel = useRef<HTMLDivElement>(null)
  const load = useRef(onLoad)

  useEffect(() => {
    load.current = onLoad
  })

  useEffect(() => {
    const node = sentinel.current
    if (!node || loading) return
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) load.current()
      },
      { rootMargin: '240px' },
    )
    observer.observe(node)
    return () => observer.disconnect()
  }, [loading])

  return (
    <div className="list-more" ref={sentinel}>
      <button className="secondary-button" type="button" onClick={onLoad} disabled={loading}>
        {loading ? <LoaderCircle className="spin" size={16} /> : <ChevronDown size={16} />}
        {loading ? '正在加载' : label}
      </button>
    </div>
  )
}
