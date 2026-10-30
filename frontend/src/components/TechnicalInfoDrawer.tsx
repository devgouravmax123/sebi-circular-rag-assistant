import { useState } from 'react';
import type { CollectionSummary } from '../types/query';

interface TechnicalInfoDrawerProps {
  selectedCollection?: CollectionSummary | null;
  initialOpen?: boolean;
}

export const TechnicalInfoDrawer = ({ selectedCollection, initialOpen = false }: TechnicalInfoDrawerProps) => {
  const [isOpen, setIsOpen] = useState(initialOpen);
  const isSebiStrict = selectedCollection?.policy_type === 'sebi_cscrf_strict';

  return (
    <div className="w-full bg-slate-900/60 border border-slate-800 rounded-xl overflow-hidden text-xs text-slate-300">
      <button
        type="button"
        onClick={() => setIsOpen(!isOpen)}
        className="w-full px-4 py-3 flex items-center justify-between text-left hover:bg-slate-800/40 transition cursor-pointer"
        aria-expanded={isOpen}
      >
        <span className="font-semibold text-slate-300 flex items-center gap-2">
          <svg
            className={`w-4 h-4 text-cyan-400 transition-transform ${isOpen ? 'rotate-90' : ''}`}
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
          >
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
          </svg>
          <span>What Mechanical Technical Validation Does and Does Not Establish</span>
        </span>
        <span className="text-[11px] text-cyan-400/90 font-mono">
          {isOpen ? 'Collapse' : 'Learn more'}
        </span>
      </button>

      {isOpen && (
        <div className="px-4 pb-4 pt-2 border-t border-slate-800/80 space-y-3 bg-slate-950/40 leading-relaxed">
          <div>
            <h5 className="font-semibold text-emerald-400 mb-1">
              ✓ What Technical Validation Guarantees:
            </h5>
            <ul className="list-disc list-inside space-y-1 text-slate-400 pl-1">
              <li>
                <strong>Verbatim Precision:</strong>{' '}
                {isSebiStrict
                  ? '100% mechanical character substring match against the indexed SEBI CSCRF corpus chunks.'
                  : '100% mechanical character substring match against registered collection document chunks.'}
              </li>
              <li>
                <strong>Retrieval-Bound Safety:</strong> Excerpts originate strictly from authorized server-side hybrid retrieval candidates.
              </li>
              <li>
                <strong>{isSebiStrict ? 'Deterministic Graph Annotation:' : 'Deterministic Provenance Tracking:'}</strong>{' '}
                {isSebiStrict
                  ? 'Cross-references verified supersession, partial amendment, and timeline extension edges.'
                  : 'Enforces exact document snapshot binding and page citation validation (graph supersession checks do not apply to static general documents).'}
              </li>
            </ul>
          </div>

          <div>
            <h5 className="font-semibold text-amber-400 mb-1">
              ⚠ What Technical Validation Does Not Guarantee:
            </h5>
            <ul className="list-disc list-inside space-y-1 text-slate-400 pl-1">
              <li>
                <strong>{isSebiStrict ? 'Legal Currency:' : 'Advisory Status:'}</strong>{' '}
                {isSebiStrict
                  ? 'Does not constitute binding legal counsel or universal currency beyond the 5-document pilot scope.'
                  : 'Document excerpts provide technical and operational information only; they do not establish regulatory compliance or legal advice.'}
              </li>
              <li>
                <strong>{isSebiStrict ? 'Exhaustive Compliance:' : 'Collection Boundary:'}</strong>{' '}
                {isSebiStrict
                  ? 'Subsequent gazette notifications, master circulars, or bilateral SEBI directions outside the pilot are not indexed.'
                  : 'External standards, unindexed manuals, and organization policies outside the active collection snapshot are not covered.'}
              </li>
              <li>
                <strong>Subjective Interpretation:</strong> The system does not synthesize generative legal opinions or waive statutory obligations.
              </li>
            </ul>
          </div>
        </div>
      )}
    </div>
  );
};
