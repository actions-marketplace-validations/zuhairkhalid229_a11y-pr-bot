"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { useAuth } from "@/lib/auth";
import { myInstallations } from "@/lib/data";
import { INSTALL_URL } from "@/lib/firebase";
import type { MembershipDoc } from "@/lib/types";
import { SignInGate, TopBar } from "./components";

export default function Home() {
  const { user, loading } = useAuth();
  const [installations, setInstallations] = useState<MembershipDoc[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!user) { setInstallations(null); return; }
    myInstallations(user.uid).then(setInstallations).catch((e) => setError(String(e)));
  }, [user]);

  if (loading) return <main><p className="muted">Loading…</p></main>;
  if (!user) return <main><SignInGate /></main>;

  return (
    <main>
      <TopBar><h1 style={{ margin: 0 }}>Your installations</h1></TopBar>
      {error && <div className="err">{error}</div>}

      {installations === null && <p className="muted">Loading installations…</p>}

      {installations?.length === 0 && (
        <div className="card">
          <p style={{ marginTop: 0 }}>No installations linked to this account yet.</p>
          <p className="muted small">
            If you just installed the app, sign out and back in — the GitHub token that lists your
            installations is only available at sign-in.
          </p>
          <a className="btn primary" href={INSTALL_URL} target="_blank" rel="noreferrer">
            Install on a repository
          </a>
        </div>
      )}

      {installations?.map((i) => (
        <Link key={i.installationId} href={`/i/${i.installationId}`} className="card row"
              style={{ textDecoration: "none", color: "inherit" }}>
          <strong>{i.account_login ?? `Installation ${i.installationId}`}</strong>
          <span className="spacer" />
          <span className="muted small mono">#{i.installationId}</span>
        </Link>
      ))}
    </main>
  );
}
