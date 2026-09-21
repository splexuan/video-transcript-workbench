/**
 * 界面文案映射。
 *
 * 后端存的是稳定的枚举值（`accurate`、`transcribing`…），界面上必须翻译成中文，
 * 不能把内部枚举直接显示给用户。
 */

export const jobStatusLabels: Record<string, string> = {
  queued: '等待中',
  running: '处理中',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
}

export const modeLabels: Record<string, string> = {
  auto: '自动',
  fast: '极速文本',
  accurate: '精准时间轴',
}

/**
 * 平台与显示名的唯一对照表：徽章、文案库的筛选下拉都从这里取。
 * 分成两份时容易漏平台（视频号就曾在硬编码的下拉里漏掉，导致筛不出来）。
 */
export const platformLabels: Record<string, string> = {
  bilibili: 'B站',
  douyin: '抖音',
  kuaishou: '快手',
  xiaohongshu: '小红书',
  wechat: '视频号',
  local: '本地文件',
  unknown: '待识别',
}

export const stageLabels: Record<string, string> = {
  waiting: '排队中',
  resolving: '读取来源',
  fetching_subtitle: '获取字幕',
  downloading: '下载媒体',
  transcribing: '识别语音',
  writing: '整理文案',
  done: '完成',
}

export function labelOf(map: Record<string, string>, value: string, fallback = '未知') {
  return map[value] ?? fallback
}

/** 模型名去掉括号里的补充说明（如「极速文本」「推荐」），用于空间紧张的位置。 */
export function shortModelName(name: string) {
  return name.replace(/（[^）]*）/g, '').trim() || name
}
