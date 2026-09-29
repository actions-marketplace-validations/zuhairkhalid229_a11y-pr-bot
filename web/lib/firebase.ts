import { initializeApp, getApps, type FirebaseApp } from "firebase/app";
import { getAuth, GithubAuthProvider, type Auth } from "firebase/auth";
import { getFirestore, type Firestore } from "firebase/firestore";

const config = {
  apiKey: process.env.NEXT_PUBLIC_FIREBASE_API_KEY!,
  authDomain: process.env.NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN!,
  projectId: process.env.NEXT_PUBLIC_FIREBASE_PROJECT_ID!,
};

export function firebaseApp(): FirebaseApp {
  return getApps()[0] ?? initializeApp(config);
}

export function auth(): Auth {
  return getAuth(firebaseApp());
}

export function db(): Firestore {
  return getFirestore(firebaseApp());
}

/**
 * `read:user` is all we need: /user/installations is authorised by the user
 * token itself. We deliberately do NOT ask for `repo` — the app's own
 * installation permissions cover repository access, and asking a developer for
 * broad repo scope at sign-in is the fastest way to lose them.
 */
export function githubProvider(): GithubAuthProvider {
  const provider = new GithubAuthProvider();
  provider.addScope("read:user");
  return provider;
}

export const APP_SLUG = process.env.NEXT_PUBLIC_APP_SLUG ?? "a11y-pr-bot";
export const INSTALL_URL = `https://github.com/apps/${APP_SLUG}/installations/new`;
