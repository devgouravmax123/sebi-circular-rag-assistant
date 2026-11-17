import { useState } from 'react';
import type { CollectionSummary } from '../types/query';

interface TechnicalInfoDrawerProps {
  selectedCollection?: CollectionSummary | null;
  initialOpen?: boolean;
}

export const TechnicalInfoDrawer = ({ selectedCollection: _selectedCollection, initialOpen = false }: TechnicalInfoDrawerProps) => {
  const [isOpen, setIsOpen] = useState(initialOpen);

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
                100% mechanical character substring match against registered collection document chunks.
              </li>
              <li>
                <strong>Retrieval-Bound Safety:</strong> Excerpts originate strictly from authorized server-side hybrid retrieval candidates.
              </li>
              <li>
                <strong>Deterministic Provenance Tracking:</strong>{' '}
                Enforces exact document snapshot binding and page citation validation.
              </li>
            </ul>
          </div>

          <div>
            <h5 className="font-semibold text-amber-400 mb-1">
              ⚠ What Technical Validation Does Not Guarantee:
            </h5>
            <ul className="list-disc list-inside space-y-1 text-slate-400 pl-1">
              <li>
                <strong>Advisory Status:</strong>{' '}
                Document excerpts provide technical and operational grounding only; they do not constitute formal legal counsel.
              </li>
              <li>
                <strong>Collection Boundary:</strong>{' '}
                External manuals, unindexed documents, and corporate policies outside the active collection snapshot are not covered.
              </li>
              <li>
                <strong>Subjective Interpretation:</strong> The system does not synthesize ungrounded opinions or hallucinated assertions.
              </li>
            </ul>
          </div>
        </div>
      )}
    </div>
  );
};
