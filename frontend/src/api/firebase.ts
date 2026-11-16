/**
 * Firebase Client SDK Authentication Service (Milestone 18).
 * Configures Firebase Auth on the free Spark plan for email/password sign-in
 * and link-based email verification.
 */
import { initializeApp, getApps, type FirebaseApp } from 'firebase/app';
import {
  getAuth,
  createUserWithEmailAndPassword,
  signInWithEmailAndPassword,
  sendEmailVerification,
  sendPasswordResetEmail,
  signOut,
  updateProfile,
  onAuthStateChanged,
  type Auth,
  type User as FirebaseUser,
  type ActionCodeSettings,
} from 'firebase/auth';

const apiKey = import.meta.env.VITE_FIREBASE_API_KEY as string | undefined;
const authDomain = import.meta.env.VITE_FIREBASE_AUTH_DOMAIN as string | undefined;
const projectId = import.meta.env.VITE_FIREBASE_PROJECT_ID as string | undefined;
const appId = import.meta.env.VITE_FIREBASE_APP_ID as string | undefined;

let firebaseApp: FirebaseApp | null = null;
let firebaseAuth: Auth | null = null;

export function isFirebaseClientConfigured(): boolean {
  return Boolean(apiKey && authDomain && projectId && appId);
}

export function getFirebaseAuth(): Auth | null {
  if (firebaseAuth) return firebaseAuth;

  if (!isFirebaseClientConfigured()) {
    return null;
  }

  try {
    if (!getApps().length) {
      firebaseApp = initializeApp({
        apiKey,
        authDomain,
        projectId,
        appId,
      });
    } else {
      firebaseApp = getApps()[0];
    }
    firebaseAuth = getAuth(firebaseApp);
    return firebaseAuth;
  } catch (err) {
    console.error('Failed to initialize Firebase Auth client:', err);
    return null;
  }
}

/**
 * Returns ActionCodeSettings with an authorized application return URL.
 */
function getActionCodeSettings(): ActionCodeSettings {
  const origin = typeof window !== 'undefined' && window.location?.origin
    ? window.location.origin
    : 'https://sebi-circular-rag-assistant.vercel.app';

  return {
    url: origin,
    handleCodeInApp: true,
  };
}

/**
 * Registers a new user with email and password, sets their display name,
 * and sends an email verification link.
 */
export async function firebaseRegisterUser(
  email: string,
  password: string,
  fullName?: string
): Promise<{ user: FirebaseUser; verificationEmailSent: boolean }> {
  const auth = getFirebaseAuth();
  if (!auth) {
    throw new Error('Firebase Authentication is not configured on this client.');
  }

  const userCredential = await createUserWithEmailAndPassword(auth, email, password);
  const user = userCredential.user;

  if (fullName && fullName.trim()) {
    try {
      await updateProfile(user, { displayName: fullName.trim() });
    } catch {
      // Non-fatal: profile name update failure
    }
  }

  let verificationEmailSent = false;
  try {
    await sendEmailVerification(user, getActionCodeSettings());
    verificationEmailSent = true;
  } catch (err) {
    console.warn('Initial email verification dispatch failed:', err);
  }

  return { user, verificationEmailSent };
}

/**
 * Authenticates user with email and password.
 */
export async function firebaseSignInUser(
  email: string,
  password: string
): Promise<FirebaseUser> {
  const auth = getFirebaseAuth();
  if (!auth) {
    throw new Error('Firebase Authentication is not configured on this client.');
  }

  const credential = await signInWithEmailAndPassword(auth, email, password);
  return credential.user;
}

/**
 * Sends a verification email link to the currently signed-in user.
 */
export async function firebaseSendVerificationEmail(): Promise<void> {
  const auth = getFirebaseAuth();
  if (!auth || !auth.currentUser) {
    throw new Error('No active user session to send verification email.');
  }

  await sendEmailVerification(auth.currentUser, getActionCodeSettings());
}

/**
 * Refreshes the current user's profile and returns their updated email verification status.
 */
export async function firebaseCheckEmailVerification(): Promise<boolean> {
  const auth = getFirebaseAuth();
  if (!auth || !auth.currentUser) return false;

  await auth.currentUser.reload();
  return auth.currentUser.emailVerified;
}

/**
 * Dispatches a password reset email link using Firebase Auth.
 */
export async function firebaseSendPasswordReset(email: string): Promise<void> {
  const auth = getFirebaseAuth();
  if (!auth) {
    throw new Error('Firebase Authentication is not configured on this client.');
  }

  await sendPasswordResetEmail(auth, email, getActionCodeSettings());
}

/**
 * Signs out the current Firebase user.
 */
export async function firebaseSignOutUser(): Promise<void> {
  const auth = getFirebaseAuth();
  if (!auth) return;
  await signOut(auth);
}

/**
 * Retrieves the current Firebase ID token for backend API requests.
 */
export async function firebaseGetIdToken(forceRefresh: boolean = false): Promise<string | null> {
  const auth = getFirebaseAuth();
  if (!auth || !auth.currentUser) return null;

  try {
    return await auth.currentUser.getIdToken(forceRefresh);
  } catch {
    return null;
  }
}

/**
 * Subscribes to Firebase Auth state changes.
 */
export function onAuthStateSubscription(
  callback: (user: FirebaseUser | null) => void
): () => void {
  const auth = getFirebaseAuth();
  if (!auth) {
    callback(null);
    return () => {};
  }

  return onAuthStateChanged(auth, callback);
}

