import type { QueryResponse, QueryError, AnswerDisposition, SynthesisStatus } from '../types/query';
import { EvidenceCard } from './EvidenceCard';

interface ResultsPanelProps {
  isLoading: boolean;
  error: QueryError | null;
  response: QueryResponse | null;
  onRetry?: () => void;
}

const SYNTHESIS_STATUS_CONFIG: Partial<Record<SynthesisStatus, { label: string; bg: string; text: string; border: string; desc: string }>> = {
  FULLY_ANSWERED: {
    label: 'Fully Supported by Evidence',
    bg: 'bg-emerald-950/80',
    text: 'text-emerald-300',
    border: 'border-emerald-700/60',
    desc: 'The synthesized answer is completely supported by retrieved passages (reflects corpus evidence, not universal legal currency).',
  },
  PARTIALLY_ANSWERED: {
    label: 'Partially Answered (Gaps Disclosed)',
    bg: 'bg-amber-950/80',
    text: 'text-amber-300',
    border: 'border-amber-700/60',
    desc: 'Some parts of the query are addressed by retrieved evidence; remaining gaps are explicitly disclosed.',
  },
  ABSTAINED: {
    label: 'Controlled Abstention',
    bg: 'bg-slate-900',
    text: 'text-slate-300',
    border: 'border-slate-700',
    desc: 'The assistant abstained safely from generating an answer due to insufficient or superseded evidence.',
  },
  PROVIDER_FAILED: {
    label: 'Synthesis Service Unavailable',
    bg: 'bg-rose-950/80',
    text: 'text-rose-300',
    border: 'border-rose-700/60',
    desc: 'Answer synthesis encountered an error or timeout; verified excerpts remain intact.',
  },
  UNSUPPORTED_CLAIMS_REJECTED: {
    label: 'Unsupported Assertions Rejected',
    bg: 'bg-rose-950/80',
    text: 'text-rose-300',
    border: 'border-rose-700/60',
    desc: 'Draft synthesis contained assertions not substantiated by corpus excerpts and was rejected.',
  },
};

const DISPOSITION_CONFIG: Record<AnswerDisposition, { label: string; bg: string; text: string; border: string; description: string }> = {
  VERIFIED_HISTORICAL_EXCERPTS: {
    label: 'Verified Historical Excerpts',
    bg: 'bg-emerald-950/80',
    text: 'text-emerald-300',
    border: 'border-emerald-700/60',
    description: 'All returned excerpts mechanically verified against official SEBI CSCRF corpus with resolved status.',
  },
  QUALIFIED_HISTORICAL_EXCERPTS: {
    label: 'Historical Excerpts — Qualifications Apply',
    bg: 'bg-amber-950/80',
    text: 'text-amber-300',
    border: 'border-amber-700/60',
    description: 'Corpus text verified; contains baseline circular provisions whose legal currency is not fully resolved beyond the 5-doc pilot.',
  },
  CONTROLLED_ABSTENTION: {
    label: 'Unable to Provide a Supported Excerpt',
    bg: 'bg-slate-900',
    text: 'text-slate-300',
    border: 'border-slate-700',
    description: 'The system abstained safely due to lack of supported evidence or full supersession.',
  },
  REJECTED_UNSAFE: {
    label: 'Response Rejected by Safety Checks',
    bg: 'bg-rose-950/80',
    text: 'text-rose-300',
    border: 'border-rose-700/60',
    description: 'Draft payload failed mechanical integrity or candidate binding checks.',
  },
};

export const ResultsPanel = ({
  isLoading,
  error,
  response,
  onRetry,
}: ResultsPanelProps) => {
  if (isLoading) {
    return (
      <div className="w-full bg-slate-900/60 border border-slate-800 rounded-2xl p-10 flex flex-col items-center justify-center text-center space-y-4 shadow-xl">
        <div className="relative">
          <div className="w-12 h-12 rounded-full border-2 border-cyan-500/20 border-t-cyan-400 animate-spin"></div>
        </div>
        <div>
          <h3 className="text-base font-semibold text-white">
            Retrieving & Validating Regulatory Passages
          </h3>
          <p className="text-xs text-slate-400 mt-1 max-w-md">
            Running hybrid retrieval (BM25 + Dense RRF), binding candidate excerpts, and resolving relationship graphs...
          </p>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div
        role="alert"
        aria-live="assertive"
        className="w-full bg-rose-950/40 border border-rose-900/80 rounded-2xl p-6 shadow-xl space-y-3"
      >
        <div className="flex items-center gap-2.5 text-rose-400 font-semibold text-sm">
          <svg className="w-5 h-5 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
          <span>Query Execution Error</span>
        </div>
        <p className="text-sm text-rose-200 font-medium">{error.message}</p>
        {error.detail && error.detail !== error.message && (
          <div className="bg-rose-950/80 rounded-lg p-3 border border-rose-900/50 text-xs font-mono text-rose-300">
            {error.detail}
          </div>
        )}
        {onRetry && (
          <div className="pt-2">
            <button
              onClick={onRetry}
              className="px-4 py-1.5 text-xs font-semibold rounded-lg bg-rose-900/60 hover:bg-rose-800/80 text-rose-100 border border-rose-700/60 transition cursor-pointer"
            >
              Retry Query
            </button>
          </div>
        )}
      </div>
    );
  }

  if (!response) {
    return null;
  }

  const dispCfg = DISPOSITION_CONFIG[response.disposition] || {
    label: response.disposition || 'UNKNOWN DISPOSITION',
    bg: 'bg-slate-900',
    text: 'text-slate-300',
    border: 'border-slate-700',
    description: 'System returned an unmapped disposition code.',
  };

  const hasVerified = response.verified_excerpts && response.verified_excerpts.length > 0;
  const hasHistorical =
    response.historical_superseded_excerpts && response.historical_superseded_excerpts.length > 0;

  return (
    <section aria-label="Query results" className="w-full space-y-6">
      {/* Response Header & Disposition Summary */}
      <div className="bg-slate-900/80 border border-slate-800 rounded-2xl p-5 sm:p-6 shadow-xl backdrop-blur-sm space-y-4">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 pb-4 border-b border-slate-800">
          <div>
            <span className="text-[11px] uppercase tracking-wider text-slate-500 font-semibold block mb-1">
              Safety Gate Disposition
            </span>
            <span
              className={`inline-flex items-center px-3 py-1 rounded-lg text-xs sm:text-sm font-bold border ${dispCfg.bg} ${dispCfg.text} ${dispCfg.border}`}
            >
              {dispCfg.label}
            </span>
          </div>

          <div className="flex items-center gap-3 text-xs text-slate-400 flex-wrap">
            <span className="bg-slate-950 px-2.5 py-1 rounded-lg border border-slate-800">
              Retrieved Candidates:{' '}
              <strong className="text-cyan-400">{response.retrieval_candidate_count}</strong>
            </span>
            <span className="bg-slate-950 px-2.5 py-1 rounded-lg border border-slate-800">
              Verified Excerpts:{' '}
              <strong className="text-emerald-400">{response.verified_excerpts.length}</strong>
            </span>
            {hasHistorical && (
              <span className="bg-slate-950 px-2.5 py-1 rounded-lg border border-slate-800">
                Superseded Excerpts:{' '}
                <strong className="text-amber-400">{response.historical_superseded_excerpts.length}</strong>
              </span>
            )}
          </div>
        </div>

        {/* Server-Provided Summary Narrative */}
        <div className="space-y-1">
          <span className="text-xs uppercase tracking-wider text-slate-400 font-semibold">
            Summary Narrative
          </span>
          <p className="text-sm text-slate-200 leading-relaxed bg-slate-950/60 p-3.5 rounded-xl border border-slate-800/80">
            {response.summary_narrative}
          </p>
        </div>

        {/* Controlled Abstention Reason */}
        {response.disposition === 'CONTROLLED_ABSTENTION' && response.abstention_reason && (
          <div className="bg-amber-950/40 border border-amber-900/60 rounded-xl p-4 text-xs text-amber-300 space-y-1">
            <span className="font-semibold text-amber-400 block">Abstention Reason:</span>
            <p className="text-slate-300">{response.abstention_reason}</p>
            <p className="text-[11px] text-amber-400/80 pt-1">
              Note: Abstention in this pilot system does not mean no relevant SEBI circular exists globally—only that no sufficient verified passage exists within the 5-document indexed pilot corpus.
            </p>
          </div>
        )}

        {/* Regulatory Warnings */}
        {response.regulatory_warnings && response.regulatory_warnings.length > 0 && (
          <div className="bg-amber-950/30 border border-amber-800/50 rounded-xl p-4 space-y-2">
            <span className="text-xs font-semibold text-amber-400 flex items-center gap-1.5">
              <svg className="w-4 h-4 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
              </svg>
              <span>Regulatory Warnings & Caveats ({response.regulatory_warnings.length})</span>
            </span>
            <ul className="list-disc list-inside space-y-1 text-xs text-amber-200/90 pl-1">
              {response.regulatory_warnings.map((warn, idx) => (
                <li key={idx}>{warn}</li>
              ))}
            </ul>
          </div>
        )}

        {/* Pilot Scope limitation footer note */}
        <div className="text-[11px] text-slate-500 pt-2 border-t border-slate-800/60 flex items-center gap-1.5">
          <span className="font-semibold">Corpus Boundary:</span>
          <span>{response.pilot_corpus_scope_limitation}</span>
        </div>
      </div>

      {/* Synthesized Direct Answer Section */}
      {Boolean(response.answer_text && response.answer_text.trim()) && (() => {
        const statusKey = response.synthesis_status as SynthesisStatus | undefined;
        const synthCfg =
          (statusKey && SYNTHESIS_STATUS_CONFIG[statusKey]) || {
            label: response.synthesis_status || 'Answer Generated',
            bg: 'bg-cyan-950/80',
            text: 'text-cyan-300',
            border: 'border-cyan-700/60',
            desc: 'Direct grounded synthesis generated from retrieved evidence.',
          };

        const showFailureReason =
          Boolean(response.synthesis_failure_reason) &&
          (response.synthesis_status === 'PROVIDER_FAILED' ||
            response.synthesis_status === 'UNSUPPORTED_CLAIMS_REJECTED' ||
            response.synthesis_status === 'ABSTAINED');

        const isMockMode =
          Boolean(response.answer_text?.includes('[MOCK SYNTHESIS')) ||
          Boolean(response.synthesis_failure_reason?.includes('Mock offline mode'));

        return (
          <div
            data-testid="direct-answer-section"
            className="bg-slate-900/90 border border-slate-700/80 rounded-2xl p-5 sm:p-6 shadow-xl backdrop-blur-sm space-y-4"
          >
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2.5 pb-3 border-b border-slate-800">
              <div className="flex items-center gap-2">
                <span className="text-xs uppercase tracking-wider text-cyan-400 font-bold flex items-center gap-1.5">
                  <svg className="w-4 h-4 text-cyan-400 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z" />
                  </svg>
                  Direct Grounded Answer
                </span>
                {isMockMode && (
                  <span
                    data-testid="mock-mode-indicator"
                    className="inline-flex items-center px-2 py-0.5 rounded text-[10px] font-mono font-bold uppercase bg-amber-500/10 text-amber-300 border border-amber-500/30"
                  >
                    Mock Test Mode
                  </span>
                )}
              </div>

              {response.synthesis_status && (
                <span
                  data-testid="synthesis-status-badge"
                  className={`inline-flex items-center px-2.5 py-0.5 rounded text-xs font-semibold border ${synthCfg.bg} ${synthCfg.text} ${synthCfg.border}`}
                  title={synthCfg.desc}
                >
                  {synthCfg.label}
                </span>
              )}
            </div>

            {isMockMode && (
              <div
                data-testid="mock-mode-disclaimer"
                className="bg-amber-950/30 border border-amber-900/40 rounded-xl p-3 text-xs text-amber-200/90 space-y-1"
              >
                <div className="flex items-center gap-1.5 font-semibold text-amber-400">
                  <svg className="w-3.5 h-3.5 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                  </svg>
                  <span>Offline Pipeline Test Simulation</span>
                </div>
                <p className="text-[11px] text-slate-300">
                  This response was generated by the deterministic offline test provider. Excerpts are extracted directly from retrieved passages for pipeline validation and do not represent live LLM legal synthesis.
                </p>
              </div>
            )}

            {/* Answer Content */}
            <div className="prose prose-invert max-w-none">
              <p
                data-testid="direct-answer-text"
                className="text-sm sm:text-base text-slate-100 leading-relaxed whitespace-pre-wrap font-sans select-text"
              >
                {response.answer_text}
              </p>
            </div>

            {/* Unresolved Subquestions (Compact View) */}
            {response.unresolved_subquestions && response.unresolved_subquestions.length > 0 && (
              <div
                data-testid="unresolved-subquestions-section"
                className="mt-3 bg-slate-950/70 border border-amber-900/40 rounded-xl p-3.5 space-y-2 text-xs"
              >
                <span className="font-semibold text-amber-400 flex items-center gap-1.5">
                  <svg className="w-3.5 h-3.5 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                  </svg>
                  Unresolved Sub-questions ({response.unresolved_subquestions.length})
                </span>
                <ul className="space-y-1.5 pl-1">
                  {response.unresolved_subquestions.map((subq, idx) => (
                    <li key={idx} className="text-slate-300">
                      <span className="font-medium text-amber-200">{subq.subquestion}</span>
                      <span className="text-slate-400 text-[11px] block sm:inline sm:ml-2">
                        — {subq.reason}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {/* Safe Failure Reason if useful */}
            {showFailureReason && (
              <div
                data-testid="synthesis-failure-reason"
                className="mt-2 bg-rose-950/40 border border-rose-900/50 rounded-lg p-2.5 text-xs text-rose-300"
              >
                <span className="font-semibold text-rose-400">Synthesis Note: </span>
                {response.synthesis_failure_reason}
              </div>
            )}
          </div>
        );
      })()}

      {/* Primary Verified Evidence Section */}
      {hasVerified && (
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-semibold uppercase tracking-wider text-slate-300 flex items-center gap-2">
              <span>Primary Verified Evidence Excerpts</span>
              <span className="text-xs font-mono bg-cyan-950 text-cyan-400 border border-cyan-800/60 px-2 py-0.5 rounded-full">
                {response.verified_excerpts.length}
              </span>
            </h3>
            <span className="text-xs text-slate-500">
              Sorted by retrieval relevance & mechanical verification
            </span>
          </div>

          <div className="space-y-4">
            {response.verified_excerpts.map((excerpt, idx) => (
              <EvidenceCard key={excerpt.excerpt_id || idx} excerpt={excerpt} index={idx} />
            ))}
          </div>
        </div>
      )}

      {/* Separated Historical / Superseded Excerpts Section */}
      {hasHistorical && (
        <div className="space-y-4 pt-4 border-t border-slate-800/80">
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-1">
            <div className="flex items-center gap-2">
              <h3 className="text-sm font-semibold uppercase tracking-wider text-rose-400/90 flex items-center gap-2">
                <span>Separated Historical / Superseded Excerpts</span>
                <span className="text-xs font-mono bg-rose-950 text-rose-400 border border-rose-800/60 px-2 py-0.5 rounded-full">
                  {response.historical_superseded_excerpts.length}
                </span>
              </h3>
            </div>
            <span className="text-xs text-slate-400 font-medium">
              ⚠ Kept separate from active provisions to prevent obsolete compliance guidance
            </span>
          </div>

          <div className="space-y-4">
            {response.historical_superseded_excerpts.map((excerpt, idx) => (
              <EvidenceCard
                key={excerpt.excerpt_id || `hist-${idx}`}
                excerpt={excerpt}
                index={idx}
                isHistorical={true}
              />
            ))}
          </div>
        </div>
      )}
    </section>
  );
};
