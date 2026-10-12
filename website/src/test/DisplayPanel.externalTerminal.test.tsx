import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'

/**
 * Settings → Display → Terminal: the "External terminal app" picker.
 *
 * Desktop app only, against a gateway on this machine, and it lists only the
 * allowlisted apps the desktop shell finds installed. Unset by default (None);
 * a pick is saved to dashboard.terminal.external_app, which the gateway's
 * launch gate reads.
 */

const { patchConfigMock, kirocrewConfigMock } = vi.hoisted(() => ({
  patchConfigMock: vi.fn((_path: string, _value: unknown) => Promise.resolve({})),
  kirocrewConfigMock: vi.fn(() => Promise.resolve({})),
}))

vi.mock('../api/client', () => {
  class ApiError extends Error {
    status: number
    body: string
    constructor(status: number, message: string, body = '') {
      super(message)
      this.name = 'ApiError'
      this.status = status
      this.body = body
    }
  }
  return {
    api: {
      kirocrewConfig: kirocrewConfigMock,
      patchConfig: patchConfigMock,
      installTheme: vi.fn(() => Promise.resolve({ ok: true })),
    },
    ApiError,
  }
})

const brandingEnv = vi.hoisted(() => ({ directLocal: true }))
vi.mock('../hooks/useBranding', () => ({
  useBranding: () => ({ botName: 'Test', avatar: '', directLocal: brandingEnv.directLocal }),
}))

const zoomCtx = {
  zoom: 100,
  zoomSupported: true,
  zoomIn: vi.fn(),
  zoomOut: vi.fn(),
  reset: vi.fn(),
  family: 'sans',
  setFontFamily: vi.fn(),
  cycleFamily: vi.fn(),
}
vi.mock('../hooks/ZoomProvider', () => ({
  useZoomCtx: () => zoomCtx,
}))

vi.mock('../hooks/useTheme', () => ({
  useTheme: () => ({
    preference: 'dark',
    setTheme: vi.fn(),
    colorTheme: 'default',
    setColorTheme: vi.fn(),
    allThemes: [{ value: 'default', label: 'Default', custom: false }],
    theme: 'dark',
    themeVersion: 0,
    themeSwitching: false,
    addCustomTheme: vi.fn(),
    deleteCustomTheme: vi.fn(),
    loadCustomThemes: vi.fn(),
  }),
  ThemeProvider: ({ children }: { children: React.ReactNode }) => children,
  CUSTOM_THEMES_CHANGED_EVENT: 'custom-themes-changed',
}))

vi.mock('../hooks/useUIMode', () => ({
  useUIMode: () => ({
    uiMode: 'chat',
    setUIMode: vi.fn(),
    toggleUIMode: vi.fn(),
  }),
  UIModeProvider: ({ children }: { children: React.ReactNode }) => children,
}))

vi.mock('../hooks/useSessionPalette', () => ({
  useSessionPalette: () => ({
    paletteColors: ['#ff0000', '#00ff00', '#0000ff'],
    colorMode: 'tint' as const,
    paletteName: 'trailhead',
    intensity: 'clear',
    boost: {
      activePct: [60, 60, 60],
      idlePct: [30, 30, 30],
    },
  }),
}))

import { DisplayPanel } from '../pages/settings/DisplayPanel'

const KEY = 'dashboard.terminal.external_app'
const LABEL = 'External terminal app'
const ROUTE = '/settings?tab=display&sub=terminal'

let detect: ReturnType<typeof vi.fn>

function installBridge(apps: Array<{ id: string; label: string }>) {
  detect = vi.fn().mockResolvedValue({ apps })
  ;(window as { externalTerminalAPI?: unknown }).externalTerminalAPI = { detect, open: vi.fn() }
}

function seed(app: unknown) {
  kirocrewConfigMock.mockImplementation(() =>
    Promise.resolve({ dashboard: { terminal: app === undefined ? {} : { external_app: app } } }),
  )
}

const picker = () => screen.findByRole('combobox', { name: LABEL })

describe('DisplayPanel → External terminal app', () => {
  beforeEach(() => {
    patchConfigMock.mockReset()
    patchConfigMock.mockImplementation(() => Promise.resolve({}))
    brandingEnv.directLocal = true
    seed(undefined)
    delete (window as { externalTerminalAPI?: unknown }).externalTerminalAPI
  })

  afterEach(() => {
    delete (window as { externalTerminalAPI?: unknown }).externalTerminalAPI
  })

  it('reads as None when nothing is picked (the default)', async () => {
    installBridge([{ id: 'kitty', label: 'kitty' }])
    renderWithProviders(<DisplayPanel />, { route: ROUTE })
    await waitFor(async () => expect(await picker()).toHaveTextContent('None'))
  })

  it('lists only the apps the desktop shell detected, after None', async () => {
    installBridge([{ id: 'kitty', label: 'kitty' }, { id: 'wezterm', label: 'WezTerm' }])
    renderWithProviders(<DisplayPanel />, { route: ROUTE })
    const trigger = await picker()
    await waitFor(() => expect(trigger).not.toBeDisabled())
    fireEvent.click(trigger)
    await waitFor(() => expect(screen.getByRole('option', { name: 'WezTerm' })).toBeInTheDocument())
    expect(screen.getAllByRole('option').map(o => o.textContent)).toEqual(['None', 'kitty', 'WezTerm'])
  })

  it('saves the picked app id', async () => {
    installBridge([{ id: 'kitty', label: 'kitty' }])
    renderWithProviders(<DisplayPanel />, { route: ROUTE })
    const trigger = await picker()
    await waitFor(() => expect(trigger).not.toBeDisabled())
    fireEvent.click(trigger)
    fireEvent.click(await screen.findByRole('option', { name: 'kitty' }))
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(KEY, 'kitty'))
  })

  it('shows a stored pick that is no longer installed as None', async () => {
    installBridge([{ id: 'kitty', label: 'kitty' }])
    seed('ghostty')
    renderWithProviders(<DisplayPanel />, { route: ROUTE })
    await waitFor(async () => expect(await picker()).toHaveTextContent('None'))
  })

  it('says so when no supported app is installed', async () => {
    installBridge([])
    renderWithProviders(<DisplayPanel />, { route: ROUTE })
    expect(await screen.findByText('No supported terminal app was found on this computer.')).toBeInTheDocument()
    await waitFor(async () => expect(await picker()).toBeDisabled())
  })

  it('reports a failed detection instead of claiming nothing is installed', async () => {
    installBridge([])
    detect.mockRejectedValue(new Error('external-terminal:detect is restricted to the local dashboard'))
    renderWithProviders(<DisplayPanel />, { route: ROUTE })
    expect(await screen.findByText("Couldn't check which terminal apps are installed on this computer.")).toBeInTheDocument()
    expect(screen.queryByText('No supported terminal app was found on this computer.')).toBeNull()
  })

  it('is absent in the browser build, where there is no launch bridge', async () => {
    renderWithProviders(<DisplayPanel />, { route: ROUTE })
    await screen.findByRole('switch', { name: 'Reuse the current terminal' })
    expect(screen.queryByRole('combobox', { name: LABEL })).toBeNull()
  })

  it('is absent for a gateway that is not on this machine', async () => {
    installBridge([{ id: 'kitty', label: 'kitty' }])
    brandingEnv.directLocal = false
    renderWithProviders(<DisplayPanel />, { route: ROUTE })
    await screen.findByRole('switch', { name: 'Reuse the current terminal' })
    expect(screen.queryByRole('combobox', { name: LABEL })).toBeNull()
    expect(detect).not.toHaveBeenCalled()
  })
})
