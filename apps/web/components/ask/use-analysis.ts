'use client';

import { useCallback, useRef, useState } from 'react';

import { API_URL, getAccessToken } from '@/lib/api-client';
import { streamSse } from '@/lib/sse';
import type { AnalysisResult, AnalysisStepEvent } from '@/lib/types';

export type AnalysisPhase = 'idle' | 'streaming' | 'complete' | 'error' | 'cancelled';

export interface AnalysisState {
  phase: AnalysisPhase;
  runId: string | null;
  question: string;
  steps: AnalysisStepEvent[];
  result: AnalysisResult | null;
  error: string | null;
  cached: boolean;
}

const INITIAL: AnalysisState = {
  phase: 'idle',
  runId: null,
  question: '',
  steps: [],
  result: null,
  error: null,
  cached: false,
};

/**
 * Drives one analysis stream.
 *
 * Steps arrive as `started` then `completed` for the same step name; we keep a
 * single entry per step and upgrade it in place, so the UI shows five cards
 * that fill in rather than ten that pile up.
 */
export function useAnalysis() {
  const [state, setState] = useState<AnalysisState>(INITIAL);
  const abortRef = useRef<AbortController | null>(null);

  const cancel = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setState((current) =>
      current.phase === 'streaming' ? { ...current, phase: 'cancelled' } : current,
    );
  }, []);

  const reset = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setState(INITIAL);
  }, []);

  const ask = useCallback(
    async (
      question: string,
      options: {
        useCache?: boolean;
        refineFromRunId?: string;
        refinement?: string;
      } = {},
    ) => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    setState({ ...INITIAL, phase: 'streaming', question });

    try {
      const token = await getAccessToken();

      await streamSse({
        url: `${API_URL}/api/analysis/runs`,
        body: {
          question,
          use_cache: options.useCache ?? true,
          refine_from_run_id: options.refineFromRunId,
          refinement: options.refinement,
        },
        token,
        signal: controller.signal,
        onFrame: ({ event, data }) => {
          if (event === 'run') {
            const payload = data as { run_id: string; cached: boolean };
            setState((current) => ({
              ...current,
              runId: payload.run_id,
              cached: payload.cached,
            }));
            return;
          }

          if (event === 'step') {
            const incoming = data as AnalysisStepEvent;
            setState((current) => {
              const existing = current.steps.findIndex((item) => item.step === incoming.step);
              if (existing === -1) return { ...current, steps: [...current.steps, incoming] };
              const steps = [...current.steps];
              steps[existing] = incoming;
              return { ...current, steps };
            });
            return;
          }

          if (event === 'result') {
            const result = data as AnalysisResult;
            setState((current) => ({
              ...current,
              phase: 'complete',
              result,
              cached: result.cached,
            }));
            return;
          }

          if (event === 'error') {
            const payload = data as { message: string };
            setState((current) => ({ ...current, phase: 'error', error: payload.message }));
          }
        },
      });

      // A stream that ends without a terminal frame is still a failure.
      setState((current) =>
        current.phase === 'streaming'
          ? {
              ...current,
              phase: 'error',
              error: 'The analysis ended before returning a result. Please retry.',
            }
          : current,
      );
    } catch (error) {
      if (controller.signal.aborted) {
        setState((current) => ({ ...current, phase: 'cancelled' }));
        return;
      }
      setState((current) => ({
        ...current,
        phase: 'error',
        error: error instanceof Error ? error.message : 'The analysis request failed.',
      }));
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
    }
  },
  [],
  );

  return { state, ask, cancel, reset };
}
