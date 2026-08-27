import { DemoBanner } from '@/components/layout/demo-banner';
import { Disclaimer } from '@/components/layout/disclaimer';
import { Header } from '@/components/layout/header';

export default function AppLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-screen flex-col">
      <Header />
      <DemoBanner />
      <main className="mx-auto w-full max-w-7xl flex-1 px-4 py-8 sm:px-6">{children}</main>
      <Disclaimer />
    </div>
  );
}
