"use client";

/**
 * Analysis history (FR-13).
 *
 * Scoped to the workspace rather than the individual: a merchandiser and their
 * manager reviewing the same store should see the same history. The API derives
 * the workspace from the token, so this page sends nothing that could widen it.
 */

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ApiError, getSupabase, listAnalyses, type Analysis } from "@/lib/api";

function when(iso: string): string {
  const date = new Date(iso);
  const minutes = Math.round((Date.now() - date.getTime()) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  if (minutes < 24 * 60) return `${Math.round(minutes / 60)} h ago`;
  return date.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

const STATUS_STYLES: Record<string, string> = {
  complete: "bg-emerald-50 text-emerald-800 border-emerald-200",
  failed: "bg-red-50 text-red-800 border-red-200",
};

export default function HistoryPage() {
  const router = useRouter();
  const [analyses, setAnalyses] = useState<Analysis[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      const { data } = await getSupabase().auth.getSession();
      if (!data.session) {
        router.replace("/login");
        return;
      }
      try {
        const rows = await listAnalyses();
        if (!cancelled) setAnalyses(rows);
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Could not load your history.");
        }
      }
    }

    load();
    return () => {
      cancelled = true;
    };
  }, [router]);

  return (
    <main className="py-8">
      <header className="mb-6 flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold text-slate-900">Past reviews</h1>
          <p className="mt-1 text-sm text-slate-600">Everything analysed in this workspace.</p>
        </div>
        <Link href="/" className="shrink-0 text-sm font-medium underline">
          New review
        </Link>
      </header>

      {error && (
        <p className="rounded-lg border border-red-300 bg-red-50 p-3 text-sm text-red-800">
          {error}
        </p>
      )}

      {!analyses && !error && <p className="text-slate-600">Loading…</p>}

      {analyses?.length === 0 && (
        <div className="rounded-lg border border-dashed border-slate-300 p-8 text-center">
          <p className="text-slate-600">No reviews yet.</p>
          <Link href="/" className="mt-2 inline-block text-sm font-medium underline">
            Analyse your first display
          </Link>
        </div>
      )}

      <ul className="space-y-3">
        {analyses?.map((analysis) => (
          <li key={analysis.id}>
            <Link
              href={`/analysis/${analysis.id}`}
              className="block rounded-lg border border-slate-200 bg-white p-4 transition
                         hover:border-slate-400 focus:outline-none focus:ring-2 focus:ring-slate-400"
            >
              <div className="flex items-center justify-between gap-3">
                <span className="text-xs text-slate-500">{when(analysis.created_at)}</span>
                <span
                  className={`rounded-full border px-2 py-0.5 text-xs ${
                    STATUS_STYLES[analysis.status] ?? "border-slate-200 bg-slate-50 text-slate-600"
                  }`}
                >
                  {analysis.status === "complete"
                    ? "Complete"
                    : analysis.status === "failed"
                      ? "Failed"
                      : "In progress"}
                </span>
              </div>
              <p className="mt-2 line-clamp-2 text-sm text-slate-800">
                {analysis.overall_summary ??
                  (analysis.status === "failed"
                    ? "This review did not complete."
                    : "Still analysing…")}
              </p>
            </Link>
          </li>
        ))}
      </ul>
    </main>
  );
}
