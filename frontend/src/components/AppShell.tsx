import {
  AudioLines,
  Cable,
  Cpu,
  FileText,
  House,
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
import { useScrollMemory } from '../lib/scrollMemory'
import type { AppSettings } from '../types'

/**
 * 导航分三组：上面三个是每天都要走的「提取 → 校对 → 看进度」，
 * 中间是拿到文案之后的创作能力，下面三个是装好就很少再动的配置。
 * 原来六项平铺，新用户看不出哪些是主流程、哪些只是配置。
 *
 * 「工作台」原先用加号图标，和下面的「新建提取」按钮撞在一起：
 * 同一个图标既表示页面又表示动作，点哪个都像要新建任务。
 * 换成房子图标，加号只留给动作按钮。
 */
const navigation = [
  {
    label: '提取与校对',
    items: [
      { to: '/', label: '工作台', icon: House },
      { to: '/library', label: '文案库', icon: Library },
      { to: '/jobs', label: '任务队列', icon: ListTodo },
    ],
  },
  {
    label: '创作',
    items: [
      { to: '/tts', label: '语音合成', icon: AudioLines },
    ],
  },
  {
    label: '运行配置',
    items: [
      { to: '/models', label: '模型管理', icon: Cpu },
      { to: '/connectors', label: '平台连接', icon: Cable },
      { to: '/settings', label: '设置', icon: Settings },
    ],
  },
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
  const { pathname, search } = useLocation()
  const [mode, setMode] = useState<ThemeMode>(cachedMode)
  // 版本号来自 /api/health（最稳的一个接口）。程序不自带更新检查：
  // 发行包由人工分发，换版本靠解压覆盖，界面只如实显示当前是哪个版本。
  const [version, setVersion] = useState('')

  // 滚动位置跟着 location 走：进编辑页改完一条再回来，停在原来那一行而不是被弹回页首。
  // 之前是每次换页都强制回顶，从长列表离开一次，位置就白丢了。
  useScrollMemory(`${pathname}${search}`)

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

  const dark = resolveTheme(mode) === 'dark'

  function toggleTheme() {
    const next: ThemeMode = dark ? 'light' : 'dark'
    setMode(next)
    // 设置页的「界面主题」下拉是同一个设置的另一个入口：广播出去，
    // 免得在侧栏切完再进设置页，看到的下拉还是旧值。
    window.dispatchEvent(new CustomEvent<ThemeMode>('vtw:theme', { detail: next }))
    void api.updateSettings({ theme: next }).catch(() => undefined)
  }

  /**
   * 「新建提取」的落点：已经在工作台时不要做一个「回到当前页」的空动作，
   * 而是把表单重置到可输入状态（清空、切回单条、聚焦输入框）。
   */
  /**
   * 已经在某一页时，点侧栏里同一个导航不该把筛选丢掉：列表的筛选条件记在地址上
   * （见 LibraryPage），`to="/library"` 会连着查询串一起清空，看起来就像「筛选自己没了」。
   */
  function navTarget(to: string) {
    return pathname === to ? `${to}${search}` : to
  }

  function startNewTask() {
    if (pathname === '/') window.dispatchEvent(new Event('vtw:new-task'))
    else navigate('/')
  }

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">跳到主要内容</a>
      <aside className="sidebar" aria-label="主导航">
        <button className="brand" type="button" onClick={() => navigate('/')} aria-label="返回工作台">
          <span className="brand-mark"><FileText size={19} strokeWidth={2.1} /></span>
          <span>
            <strong>创作工作室</strong>
            {/* 版本号跟着「本地处理」走：它是程序自己的身份信息，不占页脚位置 */}
            <small>本地处理{version ? ` · v${version}` : ''}</small>
          </span>
        </button>

        <button className="new-task-button" type="button" onClick={startNewTask} title="新建一次提取">
          <Plus size={17} />
          新建提取
        </button>

        <nav className="nav-list">
          {navigation.map((group) => (
            <div className="nav-group" key={group.label}>
              <span className="nav-group-label">{group.label}</span>
              {group.items.map((item) => (
                <NavLink
                  key={item.to}
                  to={navTarget(item.to)}
                  end={item.to === '/'}
                  aria-label={item.label}
                  title={item.label}
                  className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}
                >
                  <item.icon size={18} />
                  <span>{item.label}</span>
                </NavLink>
              ))}
            </div>
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
        </div>
      </aside>
      <main className="main-content" id="main-content">
        {/*
          key 用 pathname：换页时整块重新挂载，CSS 入场动画才会重跑一遍。
          没有它路由切换是硬替换，从长列表切到短页面会「闪」一下。
        */}
        <div className="page-transition" key={pathname}>
          <Outlet />
        </div>
      </main>
    </div>
  )
}
