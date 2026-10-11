import type { ValidatedExcerptItem, RegulatoryStatus } from '../types/query';

interface EvidenceCardProps {
  excerpt: ValidatedExcerptItem;
  index: number;
  isHistorical?: boolean;
}

const STATUS_BADGE_CONFIG: Record<RegulatoryStatus, { label: string; bg: string; text: string; border: string; desc: string }> = {
  ACTIVE_CURRENT: {
    label: 'ACTIVE / CURRENT',
    bg: 'bg-emerald-950/80',
    text: 'text-emerald-400',
    border: 'border-emerald-800/60',
    desc: 'Supported by full baseline in pilot corpus; no superseding edge found.',
  },
  PARTIALLY_AMENDED: {
    label: 'PARTIALLY AMENDED',
    bg: 'bg-amber-950/80',
    text: 'text-amber-400',
    border: 'border-amber-800/60',
    desc: 'A specific provision or clause has been amended by a subsequent circular.',
  },
  SUPERSEDED: {
    label: 'SUPERSEDED (HISTORICAL)',
    bg: 'bg-rose-950/80',
    text: 'text-rose-400',
    border: 'border-rose-800/60',
    desc: 'Fully superseded or rescinded by a newer circular in the corpus.',
  },
  INFORMATIONAL: {
    label: 'INFORMATIONAL',
    bg: 'bg-blue-950/80',
    text: 'text-blue-400',
    border: 'border-blue-800/60',
    desc: 'Background or reference passage without operative mandate changes.',
  },
  UNRESOLVED: {
    label: 'UNRESOLVED PILOT STATUS',
    bg: 'bg-purple-950/80',
    text: 'text-purple-300',
    border: 'border-purple-800/60',
    desc: 'Corpus text verified; active legal currency not established beyond 5-doc pilot.',
  },
};

export const EvidenceCard = ({ excerpt, index, isHistorical = false }: EvidenceCardProps) => {
  const statusCfg = STATUS_BADGE_CONFIG[excerpt.regulatory_status] || {
    label: excerpt.regulatory_status || 'UNKNOWN',
    bg: 'bg-slate-900',
    text: 'text-slate-300',
    border: 'border-slate-700',
    desc: 'Status not recognized in registry.',
  };

  const pageDisplay =
    excerpt.page_start === excerpt.page_end
      ? `Page ${excerpt.page_start}`
      : `Pages ${excerpt.page_start}–${excerpt.page_end}`;

  return (
    <div
      className={`rounded-xl border p-4 sm:p-5 transition-all ${
        isHistorical
          ? 'bg-slate-950/60 border-slate-800/80 hover:border-slate-700'
          : 'bg-slate-900/90 border-slate-800 hover:border-slate-700 shadow-md'
      }`}
    >
      {/* Excerpt Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2 pb-3 border-b border-slate-800/80">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-xs font-mono px-2 py-0.5 rounded bg-slate-800 text-slate-300 font-semibold">
            #{index + 1}
          </span>
          {excerpt.circular_number ? (
            <span className="text-xs font-mono font-bold text-cyan-300 bg-cyan-950/60 px-2 py-0.5 rounded border border-cyan-800/50">
              {excerpt.circular_number}
            </span>
          ) : (
            <span className="text-xs font-mono text-slate-400">
              Doc ID: {excerpt.document_id}
            </span>
          )}

          {excerpt.issue_date && (
            <span className="text-xs text-slate-400 font-mono">
              📅 {excerpt.issue_date}
            </span>
          )}

          <span className="text-xs text-slate-400 font-medium">
            📍 {pageDisplay}
          </span>
        </div>

        {/* Regulatory Status Badge */}
        <div className="flex items-center gap-1.5 self-start sm:self-auto">
          <span
            className={`inline-flex items-center px-2.5 py-1 rounded-md text-[11px] font-semibold border ${statusCfg.bg} ${statusCfg.text} ${statusCfg.border}`}
            title={statusCfg.desc}
          >
            {statusCfg.label}
          </span>
        </div>
      </div>

      {/* Official Title if present */}
      {excerpt.title && (
        <div className="mt-2.5">
          <h4 className="text-xs sm:text-sm font-medium text-slate-200 line-clamp-2">
            {excerpt.title}
          </h4>
        </div>
      )}

      {/* Verbatim Excerpt Text */}
      <div className="mt-3 bg-slate-950/90 rounded-lg p-3.5 border border-slate-800/90">
        <div className="text-[11px] uppercase tracking-wider text-slate-500 font-semibold mb-1 flex items-center justify-between">
          <span>Verbatim Excerpt Text (Verified Mechanical Match)</span>
          <span className="font-mono text-[10px] text-slate-600">{excerpt.source_chunk_id}</span>
        </div>
        <p className="text-xs sm:text-sm text-slate-200 font-mono leading-relaxed whitespace-pre-wrap select-text">
          {excerpt.excerpt_text}
        </p>
      </div>

      {/* Status Reason & Acting Documents Provenance */}
      <div className="mt-3 pt-2.5 border-t border-slate-800/60 flex flex-col gap-1.5 text-xs text-slate-400">
        <div className="flex items-start gap-1.5">
          <span className="text-slate-500 font-semibold shrink-0">Rationale:</span>
          <span className="text-slate-300">{excerpt.status_reason}</span>
        </div>

        {excerpt.acting_document_ids && excerpt.acting_document_ids.length > 0 && (
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="text-slate-500 font-semibold">Acting Document(s):</span>
            {excerpt.acting_document_ids.map((docId) => (
              <span key={docId} className="font-mono text-[11px] bg-slate-800 text-amber-300 px-1.5 py-0.5 rounded">
                {docId}
              </span>
            ))}
          </div>
        )}

        {excerpt.applicable_edge_ids && excerpt.applicable_edge_ids.length > 0 && (
          <div className="flex items-center gap-1.5 flex-wrap text-[11px]">
            <span className="text-slate-500 font-semibold">Graph Edge(s):</span>
            {excerpt.applicable_edge_ids.map((edgeId) => (
              <span key={edgeId} className="font-mono text-slate-400 bg-slate-950 px-1.5 py-0.5 rounded border border-slate-800">
                {edgeId}
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
};
