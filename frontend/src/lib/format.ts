/** 字节数显示：模型包、更新包都用它，避免每个页面各写一套换算。 */
export function formatBytes(value: number | null | undefined) {
  if (!value || value <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let size = value
  let index = 0
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024
    index += 1
  }
  return `${index === 0 ? Math.round(size) : size.toFixed(1)} ${units[index]}`
}
