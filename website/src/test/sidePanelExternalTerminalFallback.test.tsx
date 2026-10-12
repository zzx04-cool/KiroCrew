import { useEffect } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { createTestStore } from './helpers'

/**
 * "Open in <app>" falling back to the integrated terminal brings that terminal
 * tab to the front, which takes the Files tab out of view. The note saying
 * why must survive that switch, so the side panel holds it, not the Files tab.
 */

vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DiffPanel', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../components/ArtifactPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/FolderPanel', () => ({ default: () => null }))
vi.mock('../components/WebPreviewPanel', () => ({ default: () => null }))
vi.mock('../components/McpAppFrame', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../components/CliPanel', () => ({
  default: () => <div data-testid="cli-panel" />,
  disposeTerminalSession: vi.fn(),
  useDeleteTerminalSession: () => ({ mutate: vi.fn() }),
}))
vi.mock('../utils/terminalRegistry', () => ({
  useTerminalEnabled: () => true,
  useTerminalTitle: () => 'Terminal',
}))
vi.mock('../hooks/useDevMode', () => ({ useDevMode: () => false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))
vi.mock('../hooks/useBranding', () => ({
  useBranding: () => ({ botName: 'Test', avatar: '', directLocal: true }),
}))
vi.mock('../pierre/tree', () => ({
  TreeSkeleton: () => null,
  PierreWorkspaceTree: () => <div data-testid="file-browser-tree" />,
}))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as never

import SidePanel from '../pages/chat/SidePanel'
import { api } from '../api/client'
import { __resetPanelTabs, usePanelTabs } from '../hooks/usePanelTabs'

function Harness() {
  const tabsCtl = usePanelTabs('slot-a')
  const openView = tabsCtl.openView
  useEffect(() => { openView('files') }, [openView])
  return (
    <SidePanel
      tabsCtl={tabsCtl}
      slot="slot-a"
      projectDir="/repo"
      onFileOpen={vi.fn()}
      onFileSave={async () => {}}
      onClose={() => {}}
    />
  )
}

function renderPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <Provider store={createTestStore()}>
        <Harness />
      </Provider>
    </QueryClientProvider>,
  )
}

describe('side panel: the external terminal fallback note', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetPanelTabs()
    vi.spyOn(api, 'projectTree').mockResolvedValue({ root: '/repo', paths: [], repo: true })
    vi.spyOn(api, 'projectGitStatus').mockResolvedValue({ repo: true, files: [] })
    vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({ dashboard: { terminal: { external_app: 'kitty' } } } as never)
    ;(window as { externalTerminalAPI?: unknown }).externalTerminalAPI = {
      detect: vi.fn().mockResolvedValue({ apps: [{ id: 'kitty', label: 'kitty' }] }),
      open: vi.fn().mockResolvedValue({ ok: false, error: 'launch-failed' }),
    }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ grant: 'g'.repeat(43), app: 'kitty' }),
    }))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
    delete (window as { externalTerminalAPI?: unknown }).externalTerminalAPI
  })

  it('stays on screen after the integrated terminal takes the Files tab out of view', async () => {
    renderPanel()
    const trigger = await screen.findByLabelText('Project actions')
    fireEvent.keyDown(trigger, { key: 'Enter' })
    fireEvent.click(await screen.findByText('Open in kitty'))

    const note = await screen.findByTestId('side-panel-external-terminal-fallback')
    expect(note).toHaveTextContent("kitty didn't start, so the terminal opened in a new tab here instead.")
    await waitFor(() => expect(screen.getByTestId('cli-panel')).toBeInTheDocument())
    expect(screen.queryByLabelText('Project actions')).toBeNull()

    fireEvent.click(within(note).getByRole('button', { name: /dismiss/i }))
    expect(screen.queryByTestId('side-panel-external-terminal-fallback')).toBeNull()
  })
})
