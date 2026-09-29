"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";

import { listRepoConfig } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { currentUsage, recentScans } from "@/lib/data";
import type { RepoConfig, ScanDoc, UsageDoc } from "@/lib/types";
import { SignInGate, StatePill, TopBar, when } from "../../components";

export default function InstallationPage() {
  const { installationId } = useParams<{ installationId: string }>();
  const { user, loading } = useAuth();
  const [scans, setScans] = useState<ScanDoc[] | null>(null);
  const [repos, setRepos] = useState<RepoConfig[]>([]);
  const [usage, setUsage] = useState<UsageDoc | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!user) return;
    recentScans(installationId).then(setScans).catch((e) => setError(String(e)));
    currentUsage(installationId).then(setUsage).catch(() => {});
    listRepoConfig(installationId).then(setRepos).catch(() => {});
  }, [user, installationId]);

  if (loading) return <main><p className="muted">Loading…</p></main>;
  if (!user) return <main><SignInGate /></main>;

  // Repos seen in scan history, union repos that already carry config.
  const seen = new Map<number, string>();
  scans?.forEach((s) => seen.set(s.repo_id, s.repo_full_name));
  repos.forEach((r) => { if (r.full_name) seen.set(r.repo_id, r.full_name); });
  const bypassById = new Map(repos.map((r) => [r.repo_id, r.has_bypass_secret]));

  return (
    <main>
      <TopBar>
        <Link href="/">← installations</Link>
        <strong>#{installationId}</strong>
      </TopBar>
      {error && <div className="err">{error}</div>}

      <h2>This month</h2>
      <div className="card row">
        <span>Private-repo scans: <strong>{usage?.private_scans_run ?? 0}</strong></span>
        <span className="muted">·</span>
        <span>Private repos: <strong>{usage?.private_repo_ids?.length ?? 0}</strong></span>
        <span className="spacer" />
        <span className="muted small">Public repositories are unlimited and unmetered.</span>
      </div>

      <h2>Repositories</h2>
      {seen.size === 0 && <p className="muted">Nothing scanned yet. Open a pull request.</p>}
      {[...seen].map(([id, name]) => (
        <Link key={id} href={`/i/${installationId}/repo/${id}`} className="card row"
              style={{ textDecoration: "none", color: "inherit" }}>
          <span className="mono">{name}</span>
          <span className="spacer" />
          {bypassById.get(id) && <span className="pill ok">bypass token set</span>}
        </Link>
      ))}

      <h2>Recent scans</h2>
      {scans === null && <p className="muted">Loading…</p>}
      {scans?.length === 0 && <p className="muted">No scans recorded yet.</p>}
      {scans && scans.length > 0 && (
        <div className="scroll">
          <table>
            <thead>
              <tr><th>When</th><th>Repo</th><th>PR</th><th>Commit</th><th>Status</th></tr>
            </thead>
            <tbody>
              {scans.map((s) => (
                <tr key={s.id}>
                  <td className="muted small">{when(s.updated_at)}</td>
                  <td className="mono small">{s.repo_full_name}</td>
                  <td className="small">{s.pr ? `#${s.pr.number}` : "—"}</td>
                  <td className="mono small">{s.head_sha?.slice(0, 7)}</td>
                  <td><StatePill scan={s} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </main>
  );
}
