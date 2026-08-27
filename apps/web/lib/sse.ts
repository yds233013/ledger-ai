/**
 * Minimal SSE-over-fetch reader.
 *
 * EventSource cannot send a POST body or an Authorization header, and the
 * usual workarounds (token in the query string, a pre-created run id) trade
 * away security or simplicity. Reading the stream manually costs ~50 lines,
 * keeps the token in a header, and gives us AbortController cancellation.
 */

export interface SseFrame {
  event: string;
  data: unknown;
}

export interface StreamOptions {
  url: string;
  body: unknown;
  token: string;
  signal?: AbortSignal;
  onFrame: (frame: SseFrame) => void;
}

/** Split a raw SSE chunk buffer into complete frames. */
export function parseFrames(buffer: string): { frames: SseFrame[]; rest: string } {
  const frames: SseFrame[] = [];
  const blocks = buffer.split('\n\n');
  // The final element is either empty or a partial frame; keep it buffered.
  const rest = blocks.pop() ?? '';

  for (const block of blocks) {
    let event = 'message';
    const dataLines: string[] = [];

    for (const line of block.split('\n')) {
      if (line.startsWith('event:')) {
        event = line.slice(6).trim();
      } else if (line.startsWith('data:')) {
        dataLines.push(line.slice(5).trimStart());
      }
    }

    if (dataLines.length === 0) continue;
    const raw = dataLines.join('\n');
    try {
      frames.push({ event, data: JSON.parse(raw) });
    } catch {
      frames.push({ event, data: raw });
    }
  }

  return { frames, rest };
}

export async function streamSse({
  url,
  body,
  token,
  signal,
  onFrame,
}: StreamOptions): Promise<void> {
  const response = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
      Accept: 'text/event-stream',
    },
    body: JSON.stringify(body),
    signal,
  });

  if (!response.ok || !response.body) {
    throw new Error(
      response.status === 401
        ? 'Your session has expired. Please sign in again.'
        : `The analysis request failed (${response.status}).`,
    );
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const { frames, rest } = parseFrames(buffer);
      buffer = rest;
      for (const frame of frames) onFrame(frame);
    }

    // Flush any trailing frame that arrived without a blank-line terminator.
    const { frames } = parseFrames(`${buffer}\n\n`);
    for (const frame of frames) onFrame(frame);
  } finally {
    reader.releaseLock();
  }
}
