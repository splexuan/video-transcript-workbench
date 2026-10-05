import { RotateCcw } from 'lucide-react'
import { Component, type ErrorInfo, type ReactNode } from 'react'

interface Props {
  children: ReactNode
}

interface State {
  error: Error | null
}

/**
 * 全局错误边界：之前任何一处渲染期异常都会让整个界面变成白屏，
 * 用户只能去日志目录里找线索。这里至少给出「刷新 / 回工作台」两条出路。
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('界面渲染出错：', error, info.componentStack)
  }

  render() {
    const { error } = this.state
    if (!error) return this.props.children
    return (
      <div className="page crash-page" role="alert">
        <h1>界面出错了</h1>
        <p>
          这一步没能画出来，你的文案、模型和平台凭据都还在本机，没有受影响。
          可以先重试；如果反复出现，日志在数据目录的 <code>logs/app.log</code>。
        </p>
        <p className="crash-detail">{error.message}</p>
        <div className="error-actions">
          <button className="secondary-button" type="button" onClick={() => window.location.reload()}>
            <RotateCcw size={15} /> 重新加载界面
          </button>
          <a className="primary-button" href="#/">
            回到工作台
          </a>
        </div>
      </div>
    )
  }
}
