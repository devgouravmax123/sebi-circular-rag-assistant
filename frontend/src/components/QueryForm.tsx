import { useState, type FormEvent } from 'react';
import type { QueryRequest } from '../types/query';

interface QueryFormProps {
  isLoading: boolean;
  onSubmit: (params: QueryRequest) => void;
  initialQuery?: string;
  initialTopK?: number;
  initialIncludeSuperseded?: boolean;
}

const EXAMPLE_QUERIES = [
  {
    label: 'Cyber Incident Timeline',
    query: 'What is the mandatory reporting timeline for cyber incidents under SEBI CSCRF?',
    top_k: 5,
    include_superseded: false,
  },
  {
    label: 'SOC Operations & CSIRT',
    query: 'What are the technical requirements for setting up a Cyber Security Operations Center (SOC)?',
    top_k: 5,
    include_superseded: false,
  },
  {
    label: 'Superseded Timeline Query',
    query: 'What was the earlier cybersecurity compliance timeline before recent extensions?',
    top_k: 5,
    include_superseded: true,
  },
  {
    label: 'RE / MII Classification',
    query: 'How are Qualified REs and Market Infrastructure Institutions categorized?',
    top_k: 5,
    include_superseded: false,
  },
];

export const QueryForm = ({
  isLoading,
  onSubmit,
  initialQuery = '',
  initialTopK = 5,
  initialIncludeSuperseded = false,
}: QueryFormProps) => {
  const [query, setQuery] = useState(initialQuery);
  const [topK, setTopK] = useState<number>(initialTopK);
  const [includeSuperseded, setIncludeSuperseded] = useState<boolean>(initialIncludeSuperseded);
  const [clientError, setClientError] = useState<string | null>(null);

  const charCount = query.length;
  const trimmedLength = query.trim().length;
  const isTooShort = trimmedLength > 0 && trimmedLength < 2;
  const isTooLong = charCount > 500;
  const isValid = trimmedLength >= 2 && charCount <= 500;

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (!isValid) {
      if (trimmedLength < 2) {
        setClientError('Query must contain at least 2 non-whitespace characters.');
      } else if (isTooLong) {
        setClientError('Query cannot exceed 500 characters.');
      }
      return;
    }

    setClientError(null);
    onSubmit({
      query: query.trim(),
      top_k: topK,
      include_superseded: includeSuperseded,
    });
  };

  const handleApplyExample = (example: typeof EXAMPLE_QUERIES[0]) => {
    setQuery(example.query);
    setTopK(example.top_k);
    setIncludeSuperseded(example.include_superseded);
    setClientError(null);
  };

  return (
    <div className="w-full bg-slate-900/80 border border-slate-800 rounded-2xl p-5 sm:p-6 shadow-xl backdrop-blur-sm">
      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <div className="flex justify-between items-center mb-1.5">
            <label htmlFor="query-input" className="text-xs font-semibold text-slate-300 uppercase tracking-wider">
              Regulatory Question / Topic
            </label>
            <span
              className={`text-xs font-mono transition-colors ${
                isTooLong
                  ? 'text-rose-400 font-bold'
                  : isTooShort
                  ? 'text-amber-400'
                  : 'text-slate-500'
              }`}
            >
              {charCount} / 500
            </span>
          </div>

          <textarea
            id="query-input"
            name="query"
            rows={3}
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              if (clientError) setClientError(null);
            }}
            disabled={isLoading}
            placeholder="e.g. What are the audit and reporting timelines for cyber incidents under CSCRF?"
            className={`w-full bg-slate-950/80 border rounded-xl p-3.5 text-sm text-slate-100 placeholder-slate-500 focus:outline-none focus:ring-2 transition-all resize-y min-h-[90px] ${
              clientError || isTooLong
                ? 'border-rose-500/80 focus:ring-rose-500/30'
                : 'border-slate-700/80 focus:border-cyan-500/80 focus:ring-cyan-500/20'
            }`}
          />

          {clientError && (
            <p className="mt-1.5 text-xs text-rose-400 font-medium flex items-center gap-1.5">
              <svg className="w-3.5 h-3.5 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
              </svg>
              <span>{clientError}</span>
            </p>
          )}
        </div>

        {/* Query Controls */}
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 pt-2 border-t border-slate-800/80">
          <div className="flex items-center justify-between sm:justify-start gap-3 bg-slate-950/50 px-3.5 py-2.5 rounded-xl border border-slate-800">
            <label htmlFor="top-k-select" className="text-xs text-slate-300 font-medium whitespace-nowrap">
              Passage Depth (<code className="text-cyan-400 font-mono">top_k</code>):
            </label>
            <select
              id="top-k-select"
              value={topK}
              onChange={(e) => setTopK(Number(e.target.value))}
              disabled={isLoading}
              className="bg-slate-900 border border-slate-700 rounded-lg px-2.5 py-1 text-xs text-slate-100 focus:outline-none focus:border-cyan-500 cursor-pointer"
            >
              {[1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map((num) => (
                <option key={num} value={num}>
                  {num} {num === 5 ? '(Default 5)' : 'passages'}
                </option>
              ))}
            </select>
          </div>

          <div className="flex items-center justify-between sm:justify-start gap-3 bg-slate-950/50 px-3.5 py-2.5 rounded-xl border border-slate-800">
            <label htmlFor="include-superseded-toggle" className="text-xs text-slate-300 font-medium cursor-pointer">
              Include Historical / Superseded:
            </label>
            <input
              id="include-superseded-toggle"
              type="checkbox"
              checked={includeSuperseded}
              onChange={(e) => setIncludeSuperseded(e.target.checked)}
              disabled={isLoading}
              className="w-4 h-4 rounded bg-slate-900 border-slate-700 text-cyan-500 focus:ring-cyan-500/30 cursor-pointer accent-cyan-500"
            />
          </div>
        </div>

        {/* Submit Button */}
        <div className="flex flex-col sm:flex-row items-center justify-between gap-3 pt-2">
          <button
            type="submit"
            disabled={isLoading || !isValid}
            className="w-full sm:w-auto px-6 py-2.5 rounded-xl bg-gradient-to-r from-teal-500 to-cyan-600 hover:from-teal-400 hover:to-cyan-500 text-slate-950 font-semibold text-sm transition-all shadow-lg shadow-cyan-500/20 disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2 cursor-pointer"
          >
            {isLoading ? (
              <>
                <svg className="animate-spin -ml-1 mr-2 h-4 w-4 text-slate-950" fill="none" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                </svg>
                <span>Searching & Grounding...</span>
              </>
            ) : (
              <>
                <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
                </svg>
                <span>Retrieve & Validate Excerpts</span>
              </>
            )}
          </button>
        </div>
      </form>

      {/* Suggested Example Queries */}
      <div className="mt-5 pt-4 border-t border-slate-800/80">
        <span className="text-[11px] uppercase tracking-wider text-slate-500 font-semibold block mb-2">
          Sample Research Queries (Click to Populate):
        </span>
        <div className="flex flex-wrap gap-2">
          {EXAMPLE_QUERIES.map((ex, idx) => (
            <button
              key={idx}
              type="button"
              onClick={() => handleApplyExample(ex)}
              disabled={isLoading}
              className="text-xs text-left px-3 py-1.5 rounded-lg bg-slate-950/70 border border-slate-800 hover:border-cyan-500/50 hover:text-cyan-300 text-slate-400 transition cursor-pointer disabled:opacity-50"
            >
              <span className="font-medium text-slate-300">{ex.label}</span>
              {ex.include_superseded && (
                <span className="ml-1.5 text-[10px] text-amber-400/90 font-mono">(incl. superseded)</span>
              )}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
};
