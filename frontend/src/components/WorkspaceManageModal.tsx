import { useState, useEffect, useCallback, useRef, type FormEvent } from 'react';
import { useAuth } from '../context/AuthContext';
import { useWorkspace } from '../context/WorkspaceContext';
import type { WorkspaceRole, WorkspaceMember, CollectionIngestionJob, CollectionDocument } from '../types/workspace';
import {
  fetchWorkspaceMembers,
  addWorkspaceMember,
  removeWorkspaceMember,
  uploadCollectionDocument,
  fetchCollectionIngestionJobs,
  fetchCollectionDocuments,
  downloadCollectionDocumentBlob,
} from '../api/client';

interface WorkspaceManageModalProps {
  isOpen: boolean;
  onClose: () => void;
  defaultTab?: 'collections' | 'documents' | 'members' | 'create_workspace';
}

export function WorkspaceManageModal({
  isOpen,
  onClose,
  defaultTab = 'collections',
}: WorkspaceManageModalProps) {
  const { token, user } = useAuth();
  const {
    activeWorkspace,
    createNewWorkspace,
    workspaceCollections,
    createNewCollection,
    isCollectionsLoading,
  } = useWorkspace();

  const [activeTab, setActiveTab] = useState<'collections' | 'documents' | 'members' | 'create_workspace'>(defaultTab);

  // Sync tab if defaultTab changes when modal opens
  useEffect(() => {
    if (isOpen) {
      setActiveTab(defaultTab);
    }
  }, [isOpen, defaultTab]);

  // Members state
  const [members, setMembers] = useState<WorkspaceMember[]>([]);
  const [isMembersLoading, setIsMembersLoading] = useState(false);
  const [membersError, setMembersError] = useState<string | null>(null);

  // Add Member form state
  const [newMemberEmail, setNewMemberEmail] = useState('');
  const [newMemberRole, setNewMemberRole] = useState<WorkspaceRole>('member');
  const [isAddingMember, setIsAddingMember] = useState(false);
  const [addMemberError, setAddMemberError] = useState<string | null>(null);

  // Create Collection form state
  const [newColId, setNewColId] = useState('');
  const [newColName, setNewColName] = useState('');
  const [newColDesc, setNewColDesc] = useState('');
  const [newColPolicy, setNewColPolicy] = useState<'general_static' | 'sebi_cscrf_strict'>('general_static');
  const [isCreatingCol, setIsCreatingCol] = useState(false);
  const [createColError, setCreateColError] = useState<string | null>(null);

  // Create Workspace form state
  const [newWsName, setNewWsName] = useState('');
  const [newWsSlug, setNewWsSlug] = useState('');
  const [newWsDesc, setNewWsDesc] = useState('');
  const [isCreatingWs, setIsCreatingWs] = useState(false);
  const [createWsError, setCreateWsError] = useState<string | null>(null);

  // Documents & Ingestion state
  const [selectedDocColId, setSelectedDocColId] = useState<string>('');
  const [jobs, setJobs] = useState<CollectionIngestionJob[]>([]);
  const [isJobsLoading, setIsJobsLoading] = useState(false);
  const [jobsError, setJobsError] = useState<string | null>(null);

  const [documents, setDocuments] = useState<CollectionDocument[]>([]);
  const [isDocsLoading, setIsDocsLoading] = useState(false);
  const [docsError, setDocsError] = useState<string | null>(null);

  // Upload Form state
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploadTitle, setUploadTitle] = useState('');
  const [uploadCircularNumber, setUploadCircularNumber] = useState('');
  const [isUploading, setIsUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploadSuccess, setUploadSuccess] = useState<string | null>(null);
  const [downloadingDocId, setDownloadingDocId] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const canManage =
    activeWorkspace?.current_user_role === 'owner' || activeWorkspace?.current_user_role === 'admin';
  const canUpload =
    activeWorkspace?.current_user_role === 'owner' ||
    activeWorkspace?.current_user_role === 'admin' ||
    activeWorkspace?.current_user_role === 'member';

  // Keep selectedDocColId pointing to an existing collection
  useEffect(() => {
    if (workspaceCollections.length > 0) {
      if (!selectedDocColId || !workspaceCollections.some((c) => c.id === selectedDocColId)) {
        setSelectedDocColId(workspaceCollections[0].id);
      }
    } else {
      setSelectedDocColId('');
    }
  }, [workspaceCollections, selectedDocColId]);

  // Load members whenever modal opens on members tab or active workspace changes
  useEffect(() => {
    if (!isOpen || !token || !activeWorkspace || activeTab !== 'members') return;

    let ignore = false;
    setIsMembersLoading(true);
    setMembersError(null);

    fetchWorkspaceMembers(activeWorkspace.id, token)
      .then((mems) => {
        if (!ignore) {
          setMembers(mems);
        }
      })
      .catch((err: unknown) => {
        if (!ignore) {
          setMembersError(err instanceof Error ? err.message : 'Failed to fetch members.');
        }
      })
      .finally(() => {
        if (!ignore) {
          setIsMembersLoading(false);
        }
      });

    return () => {
      ignore = true;
    };
  }, [isOpen, token, activeWorkspace, activeTab]);

  // Load collection jobs & documents
  const loadCollectionData = useCallback(
    async (isPolling = false) => {
      if (!token || !activeWorkspace || !selectedDocColId) return;

      if (!isPolling) {
        setIsJobsLoading(true);
        setIsDocsLoading(true);
      }
      setJobsError(null);
      setDocsError(null);

      try {
        const [jobsRes, docsRes] = await Promise.all([
          fetchCollectionIngestionJobs(activeWorkspace.id, selectedDocColId, token),
          fetchCollectionDocuments(activeWorkspace.id, selectedDocColId, token),
        ]);
        setJobs(jobsRes);
        setDocuments(docsRes);
      } catch (err: unknown) {
        if (!isPolling) {
          const msg = err instanceof Error ? err.message : 'Failed to fetch collection data';
          setJobsError(msg);
          setDocsError(msg);
        }
      } finally {
        if (!isPolling) {
          setIsJobsLoading(false);
          setIsDocsLoading(false);
        }
      }
    },
    [token, activeWorkspace, selectedDocColId]
  );

  // Trigger loading when documents tab is open and collection selected
  useEffect(() => {
    if (!isOpen || activeTab !== 'documents' || !selectedDocColId) return;
    loadCollectionData(false);
  }, [isOpen, activeTab, selectedDocColId, loadCollectionData]);

  // Polling hook: while any job is queued or processing, poll every 3 seconds
  useEffect(() => {
    if (!isOpen || activeTab !== 'documents' || !selectedDocColId) return;
    const hasActiveJob = jobs.some((j) => j.status === 'queued' || j.status === 'processing');
    if (!hasActiveJob) return;

    const timer = setInterval(() => {
      loadCollectionData(true);
    }, 3000);

    return () => clearInterval(timer);
  }, [isOpen, activeTab, selectedDocColId, jobs, loadCollectionData]);

  if (!isOpen) return null;

  // Handle Add Member
  const handleAddMember = async (e: FormEvent) => {
    e.preventDefault();
    if (!token || !activeWorkspace) return;
    setAddMemberError(null);

    if (!newMemberEmail.trim() || !newMemberEmail.includes('@')) {
      setAddMemberError('Valid email address is required.');
      return;
    }

    setIsAddingMember(true);
    try {
      const added = await addWorkspaceMember(
        activeWorkspace.id,
        { email: newMemberEmail.trim(), role: newMemberRole },
        token
      );
      setMembers((prev) => [...prev, added]);
      setNewMemberEmail('');
      setNewMemberRole('member');
    } catch (err: unknown) {
      setAddMemberError(err instanceof Error ? err.message : 'Failed to add member.');
    } finally {
      setIsAddingMember(false);
    }
  };

  // Handle Remove Member
  const handleRemoveMember = async (memberUserId: string, memberEmail?: string | null) => {
    if (!token || !activeWorkspace) return;
    const confirmMsg = `Are you sure you want to remove ${memberEmail || 'this member'} from the workspace?`;
    if (!window.confirm(confirmMsg)) return;

    try {
      await removeWorkspaceMember(activeWorkspace.id, memberUserId, token);
      setMembers((prev) => prev.filter((m) => m.user_id !== memberUserId));
    } catch (err: unknown) {
      alert(err instanceof Error ? err.message : 'Failed to remove member.');
    }
  };

  // Handle Create Collection
  const handleCreateCollection = async (e: FormEvent) => {
    e.preventDefault();
    setCreateColError(null);

    const trimmedId = newColId.trim().toLowerCase().replace(/[^a-z0-9_-]/g, '_');
    if (trimmedId.length < 2) {
      setCreateColError('Collection identifier must be at least 2 characters.');
      return;
    }
    if (!newColName.trim()) {
      setCreateColError('Collection name is required.');
      return;
    }

    setIsCreatingCol(true);
    try {
      await createNewCollection({
        id: trimmedId,
        name: newColName.trim(),
        description: newColDesc.trim() || undefined,
        policy_name: newColPolicy,
      });
      setNewColId('');
      setNewColName('');
      setNewColDesc('');
    } catch (err: unknown) {
      setCreateColError(err instanceof Error ? err.message : 'Failed to create collection.');
    } finally {
      setIsCreatingCol(false);
    }
  };

  // Handle Create Workspace
  const handleCreateWorkspaceSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setCreateWsError(null);

    if (!newWsName.trim()) {
      setCreateWsError('Workspace name is required.');
      return;
    }
    const slug = newWsSlug.trim().toLowerCase().replace(/[^a-z0-9_-]/g, '-') || `ws-${Date.now().toString(36)}`;

    setIsCreatingWs(true);
    try {
      await createNewWorkspace({
        name: newWsName.trim(),
        slug,
        description: newWsDesc.trim() || undefined,
      });
      setActiveTab('collections');
      setNewWsName('');
      setNewWsSlug('');
      setNewWsDesc('');
    } catch (err: unknown) {
      setCreateWsError(err instanceof Error ? err.message : 'Failed to create workspace.');
    } finally {
      setIsCreatingWs(false);
    }
  };

  // Handle Document Upload
  const handleUploadSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!token || !activeWorkspace || !selectedDocColId) return;

    if (!uploadFile) {
      setUploadError('Please select a PDF document to upload.');
      return;
    }
    if (!uploadFile.name.toLowerCase().endsWith('.pdf')) {
      setUploadError('Only PDF files (.pdf) are supported.');
      return;
    }
    if (uploadFile.size > 30 * 1024 * 1024) {
      setUploadError('File size exceeds the 30MB limit.');
      return;
    }

    setIsUploading(true);
    setUploadError(null);
    setUploadSuccess(null);

    try {
      const res = await uploadCollectionDocument(
        activeWorkspace.id,
        selectedDocColId,
        uploadFile,
        uploadTitle.trim() || undefined,
        uploadCircularNumber.trim() || undefined,
        token
      );
      setUploadSuccess(
        `Uploaded "${res.document_id}". Ingestion job queued (${res.job_id.slice(0, 8)}). Processing in background...`
      );
      setUploadFile(null);
      setUploadTitle('');
      setUploadCircularNumber('');
      if (fileInputRef.current) {
        fileInputRef.current.value = '';
      }
      await loadCollectionData(false);
    } catch (err: unknown) {
      setUploadError(err instanceof Error ? err.message : 'Upload failed.');
    } finally {
      setIsUploading(false);
    }
  };

  // Handle Document Download
  const handleDownloadDoc = async (doc: CollectionDocument) => {
    if (!token || !activeWorkspace || !selectedDocColId) return;
    setDownloadingDocId(doc.document_id);
    try {
      const blob = await downloadCollectionDocumentBlob(
        activeWorkspace.id,
        selectedDocColId,
        doc.document_id,
        token
      );
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${doc.document_id}.pdf`;
      document.body.appendChild(a);
      a.click();
      window.URL.revokeObjectURL(url);
      document.body.removeChild(a);
    } catch (err: unknown) {
      alert(err instanceof Error ? err.message : 'Failed to download document');
    } finally {
      setDownloadingDocId(null);
    }
  };

  // Status Badge Component
  const renderJobStatusBadge = (jobStatus: string) => {
    switch (jobStatus.toLowerCase()) {
      case 'completed':
        return (
          <span className="px-2 py-0.5 rounded text-[10px] font-semibold bg-emerald-950 text-emerald-400 border border-emerald-800/80">
            Completed
          </span>
        );
      case 'processing':
        return (
          <span className="px-2 py-0.5 rounded text-[10px] font-semibold bg-amber-950 text-amber-300 border border-amber-800/80 animate-pulse">
            Processing
          </span>
        );
      case 'queued':
        return (
          <span className="px-2 py-0.5 rounded text-[10px] font-semibold bg-cyan-950 text-cyan-300 border border-cyan-800/80">
            Queued
          </span>
        );
      case 'failed':
        return (
          <span className="px-2 py-0.5 rounded text-[10px] font-semibold bg-rose-950 text-rose-300 border border-rose-800/80">
            Failed
          </span>
        );
      case 'duplicate':
        return (
          <span className="px-2 py-0.5 rounded text-[10px] font-semibold bg-slate-800 text-slate-300 border border-slate-700">
            Duplicate
          </span>
        );
      default:
        return (
          <span className="px-2 py-0.5 rounded text-[10px] font-semibold bg-slate-900 text-slate-400 border border-slate-800">
            {jobStatus}
          </span>
        );
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-950/80 backdrop-blur-sm animate-fadeIn">
      <div
        className="w-full max-w-3xl bg-slate-900 border border-slate-800 rounded-2xl shadow-2xl p-6 flex flex-col gap-5 text-slate-100 max-h-[90vh] overflow-hidden"
        role="dialog"
        aria-modal="true"
        aria-labelledby="workspace-modal-title"
      >
        {/* Header */}
        <div className="flex items-start justify-between border-b border-slate-800 pb-4">
          <div>
            <div className="flex items-center gap-2.5">
              <h2 id="workspace-modal-title" className="text-lg font-semibold tracking-tight text-white">
                {activeWorkspace ? activeWorkspace.name : 'Workspaces & Collections'}
              </h2>
              {activeWorkspace?.current_user_role && (
                <span className="px-2 py-0.5 rounded text-[11px] font-medium bg-cyan-950 text-cyan-400 border border-cyan-800/80 capitalize">
                  {activeWorkspace.current_user_role}
                </span>
              )}
            </div>
            {activeWorkspace && (
              <p className="text-xs text-slate-400 mt-0.5">
                Slug: <code className="text-cyan-400 font-mono">{activeWorkspace.slug}</code>
                {activeWorkspace.description ? ` • ${activeWorkspace.description}` : ''}
              </p>
            )}
          </div>
          <button
            onClick={onClose}
            className="text-slate-400 hover:text-slate-200 transition p-1 rounded-lg cursor-pointer"
            aria-label="Close modal"
          >
            ✕
          </button>
        </div>

        {/* Tab Buttons */}
        <div className="flex items-center gap-2 border-b border-slate-800 pb-2">
          <button
            onClick={() => setActiveTab('collections')}
            className={`px-3 py-1.5 rounded-lg text-xs font-medium transition cursor-pointer ${
              activeTab === 'collections'
                ? 'bg-cyan-950 text-cyan-300 border border-cyan-800/80'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            Collections ({workspaceCollections.length})
          </button>
          <button
            onClick={() => setActiveTab('documents')}
            className={`px-3 py-1.5 rounded-lg text-xs font-medium transition cursor-pointer ${
              activeTab === 'documents'
                ? 'bg-cyan-950 text-cyan-300 border border-cyan-800/80'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            Documents & Ingestion
          </button>
          <button
            onClick={() => setActiveTab('members')}
            className={`px-3 py-1.5 rounded-lg text-xs font-medium transition cursor-pointer ${
              activeTab === 'members'
                ? 'bg-cyan-950 text-cyan-300 border border-cyan-800/80'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            Members
          </button>
          <button
            onClick={() => setActiveTab('create_workspace')}
            className={`px-3 py-1.5 rounded-lg text-xs font-medium transition cursor-pointer ${
              activeTab === 'create_workspace'
                ? 'bg-cyan-950 text-cyan-300 border border-cyan-800/80'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            + New Workspace
          </button>
        </div>

        {/* Tab Content Body */}
        <div className="flex-1 overflow-y-auto pr-1 flex flex-col gap-4 text-sm">
          {/* TAB 1: COLLECTIONS */}
          {activeTab === 'collections' && (
            <div className="flex flex-col gap-4">
              <div>
                <h3 className="text-xs font-semibold text-slate-300 uppercase tracking-wider mb-2">
                  Private Collections in Workspace
                </h3>
                {isCollectionsLoading ? (
                  <p className="text-xs text-slate-400 py-3">Loading collections...</p>
                ) : workspaceCollections.length === 0 ? (
                  <div className="p-4 bg-slate-950/60 border border-slate-800/80 rounded-xl text-center text-xs text-slate-400">
                    No private collections yet in this workspace. Create one below to upload documents.
                  </div>
                ) : (
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
                    {workspaceCollections.map((col) => (
                      <div
                        key={col.id}
                        className="p-3 bg-slate-950 border border-slate-800/90 rounded-xl flex flex-col gap-1"
                      >
                        <div className="flex items-center justify-between">
                          <span className="font-semibold text-slate-200 text-xs truncate">{col.name}</span>
                          <span className="text-[10px] font-mono text-cyan-400 bg-cyan-950/80 px-1.5 py-0.5 rounded border border-cyan-900/60">
                            {col.id}
                          </span>
                        </div>
                        {col.description && (
                          <p className="text-[11px] text-slate-400 line-clamp-2">{col.description}</p>
                        )}
                        <span className="text-[10px] text-slate-500 mt-1">
                          Policy: {col.policy_name}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              {/* Create Collection Form for Owner/Admin */}
              {canManage && (
                <div className="mt-2 pt-4 border-t border-slate-800/80">
                  <h3 className="text-xs font-semibold text-slate-300 uppercase tracking-wider mb-2">
                    Create New Private Collection
                  </h3>
                  {createColError && (
                    <div className="p-2.5 mb-3 bg-rose-950/70 border border-rose-800/80 rounded-lg text-rose-300 text-xs">
                      {createColError}
                    </div>
                  )}
                  <form onSubmit={handleCreateCollection} className="flex flex-col gap-3">
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                      <div>
                        <label className="text-[11px] text-slate-400 block mb-1">
                          Collection ID (unique, lowercase) *
                        </label>
                        <input
                          type="text"
                          placeholder="e.g. internal_playbooks"
                          required
                          value={newColId}
                          onChange={(e) => setNewColId(e.target.value)}
                          className="w-full px-3 py-1.5 bg-slate-950 border border-slate-800 rounded-lg text-xs text-slate-100 focus:ring-1 focus:ring-cyan-500"
                        />
                      </div>
                      <div>
                        <label className="text-[11px] text-slate-400 block mb-1">
                          Display Name *
                        </label>
                        <input
                          type="text"
                          placeholder="e.g. Internal Playbooks"
                          required
                          value={newColName}
                          onChange={(e) => setNewColName(e.target.value)}
                          className="w-full px-3 py-1.5 bg-slate-950 border border-slate-800 rounded-lg text-xs text-slate-100 focus:ring-1 focus:ring-cyan-500"
                        />
                      </div>
                    </div>
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                      <div>
                        <label className="text-[11px] text-slate-400 block mb-1">
                          Description (Optional)
                        </label>
                        <input
                          type="text"
                          placeholder="Brief summary of document corpus"
                          value={newColDesc}
                          onChange={(e) => setNewColDesc(e.target.value)}
                          className="w-full px-3 py-1.5 bg-slate-950 border border-slate-800 rounded-lg text-xs text-slate-100 focus:ring-1 focus:ring-cyan-500"
                        />
                      </div>
                      <div>
                        <label className="text-[11px] text-slate-400 block mb-1">
                          Grounding Policy
                        </label>
                        <select
                          value={newColPolicy}
                          onChange={(e) =>
                            setNewColPolicy(e.target.value as 'general_static' | 'sebi_cscrf_strict')
                          }
                          className="w-full px-3 py-1.5 bg-slate-950 border border-slate-800 rounded-lg text-xs text-slate-100 focus:ring-1 focus:ring-cyan-500"
                        >
                          <option value="general_static">General Static (Text Quotation)</option>
                          <option value="sebi_cscrf_strict">SEBI CSCRF Strict (Regulatory Graph)</option>
                        </select>
                      </div>
                    </div>
                    <button
                      type="submit"
                      disabled={isCreatingCol}
                      className="self-end px-4 py-2 bg-gradient-to-r from-teal-500 to-cyan-600 hover:from-teal-400 hover:to-cyan-500 text-slate-950 font-semibold rounded-lg text-xs transition cursor-pointer disabled:opacity-50"
                    >
                      {isCreatingCol ? 'Creating...' : 'Create Collection'}
                    </button>
                  </form>
                </div>
              )}
            </div>
          )}

          {/* TAB 2: DOCUMENTS & INGESTION */}
          {activeTab === 'documents' && (
            <div className="flex flex-col gap-5">
              {workspaceCollections.length === 0 ? (
                <div className="p-6 bg-slate-950/60 border border-slate-800/80 rounded-xl text-center text-xs text-slate-400 flex flex-col items-center gap-2">
                  <p>You need to create a private collection before uploading documents.</p>
                  <button
                    onClick={() => setActiveTab('collections')}
                    className="mt-2 px-3 py-1.5 bg-cyan-950 border border-cyan-800 text-cyan-300 rounded-lg text-xs font-medium hover:bg-cyan-900 transition cursor-pointer"
                  >
                    Go to Collections Tab
                  </button>
                </div>
              ) : (
                <>
                  {/* Collection Selector & Snapshot Info Header */}
                  <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 p-3 bg-slate-950 border border-slate-800 rounded-xl">
                    <div className="flex items-center gap-2">
                      <label htmlFor="doc-collection-select" className="text-xs font-medium text-slate-400">
                        Target Collection:
                      </label>
                      <select
                        id="doc-collection-select"
                        value={selectedDocColId}
                        onChange={(e) => setSelectedDocColId(e.target.value)}
                        className="bg-slate-900 border border-slate-700 rounded-lg px-2.5 py-1 text-xs text-slate-100 font-medium focus:outline-none focus:border-cyan-500 cursor-pointer"
                      >
                        {workspaceCollections.map((c) => (
                          <option key={c.id} value={c.id}>
                            {c.name} ({c.id})
                          </option>
                        ))}
                      </select>
                    </div>

                    <button
                      onClick={() => loadCollectionData(false)}
                      disabled={isJobsLoading || isDocsLoading}
                      className="px-2.5 py-1 text-xs bg-slate-900 hover:bg-slate-800 border border-slate-700 rounded-lg text-slate-300 hover:text-white transition cursor-pointer flex items-center gap-1 self-start sm:self-auto"
                      title="Refresh jobs and documents"
                    >
                      <span className={isJobsLoading || isDocsLoading ? 'animate-spin' : ''}>🔄</span>
                      <span>Refresh</span>
                    </button>
                  </div>

                  {/* Readiness notice */}
                  <div className="p-3 bg-cyan-950/30 border border-cyan-900/50 rounded-xl text-xs text-cyan-300/90 flex items-start gap-2.5">
                    <span className="text-cyan-400 text-sm mt-0.5">ℹ️</span>
                    <div>
                      <p className="font-medium text-cyan-200">End-to-End Ingestion & Snapshot Indexing</p>
                      <p className="text-[11px] text-cyan-300/80 mt-0.5">
                        Uploaded documents are parsed, chunked, and embedded by the worker. After chunking, an index snapshot is generated and published automatically before the collection can be queried.
                      </p>
                    </div>
                  </div>

                  {/* SECTION 1: Document Upload */}
                  <div className="p-4 bg-slate-950/90 border border-slate-800 rounded-xl flex flex-col gap-3">
                    <h3 className="text-xs font-semibold text-slate-300 uppercase tracking-wider">
                      Upload Document (PDF)
                    </h3>

                    {!canUpload ? (
                      <div className="p-3 bg-slate-900 border border-slate-800 rounded-lg text-xs text-slate-400">
                        Viewers have read-only access and cannot upload documents to this workspace.
                      </div>
                    ) : (
                      <>
                        {uploadError && (
                          <div className="p-2.5 bg-rose-950/70 border border-rose-800/80 rounded-lg text-rose-300 text-xs">
                            {uploadError}
                          </div>
                        )}
                        {uploadSuccess && (
                          <div className="p-2.5 bg-emerald-950/70 border border-emerald-800/80 rounded-lg text-emerald-300 text-xs">
                            {uploadSuccess}
                          </div>
                        )}

                        <form onSubmit={handleUploadSubmit} className="flex flex-col gap-3">
                          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                            <div>
                              <label className="text-[11px] text-slate-400 block mb-1">
                                Document Title (Optional)
                              </label>
                              <input
                                type="text"
                                placeholder="e.g. Master Operational Resilience Guidelines"
                                value={uploadTitle}
                                onChange={(e) => setUploadTitle(e.target.value)}
                                className="w-full px-3 py-1.5 bg-slate-900 border border-slate-800 rounded-lg text-xs text-slate-100 focus:ring-1 focus:ring-cyan-500"
                              />
                            </div>
                            <div>
                              <label className="text-[11px] text-slate-400 block mb-1">
                                Circular / Reference Number (Optional)
                              </label>
                              <input
                                type="text"
                                placeholder="e.g. SEBI/HO/MRD/2026/01"
                                value={uploadCircularNumber}
                                onChange={(e) => setUploadCircularNumber(e.target.value)}
                                className="w-full px-3 py-1.5 bg-slate-900 border border-slate-800 rounded-lg text-xs text-slate-100 focus:ring-1 focus:ring-cyan-500"
                              />
                            </div>
                          </div>

                          <div className="flex flex-col sm:flex-row items-stretch sm:items-center justify-between gap-3 pt-1">
                            <div>
                              <input
                                ref={fileInputRef}
                                type="file"
                                accept=".pdf,application/pdf"
                                required
                                onChange={(e) => {
                                  if (e.target.files && e.target.files[0]) {
                                    setUploadFile(e.target.files[0]);
                                    if (!uploadTitle.trim()) {
                                      // default title to clean file name
                                      const baseName = e.target.files[0].name.replace(/\.[^/.]+$/, '');
                                      setUploadTitle(baseName);
                                    }
                                  }
                                }}
                                className="text-xs text-slate-400 file:mr-3 file:py-1.5 file:px-3 file:rounded-lg file:border-0 file:text-xs file:font-semibold file:bg-cyan-950 file:text-cyan-300 hover:file:bg-cyan-900 cursor-pointer"
                              />
                              <p className="text-[10px] text-slate-500 mt-1">PDF format only • Maximum 30MB</p>
                            </div>

                            <button
                              type="submit"
                              disabled={isUploading || !uploadFile}
                              className="px-4 py-2 bg-gradient-to-r from-teal-500 to-cyan-600 hover:from-teal-400 hover:to-cyan-500 text-slate-950 font-semibold rounded-lg text-xs transition cursor-pointer disabled:opacity-50 self-end sm:self-auto"
                            >
                              {isUploading ? 'Uploading...' : 'Upload & Queue Job'}
                            </button>
                          </div>
                        </form>
                      </>
                    )}
                  </div>

                  {/* SECTION 2: Ingestion Jobs Pipeline */}
                  <div className="flex flex-col gap-2">
                    <div className="flex items-center justify-between">
                      <h3 className="text-xs font-semibold text-slate-300 uppercase tracking-wider">
                        Ingestion Pipeline Jobs ({jobs.length})
                      </h3>
                      {jobs.some((j) => j.status === 'queued' || j.status === 'processing') && (
                        <span className="text-[11px] text-amber-400 animate-pulse flex items-center gap-1 font-mono">
                          <span className="w-1.5 h-1.5 rounded-full bg-amber-400"></span>
                          Worker active (polling)
                        </span>
                      )}
                    </div>

                    {jobsError && (
                      <div className="p-2.5 bg-rose-950/70 border border-rose-800/80 rounded-lg text-rose-300 text-xs">
                        {jobsError}
                      </div>
                    )}

                    {isJobsLoading && jobs.length === 0 ? (
                      <p className="text-xs text-slate-400 py-3">Loading jobs...</p>
                    ) : jobs.length === 0 ? (
                      <div className="p-3 bg-slate-950 border border-slate-800/80 rounded-xl text-center text-xs text-slate-400">
                        No ingestion jobs for this collection yet.
                      </div>
                    ) : (
                      <div className="border border-slate-800 rounded-xl overflow-hidden bg-slate-950 divide-y divide-slate-800/80 max-h-56 overflow-y-auto">
                        {jobs.map((job) => (
                          <div key={job.job_id} className="p-3 flex flex-col gap-1.5 text-xs">
                            <div className="flex items-center justify-between gap-2">
                              <div className="flex items-center gap-2 truncate">
                                <span className="font-mono text-[11px] text-slate-400">
                                  {job.job_id.slice(0, 8)}...
                                </span>
                                <span className="font-medium text-slate-200 truncate">
                                  Doc: {job.document_id}
                                </span>
                              </div>
                              <div className="flex items-center gap-2 shrink-0">
                                <span className="font-mono text-[10px] text-slate-400 bg-slate-900 px-1.5 py-0.5 rounded border border-slate-800">
                                  {job.stage}
                                </span>
                                {renderJobStatusBadge(job.status)}
                              </div>
                            </div>
                            {job.error_message && (
                              <div className="text-[11px] text-rose-400 bg-rose-950/40 p-2 rounded border border-rose-900/60 mt-0.5 font-mono break-all">
                                Error: {job.error_message}
                              </div>
                            )}
                            <div className="flex items-center justify-between text-[10px] text-slate-500">
                              <span>Created: {new Date(job.created_at).toLocaleString()}</span>
                              {job.completed_at && (
                                <span>Completed: {new Date(job.completed_at).toLocaleTimeString()}</span>
                              )}
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>

                  {/* SECTION 3: Processed Documents in Collection */}
                  <div className="flex flex-col gap-2 pt-2 border-t border-slate-800/80">
                    <h3 className="text-xs font-semibold text-slate-300 uppercase tracking-wider">
                      Collection Documents ({documents.length})
                    </h3>

                    {docsError && (
                      <div className="p-2.5 bg-rose-950/70 border border-rose-800/80 rounded-lg text-rose-300 text-xs">
                        {docsError}
                      </div>
                    )}

                    {isDocsLoading && documents.length === 0 ? (
                      <p className="text-xs text-slate-400 py-3">Loading documents...</p>
                    ) : documents.length === 0 ? (
                      <div className="p-3 bg-slate-950 border border-slate-800/80 rounded-xl text-center text-xs text-slate-400">
                        No processed documents found in this collection.
                      </div>
                    ) : (
                      <div className="border border-slate-800 rounded-xl overflow-hidden bg-slate-950 divide-y divide-slate-800/80 max-h-56 overflow-y-auto">
                        {documents.map((doc) => (
                          <div
                            key={doc.document_id}
                            className="p-3 flex items-center justify-between gap-3 text-xs"
                          >
                            <div className="flex flex-col truncate">
                              <span className="font-semibold text-slate-200 truncate">{doc.title}</span>
                              <div className="flex items-center gap-2 text-[10px] text-slate-400 mt-0.5 font-mono">
                                <span>ID: {doc.document_id}</span>
                                {doc.circular_number && <span>• Ref: {doc.circular_number}</span>}
                              </div>
                            </div>

                            <div className="flex items-center gap-2 shrink-0">
                              {doc.is_published ? (
                                <span className="px-2 py-0.5 rounded text-[10px] font-semibold bg-emerald-950 text-emerald-400 border border-emerald-800/80">
                                  Published
                                </span>
                              ) : (
                                <span className="px-2 py-0.5 rounded text-[10px] font-semibold bg-slate-800 text-slate-400 border border-slate-700">
                                  Unpublished
                                </span>
                              )}

                              <button
                                onClick={() => handleDownloadDoc(doc)}
                                disabled={downloadingDocId === doc.document_id}
                                className="px-2.5 py-1 bg-slate-900 hover:bg-slate-800 border border-slate-700 rounded-lg text-cyan-300 text-[11px] font-medium transition cursor-pointer disabled:opacity-50 flex items-center gap-1"
                                title="Download source PDF"
                              >
                                {downloadingDocId === doc.document_id ? (
                                  <span className="animate-spin">⏳</span>
                                ) : (
                                  <span>📥</span>
                                )}
                                <span>PDF</span>
                              </button>
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                </>
              )}
            </div>
          )}

          {/* TAB 3: MEMBERS */}
          {activeTab === 'members' && (
            <div className="flex flex-col gap-4">
              <div>
                <h3 className="text-xs font-semibold text-slate-300 uppercase tracking-wider mb-2">
                  Workspace Members
                </h3>
                {isMembersLoading ? (
                  <p className="text-xs text-slate-400 py-3">Loading members...</p>
                ) : membersError ? (
                  <div className="p-3 bg-rose-950/70 border border-rose-800/80 rounded-lg text-rose-300 text-xs">
                    {membersError}
                  </div>
                ) : members.length === 0 ? (
                  <p className="text-xs text-slate-400">No members found.</p>
                ) : (
                  <div className="divide-y divide-slate-800/80 border border-slate-800 rounded-xl overflow-hidden bg-slate-950">
                    {members.map((mem) => {
                      const isCurrentUser = mem.user_id === user?.id;
                      const isOwnerRole = mem.role === 'owner';
                      const canDeleteThisMember = canManage && !isOwnerRole && !isCurrentUser;

                      return (
                        <div
                          key={mem.id}
                          className="p-3 flex items-center justify-between gap-3 text-xs"
                        >
                          <div className="flex flex-col">
                            <span className="font-medium text-slate-200">
                              {mem.user_full_name || mem.user_email || mem.user_id}
                              {isCurrentUser && (
                                <span className="ml-1.5 text-[10px] text-cyan-400">(You)</span>
                              )}
                            </span>
                            {mem.user_full_name && mem.user_email && (
                              <span className="text-[11px] text-slate-400">{mem.user_email}</span>
                            )}
                          </div>
                          <div className="flex items-center gap-3">
                            <span className="px-2 py-0.5 rounded text-[10px] font-semibold uppercase tracking-wider bg-slate-900 border border-slate-800 text-slate-300">
                              {mem.role}
                            </span>
                            {canDeleteThisMember && (
                              <button
                                onClick={() => handleRemoveMember(mem.user_id, mem.user_email)}
                                className="text-rose-400 hover:text-rose-300 text-xs cursor-pointer"
                                title="Remove member"
                              >
                                Remove
                              </button>
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>

              {/* Invite Member form for Owner/Admin */}
              {canManage && (
                <div className="mt-2 pt-4 border-t border-slate-800/80">
                  <h3 className="text-xs font-semibold text-slate-300 uppercase tracking-wider mb-2">
                    Invite Member by Email
                  </h3>
                  {addMemberError && (
                    <div className="p-2.5 mb-3 bg-rose-950/70 border border-rose-800/80 rounded-lg text-rose-300 text-xs">
                      {addMemberError}
                    </div>
                  )}
                  <form onSubmit={handleAddMember} className="flex flex-col sm:flex-row items-stretch sm:items-end gap-2.5">
                    <div className="flex-1">
                      <label className="text-[11px] text-slate-400 block mb-1">
                        Registered User Email *
                      </label>
                      <input
                        type="email"
                        placeholder="colleague@example.com"
                        required
                        value={newMemberEmail}
                        onChange={(e) => setNewMemberEmail(e.target.value)}
                        className="w-full px-3 py-1.5 bg-slate-950 border border-slate-800 rounded-lg text-xs text-slate-100 focus:ring-1 focus:ring-cyan-500"
                      />
                    </div>
                    <div className="w-full sm:w-36">
                      <label className="text-[11px] text-slate-400 block mb-1">Role</label>
                      <select
                        value={newMemberRole}
                        onChange={(e) => setNewMemberRole(e.target.value as WorkspaceRole)}
                        className="w-full px-3 py-1.5 bg-slate-950 border border-slate-800 rounded-lg text-xs text-slate-100 focus:ring-1 focus:ring-cyan-500"
                      >
                        <option value="member">Member</option>
                        <option value="viewer">Viewer</option>
                        <option value="admin">Admin</option>
                      </select>
                    </div>
                    <button
                      type="submit"
                      disabled={isAddingMember}
                      className="px-4 py-2 bg-gradient-to-r from-teal-500 to-cyan-600 hover:from-teal-400 hover:to-cyan-500 text-slate-950 font-semibold rounded-lg text-xs transition cursor-pointer disabled:opacity-50"
                    >
                      {isAddingMember ? 'Adding...' : 'Add Member'}
                    </button>
                  </form>
                </div>
              )}
            </div>
          )}

          {/* TAB 4: CREATE WORKSPACE */}
          {activeTab === 'create_workspace' && (
            <div className="flex flex-col gap-3">
              <h3 className="text-xs font-semibold text-slate-300 uppercase tracking-wider">
                Create New Workspace
              </h3>
              {createWsError && (
                <div className="p-2.5 bg-rose-950/70 border border-rose-800/80 rounded-lg text-rose-300 text-xs">
                  {createWsError}
                </div>
              )}
              <form onSubmit={handleCreateWorkspaceSubmit} className="flex flex-col gap-3">
                <div>
                  <label className="text-[11px] text-slate-400 block mb-1">
                    Workspace Name *
                  </label>
                  <input
                    type="text"
                    placeholder="e.g. Risk & Compliance Team"
                    required
                    value={newWsName}
                    onChange={(e) => setNewWsName(e.target.value)}
                    className="w-full px-3 py-2 bg-slate-950 border border-slate-800 rounded-xl text-xs text-slate-100 focus:ring-1 focus:ring-cyan-500"
                  />
                </div>
                <div>
                  <label className="text-[11px] text-slate-400 block mb-1">
                    Slug (URL identifier, optional)
                  </label>
                  <input
                    type="text"
                    placeholder="e.g. risk-compliance"
                    value={newWsSlug}
                    onChange={(e) => setNewWsSlug(e.target.value)}
                    className="w-full px-3 py-2 bg-slate-950 border border-slate-800 rounded-xl text-xs text-slate-100 focus:ring-1 focus:ring-cyan-500"
                  />
                </div>
                <div>
                  <label className="text-[11px] text-slate-400 block mb-1">
                    Description (Optional)
                  </label>
                  <textarea
                    rows={2}
                    placeholder="What is this workspace used for?"
                    value={newWsDesc}
                    onChange={(e) => setNewWsDesc(e.target.value)}
                    className="w-full px-3 py-2 bg-slate-950 border border-slate-800 rounded-xl text-xs text-slate-100 focus:ring-1 focus:ring-cyan-500"
                  />
                </div>
                <button
                  type="submit"
                  disabled={isCreatingWs}
                  className="self-end px-5 py-2.5 bg-gradient-to-r from-teal-500 to-cyan-600 hover:from-teal-400 hover:to-cyan-500 text-slate-950 font-semibold rounded-xl text-xs transition cursor-pointer disabled:opacity-50"
                >
                  {isCreatingWs ? 'Creating Workspace...' : 'Create Workspace'}
                </button>
              </form>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
