import { Film, FileText, Search, Trash2 } from 'lucide-react'
import { useDeferredValue, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { DocumentPreviewModal } from '../components/DocumentPreviewModal'
import { EmptyState } from '../components/EmptyState'
import { LoadingState } from '../components/LoadingState'
import { PlatformBadge } from '../components/PlatformBadge'
import { api } from '../lib/api'
import type { DocumentSummary } from '../types'

function dateLabel(value: string) {
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit',
  }).format(new Date(value))
}

export function LibraryPage() {
  const [query, setQuery] = useState('')
  const deferredQuery = useDeferredValue(query)
  const [documents, setDocuments] = useState<DocumentSummary[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  // 点行先弹窗预览，要改内容再进编辑页
  const [previewId, setPreviewId] = useState('')
  const [platformFilter, setPlatformFilter] = useState('all')
  // 正在删除的文案：按钮禁用，避免重复点击
  const [removingId, setRemovingId] = useState('')

  useEffect(() => {
    let active = true
    api.documents(deferredQuery)
      .then((items) => {
        if (active) setDocuments(items)
      })
      .catch((reason: Error) => {
        if (active) setError(reason.message || '读取文案失败')
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => {
      active = false
    }
  }, [deferredQuery])

  async function remove(id: string) {
    if (!window.confirm('删除这条文案？分段、封面和关联的任务记录会一并删除，无法恢复。')) return
    setRemovingId(id)
    setError('')
    try {
      await api.deleteDocument(id)
      setDocuments((current) => current.filter((item) => item.id !== id))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '删除失败，请稍后再试')
    } finally {
      setRemovingId('')
    }
  }

  // 平台筛选在前端做：文案库一次拉全量，切换筛选不需要再请求
  const visible = platformFilter === 'all'
    ? documents
    : documents.filter((item) => item.platform === platformFilter)

  return (
    <div className="page">
      <header className="page-header">
        <div><h1>文案库</h1><p>提取过的文案都会保存在这里。</p></div>
      </header>
      <div className="toolbar">
        <label className="search-box">
          <Search size={17} />
          <span className="sr-only">搜索文案标题或正文</span>
          <input value={query} onChange={(event) => { setQuery(event.target.value); setLoading(true); setError('') }} placeholder="搜索标题或正文" />
        </label>
        <label className="filter-select">
          <span className="sr-only">按平台筛选</span>
          <select value={platformFilter} onChange={(event) => setPlatformFilter(event.target.value)}>
            <option value="all">全部平台</option>
            <option value="bilibili">B站</option>
            <option value="douyin">抖音</option>
            <option value="kuaishou">快手</option>
            <option value="xiaohongshu">小红书</option>
            <option value="local">本地文件</option>
          </select>
        </label>
      </div>
      {error && <p className="form-message error" role="alert">{error}</p>}
      <section className="panel library-panel">
        {loading ? (
          <LoadingState label="正在读取文案…" rows={5} />
        ) : visible.length === 0 ? (
          <EmptyState
            icon={FileText}
            title={documents.length > 0 ? '这个平台下还没有文案' : query ? '没有找到相关文案' : '文案库还是空的'}
            description={documents.length > 0 ? '换个平台，或把筛选切回「全部平台」。' : query ? '换个关键词再试一次。' : '从工作台提交一个链接或导入本地文件，提取完成后会自动归档。'}
            action={!query && documents.length === 0 ? <Link className="primary-button inline" to="/">开始提取</Link> : undefined}
          />
        ) : (
          <div className="library-table" role="table" aria-label="文案列表">
            <div className="library-head" role="row">
              <span>标题</span><span>平台</span><span>字数</span><span>最近更新</span><span className="sr-only">操作</span>
            </div>
            {visible.map((document) => (
              <Link
                className="library-row"
                to={`/documents/${document.id}`}
                state={{ from: '/library', label: '文案库' }}
                key={document.id}
                role="row"
                onClick={(event) => { event.preventDefault(); setPreviewId(document.id) }}
              >
                <span className="title-cell">
                  {/* 封面缩略图；没有封面或加载失败时露出底下的图标占位 */}
                  <span className="library-thumb">
                    <Film size={15} />
                    {document.has_cover && (
                      <img
                        src={api.coverUrl(document.id)}
                        alt=""
                        loading="lazy"
                        onError={(event) => { event.currentTarget.style.display = 'none' }}
                      />
                    )}
                  </span>
                  <strong>{document.title}</strong>
                  {document.status === 'reviewed' && <span className="reviewed-badge" title="已标记为校对完成">已校对</span>}
                </span>
                <span className="platform-cell"><PlatformBadge platform={document.platform} /></span>
                <span className="word-cell"><span className="mobile-only">字数 </span>{document.word_count}</span>
                <span className="date-cell">{dateLabel(document.updated_at)}</span>
                {/* 整行是链接，删除按钮要拦下冒泡，避免点删除跳进编辑页 */}
                <span className="row-actions">
                  <button
                    className="icon-button"
                    type="button"
                    onClick={(event) => { event.preventDefault(); event.stopPropagation(); void remove(document.id) }}
                    disabled={removingId === document.id}
                    aria-label={`删除《${document.title}》`}
                    title="删除"
                  >
                    <Trash2 size={15} />
                  </button>
                </span>
              </Link>
            ))}
          </div>
        )}
      </section>
      {previewId && (
        <DocumentPreviewModal key={previewId} documentId={previewId} backTo="/library" backLabel="文案库" onClose={() => setPreviewId('')} />
      )}
    </div>
  )
}
