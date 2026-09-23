import {
  Cable,
  Cpu,
  FileText,
  Library,
  ListTodo,
  Moon,
  Plus,
  Settings,
  Sun,
} from 'lucide-react'
import { useEffect, useState } from 'react'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'

import { api } from '../lib/api'
import type { AppSettings, UpdateState } from '../types'

const navigation = [
  { to: '/', label: '工作台', icon: Plus },
  { to: '/library', label: '文案库', icon: Library },
  { to: '/jobs', label: '任务队列', icon: ListTodo },
  { to: '/models', label: '模型管理', icon: Cpu },
  { to: '/connectors', label: '平台连接', icon: Cable },
  { to: '/settings', label: '设置', icon: Settings },
]

type ThemeMode = AppSettings['theme']

function prefersDark() {
  return window.matchMedia('(prefers-color-scheme: dark)').matches
}

function resolveTheme(mode: ThemeMode) {
  if (mode === 'system') return prefersDark() ? 'dark' : 'light'
  return mode
}

function cachedMode(): ThemeMode {
  const cached = localStorage.getItem('vtw-theme')
  return cached === 'dark' || cached === 'light' ? cached : 'system'
}

/**
 * 主题一翻，几乎所有元素都在改 color / background-color / border-color，
 * 每个带 transition 的元素会一起动，整页读起来是「糊」一下而不是干脆地切换。
 * 这里在切换的那两帧挂上一条把 transition 全部关掉的样式，新配色落定后再移除。
 */
function withoutTransitions(apply: () => void) {
  const override = document.createElement('style')
  override.append(document.createTextNode('*,*::before,*::after{transition:none !important}'))
  document.head.append(override)
  apply()
  // 读一次 offsetHeight 只是为了逼浏览器同步刷新样式：新配色要在覆盖仍生效时就落定
  void document.body.offsetHeight
  requestAnimationFrame(() => {
    requestAnimationFrame(() => override.remove())
  })
}

export function AppShell() {
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const [mode, setMode] = useState<ThemeMode>(cachedMode)
  // 版本号来自 /api/health（最稳的一个接口），更新提示来自 /api/updates
  const [version, setVersion] = useState('')
  const [update, setUpdate] = useState<UpdateState | null>(null)

  // 移动端从长列表切换页面时回到页首，避免新页面从旧滚动位置开始。
  useEffect(() => {
    window.scrollTo({ top: 0, left: 0, behavior: 'auto' })
  }, [pathname])

  // 后端设置是主题的唯一真实来源；localStorage 只用于首屏避免闪烁。
  useEffect(() => {
    let active = true
    api
      .settings()
      .then((settings) => {
        if (active) setMode(settings.theme)
      })
      .catch(() => undefined)
    return () => {
      active = false
    }
  }, [])

  useEffect(() => {
    const apply = () => {
      withoutTransitions(() => {
        document.documentElement.dataset.theme = resolveTheme(mode)
      })
      if (mode !== 'system') localStorage.setItem('vtw-theme', mode)
    }
    apply()
    if (mode !== 'system') return
    const media = window.matchMedia('(prefers-color-scheme: dark)')
    media.addEventListener('change', apply)
    return () => media.removeEventListener('change', apply)
  }, [mode])

  // 设置页保存主题后立即生效，无需刷新。
  useEffect(() => {
    const sync = (event: Event) => {
      const detail = (event as CustomEvent<ThemeMode>).detail
      if (detail === 'system' || detail === 'light' || detail === 'dark') setMode(detail)
    }
    window.addEventListener('vtw:theme', sync)
    return () => window.removeEventListener('vtw:theme', sync)
  }, [])

  useEffect(() => {
    let active = true
    api
      .health()
      .then((info) => {
        if (active) setVersion(info.version)
      })
      .catch(() => undefined)
    return () => {
      active = false
    }
  }, [])

  // 版本状态：后端按设置节流，正常打开页面不会每次都打远程。
  useEffect(() => {
    let active = true
    api
      .updates()
      .then((state) => {
        if (active) setUpdate(state)
      })
      .catch(() => undefined)
    return () => {
      active = false
    }
  }, [])

  // 设置页检查/下载/跳过之后同步过来，两处提示始终是同一个来源。
  useEffect(() => {
    const sync = (event: Event) => {
      const detail = (event as CustomEvent<UpdateState>).detail
      if (detail) setUpdate(detail)
    }
    window.addEventListener('vtw:updates', sync)
    return () => window.removeEventListener('vtw:updates', sync)
  }, [])

  const dark = resolveTheme(mode) === 'dark'

  function toggleTheme() {
    const next: ThemeMode = dark ? 'light' : 'dark'
    setMode(next)
    void api.updateSettings({ theme: next }).catch(() => undefined)
  }

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">跳到主要内容</a>
      <aside className="sidebar" aria-label="主导航">
        <button className="brand" type="button" onClick={() => navigate('/')} aria-label="返回工作台">
          <span className="brand-mark"><FileText size={19} strokeWidth={2.1} /></span>
          <span>
            <strong>文案工作台</strong>
            {/* 版本号跟着「本地处理」走：它是程序自己的身份信息，不占页脚位置 */}
            <small>本地处理{version ? ` · v${version}` : ''}</small>
          </span>
        </button>

        <button className="new-task-button" type="button" onClick={() => navigate('/')}>
          <Plus size={17} />
          新建提取
        </button>

        <nav className="nav-list">
          {navigation.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === '/'}
              aria-label={item.label}
              title={item.label}
              className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}
            >
              <item.icon size={18} />
              <span>{item.label}</span>
            </NavLink>
          ))}
        </nav>

        <div className="sidebar-footer">
          <div className="footer-row">
            <div className="privacy-note">
              <span className="status-dot ready" />
              <span><strong>本地运行</strong><small>内容只存在这台电脑</small></span>
            </div>
            <button
              className="icon-button"
              type="button"
              onClick={toggleTheme}
              aria-label={dark ? '切换浅色模式' : '切换深色模式'}
              title={dark ? '浅色模式' : '深色模式'}
            >
              {/* 状态变化图标：两个都留在 DOM 里做交叉淡化，不硬切可见性 */}
              <span className="icon-swap" aria-hidden="true">
                <Sun className={dark ? undefined : 'is-off'} size={18} />
                <Moon className={dark ? 'is-off' : undefined} size={18} />
              </span>
            </button>
          </div>
          {/* 只有真有新版本时才多出这一行提示，平时页脚保持两行结构 */}
          {update?.has_update && update.latest && (
            <button
              className="update-note"
              type="button"
              onClick={() => navigate('/settings')}
              title={`新版本 v${update.latest.version} 已发布，点击到设置页查看`}
            >
              <span className="status-dot ready" aria-hidden="true" />
              新版本 v{update.latest.version}
            </button>
          )}
        </div>
      </aside>
      <main className="main-content" id="main-content">
        <Outlet />
      </main>
    </div>
  )
}
