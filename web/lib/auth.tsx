"use client";

import {
  GithubAuthProvider, onAuthStateChanged, signInWithPopup, signOut, type User,
} from "firebase/auth";
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

import { linkInstallations } from "./api";
import { auth, githubProvider } from "./firebase";

interface AuthState {
  user: User | null;
  loading: boolean;
  error: string | null;
  signIn: () => Promise<void>;
  signOutNow: () => Promise<void>;
}

const Ctx = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => onAuthStateChanged(auth(), (u) => { setUser(u); setLoading(false); }), []);

  const signIn = useCallback(async () => {
    setError(null);
    try {
      const credential = await signInWithPopup(auth(), githubProvider());
      // The GitHub access token exists only in this response — Firebase does
      // not persist it — so the link call has to happen right here, before the
      // token is gone. Sign in again to re-link after installing on a new org.
      const token = GithubAuthProvider.credentialFromResult(credential)?.accessToken;
      if (token) await linkInstallations(token);
      else setError("GitHub did not return an access token; try signing in again.");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  const signOutNow = useCallback(async () => { await signOut(auth()); }, []);

  const value = useMemo(
    () => ({ user, loading, error, signIn, signOutNow }),
    [user, loading, error, signIn, signOutNow],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useAuth outside AuthProvider");
  return ctx;
}
