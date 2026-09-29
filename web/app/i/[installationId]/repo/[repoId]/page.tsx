"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";

import { listRepoConfig, setBypassToken } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { recentScans, scanFindings } from "@/lib/data";
import type { FindingDoc, ScanDoc } from "@/lib/types";
import { SignInGate, StatePill, TopBar, when } from "../../../../components";

export default function RepoPage() {
  const { installationId, repoId } = useParams<{ installationId: string; repoId: string }>();
  const { user, loading } = useAuth();
  const [scans, setScans] = useState<ScanDoc[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!user) return;
    recentScans(installationId, { repoId }).then(setScans).catch((e) => setError(String(e)));
  }, [user, installationId, repoId]);

  if (loading) return <main><p className="muted">Loading…</p></main>;
  if (!user) return <main><SignInGate /></main>;

  const name = scans?.[0]?.repo_full_name ?? `repo ${repoId}`;

  return (
    <main>
      <TopBar>
        <Link href={`/i/${installationId}`}>← installation</Link>
        <strong className="mono">{name}</strong>
      </TopBar>
      {error && <div className="err">{error}</div>}

      <BypassForm installationId={installationId} repoId={repoId} />

      <h2>Scans</h2>
      {scans === null && <p className="muted">Loading…</p>}
      {scans?.length === 0 && <p className="muted">No scans for this repository yet.</p>}
      {scans?.map((s) => <ScanCard key={s.id} scan={s} />)}
    </main>
  );
}

function BypassForm({ installationId, repoId }: { installationId: string; repoId: string }) {
  const [value, setValue] = useState("");
  const [has, setHas] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  useEffect(() => {
    listRepoConfig(installationId)
      .then((rows) =>
        setHas(rows.find((r) => String(r.repo_id) === repoId)?.has_bypass_secret ?? false),
      )
      .catch(() => setHas(false));
  }, [installationId, repoId]);

  async function save(token: string) {
    setBusy(true);
    setNote(null);
    try {
      const result = await setBypassToken(installationId, repoId, token);
      setHas(result.has_bypass_secret);
      setValue("");
      setNote(result.has_bypass_secret ? "Saved." : "Cleared.");
    } catch (e) {
      setNote(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <h2>Vercel Deployment Protection</h2>
      <div className="card">
        <p className="small" style={{ marginTop: 0 }}>
          Vercel Authentication is on by default, so preview deployments answer 401 to the
          scanner. Paste this project&apos;s <strong>Protection Bypass for Automation</strong>{" "}
          token (Vercel → Project → Settings → Deployment Protection).
        </p>
        <div className="bar" style={{ marginBottom: 8 }}>
          <input
            type="password"
            value={value}
            disabled={busy}
            placeholder="Bypass token"
            onChange={(e) => setValue(e.target.value)}
            aria-label="Vercel protection bypass token"
          />
          <button className="primary" disabled={busy || !value.trim()} onClick={() => save(value)}>
            {busy ? "Saving…" : "Save"}
          </button>
          {has && <button disabled={busy} onClick={() => save("")}>Remove</button>}
        </div>
        <p className="muted small" style={{ marginBottom: 0 }}>
          {has === null
            ? "Checking…"
            : has
              ? "A token is stored. It is encrypted at rest and never shown again."
              : "No token stored."}
          {note && <> · {note}</>}
        </p>
      </div>
    </>
  );
}

function ScanCard({ scan }: { scan: ScanDoc }) {
  const [findings, setFindings] = useState<FindingDoc[] | null>(null);
  const [open, setOpen] = useState(false);
  const counts = scan.result?.counts_by_impact ?? {};

  async function toggle() {
    const next = !open;
    setOpen(next);
    if (next && findings === null) {
      setFindings(await scanFindings(scan.id).catch(() => []));
    }
  }

  const postable = findings?.filter(
    (f) => f.disposition === "suggestion" || f.disposition === "annotation",
  );
  const rows = postable?.length ? postable : findings;

  return (
    <div className="card">
      <div className="bar" style={{ marginBottom: 0 }}>
        <StatePill scan={scan} />
        <span className="small">{scan.pr ? `PR #${scan.pr.number}` : "no PR"}</span>
        <span className="mono small muted">{scan.head_sha?.slice(0, 7)}</span>
        <span className="spacer" />
        <span className="muted small">{when(scan.updated_at)}</span>
      </div>

      {scan.state === "quota_exceeded" && (
        <p className="small" style={{ margin: 0 }}>
          Skipped — plan limit reached ({scan.quota_reason}).
        </p>
      )}
      {scan.result?.error && <p className="small" style={{ margin: 0 }}>{scan.result.error}</p>}

      {scan.result && scan.result.findings > 0 && (
        <>
          <div className="bar small" style={{ margin: "10px 0 0" }}>
            {(["critical", "serious", "moderate", "minor"] as const)
              .filter((k) => counts[k])
              .map((k) => (
                <span key={k} className="pill">
                  {k} {counts[k]}
                </span>
              ))}
            {scan.result.posting?.posted ? (
              <span className="pill ok">{scan.result.posting.posted} commented</span>
            ) : null}
            <span className="spacer" />
            <button onClick={toggle}>{open ? "Hide" : "Show"} findings</button>
          </div>

          {open && findings === null && <p className="muted small">Loading…</p>}
          {open && rows && (
            <div className="scroll" style={{ marginTop: 10 }}>
              <table>
                <thead>
                  <tr>
                    <th>Impact</th>
                    <th>Rule</th>
                    <th>WCAG</th>
                    <th>Source</th>
                    <th>Posted as</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((f) => (
                    <tr key={f.fingerprint}>
                      <td className="small">{f.impact}</td>
                      <td className="small">
                        <a href={f.help_url} target="_blank" rel="noreferrer">
                          {f.rule_id}
                        </a>
                      </td>
                      <td className="small">{f.wcag.map((w) => w.id).join(", ") || "—"}</td>
                      <td className="mono small">
                        {f.source ? `${f.source.file}:${f.source.line_start}` : "—"}
                      </td>
                      <td className="small muted">{f.disposition ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  );
}
