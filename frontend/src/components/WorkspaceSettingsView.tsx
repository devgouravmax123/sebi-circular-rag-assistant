import { useState, useEffect, type FormEvent } from 'react';
import { useAuth } from '../context/AuthContext';
import { useWorkspace } from '../context/WorkspaceContext';
import type { WorkspaceRole, WorkspaceMember } from '../types/workspace';
import { fetchWorkspaceMembers, addWorkspaceMember, removeWorkspaceMember } from '../api/client';

interface WorkspaceSettingsViewProps {
  onOpenAuthModal?: (mode: 'login' | 'signup') => void;
}

export function WorkspaceSettingsView({ onOpenAuthModal }: WorkspaceSettingsViewProps) {
  const { user, token, isAuthenticated } = useAuth();
  const {
    workspaces,
    activeWorkspace,
    selectWorkspace,
    createNewWorkspace,
    isWorkspacesLoading,
  } = useWorkspace();

  // Members state
  const [members, setMembers] = useState<WorkspaceMember[]>([]);
  const [isMembersLoading, setIsMembersLoading] = useState(false);
  const [membersError, setMembersError] = useState<string | null>(null);

  // Add Member state
  const [newMemberEmail, setNewMemberEmail] = useState('');
  const [newMemberRole, setNewMemberRole] = useState<WorkspaceRole>('member');
  const [isAddingMember, setIsAddingMember] = useState(false);
  const [addMemberError, setAddMemberError] = useState<string | null>(null);
  const [addMemberSuccess, setAddMemberSuccess] = useState<string | null>(null);

  // Create Workspace state
  const [newWsName, setNewWsName] = useState('');
  const [newWsSlug, setNewWsSlug] = useState('');
  const [newWsDesc, setNewWsDesc] = useState('');
  const [isCreatingWs, setIsCreatingWs] = useState(false);
  const [createWsError, setCreateWsError] = useState<string | null>(null);
  const [createWsSuccess, setCreateWsSuccess] = useState<string | null>(null);

  const userRole: WorkspaceRole = activeWorkspace?.current_user_role || 'member';
  const canManage = userRole === 'owner' || userRole === 'admin';

  // Load members for active workspace
  const loadMembers = async () => {
    if (!token || !activeWorkspace) return;
    setIsMembersLoading(true);
    setMembersError(null);
    try {
      const res = await fetchWorkspaceMembers(activeWorkspace.id, token);
      setMembers(res);
    } catch (err) {
      setMembersError(err instanceof Error ? err.message : 'Failed to load workspace members.');
    } finally {
      setIsMembersLoading(false);
    }
  };

  useEffect(() => {
    if (isAuthenticated && token && activeWorkspace) {
      loadMembers();
    } else {
      setMembers([]);
    }
  }, [isAuthenticated, token, activeWorkspace?.id]);

  // Handler: Add Member
  const handleAddMember = async (e: FormEvent) => {
    e.preventDefault();
    if (!newMemberEmail.trim()) {
      setAddMemberError('Email address is required.');
      return;
    }
    if (!activeWorkspace || !token) return;

    setIsAddingMember(true);
    setAddMemberError(null);
    setAddMemberSuccess(null);

    try {
      const added = await addWorkspaceMember(
        activeWorkspace.id,
        {
          email: newMemberEmail.trim(),
          role: newMemberRole,
        },
        token
      );
      setAddMemberSuccess(`Successfully added ${added.user_email || newMemberEmail.trim()} as ${added.role}!`);
      setNewMemberEmail('');
      loadMembers();
    } catch (err) {
      setAddMemberError(err instanceof Error ? err.message : 'Failed to add member.');
    } finally {
      setIsAddingMember(false);
    }
  };

  // Handler: Remove Member
  const handleRemoveMember = async (memberUserId: string, memberEmail: string) => {
    if (!activeWorkspace || !token) return;
    if (!confirm(`Are you sure you want to remove ${memberEmail} from this workspace?`)) return;

    try {
      await removeWorkspaceMember(activeWorkspace.id, memberUserId, token);
      loadMembers();
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to remove member.');
    }
  };

  // Handler: Create Workspace
  const handleCreateWorkspace = async (e: FormEvent) => {
    e.preventDefault();
    if (!newWsName.trim()) {
      setCreateWsError('Workspace name is required.');
      return;
    }

    setIsCreatingWs(true);
    setCreateWsError(null);
    setCreateWsSuccess(null);

    const generatedSlug = newWsName.trim().toLowerCase().replace(/[^a-z0-9-_]/g, '-');
    try {
      const created = await createNewWorkspace({
        name: newWsName.trim(),
        slug: newWsSlug.trim() || generatedSlug,
        description: newWsDesc.trim() || undefined,
      });
      setCreateWsSuccess(`Workspace "${created.name}" created successfully!`);
      setNewWsName('');
      setNewWsSlug('');
      setNewWsDesc('');
    } catch (err) {
      setCreateWsError(err instanceof Error ? err.message : 'Failed to create workspace.');
    } finally {
      setIsCreatingWs(false);
    }
  };

  return (
    <div className="space-y-8 animate-fadeIn">
      {/* Header section */}
      <section className="border-b border-slate-800/80 pb-6">
        <div>
          <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-amber-950/80 border border-amber-800/60 text-xs font-medium text-amber-400 mb-2">
            <span>🏢</span>
            <span>Multi-Tenant Administration</span>
          </div>
          <h1 className="text-2xl sm:text-3xl font-bold tracking-tight text-white">
            Workspace Settings &amp; RBAC Team Management
          </h1>
          <p className="text-sm text-slate-300 mt-1 max-w-3xl leading-relaxed">
            Manage organization workspaces, team memberships, and role-based permissions
            (Owner, Admin, Member, Viewer). Configure isolated tenant environments for research.
          </p>
        </div>
      </section>

      {/* Unauthenticated Alert */}
      {!isAuthenticated && (
        <div className="rounded-xl border border-amber-900/60 bg-amber-950/30 p-6 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
          <div className="space-y-1">
            <h3 className="text-sm font-semibold text-amber-300 flex items-center gap-2">
              <span>🔒</span>
              <span>Sign In Required for Workspace Management</span>
            </h3>
            <p className="text-xs text-slate-400">
              Workspaces provide isolated tenant contexts with cryptographic role protection. Sign in or register to configure your workspace.
            </p>
          </div>
          {onOpenAuthModal && (
            <div className="flex items-center gap-2 shrink-0">
              <button
                onClick={() => onOpenAuthModal('login')}
                className="px-4 py-2 text-xs font-semibold text-slate-950 bg-amber-400 hover:bg-amber-300 rounded-lg transition cursor-pointer"
              >
                Sign In
              </button>
            </div>
          )}
        </div>
      )}

      {/* Main Content Grid */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-8">
        {/* Left Column: Workspace Info & Workspace Switcher (5 cols) */}
          <div className="lg:col-span-5 space-y-6">
            {/* Active Workspace Card */}
            <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-5 space-y-4">
              <div className="flex items-center justify-between">
                <h2 className="text-base font-semibold text-white flex items-center gap-2">
                  <span>🏢</span>
                  <span>Active Workspace</span>
                </h2>
                <span className="px-2 py-0.5 rounded text-[10px] font-semibold uppercase tracking-wider bg-cyan-950/80 text-cyan-400 border border-cyan-800/60">
                  {userRole}
                </span>
              </div>

              {activeWorkspace ? (
                <div className="space-y-2 text-xs text-slate-300 bg-slate-950/60 p-4 rounded-lg border border-slate-800/80">
                  <div>
                    <span className="text-[11px] text-slate-400 uppercase tracking-wider block">Name</span>
                    <span className="font-semibold text-white text-sm">{activeWorkspace.name}</span>
                  </div>
                  <div>
                    <span className="text-[11px] text-slate-400 uppercase tracking-wider block">Slug</span>
                    <span className="font-mono text-cyan-400">{activeWorkspace.slug || 'N/A'}</span>
                  </div>
                  {activeWorkspace.description && (
                    <div>
                      <span className="text-[11px] text-slate-400 uppercase tracking-wider block">Description</span>
                      <p className="text-slate-300 leading-relaxed">{activeWorkspace.description}</p>
                    </div>
                  )}
                  <div className="pt-2 border-t border-slate-800/80">
                    <span className="text-[11px] text-slate-400 uppercase tracking-wider block">Workspace ID</span>
                    <span className="font-mono text-[11px] text-slate-500 break-all">{activeWorkspace.id}</span>
                  </div>
                </div>
              ) : (
                <div className="p-4 text-center text-xs text-slate-400">
                  No active workspace selected.
                </div>
              )}

              {/* Workspace Switcher */}
              <div className="space-y-1.5">
                <label className="block text-xs font-medium text-slate-300">
                  Switch Workspace
                </label>
                <select
                  value={activeWorkspace?.id || ''}
                  onChange={(e) => selectWorkspace(e.target.value)}
                  disabled={isWorkspacesLoading}
                  className="w-full px-3 py-2 bg-slate-950 border border-slate-700 rounded-lg text-xs text-white focus:border-cyan-500 focus:outline-none"
                >
                  {workspaces.map((ws) => (
                    <option key={ws.id} value={ws.id}>
                      {ws.name} ({ws.current_user_role})
                    </option>
                  ))}
                </select>
              </div>
            </div>

            {/* Create New Workspace */}
            <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-5 space-y-4">
              <h2 className="text-base font-semibold text-white flex items-center gap-2">
                <span>➕</span>
                <span>Create New Workspace</span>
              </h2>

              {createWsError && (
                <div className="p-3 bg-rose-950/60 border border-rose-800/80 rounded-lg text-xs text-rose-300">
                  {createWsError}
                </div>
              )}

              {createWsSuccess && (
                <div className="p-3 bg-emerald-950/60 border border-emerald-800/80 rounded-lg text-xs text-emerald-300">
                  {createWsSuccess}
                </div>
              )}

              <form onSubmit={handleCreateWorkspace} className="space-y-3">
                <div>
                  <label className="block text-xs font-medium text-slate-300 mb-1">
                    Workspace Name *
                  </label>
                  <input
                    type="text"
                    required
                    value={newWsName}
                    onChange={(e) => setNewWsName(e.target.value)}
                    placeholder="e.g. Legal Research Team"
                    className="w-full px-3 py-1.5 bg-slate-950 border border-slate-700 rounded-lg text-xs text-white focus:border-cyan-500 focus:outline-none"
                  />
                </div>

                <div>
                  <label className="block text-xs font-medium text-slate-300 mb-1">
                    Workspace Slug (Optional)
                  </label>
                  <input
                    type="text"
                    value={newWsSlug}
                    onChange={(e) => setNewWsSlug(e.target.value.toLowerCase().replace(/[^a-z0-9-_]/g, '-'))}
                    placeholder="e.g. legal-research"
                    className="w-full px-3 py-1.5 bg-slate-950 border border-slate-700 rounded-lg text-xs text-white font-mono focus:border-cyan-500 focus:outline-none"
                  />
                </div>

                <div>
                  <label className="block text-xs font-medium text-slate-300 mb-1">
                    Description
                  </label>
                  <textarea
                    rows={2}
                    value={newWsDesc}
                    onChange={(e) => setNewWsDesc(e.target.value)}
                    placeholder="Team or department purpose..."
                    className="w-full px-3 py-1.5 bg-slate-950 border border-slate-700 rounded-lg text-xs text-white focus:border-cyan-500 focus:outline-none"
                  />
                </div>

                <button
                  type="submit"
                  disabled={isCreatingWs}
                  className="w-full py-2 bg-gradient-to-r from-teal-500 to-cyan-600 hover:from-teal-400 hover:to-cyan-500 disabled:opacity-50 text-slate-950 font-semibold text-xs rounded-lg transition shadow cursor-pointer"
                >
                  {isCreatingWs ? 'Creating Workspace...' : 'Create Workspace'}
                </button>
              </form>
            </div>
          </div>

          {/* Right Column: Members & RBAC Controls (7 cols) */}
          <div className="lg:col-span-7 space-y-6">
            {/* Members Table */}
            <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-5 space-y-4">
              <div className="flex items-center justify-between">
                <h2 className="text-base font-semibold text-white flex items-center gap-2">
                  <span>👥</span>
                  <span>Workspace Members &amp; Permissions</span>
                </h2>
                <span className="text-xs text-slate-400">
                  {members.length} members
                </span>
              </div>

              {membersError && (
                <div className="p-3 bg-rose-950/60 border border-rose-800/80 rounded-lg text-xs text-rose-300">
                  {membersError}
                </div>
              )}

              {isMembersLoading ? (
                <div className="p-4 text-center text-xs text-slate-400 animate-pulse">
                  Loading workspace members...
                </div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-xs text-slate-300">
                    <thead className="border-b border-slate-800 text-[11px] text-slate-400 uppercase font-medium">
                      <tr>
                        <th className="py-2 pr-4">Member</th>
                        <th className="py-2 pr-4">Role</th>
                        <th className="py-2 pr-4">Joined</th>
                        {canManage && <th className="py-2 text-right">Actions</th>}
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-800/60">
                      {members.map((mem) => {
                        const isSelf = mem.user_id === user?.id;
                        let roleBadge = 'bg-slate-800 text-slate-300 border-slate-700';
                        if (mem.role === 'owner') roleBadge = 'bg-amber-950/80 text-amber-300 border-amber-800/80';
                        else if (mem.role === 'admin') roleBadge = 'bg-cyan-950/80 text-cyan-300 border-cyan-800/80';
                        else if (mem.role === 'member') roleBadge = 'bg-teal-950/80 text-teal-300 border-teal-800/80';

                        return (
                          <tr key={mem.user_id} className="hover:bg-slate-900/40">
                            <td className="py-2.5 pr-4">
                              <div className="font-medium text-white flex items-center gap-1.5">
                                <span>{mem.user_full_name || mem.user_email || 'Member'}</span>
                                {isSelf && (
                                  <span className="text-[10px] text-cyan-400 font-mono">(you)</span>
                                )}
                              </div>
                              <div className="text-[11px] text-slate-400 font-mono">{mem.user_email || 'No email provided'}</div>
                            </td>
                            <td className="py-2.5 pr-4">
                              <span className={`px-2 py-0.5 rounded text-[10px] font-semibold uppercase border ${roleBadge}`}>
                                {mem.role}
                              </span>
                            </td>
                            <td className="py-2.5 pr-4 text-slate-400 text-[11px]">
                              {new Date(mem.created_at).toLocaleDateString()}
                            </td>
                            {canManage && (
                              <td className="py-2.5 text-right">
                                {!isSelf && mem.role !== 'owner' && (
                                  <button
                                    type="button"
                                    onClick={() => handleRemoveMember(mem.user_id, mem.user_email || 'member')}
                                    className="px-2 py-0.5 text-[11px] text-rose-400 hover:text-white hover:bg-rose-950/80 rounded transition cursor-pointer"
                                  >
                                    Remove
                                  </button>
                                )}
                              </td>
                            )}
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </div>

            {/* Add Member Form */}
            {canManage ? (
              <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-5 space-y-4">
                <h3 className="text-sm font-semibold text-white flex items-center gap-2">
                  <span>✉️</span>
                  <span>Invite Team Member</span>
                </h3>

                {addMemberError && (
                  <div className="p-3 bg-rose-950/60 border border-rose-800/80 rounded-lg text-xs text-rose-300">
                    {addMemberError}
                  </div>
                )}

                {addMemberSuccess && (
                  <div className="p-3 bg-emerald-950/60 border border-emerald-800/80 rounded-lg text-xs text-emerald-300">
                    {addMemberSuccess}
                  </div>
                )}

                <form onSubmit={handleAddMember} className="grid grid-cols-1 sm:grid-cols-12 gap-3 items-end">
                  <div className="sm:col-span-6">
                    <label className="block text-xs font-medium text-slate-300 mb-1">
                      Member Email *
                    </label>
                    <input
                      type="email"
                      required
                      value={newMemberEmail}
                      onChange={(e) => setNewMemberEmail(e.target.value)}
                      placeholder="colleague@organization.com"
                      className="w-full px-3 py-1.5 bg-slate-950 border border-slate-700 rounded-lg text-xs text-white focus:border-cyan-500 focus:outline-none"
                    />
                  </div>

                  <div className="sm:col-span-3">
                    <label className="block text-xs font-medium text-slate-300 mb-1">
                      Role
                    </label>
                    <select
                      value={newMemberRole}
                      onChange={(e) => setNewMemberRole(e.target.value as WorkspaceRole)}
                      className="w-full px-3 py-1.5 bg-slate-950 border border-slate-700 rounded-lg text-xs text-white focus:border-cyan-500 focus:outline-none"
                    >
                      <option value="admin">Admin</option>
                      <option value="member">Member</option>
                      <option value="viewer">Viewer</option>
                    </select>
                  </div>

                  <div className="sm:col-span-3">
                    <button
                      type="submit"
                      disabled={isAddingMember}
                      className="w-full py-1.5 bg-cyan-600 hover:bg-cyan-500 disabled:opacity-50 text-white font-semibold text-xs rounded-lg transition shadow cursor-pointer"
                    >
                      {isAddingMember ? 'Inviting...' : 'Add Member'}
                    </button>
                  </div>
                </form>
              </div>
            ) : (
              <div className="bg-slate-900/40 border border-slate-800 rounded-xl p-4 text-xs text-slate-400">
                🔒 You have role <strong className="text-slate-200 uppercase">{userRole}</strong>. Only Workspace Owners and Admins can invite new team members or modify roles.
              </div>
            )}

            {/* RBAC Role Guide */}
            <div className="bg-slate-900/40 border border-slate-800 rounded-xl p-5 space-y-3">
              <h3 className="text-xs font-semibold text-slate-300 uppercase tracking-wider flex items-center gap-1.5">
                <span>🛡️</span>
                <span>Role-Based Access Control (RBAC) Reference</span>
              </h3>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-xs">
                <div className="p-2.5 rounded-lg bg-slate-950/60 border border-slate-800/80">
                  <div className="font-semibold text-amber-300">Owner</div>
                  <p className="text-slate-400 text-[11px] mt-0.5">
                    Complete administrative authority. Can manage workspaces, collections, memberships, and billing.
                  </p>
                </div>
                <div className="p-2.5 rounded-lg bg-slate-950/60 border border-slate-800/80">
                  <div className="font-semibold text-cyan-300">Admin</div>
                  <p className="text-slate-400 text-[11px] mt-0.5">
                    Can create collections, upload PDFs, invite members, and configure workspace settings.
                  </p>
                </div>
                <div className="p-2.5 rounded-lg bg-slate-950/60 border border-slate-800/80">
                  <div className="font-semibold text-teal-300">Member</div>
                  <p className="text-slate-400 text-[11px] mt-0.5">
                    Can create collections, upload PDF documents, and execute natural-language queries.
                  </p>
                </div>
                <div className="p-2.5 rounded-lg bg-slate-950/60 border border-slate-800/80">
                  <div className="font-semibold text-slate-300">Viewer</div>
                  <p className="text-slate-400 text-[11px] mt-0.5">
                    Read-only permissions. Can search indexed collections and inspect verified citations.
                  </p>
                </div>
              </div>
            </div>
          </div>
        </div>
    </div>
  );
}
