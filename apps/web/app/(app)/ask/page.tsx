'use client';

import { useQuery } from '@tanstack/react-query';
import { useEffect, useRef } from 'react';

import { Answer } from '@/components/ask/answer';
import { RefinementChips } from '@/components/ask/refinement-chips';
import { STEP_ORDER, StepCard } from '@/components/ask/step-card';
import { useAnalysis } from '@/components/ask/use-analysis';
import { AiBadge, Badge, Card, EmptyState, ErrorState, Spinner } from '@/components/ui/primitives';
import { api } from '@/lib/api-client';
import { queryKeys } from '@/lib/query-keys';
import { useUiStore } from '@/stores/ui';

export default function AskPage() {
  const { state, ask, cancel, reset } = useAnalysis();
  const draft = useUiStore((store) => store.composerDraft);
  const setDraft = useUiStore((store) => store.setComposerDraft);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const streamEndRef = useRef<HTMLDivElement>(null);

  const capabilities = useQuery({
    queryKey: queryKeys.capabilities,
    queryFn: api.capabilities,
  });

  useEffect(() => {
    streamEndRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }, [state.steps.length, state.phase]);

  const streaming = state.phase === 'streaming';

  function submit(question: string) {
    const trimmed = question.trim();
    if (trimmed.length < 2 || streaming) return;
    setDraft(trimmed);
    void ask(trimmed);
    inputRef.current?.blur();
  }

  /** A follow-up refines the plan that produced the current answer. */
  function refine(chipKey: string, chipLabel: string) {
    if (streaming || !state.result?.run_id) return;
    setDraft(chipLabel);
    void ask(chipLabel, {
      refineFromRunId: state.result.run_id,
      refinement: chipKey,
      useCache: true,
    });
  }

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Ask Ledger</h1>
          <p className="mt-1 text-sm text-ink-muted">
            Ask about your spending. Every step of the analysis is shown and can be opened.
          </p>
        </div>
        {capabilities.data ? <AiBadge aiEnabled={capabilities.data.ai_enabled} /> : null}
      </header>

      {capabilities.data ? (
        <div className="rounded-lg border border-line bg-surface-sunken px-4 py-3">
          <p className="text-xs leading-relaxed text-ink-muted">
            <strong className="font-medium text-ink">How answers are produced.</strong>{' '}
            {capabilities.data.disclosure}
          </p>
        </div>
      ) : null}

      <Card className="p-4">
        <form
          onSubmit={(event) => {
            event.preventDefault();
            submit(draft);
          }}
        >
          <label htmlFor="question" className="sr-only">
            Your question
          </label>
          <textarea
            id="question"
            ref={inputRef}
            rows={2}
            value={draft}
            disabled={streaming}
            placeholder="How much did I spend on groceries last month compared to the month before?"
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                submit(draft);
              }
            }}
            className="input resize-none"
          />

          <div className="mt-3 flex flex-wrap items-center gap-2">
            <button type="submit" disabled={streaming || draft.trim().length < 2} className="btn-primary">
              {streaming ? <Spinner /> : null}
              {streaming ? 'Analysing…' : 'Ask'}
            </button>

            {streaming ? (
              <button type="button" onClick={cancel} className="btn-secondary">
                Cancel
              </button>
            ) : null}

            {state.phase !== 'idle' && !streaming ? (
              <button type="button" onClick={reset} className="btn-ghost">
                Clear
              </button>
            ) : null}

            <span className="ml-auto text-xs text-ink-faint">Enter to send · Shift+Enter for a new line</span>
          </div>
        </form>

        {state.phase === 'idle' && capabilities.data ? (
          <div className="mt-4 border-t border-line pt-4">
            <p className="mb-2 text-xs font-medium uppercase tracking-wide text-ink-muted">
              Try one of these
            </p>
            <div className="flex flex-wrap gap-2">
              {capabilities.data.suggested_questions.map((question) => (
                <button
                  key={question}
                  type="button"
                  onClick={() => submit(question)}
                  className="rounded-full border border-line px-3 py-1.5 text-xs text-ink-muted transition-colors hover:border-brand hover:bg-brand-soft hover:text-brand"
                >
                  {question}
                </button>
              ))}
            </div>
          </div>
        ) : null}
      </Card>

      {state.phase === 'idle' ? (
        <Card>
          <EmptyState
            title="No question asked yet"
            description="Ledger reads your question, selects the matching transactions, runs a SQL aggregation, builds a chart, and explains the result — showing its work at every step."
          />
        </Card>
      ) : null}

      {state.phase !== 'idle' ? (
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,340px)_minmax(0,1fr)]">
          <section aria-label="Analysis steps">
            <Card className="p-4">
              <div className="mb-3 flex items-center justify-between">
                <h2 className="text-sm font-semibold text-ink">Analysis process</h2>
                {state.cached ? <Badge tone="neutral">Cached</Badge> : null}
              </div>

              <ol className="space-y-2">
                {state.steps.map((step) => (
                  <StepCard
                    key={step.step}
                    step={step}
                    runId={state.runId ?? 'run'}
                    isActive={streaming}
                  />
                ))}

                {/* Steps not yet reached, so the user sees the whole shape upfront. */}
                {streaming
                  ? STEP_ORDER.filter(
                      (name) => !state.steps.some((step) => step.step === name),
                    ).map((name) => (
                      <li
                        key={name}
                        className="rounded-lg border border-dashed border-line px-3.5 py-2.5"
                      >
                        <span className="text-xs text-ink-faint">{name}</span>
                      </li>
                    ))
                  : null}
              </ol>
              <div ref={streamEndRef} />

              {state.phase === 'cancelled' ? (
                <p className="mt-3 rounded-lg border border-line bg-surface-sunken px-3 py-2 text-xs text-ink-muted">
                  Analysis cancelled. Partial steps above are kept for reference.
                </p>
              ) : null}
            </Card>
          </section>

          <section aria-label="Answer">
            {state.phase === 'error' ? (
              <Card>
                <ErrorState
                  title="The analysis failed"
                  message={state.error ?? 'Please try again.'}
                  onRetry={() => submit(state.question)}
                />
              </Card>
            ) : state.phase === 'cancelled' && !state.result ? (
              <Card>
                <EmptyState
                  title="Cancelled"
                  description="You stopped this analysis before it finished."
                  action={
                    <button type="button" onClick={() => submit(state.question)} className="btn-secondary">
                      Run it again
                    </button>
                  }
                />
              </Card>
            ) : state.result ? (
              <div className="space-y-4">
                <Answer
                  result={state.result}
                  aiEnabled={capabilities.data?.ai_enabled ?? false}
                />
                {state.result.refinements?.length ? (
                  <Card className="p-4">
                    <RefinementChips
                      chips={state.result.refinements}
                      disabled={streaming}
                      onRefine={(chip) => refine(chip.key, chip.label)}
                    />
                  </Card>
                ) : null}
              </div>
            ) : (
              <Card>
                <div className="flex items-center gap-3 px-6 py-14">
                  <Spinner className="text-brand" />
                  <p className="text-sm text-ink-muted">Working through the analysis…</p>
                </div>
              </Card>
            )}
          </section>
        </div>
      ) : null}
    </div>
  );
}
