import { createContext, useContext, useState, useEffect, useCallback, useRef, type ReactNode } from 'react';
import type {
  Workspace,
  WorkspaceCreateInput,
  WorkspaceCollection,
  WorkspaceCollectionCreateInput,
} from '../types/workspace';
import {
  fetchWorkspaces,
  createWorkspace,
  fetchWorkspaceCollections,
  createWorkspaceCollection,
} from '../api/client';
import { useAuth } from './AuthContext';

const ACTIVE_WS_STORAGE_KEY = 'sebi_rag_active_workspace_id';

interface WorkspaceContextValue {
  workspaces: Workspace[];
  activeWorkspace: Workspace | null;
  activeWorkspaceId: string | null;
  isWorkspacesLoading: boolean;
  workspacesError: string | null;
  selectWorkspace: (id: string) => void;
  createNewWorkspace: (input: WorkspaceCreateInput) => Promise<Workspace>;
  refreshWorkspaces: () => Promise<void>;
  workspaceCollections: WorkspaceCollection[];
  isCollectionsLoading: boolean;
  collectionsError: string | null;
  createNewCollection: (input: WorkspaceCollectionCreateInput) => Promise<WorkspaceCollection>;
  refreshCollections: () => Promise<void>;
}

const defaultWorkspaceValue: WorkspaceContextValue = {
  workspaces: [],
  activeWorkspace: null,
  activeWorkspaceId: null,
  isWorkspacesLoading: false,
  workspacesError: null,
  selectWorkspace: () => {},
  createNewWorkspace: async () => ({
    id: '',
    name: '',
    slug: '',
    is_system: false,
    created_at: '',
    updated_at: '',
  }),
  refreshWorkspaces: async () => {},
  workspaceCollections: [],
  isCollectionsLoading: false,
  collectionsError: null,
  createNewCollection: async () => ({
    id: '',
    workspace_id: '',
    name: '',
    policy_name: '',
    enforce_regulatory_graph: false,
    is_system: false,
    is_public: false,
    created_at: '',
    updated_at: '',
  }),
  refreshCollections: async () => {},
};

const WorkspaceContext = createContext<WorkspaceContextValue>(defaultWorkspaceValue);

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const { token, isAuthenticated } = useAuth();
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [activeWorkspaceId, setActiveWorkspaceId] = useState<string | null>(() => {
    try {
      return localStorage.getItem(ACTIVE_WS_STORAGE_KEY);
    } catch {
      return null;
    }
  });
  const [isWorkspacesLoading, setIsWorkspacesLoading] = useState<boolean>(false);
  const [workspacesError, setWorkspacesError] = useState<string | null>(null);

  const [workspaceCollections, setWorkspaceCollections] = useState<WorkspaceCollection[]>([]);
  const [isCollectionsLoading, setIsCollectionsLoading] = useState<boolean>(false);
  const [collectionsError, setCollectionsError] = useState<string | null>(null);

  // Request & active workspace tracking refs to prevent race conditions from out-of-order responses
  const collectionRequestIdRef = useRef<number>(0);
  const activeWorkspaceIdRef = useRef<string | null>(activeWorkspaceId);

  // Fetch workspaces when authenticated
  const refreshWorkspaces = useCallback(async () => {
    if (!token || !isAuthenticated) {
      collectionRequestIdRef.current++;
      activeWorkspaceIdRef.current = null;
      setWorkspaces([]);
      setActiveWorkspaceId(null);
      setWorkspaceCollections([]);
      setCollectionsError(null);
      setIsCollectionsLoading(false);
      try {
        localStorage.removeItem(ACTIVE_WS_STORAGE_KEY);
      } catch {
        // Storage unavailable
      }
      return;
    }

    setIsWorkspacesLoading(true);
    setWorkspacesError(null);

    try {
      const list = await fetchWorkspaces(token);
      setWorkspaces(list);

      // Validate or assign active workspace
      if (list.length > 0) {
        const storedId = localStorage.getItem(ACTIVE_WS_STORAGE_KEY);
        const exists = list.some((w) => w.id === storedId);
        const nextId = exists && storedId ? storedId : list[0].id;
        if (nextId !== activeWorkspaceIdRef.current) {
          collectionRequestIdRef.current++;
          activeWorkspaceIdRef.current = nextId;
          setWorkspaceCollections([]);
          setCollectionsError(null);
        }
        setActiveWorkspaceId(nextId);
        try {
          localStorage.setItem(ACTIVE_WS_STORAGE_KEY, nextId);
        } catch {
          // Storage unavailable
        }
      } else {
        collectionRequestIdRef.current++;
        activeWorkspaceIdRef.current = null;
        setActiveWorkspaceId(null);
        setWorkspaceCollections([]);
        setCollectionsError(null);
        try {
          localStorage.removeItem(ACTIVE_WS_STORAGE_KEY);
        } catch {
          // Storage unavailable
        }
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to fetch workspaces.';
      setWorkspacesError(msg);
    } finally {
      setIsWorkspacesLoading(false);
    }
  }, [token, isAuthenticated]);

  useEffect(() => {
    refreshWorkspaces();
  }, [refreshWorkspaces]);

  // Fetch private collections for the active workspace
  const refreshCollections = useCallback(async () => {
    const reqId = ++collectionRequestIdRef.current;
    const targetWsId = activeWorkspaceId;
    activeWorkspaceIdRef.current = targetWsId;

    if (!token || !targetWsId) {
      setWorkspaceCollections([]);
      setIsCollectionsLoading(false);
      setCollectionsError(null);
      return;
    }

    setIsCollectionsLoading(true);
    setCollectionsError(null);

    try {
      const cols = await fetchWorkspaceCollections(targetWsId, token);
      if (
        reqId !== collectionRequestIdRef.current ||
        targetWsId !== activeWorkspaceIdRef.current
      ) {
        return;
      }
      setWorkspaceCollections(cols);
    } catch (err: unknown) {
      if (
        reqId !== collectionRequestIdRef.current ||
        targetWsId !== activeWorkspaceIdRef.current
      ) {
        return;
      }
      const msg = err instanceof Error ? err.message : 'Failed to load workspace collections.';
      setCollectionsError(msg);
    } finally {
      if (
        reqId === collectionRequestIdRef.current &&
        targetWsId === activeWorkspaceIdRef.current
      ) {
        setIsCollectionsLoading(false);
      }
    }
  }, [token, activeWorkspaceId]);

  useEffect(() => {
    refreshCollections();
  }, [refreshCollections]);

  const selectWorkspace = (id: string) => {
    if (id !== activeWorkspaceIdRef.current) {
      collectionRequestIdRef.current++;
      activeWorkspaceIdRef.current = id;
      setWorkspaceCollections([]);
      setCollectionsError(null);
      setIsCollectionsLoading(true);
    }
    setActiveWorkspaceId(id);
    try {
      localStorage.setItem(ACTIVE_WS_STORAGE_KEY, id);
    } catch {
      // Storage unavailable
    }
  };

  const createNewWorkspace = async (input: WorkspaceCreateInput): Promise<Workspace> => {
    if (!token) throw new Error('Authentication required to create a workspace.');
    const ws = await createWorkspace(input, token);
    await refreshWorkspaces();
    selectWorkspace(ws.id);
    return ws;
  };

  const createNewCollection = async (
    input: WorkspaceCollectionCreateInput
  ): Promise<WorkspaceCollection> => {
    if (!token) throw new Error('Authentication required to create a collection.');
    if (!activeWorkspaceId) throw new Error('No active workspace selected.');

    const col = await createWorkspaceCollection(activeWorkspaceId, input, token);
    await refreshCollections();
    return col;
  };

  const activeWorkspace = workspaces.find((w) => w.id === activeWorkspaceId) || null;

  return (
    <WorkspaceContext.Provider
      value={{
        workspaces,
        activeWorkspace,
        activeWorkspaceId,
        isWorkspacesLoading,
        workspacesError,
        selectWorkspace,
        createNewWorkspace,
        refreshWorkspaces,
        workspaceCollections,
        isCollectionsLoading,
        collectionsError,
        createNewCollection,
        refreshCollections,
      }}
    >
      {children}
    </WorkspaceContext.Provider>
  );
}

export function useWorkspace() {
  const context = useContext(WorkspaceContext);
  return context || defaultWorkspaceValue;
}
