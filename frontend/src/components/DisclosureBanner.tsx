import type { CollectionSummary } from '../types/query';

interface DisclosureBannerProps {
  selectedCollection?: CollectionSummary | null;
}

export const DisclosureBanner = ({ selectedCollection }: DisclosureBannerProps) => {
  return (
    <div className="w-full bg-slate-900/60 border border-slate-800/80 rounded-xl p-4 text-xs text-slate-300 leading-relaxed space-y-1.5">
      <div className="flex items-center gap-2 font-semibold text-cyan-400">
        <svg className="w-4 h-4 shrink-0 text-cyan-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
        </svg>
        <span>
          Documentation Disclosure — {selectedCollection?.name || 'Verified Collection'}
        </span>
      </div>
      <p className="text-slate-400">
        This collection operates over verified documents{selectedCollection?.collection_id ? ` (${selectedCollection.collection_id})` : ''}.
        Excerpts are mechanically validated for verbatim textual provenance against registered document snapshots.
      </p>
      <p className="text-slate-400 text-[11px]">
        Answers reflect evidence grounded directly in retrieved source passages and do not constitute formal legal or professional advice.
      </p>
    </div>
  );
};
