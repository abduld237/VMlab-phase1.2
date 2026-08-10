"use client";

/**
 * The results view: three specialist sections, the prioritised top three, and
 * feedback controls.
 *
 * Two PRD constraints shape this file more than anything else. The three
 * perspectives must be visually distinct *without implying one is superior*
 * (§7.1) -- so each gets its own accent colour but identical typography,
 * spacing and ordering weight, and none is ever collapsed by default. And no
 * fabricated percentages or uplifts may be shown as fact, so confidence is
 * rendered as a qualitative band rather than a spurious "87% confident".
 */

import { useState } from "react";
import {
  EFFORT_LABELS,
  PERSPECTIVE_LABELS,
  submitFeedback,
  type Analysis,
  type Perspective,
  type PrioritisedAction,
  type SpecialistItem,
} from "@/lib/api";

const ACCENT: Record<Perspective, string> = {
  creative_vm: "border-creative/40 bg-creative/5",
  retail_psychology: "border-psychology/40 bg-psychology/5",
  commercial: "border-commercial/40 bg-commercial/5",
};

const DOT: Record<Perspective, string> = {
  creative_vm: "bg-creative",
  retail_psychology: "bg-psychology",
  commercial: "bg-commercial",
};

/** Confidence as a band, never a fabricated precise figure. */
function confidenceLabel(value: number | null): string {
  if (value === null) return "Unrated";
  if (value >= 0.75) return "High confidence";
  if (value >= 0.5) return "Moderate confidence";
  return "Low confidence";
}

function FeedbackButtons({
  analysisId,
  target,
}: {
  analysisId: string;
  target: { perspective?: Perspective; item_index?: number; action_rank?: number };
}) {
  const [sent, setSent] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  async function send(verdict: "useful" | "partly_useful" | "not_useful") {
    setFailed(false);
    try {
      await submitFeedback(analysisId, { verdict, ...target });
      setSent(verdict);
    } catch {
      setFailed(true);
    }
  }

  if (sent) {
    return <p className="mt-2 text-xs text-slate-500">Thanks — feedback recorded.</p>;
  }

  return (
    <div className="mt-3">
      <div className="flex flex-wrap gap-2">
        {(["useful", "partly_useful", "not_useful"] as const).map((verdict) => (
          <button
            key={verdict}
            onClick={() => send(verdict)}
            className="rounded-full border border-slate-300 px-3 py-1 text-xs font-medium
                       text-slate-700 transition hover:border-slate-500 hover:bg-white
                       focus:outline-none focus:ring-2 focus:ring-slate-400"
          >
            {verdict === "useful" ? "Useful" : verdict === "partly_useful" ? "Partly useful" : "Not useful"}
          </button>
        ))}
      </div>
      {failed && (
        <p className="mt-2 text-xs text-red-600">
          Could not record that — please try again.
        </p>
      )}
    </div>
  );
}

function Item({
  item,
  analysisId,
  perspective,
  index,
}: {
  item: SpecialistItem;
  analysisId: string;
  perspective: Perspective;
  index: number;
}) {
  return (
    <li className="border-t border-slate-200 py-4 first:border-t-0 first:pt-0">
      <p className="text-sm text-slate-600">{item.observation}</p>
      <p className="mt-2 font-medium text-slate-900">{item.recommendation}</p>
      <p className="mt-1 text-sm text-slate-600">{item.reason}</p>

      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500">
        <span>{confidenceLabel(item.confidence)}</span>
        {item.supporting_rule_ids.length > 0 && (
          // Rule IDs are the whole basis of the "grounded, not generic" claim,
          // so they are shown rather than hidden behind a disclosure.
          <span className="font-mono text-slate-400">
            {item.supporting_rule_ids.join(", ")}
          </span>
        )}
      </div>

      <FeedbackButtons
        analysisId={analysisId}
        target={{ perspective, item_index: index }}
      />
    </li>
  );
}

function ActionCard({
  action,
  analysisId,
}: {
  action: PrioritisedAction;
  analysisId: string;
}) {
  return (
    <li className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex items-start gap-3">
        <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full
                         bg-slate-900 text-sm font-semibold text-white">
          {action.rank}
        </span>
        <div className="min-w-0 flex-1">
          <p className="font-medium text-slate-900">{action.action}</p>
          <p className="mt-1 text-sm text-slate-600">{action.rationale}</p>

          {action.trade_off && (
            // Where specialists disagreed the PRD requires the trade-off be
            // stated, not hidden by silently picking a side.
            <p className="mt-2 rounded border-l-2 border-amber-400 bg-amber-50 px-3 py-2
                          text-sm text-amber-900">
              <span className="font-medium">Trade-off: </span>
              {action.trade_off}
            </p>
          )}

          <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
            <span className="rounded-full bg-slate-100 px-2 py-0.5 font-medium text-slate-700">
              {EFFORT_LABELS[action.effort]}
            </span>
            <span className="text-slate-500">{confidenceLabel(action.confidence)}</span>
          </div>

          <FeedbackButtons analysisId={analysisId} target={{ action_rank: action.rank }} />
        </div>
      </div>
    </li>
  );
}

export default function Results({ analysis }: { analysis: Analysis }) {
  const ordered: Perspective[] = ["creative_vm", "retail_psychology", "commercial"];
  const present = new Set(analysis.sections.map((s) => s.perspective));
  const missing = ordered.filter((p) => !present.has(p));

  return (
    <div className="space-y-8">
      {analysis.overall_summary && (
        <section>
          <h2 className="text-lg font-semibold text-slate-900">Summary</h2>
          <p className="mt-2 text-slate-700">{analysis.overall_summary}</p>
        </section>
      )}

      {(analysis.uncertainty_note || missing.length > 0) && (
        <div className="rounded-lg border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900">
          <p className="font-medium">Read this result with care</p>
          {analysis.uncertainty_note && <p className="mt-1">{analysis.uncertainty_note}</p>}
          {missing.length > 0 && (
            <p className="mt-1">
              Missing perspective{missing.length > 1 ? "s" : ""}:{" "}
              {missing.map((p) => PERSPECTIVE_LABELS[p]).join(", ")}.
            </p>
          )}
        </div>
      )}

      {analysis.actions.length > 0 && (
        <section>
          <h2 className="text-lg font-semibold text-slate-900">Top priorities</h2>
          <ul className="mt-3 space-y-3">
            {analysis.actions.map((action) => (
              <ActionCard key={action.rank} action={action} analysisId={analysis.id} />
            ))}
          </ul>
        </section>
      )}

      <section>
        <h2 className="text-lg font-semibold text-slate-900">Specialist reviews</h2>
        <div className="mt-3 space-y-4">
          {ordered.map((perspective) => {
            const section = analysis.sections.find((s) => s.perspective === perspective);
            if (!section) return null;
            return (
              <article
                key={perspective}
                className={`rounded-lg border p-4 ${ACCENT[perspective]}`}
              >
                <h3 className="flex items-center gap-2 font-semibold text-slate-900">
                  <span className={`h-2.5 w-2.5 rounded-full ${DOT[perspective]}`} />
                  {PERSPECTIVE_LABELS[perspective]}
                </h3>
                <ul className="mt-3">
                  {section.items.map((item, index) => (
                    <Item
                      key={index}
                      item={item}
                      index={index}
                      perspective={perspective}
                      analysisId={analysis.id}
                    />
                  ))}
                </ul>
              </article>
            );
          })}
        </div>
      </section>

      <section className="border-t border-slate-200 pt-4">
        <h2 className="text-sm font-semibold text-slate-700">Overall</h2>
        <FeedbackButtons analysisId={analysis.id} target={{}} />
      </section>
    </div>
  );
}
