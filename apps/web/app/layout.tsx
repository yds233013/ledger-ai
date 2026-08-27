import type { Metadata } from 'next';

import './globals.css';
import { Providers } from './providers';

export const metadata: Metadata = {
  title: 'Ledger AI — Personal finance data workspace',
  description:
    'Upload synthetic bank statements, categorize spending, and ask questions with a fully inspectable analysis process.',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen font-sans antialiased">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
