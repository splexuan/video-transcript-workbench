import { ModelManager } from '../components/ModelManager'

export function ModelsPage() {
  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1>模型管理</h1>
          <p>下载、校验和删除本机识别模型。模型、音频与文案都只保存在这台电脑上。</p>
        </div>
      </header>

      <ModelManager />
    </div>
  )
}
