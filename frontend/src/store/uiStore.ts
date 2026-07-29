/** Ephemeral UI state: toasts, the mobile sidebar, and the search palette. */

import { create } from 'zustand'

export interface Toast {
  id: number
  message: string
  tone: 'ok' | 'bad'
}

interface UiState {
  toasts: Toast[]
  sidebarOpen: boolean
  pushToast: (message: string, tone?: Toast['tone']) => void
  dismissToast: (id: number) => void
  setSidebarOpen: (open: boolean) => void
}

let nextToastId = 1

export const useUiStore = create<UiState>((set) => ({
  toasts: [],
  sidebarOpen: false,
  pushToast: (message, tone = 'ok') => {
    const id = nextToastId++
    set((state) => ({ toasts: [...state.toasts, { id, message, tone }] }))
    // Self-dismiss. A toast is an acknowledgement, not a log; if the user needs
    // to act on it, it should have been an inline error instead.
    setTimeout(() => {
      set((state) => ({ toasts: state.toasts.filter((t) => t.id !== id) }))
    }, 3200)
  },
  dismissToast: (id) =>
    set((state) => ({ toasts: state.toasts.filter((t) => t.id !== id) })),
  setSidebarOpen: (sidebarOpen) => set({ sidebarOpen }),
}))
