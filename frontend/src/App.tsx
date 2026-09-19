import { HashRouter, Route, Routes } from 'react-router-dom'

import { AppShell } from './components/AppShell'
import { ConnectorsPage } from './pages/ConnectorsPage'
import { EditorPage } from './pages/EditorPage'
import { JobsPage } from './pages/JobsPage'
import { LibraryPage } from './pages/LibraryPage'
import { ModelsPage } from './pages/ModelsPage'
import { SettingsPage } from './pages/SettingsPage'
import { WorkbenchPage } from './pages/WorkbenchPage'

export default function App() {
  return (
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
  )
}

