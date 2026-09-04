/**
 * Typed client for the VMlab API.
 *
 * The access token is attached here and nowhere else. Note what is absent: no
 * tenant id is ever sent. The workspace a request applies to is derived from the
 * token server-side, so there is no client-side value that could be tampered
 * with to reach another retailer's data.
 */

import { createClient, type SupabaseClient } from "@supabase/supabase-js";

/**
 * Trailing slashes are stripped, because leaving one on is silent and awful.
 * A base URL entered as `https://api.example.com/` builds `…com//api/uploads`,
 * which matches no route, so FastAPI answers with its own `{"detail":"Not
 * Found"}` -- indistinguishable in the UI from a missing record, and nowhere
 * near the actual mistake. Every deployment surface that accepts a URL will
 * eventually be given one with a slash on the end; normalise it here once.
 */
const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(/\/+$/, "");

/**
 * The Supabase client is built on first use rather than at module load.
 *
 * Constructing it eagerly throws when the environment is not configured, which
 * breaks the production build during prerendering -- Next evaluates every page
 * module at build time, where these variables legitimately may not be set. Lazy
 * construction also means a misconfigured deployment fails at the point someone
 * tries to sign in, with a clear message, rather than as an opaque build error.
 */
let client: SupabaseClient | null = null;

export function getSupabase(): SupabaseClient {
  if (client) return client;

  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
  if (!url || !key) {
    throw new Error(
      "Supabase is not configured. Set NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_ANON_KEY.",
    );
  }
  client = createClient(url, key);
  return client;
}

export type Perspective = "creative_vm" | "retail_psychology" | "commercial";

export type SpecialistItem = {
  observation: string;
  recommendation: string;
  reason: string;
  confidence: number;
  severity: string;
  supporting_rule_ids: string[];
  /**
   * Perspectives that independently made this same point before the duplicate
   * was removed from their section. Optional because analyses stored before the
   * reconcile stage existed have no such field, and rendering `undefined` as
   * "also raised by nobody" would be a lie about old results.
   */
  also_raised_by?: Perspective[];
};

/**
 * How much of the review each specialist owns, as percentages totalling 100.
 *
 * The API validates this, so a mix that does not add up comes back as a 422
 * rather than being quietly rescaled -- which is why the slider component below
 * must keep the sum exact rather than leaving it to the server to sort out.
 */
export type Priorities = {
  creative_vm: number;
  retail_psychology: number;
  commercial: number;
};

export const PRIORITY_MIN = 10;
export const PRIORITY_MAX = 80;
export const PRIORITY_TOTAL = 100;

/** Matches default_priorities() in the API. The remainder has to land somewhere. */
export const BALANCED_PRIORITIES: Priorities = {
  creative_vm: 34,
  retail_psychology: 33,
  commercial: 33,
};

export type AnalysisSection = {
  perspective: Perspective;
  items: SpecialistItem[];
  evidence_refs: unknown[];
};

export type PrioritisedAction = {
  rank: number;
  action: string;
  rationale: string;
  effort: "quick_win" | "moderate" | "major_change";
  confidence: number | null;
  priority_score: number | null;
  trade_off: string | null;
};

export type Analysis = {
  id: string;
  status:
    | "pending" | "validating" | "extracting" | "retrieving"
    | "reasoning" | "synthesising" | "complete" | "failed";
  overall_summary: string | null;
  uncertainty_note: string | null;
  visual_evidence: Record<string, unknown> | null;
  stage_timings_ms: Record<string, number>;
  cost_usd: number | null;
  /** The mix this analysis ran with. `{}` for anything run before sliders existed. */
  priority_weights: Partial<Priorities>;
  error_detail: string | null;
  created_at: string;
  completed_at: string | null;
  sections: AnalysisSection[];
  actions: PrioritisedAction[];
};

export type UploadResult = {
  upload_id: string;
  width: number;
  height: number;
  quality_flags: string[];
  low_confidence: boolean;
};

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function authHeaders(): Promise<HeadersInit> {
  const {
    data: { session },
  } = await getSupabase().auth.getSession();
  if (!session) throw new ApiError("Not signed in", 401);
  return { Authorization: `Bearer ${session.access_token}` };
}

async function handle<T>(response: Response): Promise<T> {
  if (!response.ok) {
    // FastAPI puts the human-readable reason in `detail`. Surfacing it matters
    // for uploads especially -- "the image is very dark" tells the user what to
    // do differently, where a generic failure does not.
    let detail = response.statusText;
    try {
      const body = await response.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(detail, response.status);
  }
  return response.json() as Promise<T>;
}

export async function uploadImage(
  file: File,
  context: { displayType?: string; campaignObjective?: string; heroProduct?: string },
): Promise<UploadResult> {
  const form = new FormData();
  form.append("file", file);
  if (context.displayType) form.append("display_type", context.displayType);
  if (context.campaignObjective) form.append("campaign_objective", context.campaignObjective);
  if (context.heroProduct) form.append("hero_product", context.heroProduct);

  const response = await fetch(`${API_URL}/api/uploads`, {
    method: "POST",
    headers: await authHeaders(),
    body: form,
  });
  return handle<UploadResult>(response);
}

/**
 * Run the pipeline over a stored upload.
 *
 * Returns as soon as the row exists -- the graph keeps running server-side and
 * the caller polls getAnalysis. Only the id and initial status come back, not a
 * finished analysis.
 */
export async function createAnalysis(
  uploadId: string,
  priorities: Priorities = BALANCED_PRIORITIES,
): Promise<{ id: string; status: Analysis["status"] }> {
  const response = await fetch(`${API_URL}/api/analyses`, {
    method: "POST",
    headers: { ...(await authHeaders()), "Content-Type": "application/json" },
    body: JSON.stringify({ upload_id: uploadId, priorities }),
  });
  return handle<{ id: string; status: Analysis["status"] }>(response);
}

export async function listAnalyses(): Promise<Analysis[]> {
  const response = await fetch(`${API_URL}/api/analyses`, {
    headers: await authHeaders(),
  });
  return handle<Analysis[]>(response);
}

export async function getAnalysis(id: string): Promise<Analysis> {
  const response = await fetch(`${API_URL}/api/analyses/${id}`, {
    headers: await authHeaders(),
  });
  return handle<Analysis>(response);
}

export async function submitFeedback(
  analysisId: string,
  body: {
    verdict: "useful" | "partly_useful" | "not_useful";
    perspective?: Perspective;
    item_index?: number;
    action_rank?: number;
    correction?: string;
  },
): Promise<{ feedback_id: string }> {
  const response = await fetch(`${API_URL}/api/analyses/${analysisId}/feedback`, {
    method: "POST",
    headers: { ...(await authHeaders()), "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return handle<{ feedback_id: string }>(response);
}

export type Brand = {
  brand_name: string | null;
  tone_of_voice: string | null;
  guidelines: string | null;
  colours: string[];
  fonts: string[];
  categories: string[];
};

export async function getBrand(): Promise<Partial<Brand>> {
  const response = await fetch(`${API_URL}/api/brand`, { headers: await authHeaders() });
  return handle<Partial<Brand>>(response);
}

/** Administrators only; the API returns 403 for standard users. */
export async function updateBrand(brand: Brand): Promise<{ brand_name: string }> {
  const response = await fetch(`${API_URL}/api/brand`, {
    method: "PUT",
    headers: { ...(await authHeaders()), "Content-Type": "application/json" },
    body: JSON.stringify(brand),
  });
  return handle<{ brand_name: string }>(response);
}

export const PERSPECTIVE_LABELS: Record<Perspective, string> = {
  creative_vm: "Creative / Visual Merchandising",
  retail_psychology: "Retail Psychology & Customer Behaviour",
  commercial: "Commercial Performance",
};

/**
 * What a higher slider buys, in the client's own words from the design deck.
 * Shown under each slider so the setting is self-explanatory on the screen
 * rather than in a help page nobody opens.
 */
export const PRIORITY_EFFECTS: Record<Perspective, string> = {
  creative_vm: "More on styling, composition and brand presentation",
  retail_psychology: "More on eye flow, attention and shopper behaviour",
  commercial: "More on hero products, pricing and offer clarity",
};

export const EFFORT_LABELS: Record<PrioritisedAction["effort"], string> = {
  quick_win: "Quick win",
  moderate: "Moderate",
  major_change: "Major change",
};
