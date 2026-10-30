import type { CollectionSummary } from '../types/query';

interface DisclosureBannerProps {
  selectedCollection?: CollectionSummary | null;
}

export const DisclosureBanner = ({ selectedCollection }: DisclosureBannerProps) => {
  const isGeneralStatic = selectedCollection?.policy_type === 'general_static';

  return (
    <div className="w-full bg-slate-900/60 border border-slate-800/80 rounded-xl p-4 text-xs text-slate-300 leading-relaxed space-y-1.5">
      <div className="flex items-center gap-2 font-semibold text-cyan-400">
        <svg className="w-4 h-4 shrink-0 text-cyan-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
        </svg>
        <span>
          {isGeneralStatic
            ? `Documentation Disclosure — ${selectedCollection?.name || 'General Documentation'}`
            : 'Authoritative Scope & Non-Legal-Advice Disclosure'}
        </span>
      </div>
      {isGeneralStatic ? (
        <>
          <p className="text-slate-400">
            This collection operates over static organizational and technical documentation (<code className="text-cyan-300 font-mono text-[11px]">{selectedCollection?.collection_id}</code>).
            Excerpts are mechanically validated for verbatim textual provenance against registered document snapshots.
          </p>
          <p className="text-slate-400 text-[11px]">
            General technical documentation does not represent regulatory circulars or official legal advice. Temporal regulatory supersession and graph relationship checks do not apply.
          </p>
        </>
      ) : (
        <>
          <p className="text-slate-400">
            This prototype retrieves and mechanically validates verbatim excerpts strictly against the indexed <strong>SEBI Cybersecurity & Cyber Resilience Framework (CSCRF)</strong> circular collection (<code className="text-cyan-300 font-mono text-[11px]">CIR/2024/113</code>, <code className="text-cyan-300 font-mono text-[11px]">CIR/2025/045</code>, <code className="text-cyan-300 font-mono text-[11px]">CIR/2025/060</code>, <code className="text-cyan-300 font-mono text-[11px]">CIR/2025/096</code>, and <code className="text-cyan-300 font-mono text-[11px]">CIR/2025/119</code>).
          </p>
          <p className="text-slate-400 text-[11px]">
            Retrieved excerpts reflect technical document grounding only and do not independently establish universal legal validity, complete regulatory compliance, or binding legal advice.
          </p>
        </>
      )}
    </div>
  );
};
