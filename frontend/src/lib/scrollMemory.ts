import { useEffect, useLayoutEffect, useRef } from 'react'

/**
 * 列表页的滚动位置：按 location（路径 + 查询串）分别记在 sessionStorage 里。
 *
 * 「点行进预览、再进编辑、回来重新找位置」这一步的主要代价就在这里：改完一个字
 * 回到列表却被弹回页首，之前滚到第几行、翻了哪几页全得重来。
 */

const PREFIX = 'vtw-scroll:'

/** 读某个 location 记下的位置；没记过就是 0（页首） */
export function readScroll(location: string): number {
  try {
    const raw = sessionStorage.getItem(PREFIX + location)
    const value = raw ? Number(raw) : 0
    return Number.isFinite(value) && value > 0 ? value : 0
  } catch {
    // 隐私模式下 sessionStorage 可能直接抛错；记不住位置不影响使用
    return 0
  }
}

function writeScroll(location: string, top: number) {
  try {
    sessionStorage.setItem(PREFIX + location, String(top))
  } catch {
    // 同上
  }
}

/**
 * 跟着 location 走：离开时保留位置，回来时恢复。放在 AppShell 里，所有页面共用。
 *
 * 三个细节：
 * - 关掉浏览器的自动恢复（`scrollRestoration = manual`），否则它和这里的恢复会互相拉扯；
 * - 只在 location 没变时才记录（`current`）：切页时页面变矮，浏览器会把滚动位置夹回
 *   顶部并触发一次 scroll，那不是用户滚的，记下来会把原来那一行的位置冲掉；
 * - 恢复要重试几帧：列表数据是异步来的，页面一开始还没长到目标高度，`scrollTo` 会被
 *   夹到底部，等列表渲染出来再滚一次才真正到位。
 */
export function useScrollMemory(location: string) {
  // 用 layout effect 更新：它在浏览器派发上面那次「夹回来」的 scroll 之前就执行完了
  const current = useRef(location)
  useLayoutEffect(() => {
    current.current = location
  }, [location])

  useEffect(() => {
    if ('scrollRestoration' in window.history) window.history.scrollRestoration = 'manual'
  }, [])

  useEffect(() => {
    let frame = 0
    const save = () => {
      frame = 0
      if (current.current !== location) return
      writeScroll(location, window.scrollY)
    }
    const onScroll = () => {
      // rAF 节流：滚动一次能打几十个事件，没必要每个都写一遍 sessionStorage
      if (!frame) frame = window.requestAnimationFrame(save)
    }
    window.addEventListener('scroll', onScroll, { passive: true })

    const target = readScroll(location)
    let tries = 0
    let timer = 0
    if (target > 0) {
      const restore = () => {
        window.scrollTo({ top: target, left: 0, behavior: 'auto' })
        // 没到位就是页面还没那么高，再等一帧；最多约 1 秒，之后交给用户自己滚
        if (Math.abs(window.scrollY - target) > 1 && tries < 20) {
          tries += 1
          timer = window.setTimeout(restore, 50)
        }
      }
      restore()
    } else {
      window.scrollTo({ top: 0, left: 0, behavior: 'auto' })
    }

    return () => {
      window.removeEventListener('scroll', onScroll)
      if (frame) window.cancelAnimationFrame(frame)
      if (timer) window.clearTimeout(timer)
    }
  }, [location])
}
