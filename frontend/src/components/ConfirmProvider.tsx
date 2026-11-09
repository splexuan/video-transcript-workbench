import { TriangleAlert } from 'lucide-react'
import { useCallback, useState, type ReactNode } from 'react'

import { Modal } from './Modal'
import { ConfirmContext, type ConfirmFn, type ConfirmOptions } from '../lib/useConfirm'

/**
 * 统一的自绘确认弹窗。
 *
 * 之前 8 处破坏性操作都用 `window.confirm`：浏览器原生弹窗在自绘界面里非常突兀，
 * 也没法把「删的是哪一条」「为什么不可恢复」讲清楚。这里换成 Promise 版本，
 * 调用处从 `if (!window.confirm('…')) return` 变成 `if (!(await confirm({…}))) return`。
 */
export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [request, setRequest] = useState<{ options: ConfirmOptions; resolve: (value: boolean) => void } | null>(null)

  const confirm = useCallback<ConfirmFn>(
    (options) => new Promise<boolean>((resolve) => setRequest({ options, resolve })),
    [],
  )

  // 只在事件回调里调用：这里读到的 request 就是当前渲染出来的那个弹窗，
  // 不必把 resolve 塞进 setState 的更新函数里（那会在更新函数里做副作用）
  function settle(value: boolean) {
    if (!request) return
    setRequest(null)
    request.resolve(value)
  }

  const options = request?.options

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      {request && options && (
        <Modal
          role="alertdialog"
          size="sm"
          layer="top"
          ariaLabel={options.title}
          onClose={() => settle(false)}
          className="confirm-dialog"
        >
          <div className={`confirm-icon${options.tone === 'danger' ? ' danger' : ''}`} aria-hidden="true">
            <TriangleAlert size={20} />
          </div>
          <h2 className="confirm-title">{options.title}</h2>
          {options.description && <p className="confirm-description">{options.description}</p>}
          <div className="confirm-actions">
            <button className="secondary-button" type="button" onClick={() => settle(false)}>
              {options.cancelLabel ?? '取消'}
            </button>
            <button
              className={options.tone === 'danger' ? 'primary-button danger' : 'primary-button'}
              type="button"
              data-autofocus
              onClick={() => settle(true)}
            >
              {options.confirmLabel ?? '确定'}
            </button>
          </div>
        </Modal>
      )}
    </ConfirmContext.Provider>
  )
}
