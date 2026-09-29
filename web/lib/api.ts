import { auth } from "./firebase";

const BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "";

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const user = auth().currentUser;
  if (!user) throw new Error("not signed in");
  const token = await user.getIdToken();
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      ...(init?.headers ?? {}),
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(`${response.status} ${detail.slice(0, 200)}`);
  }
  return response.json() as Promise<T>;
}

/** Records which installations this user may see. Must run before any read. */
export function linkInstallations(githubToken: string) {
  return call<{ installation_id: number; account_login: string | null }[]>("/api/link", {
    method: "POST",
    body: JSON.stringify({ github_token: githubToken }),
  });
}

export function listRepoConfig(installationId: string) {
  return call<{ repo_id: number; full_name?: string; has_bypass_secret: boolean }[]>(
    `/api/installations/${installationId}/repos`,
  );
}

/** Empty token clears the stored secret. */
export function setBypassToken(installationId: string, repoId: string, token: string) {
  return call<{ has_bypass_secret: boolean }>(
    `/api/installations/${installationId}/repos/${repoId}/bypass`,
    { method: "POST", body: JSON.stringify({ token }) },
  );
}
