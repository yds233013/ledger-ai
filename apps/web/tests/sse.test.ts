import { describe, expect, it } from 'vitest';

import { parseFrames } from '@/lib/sse';

describe('parseFrames', () => {
  it('parses a complete frame', () => {
    const { frames, rest } = parseFrames('event: step\ndata: {"seq":1}\n\n');
    expect(frames).toEqual([{ event: 'step', data: { seq: 1 } }]);
    expect(rest).toBe('');
  });

  it('parses several frames from one chunk', () => {
    const buffer = 'event: run\ndata: {"a":1}\n\nevent: step\ndata: {"b":2}\n\n';
    const { frames } = parseFrames(buffer);
    expect(frames).toHaveLength(2);
    expect(frames[0].event).toBe('run');
    expect(frames[1].data).toEqual({ b: 2 });
  });

  it('buffers a partial frame instead of dropping it', () => {
    // This is the case that matters: a network chunk can split mid-frame.
    const { frames, rest } = parseFrames('event: step\ndata: {"seq":1}\n\nevent: result\ndata: {"par');
    expect(frames).toHaveLength(1);
    expect(rest).toBe('event: result\ndata: {"par');
  });

  it('reassembles a frame split across two chunks', () => {
    const first = parseFrames('event: result\ndata: {"tot');
    expect(first.frames).toHaveLength(0);

    const second = parseFrames(`${first.rest}al":42}\n\n`);
    expect(second.frames[0].data).toEqual({ total: 42 });
  });

  it('falls back to the raw string when data is not JSON', () => {
    const { frames } = parseFrames('event: ping\ndata: hello\n\n');
    expect(frames[0].data).toBe('hello');
  });

  it('ignores frames with no data line', () => {
    const { frames } = parseFrames('event: comment\n\n');
    expect(frames).toHaveLength(0);
  });
});
