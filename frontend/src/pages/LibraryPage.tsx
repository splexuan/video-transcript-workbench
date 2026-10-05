import { ArrowDown, Download, FileText, Search, Trash2, Users, X } from 'lucide-react'
import { useDeferredValue, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { DocumentPreviewModal } from '../components/DocumentPreviewModal'
import { EmptyState } from '../components/EmptyState'
import { LoadingState } from '../components/LoadingState'
import { LoadMore } from '../components/LoadMore'
import { Modal, ModalHeader } from '../components/Modal'
import { PlatformBadge } from '../components/PlatformBadge'
import { Thumb } from '../components/Thumb'
import { api } from '../lib/api'
import { useConfirm } from '../lib/useConfirm'
import { platformLabels } from '../lib/labels'
import type { AuthorSummary, DocumentSummary } from '../types'

function dateLabel(value: string) {
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit',
  }).format(new Date(value))
}

/** 每页条数：和表格一屏能看下的量对齐，翻页时不会一次性渲染太多行。 */
const PAGE_SIZE = 20

/** 一次拉多少位作者。作者数是内容量级的零头，封顶后按名字搜比翻页更省事。 */
const AUTHOR_PAGE_SIZE = 100

/**
 * 平台下拉的选项与徽章共用同一份对照表，别各写一套——两处不同步时，
 * 某个平台的文案就会在库里筛不出来（视频号曾漏在硬编码的列表外）。
 * 排除「待识别」：那是解析未完成的中间态，不是可筛的平台。
 */
const platformOptions = Object.entries(platformLabels).filter(([value]) => value !== 'unknown')

export function LibraryPage() {
  const [query, setQuery] = useState('')
  const deferredQuery = useDeferredValue(query)
  const [platformFilter, setPlatformFilter] = useState('all')
  // 排序与作者筛选同样交给服务端：按字数排全库和排当前页是两回事
  const [sort, setSort] = useState('updated')
  // 选中的作者；空串表示不按作者筛
  const [uploader, setUploader] = useState('')
  const [documents, setDocuments] = useState<DocumentSummary[]>([])
  // 下一页游标；为 null 表示已经到底
  const [cursor, setCursor] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  // 点行先弹窗预览，要改内容再进编辑页
  const [previewId, setPreviewId] = useState('')
  // 正在删除的文案：按钮禁用，避免重复点击
  const [removingId, setRemovingId] = useState('')
  // 勾选导出：勾了才按勾选取；一篇都没勾时还是「导出当前筛选的全部」
  const [selected, setSelected] = useState<Set<string>>(new Set())
  // 「按作者浏览」面板：只在打开时去拉作者聚合，平时不额外打接口
  const [authorPanel, setAuthorPanel] = useState(false)
  const [authorQuery, setAuthorQuery] = useState('')
  const deferredAuthorQuery = useDeferredValue(authorQuery)
  const [authors, setAuthors] = useState<AuthorSummary[]>([])
  const [authorTotal, setAuthorTotal] = useState(0)
  const [authorError, setAuthorError] = useState('')

  // 搜索、平台、作者、排序都交给服务端。分页之后前端只有一页数据，再本地过滤就变成
  // 「只筛当前这一页」，用户会以为库里只有这几条。
  //
  // 列表与批量导出共用这一份条件：各写一套的话会出现「列表看着筛过了、导出来却是全部」。
  const filters = {
    q: deferredQuery || undefined,
    platform: platformFilter === 'all' ? undefined : platformFilter,
    uploader: uploader || undefined,
    sort,
  }
  const filterParams = { ...filters, limit: PAGE_SIZE }

  useEffect(() => {
    let active = true
    api.documents(filterParams)
      .then((page) => {
        if (!active) return
        setDocuments(page.items)
        setCursor(page.next_cursor)
        setError('')
        // 勾选只属于「当前这个列表视图」：换了筛选或排序就清掉，
        // 否则会导出那几篇「已经看不见、但还记在勾选里」的文案
        setSelected(new Set())
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
    // filterParams 每次渲染都是新对象，所以按字段列依赖
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deferredQuery, platformFilter, uploader, sort])

  useEffect(() => {
    if (!authorPanel) return
    let active = true
    api.documentAuthors({ q: deferredAuthorQuery.trim() || undefined, limit: AUTHOR_PAGE_SIZE })
      .then((page) => {
        if (!active) return
        setAuthors(page.items)
        setAuthorTotal(page.total)
        setAuthorError('')
      })
      .catch((reason: Error) => {
        if (active) setAuthorError(reason.message || '读取作者失败')
      })
    return () => {
      active = false
    }
  }, [authorPanel, deferredAuthorQuery])

  // 破坏性操作统一走自绘确认弹窗：Esc、点遮罩、焦点陷阱都由 Modal 负责
  const confirm = useConfirm()

  /** 选中一位作者：收起面板，列表只留他的文案。 */
  function pickAuthor(name: string) {
    setUploader(name)
    setAuthorPanel(false)
    setLoading(true)
    setError('')
  }

  /** 工具栏下拉与列头点排序改的是同一件事，收在一个函数里 */
  function applySort(next: string) {
    if (next === sort) return
    setSort(next)
    setLoading(true)
    setError('')
  }

  /** 勾选/取消一篇。必须换一个 Set 实例，原地改不会被 React 认出来。 */
  function toggleSelected(id: string, checked: boolean) {
    setSelected((current) => {
      const next = new Set(current)
      if (checked) next.add(id)
      else next.delete(id)
      return next
    })
  }

  /**
   * 批量导出：勾了就只导勾选的，一篇没勾就导当前筛选的全部。
   * 交给浏览器直接下载（与预览弹窗里的单篇导出同一套做法）。
   */
  function exportBundle() {
    const anchor = window.document.createElement('a')
    anchor.href = api.exportDocumentsUrl(
      selected.size > 0 ? { ids: [...selected], format: 'txt' } : { ...filters, format: 'txt' },
    )
    anchor.download = ''
    anchor.click()
  }

  async function loadMore() {
    if (!cursor || loadingMore) return
    setLoadingMore(true)
    setError('')
    try {
      const page = await api.documents({ ...filterParams, cursor })
      // 追加而不是替换；翻页期间被编辑过的文案会浮回第一页，再次出现在后续页时按 id 去重
      setDocuments((current) => {
        const seen = new Set(current.map((item) => item.id))
        return [...current, ...page.items.filter((item) => !seen.has(item.id))]
      })
      setCursor(page.next_cursor)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '加载更多失败，请稍后再试')
    } finally {
      setLoadingMore(false)
    }
  }

  async function remove(id: string, title: string) {
    // 自绘确认弹窗能把「删的是哪一条」写进标题，原生 confirm 只能给一句通用文案
    const confirmed = await confirm({
      title: `删除《${title}》？`,
      description: '这条文案的分段、封面与关联的任务记录会一并删除，无法恢复。',
      confirmLabel: '删除',
      tone: 'danger',
    })
    if (!confirmed) return
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

  return (
    <div className="page">
      <header className="page-header">
        <div><h1>文案库</h1><p>提取过的文案都会保存在这里。</p></div>
      </header>
      <div className="toolbar">
        <label className="search-box">
          <Search size={17} />
          <span className="sr-only">搜索文案标题、正文、作者或作品介绍</span>
          <input value={query} onChange={(event) => { setQuery(event.target.value); setLoading(true); setError('') }} placeholder="搜索标题、正文或作者" />
        </label>
        <div className="toolbar-filters">
          <label className="filter-select">
            <span className="sr-only">按平台筛选</span>
            <select value={platformFilter} onChange={(event) => { setPlatformFilter(event.target.value); setLoading(true); setError('') }}>
              <option value="all">全部平台</option>
              {platformOptions.map(([value, label]) => (
                <option value={value} key={value}>{label}</option>
              ))}
            </select>
          </label>
          <label className="filter-select">
            <span className="sr-only">排序方式</span>
            <select value={sort} onChange={(event) => applySort(event.target.value)}>
              <option value="updated">最近更新</option>
              <option value="created">最近提取</option>
              <option value="words">字数最多</option>
            </select>
          </label>
          {/* 排序与列头是同一个状态，工具栏这里保留整体入口，列头再给一个就近入口 */}
          <button
            className="secondary-button"
            type="button"
            aria-haspopup="dialog"
            aria-expanded={authorPanel}
            onClick={() => setAuthorPanel((open) => !open)}
          >
            <Users size={16} /> 按作者浏览
          </button>
          {uploader && (
            <button
              className="filter-chip"
              type="button"
              title="清掉作者筛选"
              onClick={() => { setUploader(''); setLoading(true); setError('') }}
            >
              作者：{uploader}
              <X size={14} />
            </button>
          )}
          <button
            className="secondary-button"
            type="button"
            disabled={documents.length === 0}
            title={
              selected.size > 0
                ? `只导出勾选的 ${selected.size} 篇；清空勾选可改为导出当前筛选的全部`
                : '把当前筛选结果全部导出成一个压缩包，每篇一个 txt 文件'
            }
            onClick={exportBundle}
          >
            <Download size={16} />
            {/* 直接写在按钮上：导出范围是「勾选」还是「当前筛选」这件事，
                原来只在悬停提示里说明，用户点之前看不到 */}
            {selected.size > 0 ? `导出选中 ${selected.size} 篇` : '导出筛选结果'}
          </button>
        </div>
      </div>
      {error && <p className="form-message error" role="alert">{error}</p>}
      <section className="panel library-panel">
        {loading ? (
          <LoadingState label="正在读取文案…" rows={5} />
        ) : documents.length === 0 ? (
          <EmptyState
            icon={FileText}
            title={query ? '没有找到相关文案' : uploader ? '这位作者下还没有文案' : platformFilter !== 'all' ? '这个平台下还没有文案' : '文案库还是空的'}
            description={query ? '换个关键词再试一次。' : uploader ? '换个作者，或点上面的「作者」标签清掉筛选。' : platformFilter !== 'all' ? '换个平台，或把筛选切回「全部平台」。' : '从工作台提交一个链接或导入本地文件，提取完成后会自动归档。'}
            action={!query && !uploader && platformFilter === 'all' ? <Link className="primary-button inline" to="/">开始提取</Link> : undefined}
          />
        ) : <>
          <div className="library-table" role="table" aria-label="文案列表">
            <div className="library-head" role="row">
              <span className="pick-cell" role="columnheader">
                <input
                  type="checkbox"
                  aria-label={`全选当前已加载的 ${documents.length} 篇`}
                  title={`全选已加载的 ${documents.length} 篇；想导出当前筛选的全部，清空勾选即可`}
                  checked={documents.length > 0 && selected.size === documents.length}
                  // 半选不是 HTML 属性，只能设到 DOM 上
                  ref={(element) => {
                    if (element) element.indeterminate = selected.size > 0 && selected.size < documents.length
                  }}
                  onChange={(event) => setSelected(event.target.checked ? new Set(documents.map((item) => item.id)) : new Set())}
                />
              </span>
              <span role="columnheader">标题</span>
              {/* 「来源」与「平台」原来是两列，每列只放一个小标签，合成一列后
                  表格最低宽度少 110px，1200px 档的屏幕不用再横向滚动 */}
              <span role="columnheader">来源</span>
              <span role="columnheader">作者</span>
              {/* 列头就近点就能排序，不用再把手移到右上角的下拉里 */}
              <span role="columnheader" aria-sort={sort === 'words' ? 'descending' : 'none'}>
                <button
                  className={sort === 'words' ? 'sort-button active' : 'sort-button'}
                  type="button"
                  title="按字数从多到少排列"
                  onClick={() => applySort('words')}
                >
                  字数 {sort === 'words' && <ArrowDown size={12} aria-hidden="true" />}
                </button>
              </span>
              <span role="columnheader" aria-sort={sort === 'updated' ? 'descending' : 'none'}>
                <button
                  className={sort === 'updated' ? 'sort-button active' : 'sort-button'}
                  type="button"
                  title="按最近更新排列"
                  onClick={() => applySort('updated')}
                >
                  最近更新 {sort === 'updated' && <ArrowDown size={12} aria-hidden="true" />}
                </button>
              </span>
              <span className="sr-only" role="columnheader">操作</span>
            </div>
            {documents.map((document) => (
              <Link
                className={selected.has(document.id) ? 'library-row selected' : 'library-row'}
                to={`/documents/${document.id}`}
                state={{ from: '/library', label: '文案库' }}
                key={document.id}
                role="row"
                onClick={(event) => { event.preventDefault(); setPreviewId(document.id) }}
              >
                {/* 整行是链接，勾选框要把点击拦住，否则点勾选会顺带打开预览 */}
                <span className="pick-cell" role="cell">
                  <input
                    type="checkbox"
                    aria-label={`选择《${document.title}》`}
                    checked={selected.has(document.id)}
                    onClick={(event) => event.stopPropagation()}
                    onChange={(event) => toggleSelected(document.id, event.target.checked)}
                  />
                </span>
                <span className="title-cell" role="cell">
                  <Thumb src={document.has_cover ? api.coverUrl(document.id) : null} />
                  <strong title={document.title}>{document.title}</strong>
                </span>
                {/* 来源：平台徽标 + 单条/批量。批量用主色、单条弱化，
                    整页每行都挂标签才不会太吵 */}
                <span className="source-cell" role="cell">
                  <PlatformBadge platform={document.platform} />
                  <span
                    className={`origin-badge ${document.source_kind}`}
                    title={document.source_kind === 'batch' ? '来自批量提取' : '来自单条提取'}
                  >
                    {document.source_kind === 'batch' ? '批量' : '单条'}
                  </span>
                </span>
                {/* 整行是链接，作者要拦下冒泡，否则点作者会先跳进编辑页 */}
                <span className="author-cell" role="cell">
                  {document.uploader ? (
                    <button
                      className="author-link"
                      type="button"
                      title={`只看「${document.uploader}」的文案`}
                      onClick={(event) => { event.preventDefault(); event.stopPropagation(); pickAuthor(document.uploader as string) }}
                    >
                      {document.uploader}
                    </button>
                  ) : '—'}
                </span>
                <span className="word-cell" role="cell"><span className="mobile-only">字数 </span>{document.word_count}</span>
                <span className="date-cell" role="cell">{dateLabel(document.updated_at)}</span>
                {/* 整行是链接，删除按钮要拦下冒泡，避免点删除跳进编辑页 */}
                <span className="row-actions" role="cell">
                  <button
                    className="icon-button"
                    type="button"
                    onClick={(event) => { event.preventDefault(); event.stopPropagation(); void remove(document.id, document.title) }}
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
          {cursor && <LoadMore label="加载更多" loading={loadingMore} onLoad={() => void loadMore()} />}
        </>}
      </section>
      {/* 按作者浏览做成弹窗：作者可能有上百位，铺在页面里会把下面的文案列表整个顶下去 */}
      {authorPanel && (
        <Modal ariaLabel="按作者浏览" className="author-picker" onClose={() => setAuthorPanel(false)}>
          <ModalHeader
            icon={Users}
            title="按作者浏览"
            onClose={() => setAuthorPanel(false)}
            closeLabel="关闭作者列表"
          />
          <div className="modal-body">
            <div className="author-picker-head">
              <label className="search-box">
                <Search size={17} />
                <span className="sr-only">搜索作者</span>
                {/* 焦点陷阱会优先聚焦带 data-autofocus 的元素，
                    打开弹窗时光标直接落在搜索框里 */}
                <input
                  data-autofocus
                  value={authorQuery}
                  onChange={(event) => setAuthorQuery(event.target.value)}
                  placeholder="搜索作者名"
                />
              </label>
              <small>
                共 {authorTotal} 位作者
                {authorTotal > authors.length ? `，按篇数显示前 ${authors.length} 位` : ''}
              </small>
            </div>
            {authorError && <p className="form-message error" role="alert">{authorError}</p>}
            {authors.length === 0 ? (
              <p className="author-empty">
                {authorQuery.trim() ? '没有匹配的作者，换个名字试试。' : '还没有带作者信息的文案。'}
              </p>
            ) : (
              <div className="author-grid">
                {authors.map((author) => (
                  <button className="author-card" type="button" key={author.name} onClick={() => pickAuthor(author.name)}>
                    <strong title={author.name}>{author.name}</strong>
                    <span>{author.count} 篇 · {author.total_words} 字</span>
                    <span>最近 {dateLabel(author.latest_at)}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
        </Modal>
      )}
      {previewId && (
        <DocumentPreviewModal key={previewId} documentId={previewId} backTo="/library" backLabel="文案库" onClose={() => setPreviewId('')} />
      )}
    </div>
  )
}
