import test from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

import { AuthModal } from '../src/components/AuthModal.tsx';
import { LandingPage } from '../src/components/LandingPage.tsx';
import { AuthProvider } from '../src/context/AuthContext.tsx';
import { WorkspaceProvider } from '../src/context/WorkspaceContext.tsx';
import { authLinkLegacy } from '../src/api/client.ts';

function renderWithProviders(element) {
  return renderToStaticMarkup(
    React.createElement(
      AuthProvider,
      null,
      React.createElement(WorkspaceProvider, null, element)
    )
  );
}

// ----------------------------------------------------------------------------
// Test 1: Landing Page CTAs & Modal Mounting
// ----------------------------------------------------------------------------
test('1. LandingPage: Sign In, Sign Up, and Guest CTAs are present and accessible', () => {
  let openedMode = null;
  const onOpenAuth = (mode) => {
    openedMode = mode;
  };

  const html = renderWithProviders(
    React.createElement(LandingPage, { onOpenAuth })
  );

  // Check buttons
  assert.match(html, /Sign In/);
  assert.match(html, /Sign Up/);
  assert.match(html, /Create Free Account/);
  assert.match(html, /Continue as Guest/);

  // Assert no hallucinations claim was replaced with evidence-grounded copy
  assert.doesNotMatch(html, /zero hallucinations/i);
  assert.match(html, /evidence-grounded with controlled abstention/i);
  assert.doesNotMatch(html, /Zero Unverified Claims/i);
  assert.match(html, /Deterministic Citation Verification/i);
});

// ----------------------------------------------------------------------------
// Test 2: AuthModal - Login Mode
// ----------------------------------------------------------------------------
test('2. AuthModal: renders Login mode with email, password, forgot password link, and sign-in button', () => {
  const html = renderToStaticMarkup(
    React.createElement(AuthModal, { isOpen: true, onClose: () => {}, defaultMode: 'login' })
  );

  assert.match(html, /Sign In to Workspace/);
  assert.match(html, /Email Address/);
  assert.match(html, /Password/);
  assert.match(html, /Forgot password\?/);
  assert.match(html, /Sign In/);
  assert.match(html, /Don(?:'|&#x27;)t have an account\?/);
});

// ----------------------------------------------------------------------------
// Test 3: AuthModal - Signup Mode
// ----------------------------------------------------------------------------
test('3. AuthModal: renders Signup mode with email, password, full name, and create account button', () => {
  const html = renderToStaticMarkup(
    React.createElement(AuthModal, { isOpen: true, onClose: () => {}, defaultMode: 'signup' })
  );

  assert.match(html, /Create an Account/);
  assert.match(html, /Full Name \(Optional\)/);
  assert.match(html, /Email Address/);
  assert.match(html, /Password/);
  assert.match(html, /Must be at least 8 characters/);
  assert.match(html, /Create Account/);
  assert.match(html, /Already have an account\?/);
});

// ----------------------------------------------------------------------------
// Test 4: AuthModal - Verify Email Mode (Link-based verification, NO OTP)
// ----------------------------------------------------------------------------
test('4. AuthModal: renders Verify Email mode using link verification instructions and NO numeric OTP fields', () => {
  const html = renderToStaticMarkup(
    React.createElement(AuthModal, { isOpen: true, onClose: () => {}, defaultMode: 'verify-email' })
  );

  // Email verification link instructions
  assert.match(html, /Verify Your Email/);
  assert.match(html, /Check Your Inbox/);
  assert.match(html, /We sent a verification link to/);
  assert.match(html, /Click the verification link in your email to activate your account/);
  assert.match(html, /I(?:'|&#x27;)ve Verified My Email/);
  assert.match(html, /Resend Verification Link/);

  // CRITICAL REQUIREMENT: Strictly NO numeric OTP input or OTP wording!
  assert.doesNotMatch(html, /otp/i);
  assert.doesNotMatch(html, /one-time password/i);
  assert.doesNotMatch(html, /6-digit/i);
  assert.doesNotMatch(html, /digit code/i);
  assert.doesNotMatch(html, /enter code/i);
});

// ----------------------------------------------------------------------------
// Test 5: AuthModal - Forgot Password Mode
// ----------------------------------------------------------------------------
test('5. AuthModal: renders Forgot Password mode with email input and reset button', () => {
  const html = renderToStaticMarkup(
    React.createElement(AuthModal, { isOpen: true, onClose: () => {}, defaultMode: 'forgot-password' })
  );

  assert.match(html, /Reset Your Password/);
  assert.match(html, /Enter your email address and we(?:'|&#x27;)ll send you a secure link/);
  assert.match(html, /Email Address/);
  assert.match(html, /Send Password Reset Link/);
  assert.match(html, /Back to Sign In/);
});

// ----------------------------------------------------------------------------
// Test 6: AuthModal - Link Legacy Account Mode (Migration on 409 Conflict)
// ----------------------------------------------------------------------------
test('6. AuthModal: renders Link Existing Workspace mode with password confirmation prompt', () => {
  const html = renderToStaticMarkup(
    React.createElement(AuthModal, { isOpen: true, onClose: () => {}, defaultMode: 'link-legacy' })
  );

  assert.match(html, /Link Existing Workspace/);
  assert.match(html, /An existing workspace was found for/);
  assert.match(html, /Enter your previous password to securely link your documents/);
  assert.match(html, /Previous Account Password/);
  assert.match(html, /Link Workspace &amp; Enter/);
  assert.match(html, /Cancel and Return to Sign In/);
});

// ----------------------------------------------------------------------------
// Test 7: API Client - authLinkLegacy sends legacy password and bearer token
// ----------------------------------------------------------------------------
test('7. API Client: authLinkLegacy calls /api/v1/auth/link-legacy with token and password payload', async () => {
  const originalFetch = globalThis.fetch;
  let capturedUrl = '';
  let capturedOptions = null;

  globalThis.fetch = async (url, options) => {
    capturedUrl = url;
    capturedOptions = options;
    return {
      ok: true,
      status: 200,
      json: async () => ({
        id: 'usr-linked-123',
        email: 'migrated@example.com',
        full_name: 'Migrated User',
        is_active: true,
        firebase_uid: 'fb-migrated-uid',
        email_verified: true,
      }),
      text: async () => '',
    };
  };

  try {
    const user = await authLinkLegacy('OldSecretPassword123!', 'mock-firebase-id-token');
    assert.match(capturedUrl, /\/api\/v1\/auth\/link-legacy$/);
    assert.equal(capturedOptions.method, 'POST');
    assert.equal(capturedOptions.headers['Authorization'], 'Bearer mock-firebase-id-token');
    assert.equal(capturedOptions.headers['Content-Type'], 'application/json');
    const body = JSON.parse(capturedOptions.body);
    assert.equal(body.password, 'OldSecretPassword123!');

    assert.equal(user.id, 'usr-linked-123');
    assert.equal(user.firebase_uid, 'fb-migrated-uid');
    assert.equal(user.email_verified, true);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
