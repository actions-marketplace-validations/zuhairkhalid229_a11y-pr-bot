"use client";

/**
 * GitHub sends the installer here after install or a permission change -- the
 * app Setup URL, with ?installation_id=&setup_action=install|update.
 *
 * It exists because of one hard ordering problem: the GitHub token that tells us
 * which installations a user owns is only available in the sign-in response, so a
 * brand-new installation cannot be linked until the user signs in *after*
 * installing. This page turns that into an obvious next click rather than a
 * dashboard that mysteriously shows nothing.
 */

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";

import { useAuth } from "@/lib/auth";
import { myInstallations } from "@/lib/data";
import { INSTALL_URL } from "@/lib/firebase";
import { TopBar } from "../components";

export default function SetupPage() {
  // useSearchParams needs a Suspense boundary during prerender.
  return (
    <Suspense fallback={<main><p className="muted">Loading…</p></main>}>
      <Setup />
    </Suspense>
  );
}

function Setup() {
  const params = useSearchParams();
  const installationId = params.get("installation_id");
  const action = params.get("setup_action") ?? "install";
  const { user, loading, signIn, error } = useAuth();
  const [linked, setLinked] = useState<boolean | null>(null);

  useEffect(() => {
    if (!user || !installationId) return;
    myInstallations(user.uid)
      .then((rows) => setLinked(rows.some((r) => r.installationId === installationId)))
      .catch(() => setLinked(false));
  }, [user, installationId]);

  return (
    <main>
      <TopBar>
        <h1 style={{ margin: 0 }}>{action === "update" ? "Installation updated" : "Installed"}</h1>
      </TopBar>
      {error && <div className="err">{error}</div>}

      <ol className="card" style={{ margin: 0, paddingLeft: 26 }}>
        <li style={{ marginBottom: 10 }}>
          <strong>App installed</strong>
          {installationId && <span className="muted mono small"> · #{installationId}</span>}
        </li>

        <li style={{ marginBottom: 10 }}>
          {loading ? (
            <span className="muted">checking sign-in…</span>
          ) : !user ? (
            <>
              <strong>Sign in with GitHub</strong>{" "}
              <button className="primary" onClick={signIn}>Sign in</button>
            </>
          ) : linked === false ? (
            <>
              <strong>Sign in again to link this installation.</strong>{" "}
              <button onClick={signIn}>Re-link with GitHub</button>
              <div className="muted small">
                GitHub only hands us the installation list during sign-in.
              </div>
            </>
          ) : (
            <>
              <strong>Linked to your account</strong>{" "}
              <span className="muted small">({user.email})</span>
            </>
          )}
        </li>

        <li style={{ marginBottom: 10 }}>
          <strong>Connect a preview deployment.</strong>{" "}
          <span className="muted small">
            Vercel, Netlify or anything that reports deployments to GitHub. No preview, no scan —
            the check run will say so.
          </span>
        </li>

        <li style={{ marginBottom: 10 }}>
          <strong>If previews are protected,</strong>{" "}
          <span className="muted small">
            add the Vercel bypass token on the repository page.
          </span>
        </li>

        <li>
          <strong>Open a pull request.</strong>{" "}
          <span className="muted small">
            A check appears within seconds of the preview going live.
          </span>
        </li>
      </ol>

      <div className="bar" style={{ marginTop: 20 }}>
        {installationId && linked ? (
          <Link className="btn primary" href={`/i/${installationId}`}>Open dashboard</Link>
        ) : (
          <Link className="btn" href="/">Dashboard</Link>
        )}
        <a className="btn" href={INSTALL_URL} target="_blank" rel="noreferrer">
          Add more repositories
        </a>
      </div>
    </main>
  );
}
