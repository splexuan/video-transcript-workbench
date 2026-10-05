import { createContext, useContext } from 'react'

export interface ConfirmOptions {
  title: string
  description?: string
  confirmLabel?: string
  cancelLabel?: string
  /** 删除、覆盖这类不可逆操作走 danger，主按钮变红 */
  tone?: 'default' | 'danger'
}

export type ConfirmFn = (options: ConfirmOptions) => Promise<boolean>

/**
 * 确认弹窗的 context 与读取入口。
 *
 * 实现（ConfirmProvider）放在 components/ConfirmProvider.tsx：
 * 组件文件只导出组件，React Fast Refresh 才能正常工作。
 */
export const ConfirmContext = createContext<ConfirmFn>(() => Promise.resolve(false))

export function useConfirm(): ConfirmFn {
  return useContext(ConfirmContext)
}
