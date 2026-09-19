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
import type { AppSettings } from '../types'

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

export function AppShell() {
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const [mode, setMode] = useState<ThemeMode>(cachedMode)

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
      const resolved = resolveTheme(mode)
      document.documentElement.dataset.theme = resolved
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
            <small>本地处理</small>
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
            {dark ? <Sun size={18} /> : <Moon size={18} />}
          </button>
        </div>
      </aside>
      <main className="main-content" id="main-content">
        <Outlet />
      </main>
    </div>
  )
}
