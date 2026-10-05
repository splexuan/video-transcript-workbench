import { HashRouter, Route, Routes } from 'react-router-dom'

import { AppShell } from './components/AppShell'
import { ErrorBoundary } from './components/ErrorBoundary'
import { ConfirmProvider } from './components/ConfirmProvider'
import { ConnectorsPage } from './pages/ConnectorsPage'
import { EditorPage } from './pages/EditorPage'
import { JobsPage } from './pages/JobsPage'
import { LibraryPage } from './pages/LibraryPage'
import { ModelsPage } from './pages/ModelsPage'
import { SettingsPage } from './pages/SettingsPage'
import { WorkbenchPage } from './pages/WorkbenchPage'

export default function App() {
  return (
    // 错误边界与确认弹窗都放在路由之外：编辑页不在 AppShell 里，
    // 但它同样需要确认弹窗，也需要在崩掉时有人兜住。
    <ErrorBoundary>
      <ConfirmProvider>
        <HashRouter>
          <Routes>
            <Route element={<AppShell />}>
              <Route index element={<WorkbenchPage />} />
              <Route path="library" element={<LibraryPage />} />
              <Route path="jobs" element={<JobsPage />} />
              <Route path="models" element={<ModelsPage />} />
              <Route path="connectors" element={<ConnectorsPage />} />
              <Route path="settings" element={<SettingsPage />} />
            </Route>
            <Route path="documents/:id" element={<EditorPage />} />
          </Routes>
        </HashRouter>
      </ConfirmProvider>
    </ErrorBoundary>
  )
}
