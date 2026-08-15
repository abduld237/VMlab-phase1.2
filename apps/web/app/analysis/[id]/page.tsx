"use client";

/**
 * Screens 4 and 5: processing states, then the result.
 *
 * The PRD (§7.1) asks for named progress states rather than a spinner, because
 * an analysis takes tens of seconds and an unlabelled wait reads as a hang. The
 * stages shown here mirror the actual pipeline, so what the user sees is what
 * the backend is really doing.
 */

import { useEffect, useRef, useState } from "react";
import { use } from "react";
import Link from "next/link";
import Results from "@/components/Results";
import { ApiError, getAnalysis, type Analysis } from "@/lib/api";

const STAGES: { key: Analysis["status"]; label: string }[] = [
  { key: "validating", label: "Checking the image" },
  { key: "extracting", label: "Reading the display" },
  { key: "retrieving", label: "Retrieving merchandising guidance" },
  { key: "reasoning", label: "Consulting the three specialists" },
  { key: "synthesising", label: "Prioritising the actions" },
];

const POLL_MS = 2000;
// Generous on purpose. A measured run against the live knowledge base took
// 237s, so the old 60s-era ceiling would have declared a healthy analysis dead.
// The server marks its own stalled rows failed, so this is only a backstop for
// the case where polling itself cannot reach it.
const GIVE_UP_MS = 600_000;

export default function AnalysisPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [error, setError] = useState<string | null>(null);
  const startedAt = useRef(Date.now());

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;

    async function poll() {
      try {
        const next = await getAnalysis(id);
        if (cancelled) return;
        setAnalysis(next);

        if (next.status === "complete" || next.status === "failed") return;

        if (Date.now() - startedAt.current > GIVE_UP_MS) {
          setError("This is taking longer than expected. Please try again.");
          return;
        }
        timer = setTimeout(poll, POLL_MS);
      } catch (err) {
        if (cancelled) return;
        setError(
          err instanceof ApiError && err.status === 404
            ? "That analysis could not be found."
            : "Could not load this analysis.",
        );
      }
    }

    poll();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [id]);

  if (error) {
    return (
      <main className="py-12">
        <p className="rounded-lg border border-red-300 bg-red-50 p-4 text-sm text-red-800">
          {error}
        </p>
        <Link href="/" className="mt-4 inline-block text-sm font-medium underline">
          Start another review
        </Link>
      </main>
    );
  }

  if (!analysis) {
    return (
      <main className="py-12">
        <p className="text-slate-600">Loading…</p>
      </main>
    );
  }

  if (analysis.status === "failed") {
    return (
      <main className="py-12">
        <h1 className="text-xl font-semibold text-slate-900">Analysis failed</h1>
        <p className="mt-2 text-sm text-slate-700">
          {analysis.error_detail ??
            "Something went wrong while analysing this image. Your photo was saved."}
        </p>
        <Link href="/" className="mt-4 inline-block text-sm font-medium underline">
          Try another photo
        </Link>
      </main>
    );
  }

  if (analysis.status !== "complete") {
    const currentIndex = STAGES.findIndex((stage) => stage.key === analysis.status);
    return (
      <main className="py-12">
        <h1 className="text-xl font-semibold text-slate-900">Analysing your display</h1>
        <ol className="mt-6 space-y-3" aria-live="polite">
          {STAGES.map((stage, index) => {
            const done = currentIndex > index;
            const active = currentIndex === index;
            return (
              <li key={stage.key} className="flex items-center gap-3 text-sm">
                <span
                  className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-full border text-xs ${
                    done
                      ? "border-slate-900 bg-slate-900 text-white"
                      : active
                        ? "border-slate-900 text-slate-900"
                        : "border-slate-300 text-slate-400"
                  }`}
                >
                  {done ? "✓" : index + 1}
                </span>
                <span className={active ? "font-medium text-slate-900" : "text-slate-500"}>
                  {stage.label}
                </span>
              </li>
            );
          })}
        </ol>
      </main>
    );
  }

  const totalMs = analysis.stage_timings_ms?.total;

  return (
    <main className="py-8">
      <header className="mb-6 flex items-start justify-between gap-4">
        <h1 className="text-2xl font-semibold text-slate-900">Display review</h1>
        <div className="flex shrink-0 gap-4">
          <Link href="/history" className="text-sm font-medium underline">
            Past reviews
          </Link>
          <Link href="/" className="text-sm font-medium underline">
            New review
          </Link>
        </div>
      </header>

      <Results analysis={analysis} />

      {totalMs != null && (
        <p className="mt-8 border-t border-slate-200 pt-4 text-xs text-slate-400">
          Completed in {(totalMs / 1000).toFixed(1)}s
        </p>
      )}
    </main>
  );
}
