import { Film } from 'lucide-react'
import { useState } from 'react'

/**
 * 封面缩略图：图片盖在图标占位之上，没有封面或加载失败（封面被清理、
 * 平台临时地址过期）时自动露出底下的占位，行高与骨架始终稳定。
 *
 * 这段「图标占位 + onError 收起图片」的逻辑原先在三个列表里各写一遍，
 * 处理方式还不一致（有的 `display: none`、有的 `visibility: hidden`，
 * 后者会在行里留一块空洞）。统一到这里，外观交给 `.cover-thumb`。
 */
export function Thumb({
  src,
  className,
  size = 15,
}: {
  src?: string | null
  className?: string
  size?: number
}) {
  const [failed, setFailed] = useState(false)
  return (
    <span className={className ? `cover-thumb ${className}` : 'cover-thumb'}>
      <Film size={size} />
      {src && !failed && <img src={src} alt="" loading="lazy" onError={() => setFailed(true)} />}
    </span>
  )
}
