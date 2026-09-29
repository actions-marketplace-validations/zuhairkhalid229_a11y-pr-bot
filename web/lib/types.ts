export type ScanState =
  | "awaiting_deployment" | "awaiting_pr" | "queued" | "in_progress"
  | "completed" | "failed" | "no_preview" | "quota_exceeded";

export interface ScanDoc {
  id: string;
  installation_id: number;
  repo_id: number;
  repo_full_name: string;
  repo_private?: boolean;
  head_sha: string;
  state: ScanState;
  pr?: { number: number; head_ref?: string };
  preview?: { url: string; provider: string };
  check_run_id?: number | null;
  quota_reason?: string;
  result?: {
    status: string;
    findings: number;
    needs_review: number;
    counts_by_impact?: Record<string, number>;
    duration_ms?: number;
    error?: string | null;
    engine_version?: string;
    posting?: { posted?: number; still_open?: number; fixed?: number } | null;
    mapping?: { suggestions?: number; annotations?: number; drops?: number } | null;
  } | null;
  updated_at?: { seconds: number } | null;
}

export interface FindingDoc {
  fingerprint: string;
  rule_id: string;
  impact: "critical" | "serious" | "moderate" | "minor";
  help: string;
  help_url: string;
  wcag: { id: string; name: string; level: string }[];
  en_301_549: string[];
  page_path: string;
  selector: string;
  html: string;
  disposition?: "suggestion" | "annotation" | "drop" | "duplicate" | null;
  disposition_reason?: string | null;
  source?: { file: string; line_start: number; line_end: number } | null;
}

export interface MembershipDoc {
  installationId: string;
  account_login?: string;
}

export interface RepoConfig {
  repo_id: number;
  full_name?: string;
  has_bypass_secret: boolean;
}

export interface UsageDoc {
  private_scans_run?: number;
  private_repo_ids?: number[];
}
