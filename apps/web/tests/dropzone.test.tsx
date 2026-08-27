import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { Dropzone } from '@/components/upload/dropzone';

function makeFile(name: string, sizeBytes: number, type = 'text/csv'): File {
  const file = new File(['x'], name, { type });
  Object.defineProperty(file, 'size', { value: sizeBytes });
  return file;
}

describe('Dropzone', () => {
  it('accepts a valid CSV', async () => {
    const user = userEvent.setup();
    const onFiles = vi.fn();
    const { container } = render(<Dropzone onFiles={onFiles} />);

    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    await user.upload(input, makeFile('statement.csv', 2048));

    expect(onFiles).toHaveBeenCalledTimes(1);
    expect(onFiles.mock.calls[0][0][0].name).toBe('statement.csv');
  });

  it('rejects a file over the 10 MB limit before uploading it', async () => {
    const user = userEvent.setup();
    const onFiles = vi.fn();
    const { container } = render(<Dropzone onFiles={onFiles} />);

    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    await user.upload(input, makeFile('huge.csv', 11 * 1024 * 1024));

    expect(onFiles).not.toHaveBeenCalled();
    expect(screen.getByRole('alert')).toHaveTextContent(/exceeds the 10 MB limit/);
  });

  it('rejects an unsupported file type dropped onto the zone', () => {
    // The file input's accept attribute filters the picker, but a drag-and-drop
    // bypasses it entirely — so that is the path worth testing.
    const onFiles = vi.fn();
    render(<Dropzone onFiles={onFiles} />);

    const file = makeFile('payload.exe', 1024, 'application/exe');
    fireEvent.drop(screen.getByRole('button', { name: /Upload a statement or receipt/ }), {
      dataTransfer: { files: [file], types: ['Files'] },
    });

    expect(onFiles).not.toHaveBeenCalled();
    expect(screen.getByRole('alert')).toHaveTextContent(/Only \.csv statements/);
  });

  it('is keyboard operable', () => {
    render(<Dropzone onFiles={vi.fn()} />);
    expect(screen.getByRole('button', { name: /Upload a statement or receipt/ })).toHaveAttribute(
      'tabindex',
      '0',
    );
  });

  it('is inert while an upload is already in flight', () => {
    render(<Dropzone onFiles={vi.fn()} disabled />);
    const zone = screen.getByRole('button', { name: /Upload a statement or receipt/ });
    expect(zone).toHaveAttribute('aria-disabled', 'true');
    expect(zone).toHaveAttribute('tabindex', '-1');
  });
});
