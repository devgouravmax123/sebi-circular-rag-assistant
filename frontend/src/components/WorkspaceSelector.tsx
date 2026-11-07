import { useState, useRef, useEffect } from 'react';
import { useAuth } from '../context/AuthContext';
import { useWorkspace } from '../context/WorkspaceContext';
import { WorkspaceManageModal } from './WorkspaceManageModal';

export function WorkspaceSelector() {
  const { isAuthenticated } = useAuth();
  const { workspaces, activeWorkspace, selectWorkspace, isWorkspacesLoading } = useWorkspace();
  const [isOpen, setIsOpen] = useState(false);
  const [modalTab, setModalTab] = useState<'collections' | 'documents' | 'members' | 'create_workspace'>('collections');
  const [isModalOpen, setIsModalOpen] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  // Close dropdown on outside click
  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target as Node)) {
        setIsOpen(false);
      }
    }
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  if (!isAuthenticated) {
    return (
      <div className="flex items-center gap-1.5 px-2.5 py-1 bg-slate-950/60 border border-slate-800 rounded-lg text-xs text-slate-400">
        <span className="w-2 h-2 rounded-full bg-cyan-500"></span>
        <span className="font-medium text-slate-300">Public Demo Mode</span>
      </div>
    );
  }

  const handleOpenModal = (tab: 'collections' | 'documents' | 'members' | 'create_workspace') => {
    setModalTab(tab);
    setIsModalOpen(true);
    setIsOpen(false);
  };

  return (
    <>
      <div className="relative" ref={dropdownRef}>
        <button
          onClick={() => setIsOpen((prev) => !prev)}
          className="flex items-center gap-2 px-3 py-1.5 bg-slate-950/80 hover:bg-slate-900 border border-slate-800 rounded-lg text-xs font-medium text-slate-200 transition cursor-pointer"
          aria-haspopup="true"
          aria-expanded={isOpen}
          title="Select active workspace"
        >
          <span className="w-2 h-2 rounded-full bg-teal-400"></span>
          <span className="truncate max-w-[140px] sm:max-w-[180px]">
            {isWorkspacesLoading
              ? 'Loading...'
              : activeWorkspace
              ? activeWorkspace.name
              : 'Select Workspace'}
          </span>
          {activeWorkspace?.current_user_role && (
            <span className="px-1.5 py-0.2 rounded text-[10px] uppercase font-semibold bg-cyan-950/80 text-cyan-400 border border-cyan-800/60">
              {activeWorkspace.current_user_role}
            </span>
          )}
          <span className="text-[10px] text-slate-400">▼</span>
        </button>

        {isOpen && (
          <div className="absolute right-0 mt-2 w-64 bg-slate-900 border border-slate-800 rounded-xl shadow-xl z-30 py-2 divide-y divide-slate-800 text-xs animate-fadeIn">
            {/* Workspaces list */}
            <div className="max-h-52 overflow-y-auto py-1">
              <div className="px-3 py-1 text-[10px] font-semibold text-slate-400 uppercase tracking-wider">
                Your Workspaces
              </div>
              {workspaces.length === 0 ? (
                <div className="px-3 py-2 text-slate-500">No workspaces yet.</div>
              ) : (
                workspaces.map((ws) => {
                  const isSelected = ws.id === activeWorkspace?.id;
                  return (
                    <button
                      key={ws.id}
                      onClick={() => {
                        selectWorkspace(ws.id);
                        setIsOpen(false);
                      }}
                      className={`w-full text-left px-3 py-2 flex items-center justify-between hover:bg-slate-800/80 transition cursor-pointer ${
                        isSelected ? 'bg-cyan-950/50 text-cyan-300 font-medium' : 'text-slate-300'
                      }`}
                    >
                      <div className="flex flex-col truncate">
                        <span className="truncate">{ws.name}</span>
                        <span className="text-[10px] text-slate-500 font-mono">{ws.slug}</span>
                      </div>
                      {ws.current_user_role && (
                        <span className="text-[10px] capitalize text-slate-400">
                          {ws.current_user_role}
                        </span>
                      )}
                    </button>
                  );
                })
              )}
            </div>

            {/* Actions */}
            <div className="pt-1.5 pb-1 flex flex-col gap-0.5 px-1">
              {activeWorkspace && (
                <>
                  <button
                    onClick={() => handleOpenModal('documents')}
                    className="w-full text-left px-2.5 py-1.5 rounded-lg text-slate-300 hover:text-white hover:bg-slate-800 transition flex items-center justify-between cursor-pointer"
                  >
                    <span>Upload & Document Ingestion</span>
                    <span className="text-cyan-400 text-xs">📄</span>
                  </button>
                  <button
                    onClick={() => handleOpenModal('collections')}
                    className="w-full text-left px-2.5 py-1.5 rounded-lg text-slate-300 hover:text-white hover:bg-slate-800 transition flex items-center justify-between cursor-pointer"
                  >
                    <span>Manage Workspace & Collections</span>
                    <span className="text-cyan-400 text-xs">⚙</span>
                  </button>
                </>
              )}
              <button
                onClick={() => handleOpenModal('create_workspace')}
                className="w-full text-left px-2.5 py-1.5 rounded-lg text-cyan-400 hover:bg-slate-800 transition flex items-center justify-between cursor-pointer font-medium"
              >
                <span>+ Create New Workspace</span>
              </button>
            </div>
          </div>
        )}
      </div>

      <WorkspaceManageModal
        isOpen={isModalOpen}
        onClose={() => setIsModalOpen(false)}
        defaultTab={modalTab}
      />
    </>
  );
}

