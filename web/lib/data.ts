"use client";

import {
  collection, collectionGroup, doc, getDoc, getDocs, limit as qLimit,
  orderBy, query, where,
} from "firebase/firestore";

import { db } from "./firebase";
import type { FindingDoc, MembershipDoc, ScanDoc, UsageDoc } from "./types";

/**
 * "My installations" via a collection-group query on the membership markers
 * /api/link wrote. The rule for members/{uid} allows only request.auth.uid ==
 * uid, so this returns exactly the caller's own markers.
 */
export async function myInstallations(uid: string): Promise<MembershipDoc[]> {
  const snap = await getDocs(
    query(collectionGroup(db(), "members"), where("uid", "==", uid), qLimit(50)),
  );
  return snap.docs.map((d) => ({
    // .../installations/{iid}/members/{uid}
    installationId: d.ref.parent.parent!.id,
    account_login: d.data().account_login,
  }));
}

/** Scans must always be filtered by installation_id — the rules require it. */
export async function recentScans(
  installationId: string, opts: { repoId?: string; max?: number } = {},
): Promise<ScanDoc[]> {
  const clauses = [where("installation_id", "==", Number(installationId))];
  if (opts.repoId) clauses.push(where("repo_id", "==", Number(opts.repoId)));
  const snap = await getDocs(
    query(collection(db(), "scans"), ...clauses, orderBy("updated_at", "desc"),
          qLimit(opts.max ?? 25)),
  );
  return snap.docs.map((d) => ({ id: d.id, ...(d.data() as Omit<ScanDoc, "id">) }));
}

export async function scanFindings(scanId: string): Promise<FindingDoc[]> {
  const snap = await getDocs(
    query(collection(db(), "scans", scanId, "findings"), qLimit(100)),
  );
  return snap.docs.map((d) => d.data() as FindingDoc);
}

export async function currentUsage(installationId: string): Promise<UsageDoc | null> {
  const now = new Date();
  const month = `${now.getUTCFullYear()}${String(now.getUTCMonth() + 1).padStart(2, "0")}`;
  const snap = await getDoc(doc(db(), "usage", `${installationId}_${month}`));
  return snap.exists() ? (snap.data() as UsageDoc) : null;
}
