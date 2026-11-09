import {
  Cable,
  BookOpen,
  CheckCircle2,
  CircleDashed,
  ClipboardPaste,
  Cpu,
  HardDrive,
  KeyRound,
  LoaderCircle,
  LogIn,
  MessageCircle,
  Music2,
  Play,
  Trash2,
  Tv,
  Wrench,
} from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../lib/api'
import { useConfirm } from '../lib/useConfirm'
import { LoadingState } from '../components/LoadingState'
import type { Connector, CredentialStatus, LoginStatus } from '../types'

const descriptions: Record<string, string> = {
  local: '导入电脑里的 MP4、MP3、WAV 等音视频文件。',
  bilibili: '粘贴视频链接即可提取；公开视频字幕优先，会员内容需配置访问凭据。',
  douyin: '粘贴抖音作品链接或分享文案即可提取，首次需要一次访问授权。',
  kuaishou: '粘贴快手分享链接或分享文案即可提取，不需要登录。',
  xiaohongshu: '粘贴小红书视频笔记链接或分享文案即可提取，公开视频无需登录。',
  wechat: '粘贴视频号分享链接即可提取，需要在「设置」里配置兜底解析接口的 API Key。',
}

// 每个平台最关键的两个 Cookie 字段，给用户一个「粘对了」的参照。
const cookieSamples: Record<string, string> = {
  bilibili: 'SESSDATA=...; bili_jct=...',
  douyin: 'ttwid=...; s_v_web_id=...',
  xiaohongshu: 'web_session=...; a1=...',
}

const connectorIcons: Record<string, typeof Cable> = {
  local: HardDrive,
  bilibili: Tv,
  douyin: Music2,
  kuaishou: Play,
  xiaohongshu: BookOpen,
  wechat: MessageCircle,
}

/**
 * 六张卡片按「要用它得先准备什么」分三组。
 *
 * 原来是六张等宽卡片平铺：内容少的（本地文件、快手）被撑到和内容多的一样高，
 * 下半张全是空白；而「哪个平台要授权」这件事只能靠页头一串标签去猜。
 * 分组之后，每组的准备成本写在组标题上，卡片内容量也接近。
 */
const connectorGroups: { label: string; hint: string; ids: string[] }[] = [
  {
    label: '本机与免登录',
    hint: '不需要任何凭据，粘贴链接或导入文件即可',
    ids: ['local', 'kuaishou'],
  },
  {
    label: '需要访问授权',
    hint: '抖音、小红书首次使用点一次授权；B站凭据是可选项',
    ids: ['bilibili', 'douyin', 'xiaohongshu'],
  },
  {
    label: '需要第三方兜底解析',
    hint: '视频号没有本机解析方案，先在「设置」里填好兜底解析的 API Key',
    ids: ['wechat'],
  },
]

export function ConnectorsPage() {
  const [connectors, setConnectors] = useState<Connector[]>([])
  const [credentials, setCredentials] = useState<Record<string, CredentialStatus>>({})
  const [session, setSession] = useState<LoginStatus | null>(null)
  const [editing, setEditing] = useState('')
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [refreshing, setRefreshing] = useState(false)
  const [reloadKey, setReloadKey] = useState(0)
  const [loading, setLoading] = useState(true)
  const confirm = useConfirm()

  useEffect(() => {
    let active = true
    Promise.all([api.connectors(), api.credentials()])
      .then(([items, credentialItems]) => {
        if (!active) return
        setConnectors(items)
        setCredentials(
          Object.fromEntries(credentialItems.map((item) => [item.platform, item] as const)),
        )
        setError('')
      })
      .catch((reason: Error) => {
        if (active) setError(reason.message || '读取连接器状态失败')
      })
      .finally(() => {
        if (active) {
          setLoading(false)
          setRefreshing(false)
        }
      })
    return () => {
      active = false
    }
  }, [reloadKey])

  // 浏览器助手运行期间轮询，取到凭据或失败后停止。
  useEffect(() => {
    if (session?.status !== 'running') return
    const platform = session.platform
    let active = true
    const timer = window.setInterval(() => {
      api.browserLoginStatus(platform)
        .then((next) => {
          if (!active) return
          setSession(next)
          if (next.status === 'saved') {
            setNotice(next.message)
            setReloadKey((value) => value + 1)
          } else if (next.status === 'failed') {
            setError(next.message)
          }
        })
        .catch(() => undefined)
    }, 1500)
    return () => {
      active = false
      window.clearInterval(timer)
    }
  }, [session])

  function refresh() {
    setRefreshing(true)
    setReloadKey((value) => value + 1)
  }

  async function startBrowserLogin(platform: string, mode: 'guest' | 'login') {
    setBusy(platform)
    setError('')
    setNotice('')
    setEditing('')
    try {
      setSession(await api.startBrowserLogin(platform, mode))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '启动浏览器助手失败')
    } finally {
      setBusy('')
    }
  }

  async function cancelBrowserLogin(platform: string) {
    try {
      setSession(await api.cancelBrowserLogin(platform))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '取消失败')
    }
  }

  async function saveCookie(platform: string) {
    const value = draft.trim()
    if (!value) return
    setBusy(platform)
    setError('')
    setNotice('')
    try {
      const saved = await api.saveCredential(platform, value)
      setCredentials((current) => ({ ...current, [platform]: saved }))
      setEditing('')
      setDraft('')
      setNotice(`已保存 ${saved.entries} 条访问凭据。`)
      refresh()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '保存访问凭据失败')
    } finally {
      setBusy('')
    }
  }

  async function clearCookie(platform: string) {
    // 清除后需要重新授权，先确认一次
    const confirmed = await confirm({
      title: '清除该平台保存的访问凭据？',
      description: '清除后需要重新获取一次，才能解析需要登录才能观看的内容。',
      confirmLabel: '清除凭据',
      tone: 'danger',
    })
    if (!confirmed) return
    setBusy(platform)
    setError('')
    setNotice('')
    try {
      await api.deleteCredential(platform)
      setSession(null)
      setNotice('已清除保存的访问凭据。')
      refresh()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '清除访问凭据失败')
    } finally {
      setBusy('')
    }
  }

  const grouped = connectorGroups.map((group) => ({
    label: group.label,
    hint: group.hint,
    items: connectors.filter((connector) => group.ids.includes(connector.id)),
  }))
  // 后端加了新平台、这里还没归类时兜住它们，别让卡片从页面上消失
  const ungrouped = connectors.filter(
    (connector) => !connectorGroups.some((group) => group.ids.includes(connector.id)),
  )
  if (ungrouped.length > 0) grouped.push({ label: '其他平台', hint: '', items: ungrouped })

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1>平台连接</h1>
          {/* 「谁需要授权」原来挤在这一行小标签里；现在每个分组的标题上就写清楚了，
              页头只留一个真正会点出去的入口 */}
          <p>管理视频来源的解析能力和访问授权。</p>
        </div>
        <Link className="secondary-button" to="/models">
          <Cpu size={16} /> 管理识别模型
        </Link>
      </header>

      {error && <p className="form-message error" role="alert">{error}</p>}
      {notice && <p className="form-message success" role="status">{notice}</p>}

      {loading && connectors.length === 0 ? (
        <LoadingState label="正在检查平台状态…" rows={4} />
      ) : (
        grouped.map((group) => (
          <section className="connector-group" key={group.label} aria-label={group.label}>
            <div className="connector-group-head">
              <h2>{group.label}</h2>
              {group.hint && <p>{group.hint}</p>}
            </div>
            <div className="connector-grid">
              {group.items.map((connector) => {
                const credential = credentials[connector.id]
                const working = busy === connector.id
                // 后端为哪些平台开放了凭据配置：B站是可选的，抖音、小红书是必需的。
                const cookiePlatform = credential !== undefined
                const running = session?.platform === connector.id && session.status === 'running'
                const ConnectorIcon = connectorIcons[connector.id] ?? Cable
                return (
                  <article className="connector-card" key={connector.id}>
                    <div className="connector-top">
                      <span className={`connector-logo connector-${connector.id}`}><ConnectorIcon size={20} /></span>
                      <span className={`connector-status ${connector.status}`}>
                        {connector.status === 'ready' ? <CheckCircle2 size={15} /> : connector.status === 'needs_setup' ? <Wrench size={15} /> : <CircleDashed size={15} />}
                        {connector.status === 'ready' ? '可用' : connector.status === 'needs_setup' ? '需要配置' : '待接入'}
                      </span>
                    </div>
                    <h3>{connector.name}</h3>
                    <p>{descriptions[connector.id]}</p>
                    <div className="connector-detail">{connector.detail}</div>

                    {cookiePlatform && running && (
                      <div className="cookie-bar">
                        <span className="cookie-state running">
                          <LoaderCircle className="spin" size={13} />
                          {session.message}
                        </span>
                        <div className="cookie-actions">
                          <button
                            className="secondary-button"
                            type="button"
                            onClick={() => void cancelBrowserLogin(connector.id)}
                          >
                            取消
                          </button>
                        </div>
                      </div>
                    )}

                    {cookiePlatform && !running && editing === connector.id && (
                      <div className="cookie-editor">
                        <label className="sr-only" htmlFor={`cookie-${connector.id}`}>访问凭据（Cookie）内容</label>
                        <textarea
                          id={`cookie-${connector.id}`}
                          value={draft}
                          rows={4}
                          spellCheck={false}
                          placeholder={`${cookieSamples[connector.id] ?? 'name=value; name2=value2'}（也可直接粘贴导出的 cookies.txt 内容）`}
                          onChange={(event) => setDraft(event.target.value)}
                        />
                        <ol className="cookie-steps">
                          <li>浏览器打开并登录{connector.name}网页版</li>
                          <li>按 F12 打开开发者工具，切到 Network（网络）</li>
                          <li>刷新页面，点第一条请求，在 Request Headers 里复制 Cookie 整行</li>
                        </ol>
                        <div className="cookie-actions">
                          <button
                            className="primary-button"
                            type="button"
                            disabled={working || !draft.trim()}
                            onClick={() => void saveCookie(connector.id)}
                          >
                            {working ? <LoaderCircle className="spin" size={15} /> : <KeyRound size={15} />}
                            保存访问凭据
                          </button>
                          <button
                            className="secondary-button"
                            type="button"
                            onClick={() => { setEditing(''); setDraft('') }}
                          >
                            取消
                          </button>
                        </div>
                      </div>
                    )}

                    {cookiePlatform && !running && editing !== connector.id && (
                      <>
                        <div className="cookie-bar">
                          <span className={credential?.configured ? 'cookie-state ready' : 'cookie-state'}>
                            {cookieStateText(credential)}
                          </span>
                          <div className="cookie-actions">
                            <button
                              className={credential?.configured ? 'secondary-button' : 'primary-button'}
                              type="button"
                              disabled={working}
                              onClick={() => void startBrowserLogin(connector.id, 'guest')}
                            >
                              {working ? <LoaderCircle className="spin" size={15} /> : <KeyRound size={15} />}
                              {credential?.configured ? '重新获取访问权限' : '一键获取访问权限'}
                            </button>
                          </div>
                        </div>
                        <details className="connector-more">
                          <summary>其他授权方式</summary>
                          <div className="cookie-links">
                            <button
                              className="secondary-button"
                              type="button"
                              disabled={working}
                              title="只有登录后才能观看的内容，用浏览器登录一次即可"
                              onClick={() => void startBrowserLogin(connector.id, 'login')}
                            >
                              <LogIn size={14} />
                              浏览器登录
                            </button>
                            <button
                              className="secondary-button"
                              type="button"
                              title="自动获取失败或本机没有浏览器时，直接粘贴 Cookie 文本"
                              onClick={() => { setEditing(connector.id); setDraft(''); setNotice('') }}
                            >
                              <ClipboardPaste size={14} />
                              手动粘贴访问凭据
                            </button>
                            {credential?.configured && (
                              <button
                                className="secondary-button danger"
                                type="button"
                                disabled={working}
                                onClick={() => void clearCookie(connector.id)}
                              >
                                {working ? <LoaderCircle className="spin" size={15} /> : <Trash2 size={15} />}
                                清除访问凭据
                              </button>
                            )}
                          </div>
                        </details>
                      </>
                    )}

                    {!cookiePlatform && (
                      <button
                        className="secondary-button full"
                        type="button"
                        disabled={connector.status === 'planned' || refreshing}
                        onClick={refresh}
                      >
                        {refreshing && <LoaderCircle className="spin" size={15} />}
                        {buttonLabel(connector, refreshing)}
                      </button>
                    )}
                  </article>
                )
              })}
            </div>
          </section>
        ))
      )}
    </div>
  )
}

function cookieStateText(credential?: CredentialStatus) {
  if (!credential) return ''
  if (credential.configured) return `${credential.entries} 条访问凭据 · 仅保存在本机`
  // 可选平台（B站）不配置也能用，要说明是可选的，别让人以为缺了配置；
  // 「配了能多拿到什么」写在卡片的状态详情里，这里保持一行放得下。
  return credential.required ? '还没有配置访问凭据' : '未配置（可选）'
}

function buttonLabel(connector: Connector, refreshing: boolean) {
  if (connector.status === 'planned') return '暂未开放'
  return refreshing ? '刷新中' : '刷新状态'
}
