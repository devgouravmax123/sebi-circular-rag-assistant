import { useState, useEffect, useRef, type FormEvent } from 'react';
import { useAuth } from '../context/AuthContext';
import { useWorkspace } from '../context/WorkspaceContext';
import type {
  WorkspaceRole,
  CollectionIngestionJob,
  CollectionDocument,
} from '../types/workspace';
import {
  uploadCollectionDocument,
  fetchCollectionIngestionJobs,
  fetchCollectionDocuments,
  downloadCollectionDocumentBlob,
} from '../api/client';

interface CollectionsViewProps {
  onNavigateToResearch: (collectionId: string) => void;
  onOpenAuthModal?: (mode: 'login' | 'signup') => void;
}

export function CollectionsView({
  onNavigateToResearch,
  onOpenAuthModal,
}: CollectionsViewProps) {
  const { token, isAuthenticated } = useAuth();
  const {
    activeWorkspace,
    workspaceCollections,
    createNewCollection,
    isCollectionsLoading,
  } = useWorkspace();

  // Create Collection Form State
  const [newColId, setNewColId] = useState('');
  const [newColName, setNewColName] = useState('');
  const [newColDesc, setNewColDesc] = useState('');
  const [newColPolicy, setNewColPolicy] = useState<'general_static' | 'sebi_cscrf_strict'>('general_static');
  const [isCreatingCol, setIsCreatingCol] = useState(false);
  const [createColError, setCreateColError] = useState<string | null>(null);
  const [createColSuccess, setCreateColSuccess] = useState<string | null>(null);

  // Selected collection for documents / jobs
  const [selectedColId, setSelectedColId] = useState<string>('');

  // Documents & Jobs State
  const [jobs, setJobs] = useState<CollectionIngestionJob[]>([]);
  const [isJobsLoading, setIsJobsLoading] = useState(false);
  const [jobsError, setJobsError] = useState<string | null>(null);

  const [documents, setDocuments] = useState<CollectionDocument[]>([]);
  const [isDocsLoading, setIsDocsLoading] = useState(false);
  const [docsError, setDocsError] = useState<string | null>(null);

  // Upload Form State
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploadTitle, setUploadTitle] = useState('');
  const [uploadCircularNumber, setUploadCircularNumber] = useState('');
  const [isUploading, setIsUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploadSuccess, setUploadSuccess] = useState<string | null>(null);
  const [downloadingDocId, setDownloadingDocId] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const userRole: WorkspaceRole = activeWorkspace?.current_user_role || 'member';
  const canUpload = userRole === 'owner' || userRole === 'admin' || userRole === 'member';

  // Keep selectedColId pointing to an existing collection
  useEffect(() => {
    if (workspaceCollections.length > 0) {
      if (!selectedColId || !workspaceCollections.some((c) => c.id === selectedColId)) {
        setSelectedColId(workspaceCollections[0].id);
      }
    } else {
      setSelectedColId('');
    }
  }, [workspaceCollections, selectedColId]);

  // Load jobs and documents whenever selected collection or active workspace changes
  useEffect(() => {
    if (!token || !activeWorkspace || !selectedColId) {
      setJobs([]);
      setDocuments([]);
      return;
    }

    let ignore = false;
    setIsJobsLoading(true);
    setJobsError(null);
    setIsDocsLoading(true);
    setDocsError(null);

    Promise.all([
      fetchCollectionIngestionJobs(activeWorkspace.id, selectedColId, token)
        .then((res) => {
          if (!ignore) setJobs(res);
        })
        .catch((err) => {
          if (!ignore) setJobsError(err instanceof Error ? err.message : 'Failed to load ingestion jobs.');
        })
        .finally(() => {
          if (!ignore) setIsJobsLoading(false);
        }),

      fetchCollectionDocuments(activeWorkspace.id, selectedColId, token)
        .then((res) => {
          if (!ignore) setDocuments(res);
        })
        .catch((err) => {
          if (!ignore) setDocsError(err instanceof Error ? err.message : 'Failed to load documents catalog.');
        })
        .finally(() => {
          if (!ignore) setIsDocsLoading(false);
        }),
    ]);

    return () => {
      ignore = true;
    };
  }, [token, activeWorkspace, selectedColId]);

  // Handler: Preset selection for rapid collection creation
  const handleSelectPreset = (preset: {
    id: string;
    name: string;
    desc: string;
    policy: 'general_static' | 'sebi_cscrf_strict';
  }) => {
    setNewColId(preset.id);
    setNewColName(preset.name);
    setNewColDesc(preset.desc);
    setNewColPolicy(preset.policy);
    setCreateColError(null);
    setCreateColSuccess(null);
  };

  // Handler: Create Collection
  const handleCreateCollection = async (e: FormEvent) => {
    e.preventDefault();
    if (!newColId.trim() || !newColName.trim()) {
      setCreateColError('Collection ID and Display Name are required.');
      return;
    }

    setIsCreatingCol(true);
    setCreateColError(null);
    setCreateColSuccess(null);

    try {
      await createNewCollection({
        id: newColId.trim().toLowerCase(),
        name: newColName.trim(),
        description: newColDesc.trim() || undefined,
        policy_name: newColPolicy,
      });

      setCreateColSuccess(`Collection "${newColName}" created successfully!`);
      setSelectedColId(newColId.trim().toLowerCase());
      setNewColId('');
      setNewColName('');
      setNewColDesc('');
    } catch (err) {
      setCreateColError(err instanceof Error ? err.message : 'Failed to create collection.');
    } finally {
      setIsCreatingCol(false);
    }
  };

  // Handler: File selection & validation
  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setUploadError(null);
    setUploadSuccess(null);
    const file = e.target.files?.[0];
    if (!file) {
      setUploadFile(null);
      return;
    }

    if (!file.name.toLowerCase().endsWith('.pdf') && file.type !== 'application/pdf') {
      setUploadError('Only PDF documents are supported for ingestion.');
      setUploadFile(null);
      return;
    }

    const maxSizeBytes = 30 * 1024 * 1024;
    if (file.size > maxSizeBytes) {
      setUploadError('File size exceeds 30MB limit.');
      setUploadFile(null);
      return;
    }

    setUploadFile(file);
    if (!uploadTitle.trim()) {
      const suggestedTitle = file.name.replace(/\.[^/.]+$/, '').replace(/[-_]/g, ' ');
      setUploadTitle(suggestedTitle);
    }
  };

  // Handler: Upload Document
  const handleUploadSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!uploadFile) {
      setUploadError('Please select a PDF document to upload.');
      return;
    }
    if (!uploadTitle.trim()) {
      setUploadError('Document title is required.');
      return;
    }
    if (!activeWorkspace || !selectedColId || !token) {
      setUploadError('Please select an active workspace collection.');
      return;
    }

    setIsUploading(true);
    setUploadError(null);
    setUploadSuccess(null);

    try {
      const res = await uploadCollectionDocument(
        activeWorkspace.id,
        selectedColId,
        uploadFile,
        uploadTitle.trim(),
        uploadCircularNumber.trim() || undefined,
        token
      );

      setUploadSuccess(`Upload successful! Ingestion Job ID: ${res.job_id} (Status: ${res.status})`);
      setUploadFile(null);
      setUploadTitle('');
      setUploadCircularNumber('');
      if (fileInputRef.current) {
        fileInputRef.current.value = '';
      }

      // Refresh jobs and documents
      if (activeWorkspace && token && selectedColId) {
        const [jobsRes, docsRes] = await Promise.all([
          fetchCollectionIngestionJobs(activeWorkspace.id, selectedColId, token),
          fetchCollectionDocuments(activeWorkspace.id, selectedColId, token),
        ]);
        setJobs(jobsRes);
        setDocuments(docsRes);
      }
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : 'Upload failed.');
    } finally {
      setIsUploading(false);
    }
  };

  // Handler: Download Document Blob
  const handleDownloadDoc = async (documentId: string, filename: string) => {
    if (!activeWorkspace || !selectedColId || !token) return;
    setDownloadingDocId(documentId);
    try {
      const blob = await downloadCollectionDocumentBlob(
        activeWorkspace.id,
        selectedColId,
        documentId,
        token
      );
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename || `${documentId}.pdf`;
      document.body.appendChild(a);
      a.click();
      window.URL.revokeObjectURL(url);
      document.body.removeChild(a);
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to download document.');
    } finally {
      setDownloadingDocId(null);
    }
  };

  // Handler: Manual Refresh
  const handleRefresh = async () => {
    if (!activeWorkspace || !selectedColId || !token) return;
    setIsJobsLoading(true);
    setIsDocsLoading(true);
    try {
      const [jobsRes, docsRes] = await Promise.all([
        fetchCollectionIngestionJobs(activeWorkspace.id, selectedColId, token),
        fetchCollectionDocuments(activeWorkspace.id, selectedColId, token),
      ]);
      setJobs(jobsRes);
      setDocuments(docsRes);
    } catch (err) {
      console.error('Refresh failed:', err);
    } finally {
      setIsJobsLoading(false);
      setIsDocsLoading(false);
    }
  };

  return (
    <div className="space-y-8 animate-fadeIn">
      {/* Header section */}
      <section className="border-b border-slate-800/80 pb-6">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
          <div>
            <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-teal-950/80 border border-teal-800/60 text-xs font-medium text-teal-400 mb-2">
              <span>📁</span>
              <span>Tenant Collections &amp; Document Ingestion</span>
            </div>
            <h1 className="text-2xl sm:text-3xl font-bold tracking-tight text-white">
              Document Collections &amp; Ingestion Pipeline
            </h1>
            <p className="text-sm text-slate-300 mt-1 max-w-3xl leading-relaxed">
              Organize documents into isolated collections across research papers, financial filings, technical
              documentation, and regulatory frameworks. Upload PDFs to secure cloud storage and track chunking/ingestion jobs.
            </p>
          </div>

          {selectedColId && (
            <button
              onClick={() => onNavigateToResearch(selectedColId)}
              className="px-4 py-2 bg-gradient-to-r from-teal-500 to-cyan-600 hover:from-teal-400 hover:to-cyan-500 text-slate-950 font-semibold text-xs rounded-xl transition shadow-md shadow-teal-500/20 cursor-pointer flex items-center justify-center gap-1.5 self-start sm:self-auto shrink-0"
            >
              <span>🔍</span>
              <span>Query Active Collection</span>
            </button>
          )}
        </div>
      </section>

      {/* Unauthenticated Alert */}
      {!isAuthenticated && (
        <div className="rounded-xl border border-amber-900/60 bg-amber-950/30 p-5 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
          <div className="space-y-1">
            <h3 className="text-sm font-semibold text-amber-300 flex items-center gap-2">
              <span>🔒</span>
              <span>Sign In Required for Custom Collections &amp; PDF Uploads</span>
            </h3>
            <p className="text-xs text-slate-400">
              You are currently in Public Demo Mode. Sign in or create an account to configure isolated workspace collections, upload PDF files up to 30MB, and track document ingestion jobs.
            </p>
          </div>
          {onOpenAuthModal && (
            <div className="flex items-center gap-2 shrink-0">
              <button
                onClick={() => onOpenAuthModal('login')}
                className="px-3.5 py-1.5 text-xs font-semibold text-slate-950 bg-amber-400 hover:bg-amber-300 rounded-lg transition cursor-pointer"
              >
                Sign In
              </button>
              <button
                onClick={() => onOpenAuthModal('signup')}
                className="px-3.5 py-1.5 text-xs font-medium text-slate-300 hover:text-white bg-slate-800 hover:bg-slate-700 rounded-lg transition border border-slate-700 cursor-pointer"
              >
                Sign Up
              </button>
            </div>
          )}
        </div>
      )}

      {/* Main Content Grid */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-8">
        {/* Left Column: Collection Management & Creation (5 cols) */}
        <div className="lg:col-span-5 space-y-6">
            {/* Workspace Collections Card */}
            <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-5 space-y-4">
              <div className="flex items-center justify-between">
                <h2 className="text-base font-semibold text-white flex items-center gap-2">
                  <span>📚</span>
                  <span>Workspace Collections</span>
                </h2>
                <span className="text-xs font-medium px-2 py-0.5 rounded bg-slate-800 text-slate-300 border border-slate-700">
                  {workspaceCollections.length} total
                </span>
              </div>

              {isCollectionsLoading ? (
                <div className="p-4 text-center text-xs text-slate-400 animate-pulse">
                  Loading workspace collections...
                </div>
              ) : workspaceCollections.length === 0 ? (
                <div className="p-4 text-center rounded-lg border border-dashed border-slate-800 text-xs text-slate-400">
                  No private collections yet in this workspace. Create one below to upload PDFs!
                </div>
              ) : (
                <div className="space-y-2 max-h-64 overflow-y-auto pr-1">
                  {workspaceCollections.map((col) => {
                    const isSelected = col.id === selectedColId;
                    return (
                      <div
                        key={col.id}
                        onClick={() => setSelectedColId(col.id)}
                        className={`p-3 rounded-lg border transition cursor-pointer flex items-center justify-between gap-3 ${
                          isSelected
                            ? 'bg-cyan-950/40 border-cyan-700/80 shadow-sm'
                            : 'bg-slate-950/60 border-slate-800/80 hover:border-slate-700'
                        }`}
                      >
                        <div className="min-w-0">
                          <div className="flex items-center gap-2">
                            <span className="font-semibold text-xs text-white truncate">
                              {col.name}
                            </span>
                            {isSelected && (
                              <span className="w-1.5 h-1.5 rounded-full bg-cyan-400 shrink-0" />
                            )}
                          </div>
                          <span className="text-[11px] font-mono text-slate-400 block truncate">
                            {col.id}
                          </span>
                        </div>
                        <span
                          className={`text-[10px] font-semibold uppercase px-2 py-0.5 rounded border shrink-0 ${
                            col.policy_name === 'sebi_cscrf_strict'
                              ? 'bg-rose-950/60 text-rose-300 border-rose-800/60'
                              : 'bg-teal-950/60 text-teal-300 border-teal-800/60'
                          }`}
                        >
                          {col.policy_name === 'sebi_cscrf_strict' ? 'Regulatory' : 'General'}
                        </span>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>

            {/* Create Collection Form */}
            <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-5 space-y-4">
              <div>
                <h2 className="text-base font-semibold text-white flex items-center gap-2">
                  <span>✨</span>
                  <span>Create New Collection</span>
                </h2>
                <p className="text-xs text-slate-400 mt-1">
                  Select a domain preset or configure a custom document collection.
                </p>
              </div>

              {/* Domain Presets */}
              <div className="space-y-1.5">
                <label className="text-[11px] font-medium text-slate-400 uppercase tracking-wider block">
                  Domain Templates
                </label>
                <div className="flex flex-wrap gap-1.5">
                  {[
                    {
                      id: 'research-papers',
                      name: 'Academic & Research Papers',
                      desc: 'Preprints, peer-reviewed journals, and scientific literature.',
                      policy: 'general_static' as const,
                      label: 'Research',
                    },
                    {
                      id: 'financial-reports',
                      name: 'Financial & Quarterly Filings',
                      desc: 'Quarterly statements, audit reports, and investor disclosures.',
                      policy: 'general_static' as const,
                      label: 'Financial',
                    },
                    {
                      id: 'legal-contracts',
                      name: 'Legal Contracts & Agreements',
                      desc: 'Master service agreements, vendor terms, and contractual clauses.',
                      policy: 'general_static' as const,
                      label: 'Legal',
                    },
                    {
                      id: 'tech-manuals',
                      name: 'Technical Architecture & RFCs',
                      desc: 'System architecture specs, API guidelines, and technical runbooks.',
                      policy: 'general_static' as const,
                      label: 'Tech Docs',
                    },
                    {
                      id: 'regulatory-cscrf',
                      name: 'Cybersecurity Regulatory Guidelines',
                      desc: 'Regulatory circulars, frameworks, and compliance mandates.',
                      policy: 'sebi_cscrf_strict' as const,
                      label: 'Regulatory',
                    },
                  ].map((preset) => (
                    <button
                      key={preset.id}
                      type="button"
                      onClick={() => handleSelectPreset(preset)}
                      className="px-2 py-1 text-[11px] font-medium bg-slate-800/80 hover:bg-slate-700 text-slate-300 hover:text-white border border-slate-700 rounded-md transition cursor-pointer"
                    >
                      + {preset.label}
                    </button>
                  ))}
                </div>
              </div>

              {createColError && (
                <div className="p-3 bg-rose-950/60 border border-rose-800/80 rounded-lg text-xs text-rose-300">
                  {createColError}
                </div>
              )}

              {createColSuccess && (
                <div className="p-3 bg-emerald-950/60 border border-emerald-800/80 rounded-lg text-xs text-emerald-300">
                  {createColSuccess}
                </div>
              )}

              <form onSubmit={handleCreateCollection} className="space-y-3">
                <div>
                  <label className="block text-xs font-medium text-slate-300 mb-1">
                    Collection Identifier (kebab-case) *
                  </label>
                  <input
                    type="text"
                    required
                    value={newColId}
                    onChange={(e) => setNewColId(e.target.value.toLowerCase().replace(/[^a-z0-9-_]/g, '-'))}
                    placeholder="e.g. quarterly-filings-2025"
                    className="w-full px-3 py-1.5 bg-slate-950 border border-slate-700 rounded-lg text-xs text-white font-mono focus:border-cyan-500 focus:outline-none"
                  />
                </div>

                <div>
                  <label className="block text-xs font-medium text-slate-300 mb-1">
                    Display Name *
                  </label>
                  <input
                    type="text"
                    required
                    value={newColName}
                    onChange={(e) => setNewColName(e.target.value)}
                    placeholder="e.g. Q1 2025 Financial Filings"
                    className="w-full px-3 py-1.5 bg-slate-950 border border-slate-700 rounded-lg text-xs text-white focus:border-cyan-500 focus:outline-none"
                  />
                </div>

                <div>
                  <label className="block text-xs font-medium text-slate-300 mb-1">
                    Description
                  </label>
                  <textarea
                    rows={2}
                    value={newColDesc}
                    onChange={(e) => setNewColDesc(e.target.value)}
                    placeholder="Purpose, domain context, or ingestion notes..."
                    className="w-full px-3 py-1.5 bg-slate-950 border border-slate-700 rounded-lg text-xs text-white focus:border-cyan-500 focus:outline-none"
                  />
                </div>

                <div>
                  <label className="block text-xs font-medium text-slate-300 mb-1">
                    Grounding Policy
                  </label>
                  <select
                    value={newColPolicy}
                    onChange={(e) => setNewColPolicy(e.target.value as 'general_static' | 'sebi_cscrf_strict')}
                    className="w-full px-3 py-1.5 bg-slate-950 border border-slate-700 rounded-lg text-xs text-white focus:border-cyan-500 focus:outline-none"
                  >
                    <option value="general_static">General Static (General Documents, Research, Tech)</option>
                    <option value="sebi_cscrf_strict">SEBI CSCRF Strict (Regulatory Compliance &amp; Circulars)</option>
                  </select>
                </div>

                <button
                  type={isAuthenticated ? "submit" : "button"}
                  onClick={isAuthenticated ? undefined : () => onOpenAuthModal?.('login')}
                  disabled={isAuthenticated && isCreatingCol}
                  className="w-full py-2 bg-teal-600 hover:bg-teal-500 disabled:opacity-50 text-white font-semibold text-xs rounded-lg transition shadow cursor-pointer"
                >
                  {isCreatingCol
                    ? 'Creating Collection...'
                    : isAuthenticated
                    ? 'Create Collection'
                    : 'Sign In to Create Collection'}
                </button>
              </form>
            </div>
          </div>

          {/* Right Column: PDF Upload & Ingestion Tracking (7 cols) */}
          <div className="lg:col-span-7 space-y-6">
            {/* Selected Collection Switcher Bar */}
            <div className="bg-slate-900/80 border border-slate-800 rounded-xl p-4 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
              <div>
                <span className="text-xs text-slate-400 block">Target Collection:</span>
                <span className="text-sm font-semibold text-white">
                  {workspaceCollections.find((c) => c.id === selectedColId)?.name ||
                    (workspaceCollections.length > 0 ? 'Select a collection' : 'No collections available')}
                </span>
              </div>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={handleRefresh}
                  disabled={isJobsLoading || isDocsLoading}
                  className="px-2.5 py-1 text-xs text-slate-300 hover:text-white bg-slate-800 hover:bg-slate-700 border border-slate-700 rounded-lg transition cursor-pointer flex items-center gap-1"
                >
                  <span className={isJobsLoading ? 'animate-spin' : ''}>🔄</span>
                  <span>Refresh</span>
                </button>
                {selectedColId && (
                  <button
                    type="button"
                    onClick={() => onNavigateToResearch(selectedColId)}
                    className="px-3 py-1 text-xs font-semibold text-cyan-300 hover:text-white bg-cyan-950/80 border border-cyan-800/80 rounded-lg transition cursor-pointer"
                  >
                    Research &rarr;
                  </button>
                )}
              </div>
            </div>

            {/* Ingestion Worker Architectural Guidance Notice */}
            <div className="rounded-xl border border-cyan-900/50 bg-gradient-to-r from-cyan-950/30 via-slate-900/40 to-slate-950 p-4 text-xs text-slate-300 space-y-2">
              <div className="flex items-center gap-2 text-cyan-300 font-semibold">
                <span>⚙️</span>
                <span>Asynchronous Ingestion Pipeline Notice</span>
              </div>
              <p className="leading-relaxed text-slate-300">
                Uploaded PDF documents are securely saved to object storage (Backblaze B2 / S3) and queued as ingestion jobs.
                Document chunking, vector embedding, and index snapshot publication occur asynchronously via the ingestion worker.
              </p>
              <div className="p-2.5 rounded-lg bg-slate-950/80 border border-slate-800/80 flex items-start gap-2 text-[11px] text-amber-300/90">
                <span className="shrink-0 mt-0.5">⚠️</span>
                <span>
                  <strong>Snapshot Readiness Rule:</strong> Documents in state <code className="text-amber-200">queued</code> or <code className="text-amber-200">processing</code> cannot be queried until index publication completes.
                </span>
              </div>
            </div>

            {/* PDF Upload Box */}
            {canUpload ? (
              <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-5 space-y-4">
                <h3 className="text-sm font-semibold text-white flex items-center gap-2">
                  <span>📄</span>
                  <span>Upload PDF Document</span>
                </h3>

                {uploadError && (
                  <div className="p-3 bg-rose-950/60 border border-rose-800/80 rounded-lg text-xs text-rose-300">
                    {uploadError}
                  </div>
                )}

                {uploadSuccess && (
                  <div className="p-3 bg-emerald-950/60 border border-emerald-800/80 rounded-lg text-xs text-emerald-300">
                    {uploadSuccess}
                  </div>
                )}

                <form onSubmit={handleUploadSubmit} className="space-y-4">
                  {/* Drag-drop zone */}
                  <div
                    onClick={() => fileInputRef.current?.click()}
                    className="border-2 border-dashed border-slate-700 hover:border-cyan-500/80 rounded-xl p-6 text-center cursor-pointer transition bg-slate-950/40 hover:bg-slate-950/70"
                  >
                    <input
                      ref={fileInputRef}
                      type="file"
                      accept=".pdf,application/pdf"
                      onChange={handleFileChange}
                      className="hidden"
                    />
                    <div className="text-3xl mb-2">📑</div>
                    <p className="text-xs font-semibold text-slate-200">
                      {uploadFile ? uploadFile.name : 'Click to select or drag and drop a PDF file'}
                    </p>
                    <p className="text-[11px] text-slate-400 mt-1">
                      {uploadFile
                        ? `${(uploadFile.size / (1024 * 1024)).toFixed(2)} MB — Ready to upload`
                        : 'PDF format only &bull; Up to 30MB maximum'}
                    </p>
                  </div>

                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    <div>
                      <label className="block text-xs font-medium text-slate-300 mb-1">
                        Document Title *
                      </label>
                      <input
                        type="text"
                        required
                        value={uploadTitle}
                        onChange={(e) => setUploadTitle(e.target.value)}
                        placeholder="e.g. Q4 Financial Audit Report"
                        className="w-full px-3 py-1.5 bg-slate-950 border border-slate-700 rounded-lg text-xs text-white focus:border-cyan-500 focus:outline-none"
                      />
                    </div>
                    <div>
                      <label className="block text-xs font-medium text-slate-300 mb-1">
                        Circular / Reference Number
                      </label>
                      <input
                        type="text"
                        value={uploadCircularNumber}
                        onChange={(e) => setUploadCircularNumber(e.target.value)}
                        placeholder="Optional: e.g. REF-2025-001"
                        className="w-full px-3 py-1.5 bg-slate-950 border border-slate-700 rounded-lg text-xs text-white focus:border-cyan-500 focus:outline-none"
                      />
                    </div>
                  </div>

                  <button
                    type="submit"
                    disabled={isUploading || !uploadFile || !selectedColId}
                    className="w-full py-2 bg-gradient-to-r from-teal-500 to-cyan-600 hover:from-teal-400 hover:to-cyan-500 disabled:opacity-50 text-slate-950 font-semibold text-xs rounded-lg transition shadow-md cursor-pointer flex items-center justify-center gap-2"
                  >
                    {isUploading ? (
                      <>
                        <span className="w-3 h-3 border-2 border-slate-950 border-t-transparent rounded-full animate-spin" />
                        <span>Uploading to Object Storage...</span>
                      </>
                    ) : (
                      <>
                        <span>⬆️</span>
                        <span>Upload Document</span>
                      </>
                    )}
                  </button>
                </form>
              </div>
            ) : (
              <div className="bg-slate-900/40 border border-slate-800 rounded-xl p-4 text-xs text-slate-400">
                🔒 You have read-only (Viewer) access. Uploading documents requires Member, Admin, or Owner permissions.
              </div>
            )}

            {/* Ingestion Jobs Table */}
            <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-5 space-y-4">
              <div className="flex items-center justify-between">
                <h3 className="text-sm font-semibold text-white flex items-center gap-2">
                  <span>⚙️</span>
                  <span>Ingestion Pipeline Jobs</span>
                </h3>
                <span className="text-xs text-slate-400">
                  {jobs.length} tracked
                </span>
              </div>

              {jobsError && (
                <div className="p-3 bg-rose-950/60 border border-rose-800/80 rounded-lg text-xs text-rose-300">
                  {jobsError}
                </div>
              )}

              {isJobsLoading ? (
                <div className="p-4 text-center text-xs text-slate-400 animate-pulse">
                  Checking ingestion jobs...
                </div>
              ) : jobs.length === 0 ? (
                <div className="p-4 text-center rounded-lg border border-dashed border-slate-800 text-xs text-slate-400">
                  No ingestion jobs queued for this collection yet.
                </div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-xs text-slate-300">
                    <thead className="border-b border-slate-800 text-[11px] text-slate-400 uppercase font-medium">
                      <tr>
                        <th className="py-2 pr-4">Job ID</th>
                        <th className="py-2 pr-4">Status</th>
                        <th className="py-2 pr-4">Created</th>
                        <th className="py-2">Notes</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-800/60">
                      {jobs.map((job) => {
                        let statusColor = 'bg-slate-800 text-slate-300 border-slate-700';
                        if (job.status === 'queued') {
                          statusColor = 'bg-amber-950/80 text-amber-300 border-amber-800/80';
                        } else if (job.status === 'processing') {
                          statusColor = 'bg-blue-950/80 text-blue-300 border-blue-800/80';
                        } else if (job.status === 'completed') {
                          statusColor = 'bg-emerald-950/80 text-emerald-300 border-emerald-800/80';
                        } else if (job.status === 'failed') {
                          statusColor = 'bg-rose-950/80 text-rose-300 border-rose-800/80';
                        }

                        return (
                          <tr key={job.job_id} className="hover:bg-slate-900/40">
                            <td className="py-2.5 pr-4 font-mono text-[11px] text-slate-400">
                              {job.job_id.slice(0, 8)}...
                            </td>
                            <td className="py-2.5 pr-4">
                              <span className={`px-2 py-0.5 rounded text-[10px] font-semibold uppercase border ${statusColor}`}>
                                {job.status}
                              </span>
                            </td>
                            <td className="py-2.5 pr-4 text-slate-400 text-[11px]">
                              {new Date(job.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                            </td>
                            <td className="py-2.5 text-[11px] text-slate-400 truncate max-w-xs">
                              {job.error_message || 'Awaiting worker execution'}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </div>

            {/* Documents Catalog Table */}
            <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-5 space-y-4">
              <div className="flex items-center justify-between">
                <h3 className="text-sm font-semibold text-white flex items-center gap-2">
                  <span>📑</span>
                  <span>Uploaded Documents Catalog</span>
                </h3>
                <span className="text-xs text-slate-400">
                  {documents.length} documents
                </span>
              </div>

              {docsError && (
                <div className="p-3 bg-rose-950/60 border border-rose-800/80 rounded-lg text-xs text-rose-300">
                  {docsError}
                </div>
              )}

              {isDocsLoading ? (
                <div className="p-4 text-center text-xs text-slate-400 animate-pulse">
                  Loading documents...
                </div>
              ) : documents.length === 0 ? (
                <div className="p-4 text-center rounded-lg border border-dashed border-slate-800 text-xs text-slate-400">
                  No documents found in this collection.
                </div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-xs text-slate-300">
                    <thead className="border-b border-slate-800 text-[11px] text-slate-400 uppercase font-medium">
                      <tr>
                        <th className="py-2 pr-4">Title / Ref</th>
                        <th className="py-2 pr-4">Status</th>
                        <th className="py-2 pr-4">Uploaded</th>
                        <th className="py-2 text-right">Actions</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-800/60">
                      {documents.map((doc) => (
                        <tr key={doc.document_id} className="hover:bg-slate-900/40">
                          <td className="py-2.5 pr-4">
                            <div className="font-medium text-white">{doc.title}</div>
                            {doc.circular_number && (
                              <div className="text-[11px] font-mono text-cyan-400">{doc.circular_number}</div>
                            )}
                          </td>
                          <td className="py-2.5 pr-4 text-slate-400 text-[11px]">
                            {doc.is_published ? 'Published' : 'Uploaded'}
                          </td>
                          <td className="py-2.5 pr-4 text-slate-400 text-[11px]">
                            {new Date(doc.created_at).toLocaleDateString()}
                          </td>
                          <td className="py-2.5 text-right">
                            <button
                              type="button"
                              onClick={() => handleDownloadDoc(doc.document_id, `${doc.title}.pdf`)}
                              disabled={downloadingDocId === doc.document_id}
                              className="px-2.5 py-1 text-xs text-cyan-300 hover:text-white bg-cyan-950/60 hover:bg-cyan-900/60 border border-cyan-800/60 rounded-md transition cursor-pointer"
                            >
                              {downloadingDocId === doc.document_id ? 'Downloading...' : '⬇️ Download'}
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </div>
        </div>
    </div>
  );
}
