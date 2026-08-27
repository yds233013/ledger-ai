/**
 * Ephemeral UI state only.
 *
 * TanStack Query owns every piece of server state. This store deliberately
 * holds nothing that exists on the server — duplicating server data into a
 * client store is the classic way this stack pairing goes wrong.
 */
import { create } from 'zustand';

interface UiState {
  filtersOpen: boolean;
  toggleFilters: () => void;
  setFiltersOpen: (open: boolean) => void;

  expandedSteps: Record<string, boolean>;
  toggleStep: (key: string) => void;
  setStepExpanded: (key: string, expanded: boolean) => void;

  composerDraft: string;
  setComposerDraft: (draft: string) => void;
}

export const useUiStore = create<UiState>((set) => ({
  filtersOpen: false,
  toggleFilters: () => set((state) => ({ filtersOpen: !state.filtersOpen })),
  setFiltersOpen: (open) => set({ filtersOpen: open }),

  expandedSteps: {},
  toggleStep: (key) =>
    set((state) => ({
      expandedSteps: { ...state.expandedSteps, [key]: !state.expandedSteps[key] },
    })),
  setStepExpanded: (key, expanded) =>
    set((state) => ({ expandedSteps: { ...state.expandedSteps, [key]: expanded } })),

  composerDraft: '',
  setComposerDraft: (draft) => set({ composerDraft: draft }),
}));
