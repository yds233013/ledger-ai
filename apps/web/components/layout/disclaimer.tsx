/**
 * Standing disclosure. Present on every page of the app shell — Ledger AI
 * reports on uploaded data and does not give financial advice.
 */
export function Disclaimer() {
  return (
    <footer className="border-t border-line bg-surface-sunken">
      <div className="mx-auto max-w-7xl px-4 py-5 sm:px-6">
        <p className="text-xs leading-relaxed text-ink-muted">
          <strong className="font-medium text-ink">Demo project.</strong> All data shown is
          synthetic and generated for demonstration. Ledger AI does not connect to any real
          financial institution, is not a financial adviser, and does not provide financial,
          investment or tax advice. Figures are computed from the transactions you upload.
        </p>
      </div>
    </footer>
  );
}
