"use client";

import { useAuth } from "@/lib/auth";
import { INSTALL_URL } from "@/lib/firebase";
import type { ScanDoc } from "@/lib/types";

export function TopBar({ children }: { children?: React.ReactNode }) {
  const { user, signOutNow } = useAuth();
  return (
    <div className="bar">
      {children}
      <span className="spacer" />
      {user && (
        <>
          <span className="muted small">{user.email ?? user.displayName}</span>
          <button onClick={signOutNow}>Sign out</button>
        </>
      )}
    </div>
  );
}

export function SignInGate() {
  const { signIn, error } = useAuth();
  return (
    <>
      <h1>Accessibility checks on every pull request</h1>
      <p className="sub">
        Sign in with GitHub to see your installations, scan history and per-repo settings.
      </p>
      {error && <div className="err">{error}</div>}
      <div className="bar">
        <button className="primary" onClick={signIn}>Sign in with GitHub</button>
        <a className="btn" href={INSTALL_URL} target="_blank" rel="noreferrer">
          Install the app
        </a>
      </div>
      <p className="muted small">
        We request <span className="mono">read:user</span> only. Repository access comes from the
        GitHub App installation, not from this sign-in.
      </p>
    </>
  );
}

const STATE_TONE: Record<string, string> = {
  completed: "ok", failed: "bad", quota_exceeded: "warn", no_preview: "warn",
  in_progress: "", queued: "", awaiting_deployment: "", awaiting_pr: "",
};

export function StatePill({ scan }: { scan: ScanDoc }) {
  const findings = scan.result?.findings;
  const label =
    scan.state === "completed"
      ? findings === 0 ? "clean" : `${findings} issue${findings === 1 ? "" : "s"}`
      : scan.state.replace(/_/g, " ");
  const tone = scan.state === "completed" && findings ? "warn" : STATE_TONE[scan.state] ?? "";
  return <span className={`pill ${tone}`}>{label}</span>;
}

export function when(ts?: { seconds: number } | null): string {
  if (!ts?.seconds) return "—";
  return new Date(ts.seconds * 1000).toLocaleString();
}
