import { MoreHorizontal, type LucideIcon } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

export interface MenuItem {
  key: string
  label: string
  icon?: LucideIcon
  danger?: boolean
  disabled?: boolean
  onSelect: () => void
}

/**
 * 行内/卡片内的「更多操作」菜单。低频且有破坏性的操作（取消、删除）收进菜单，
 * 主位只留最常按的那一个按钮，卡片上的按钮组从 4 个降到 2 个。
 */
export function DropdownMenu({
  items,
  label = '更多操作',
  align = 'end',
}: {
  items: MenuItem[]
  label?: string
  align?: 'start' | 'end'
}) {
  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    function onPointerDown(event: MouseEvent) {
      if (!wrapRef.current?.contains(event.target as Node)) setOpen(false)
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('mousedown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  const enabled = items.filter((item) => !item.disabled)
  if (enabled.length === 0) return null

  return (
    <div className="dropdown" ref={wrapRef}>
      <button
        className="icon-button"
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={label}
        title={label}
        onClick={() => setOpen((current) => !current)}
      >
        <MoreHorizontal size={17} />
      </button>
      {open && (
        <div className={`dropdown-menu ${align}`} role="menu" aria-label={label}>
          {items.map((item) => {
            const Icon = item.icon
            return (
              <button
                key={item.key}
                className={`dropdown-item${item.danger ? ' danger' : ''}`}
                type="button"
                role="menuitem"
                disabled={item.disabled}
                onClick={() => {
                  setOpen(false)
                  item.onSelect()
                }}
              >
                {Icon && <Icon size={15} aria-hidden="true" />}
                {item.label}
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}
