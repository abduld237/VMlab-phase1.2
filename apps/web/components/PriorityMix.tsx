"use client";

/**
 * The three specialist sliders, set before an analysis runs.
 *
 * They always total 100. That constraint is the whole design: a mix is a
 * division of one review between three reviewers, not three independent dials,
 * so raising one has to lower the others and the user can see what it costs.
 * The API enforces the same rule and answers 422 rather than rescaling, so
 * getting the arithmetic exactly right here is what stops a legitimate drag
 * from being rejected on submit.
 *
 * The floor of 10 is not cosmetic. Three sliders and a fixed total means zero
 * is always reachable, and zero would silence a perspective the PRD requires
 * every analysis to carry. Ten percent is quiet, not absent -- and the ceiling
 * of 80 follows from it, since the other two cannot go below 10 between them.
 */

import { useEffect, useState } from "react";
import {
  BALANCED_PRIORITIES,
  PERSPECTIVE_LABELS,
  PRIORITY_EFFECTS,
  PRIORITY_MAX,
  PRIORITY_MIN,
  PRIORITY_TOTAL,
  type Perspective,
  type Priorities,
} from "@/lib/api";

const ORDER: Perspective[] = ["creative_vm", "retail_psychology", "commercial"];

const BAR: Record<Perspective, string> = {
  creative_vm: "bg-creative",
  retail_psychology: "bg-psychology",
  commercial: "bg-commercial",
};

const ACCENT: Record<Perspective, string> = {
  creative_vm: "accent-creative",
  retail_psychology: "accent-psychology",
  commercial: "accent-commercial",
};

const STORAGE_KEY = "vmlab.priorities";

/**
 * Move one slider and take the difference from the other two.
 *
 * Proportional to headroom above the floor rather than to the raw values, which
 * is what keeps a slider already sitting at 10 from being pushed below it and
 * then clamped back -- clamping after the fact is how the total drifts off 100.
 * Whatever integer remains after rounding goes to whichever of the two has more
 * room to absorb it.
 */
function redistribute(current: Priorities, moved: Perspective, next: number): Priorities {
  const target = Math.max(PRIORITY_MIN, Math.min(PRIORITY_MAX, Math.round(next)));
  const others = ORDER.filter((p) => p !== moved);
  const remaining = PRIORITY_TOTAL - target;

  const headroom = others.map((p) => current[p] - PRIORITY_MIN);
  const spare = headroom[0] + headroom[1];
  const distributable = remaining - PRIORITY_MIN * others.length;

  const share = others.map((p, index) =>
    spare === 0
      ? // Both already at the floor, so there is nothing to take proportionally
        // to. Split what is available evenly and let the residue below settle it.
        PRIORITY_MIN + Math.floor(distributable / 2)
      : PRIORITY_MIN + Math.round((headroom[index] / spare) * distributable),
  );

  const clamped = share.map((value) =>
    Math.max(PRIORITY_MIN, Math.min(PRIORITY_MAX, value)),
  );
  const residue = remaining - (clamped[0] + clamped[1]);
  // Give the leftover to whichever can take it without breaching a bound.
  const receiver = residue > 0
    ? clamped[0] <= clamped[1] ? 0 : 1
    : clamped[0] >= clamped[1] ? 0 : 1;
  clamped[receiver] += residue;

  return {
    ...current,
    [moved]: target,
    [others[0]]: clamped[0],
    [others[1]]: clamped[1],
  } as Priorities;
}

/** The last mix this browser used, or balanced. */
function remembered(): Priorities {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (!stored) return BALANCED_PRIORITIES;
    const parsed = JSON.parse(stored) as Priorities;
    const total = ORDER.reduce((sum, p) => sum + (parsed?.[p] ?? 0), 0);
    // Validate rather than trust: a stale or hand-edited value would otherwise
    // reach the API and come back as a 422 the user cannot explain.
    const legal = ORDER.every(
      (p) => Number.isInteger(parsed?.[p]) && parsed[p] >= PRIORITY_MIN && parsed[p] <= PRIORITY_MAX,
    );
    return legal && total === PRIORITY_TOTAL ? parsed : BALANCED_PRIORITIES;
  } catch {
    // A private window, blocked site data, or malformed JSON. None of them are
    // worth failing the page over.
    return BALANCED_PRIORITIES;
  }
}

export default function PriorityMix({
  value,
  onChange,
  disabled,
}: {
  value: Priorities;
  onChange: (next: Priorities) => void;
  disabled?: boolean;
}) {
  // Read on mount rather than during render: localStorage does not exist on the
  // server, and touching it during the first render mismatches the markup Next
  // prerendered.
  const [restored, setRestored] = useState(false);
  useEffect(() => {
    if (restored) return;
    setRestored(true);
    const stored = remembered();
    if (ORDER.some((p) => stored[p] !== value[p])) onChange(stored);
    // Runs once; onChange and value would otherwise re-trigger it on every edit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [restored]);

  function apply(mix: Priorities) {
    onChange(mix);
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(mix));
    } catch {
      /* the mix is still applied to this analysis; only the memory is lost */
    }
  }

  function set(perspective: Perspective, next: number) {
    apply(redistribute(value, perspective, next));
  }

  const balanced = ORDER.every((p) => value[p] === BALANCED_PRIORITIES[p]);

  return (
    <section className="mt-6">
      <div className="flex items-baseline justify-between gap-4">
        <h2 className="text-sm font-semibold text-slate-700">Priority mix</h2>
        {!balanced && (
          <button
            type="button"
            // Set outright, not through redistribute: moving one slider back to
            // 34 spreads the remaining 66 in proportion to where the other two
            // happen to be, which lands somewhere lopsided rather than balanced.
            onClick={() => apply(BALANCED_PRIORITIES)}
            className="text-xs font-medium text-slate-500 underline"
          >
            Reset to balanced
          </button>
        )}
      </div>
      <p className="mt-1 text-sm text-slate-600">
        Decide how much of this review each specialist takes. Raising one lowers
        the others — the three always add up to 100%.
      </p>

      {/* The deck's "Live Balance" panel, reduced to the part that carries the
          information: one bar showing how the review is divided. */}
      <div
        className="mt-3 flex h-2 overflow-hidden rounded-full bg-slate-200"
        role="presentation"
      >
        {ORDER.map((perspective) => (
          <div
            key={perspective}
            className={BAR[perspective]}
            style={{ width: `${value[perspective]}%` }}
          />
        ))}
      </div>

      <div className="mt-4 space-y-4">
        {ORDER.map((perspective) => (
          <div key={perspective}>
            <div className="flex items-baseline justify-between gap-3">
              <label
                htmlFor={`priority-${perspective}`}
                className="text-sm font-medium text-slate-700"
              >
                {PERSPECTIVE_LABELS[perspective]}
              </label>
              <span className="text-sm font-semibold tabular-nums text-slate-900">
                {value[perspective]}%
              </span>
            </div>
            <input
              id={`priority-${perspective}`}
              type="range"
              min={PRIORITY_MIN}
              max={PRIORITY_MAX}
              step={5}
              value={value[perspective]}
              disabled={disabled}
              onChange={(event) => set(perspective, Number(event.target.value))}
              className={`mt-1 w-full ${ACCENT[perspective]} disabled:opacity-50`}
              aria-describedby={`priority-${perspective}-effect`}
            />
            <p id={`priority-${perspective}-effect`} className="text-xs text-slate-500">
              {PRIORITY_EFFECTS[perspective]}
            </p>
          </div>
        ))}
      </div>
    </section>
  );
}
