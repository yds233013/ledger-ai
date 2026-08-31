import type { ConsentState } from './types';

/**
 * What each consent document says, in the words shown next to its checkbox.
 *
 * Three separate documents rather than one bundled "I agree", because they ask
 * for three different things: how the service may be used, what happens to the
 * data, and what the person is promising about the files they send. Bundling
 * them would make it impossible to tell which one somebody actually read.
 *
 * The copy deliberately does not claim any legal review. This is a private
 * beta run by one person, and saying otherwise would be the one thing in a
 * consent flow that must never be untrue.
 */
export const CONSENT_COPY: Record<string, { label: string; summary: string; points: string[] }> = {
  terms: {
    label: 'Terms of use',
    summary: 'What Ledger AI is, and what it is not.',
    points: [
      'Ledger AI is a personal project in private beta, provided as-is and with no guarantee of availability or accuracy.',
      'It reports on the files you upload. It does not give financial, tax or investment advice.',
      'It connects to no bank and holds no credentials for one.',
      'These terms have not been reviewed by a lawyer.',
    ],
  },
  privacy: {
    label: 'Privacy',
    summary: 'What is stored, where, and for how long.',
    points: [
      'Your uploaded files, the transactions read from them, and your account record are stored on servers rented for this project.',
      'Nothing is sold, shared, or used to train a model.',
      'You can export everything this account holds, or delete it, at any time from Settings.',
      'Deleting your account removes your data; backups may take longer to age out.',
    ],
  },
  upload: {
    label: 'What you upload',
    summary: 'The one thing this asks of you.',
    points: [
      'Upload only files you are entitled to upload.',
      'Remove or mask full account numbers, card numbers, Social Security numbers and similar identifiers first — the last four digits are fine.',
      'Ledger AI tries to catch unmasked identifiers and refuses those files, but the check is best-effort and will not catch everything.',
    ],
  },
};

/** The order the documents are presented in, most general first. */
export const CONSENT_ORDER = ['terms', 'privacy', 'upload'] as const;

/** Consent types the account still owes at the current version. */
export function outstanding(state: ConsentState | undefined): string[] {
  return state?.missing ?? [];
}

/**
 * Whether this account has already accepted a *previous* version of a document.
 *
 * Worth distinguishing in the UI: being asked again because the document
 * changed is a different situation from being asked for the first time, and
 * saying which one it is avoids the impression that an earlier acceptance was
 * quietly discarded.
 */
export function isReprompt(state: ConsentState | undefined, consentType: string): boolean {
  if (!state) return false;
  const accepted = state.accepted[consentType];
  return Boolean(accepted) && accepted !== state.required[consentType];
}
