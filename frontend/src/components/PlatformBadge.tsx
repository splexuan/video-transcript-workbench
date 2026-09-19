const labels: Record<string, string> = {
  bilibili: 'B站',
  douyin: '抖音',
  kuaishou: '快手',
  xiaohongshu: '小红书',
  wechat: '视频号',
  local: '本地文件',
  unknown: '待识别',
}

export function PlatformBadge({ platform }: { platform: string }) {
  return <span className={`platform-badge platform-${platform}`}>{labels[platform] ?? platform}</span>
}

