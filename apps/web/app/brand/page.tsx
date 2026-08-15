"use client";

/**
 * Screen 2A: the workspace brand profile (FR-17).
 *
 * What is entered here is fed to the specialists as context, so a display is
 * judged against this retailer's own voice and categories rather than a generic
 * notion of good merchandising.
 *
 * Administrator-only, but enforced by the database rather than by hiding the
 * form: a standard user can open the page and will get a clear 403 on save. A
 * disabled input is a courtesy, not a control.
 */

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ApiError, getBrand, getSupabase, updateBrand, type Brand } from "@/lib/api";

const EMPTY: Brand = {
  brand_name: "",
  tone_of_voice: "",
  guidelines: "",
  colours: [],
  fonts: [],
  categories: [],
};

/** Comma-separated input is the fastest way to enter a short list on a phone. */
function toList(value: string): string[] {
  return value
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean);
}

export default function BrandPage() {
  const router = useRouter();
  const [brand, setBrand] = useState<Brand>(EMPTY);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      const { data } = await getSupabase().auth.getSession();
      if (!data.session) {
        router.replace("/login");
        return;
      }
      try {
        const existing = await getBrand();
        if (!cancelled) {
          setBrand({ ...EMPTY, ...existing });
          setLoaded(true);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Could not load the brand profile.");
          setLoaded(true);
        }
      }
    }

    load();
    return () => {
      cancelled = true;
    };
  }, [router]);

  async function save() {
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      await updateBrand(brand);
      setSaved(true);
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : "Could not save the brand profile. Please try again.",
      );
    } finally {
      setBusy(false);
    }
  }

  function set<K extends keyof Brand>(key: K, value: Brand[K]) {
    setBrand((current) => ({ ...current, [key]: value }));
    setSaved(false);
  }

  return (
    <main className="py-8">
      <header className="mb-6 flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold text-slate-900">Brand profile</h1>
          <p className="mt-1 text-sm text-slate-600">
            Used as context in every review, so recommendations match how this brand
            actually talks and what it sells.
          </p>
        </div>
        <Link href="/" className="shrink-0 text-sm font-medium underline">
          New review
        </Link>
      </header>

      {!loaded ? (
        <p className="text-slate-600">Loading…</p>
      ) : (
        <div className="space-y-5">
          <label className="block">
            <span className="text-sm font-medium text-slate-700">Brand name</span>
            <input
              type="text"
              value={brand.brand_name ?? ""}
              onChange={(event) => set("brand_name", event.target.value)}
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-base
                         focus:border-slate-500 focus:outline-none focus:ring-1 focus:ring-slate-500"
            />
          </label>

          <label className="block">
            <span className="text-sm font-medium text-slate-700">Tone of voice</span>
            <textarea
              rows={3}
              value={brand.tone_of_voice ?? ""}
              placeholder="Warm and practical; never pushy."
              onChange={(event) => set("tone_of_voice", event.target.value)}
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-base
                         focus:border-slate-500 focus:outline-none focus:ring-1 focus:ring-slate-500"
            />
          </label>

          {(
            [
              { key: "colours", label: "Brand colours", hint: "navy, cream, brass" },
              { key: "fonts", label: "Typefaces", hint: "Gill Sans, Caslon" },
              { key: "categories", label: "Categories sold", hint: "womenswear, homeware, gifting" },
            ] as const
          ).map((field) => (
            <label key={field.key} className="block">
              <span className="text-sm font-medium text-slate-700">{field.label}</span>
              <input
                type="text"
                value={brand[field.key].join(", ")}
                placeholder={field.hint}
                onChange={(event) => set(field.key, toList(event.target.value))}
                className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-base
                           focus:border-slate-500 focus:outline-none focus:ring-1 focus:ring-slate-500"
              />
              <span className="mt-1 block text-xs text-slate-500">Separate with commas</span>
            </label>
          ))}

          <label className="block">
            <span className="text-sm font-medium text-slate-700">
              Merchandising guidelines
            </span>
            <textarea
              rows={6}
              value={brand.guidelines ?? ""}
              placeholder="House rules a reviewer should judge against — fixture standards, signage conventions, anything a new merchandiser would need told."
              onChange={(event) => set("guidelines", event.target.value)}
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-base
                         focus:border-slate-500 focus:outline-none focus:ring-1 focus:ring-slate-500"
            />
          </label>

          {error && (
            <div className="rounded-lg border border-red-300 bg-red-50 p-3 text-sm text-red-800">
              {error}
            </div>
          )}

          {saved && (
            <div className="rounded-lg border border-emerald-300 bg-emerald-50 p-3 text-sm text-emerald-900">
              Saved. Future reviews will use this.
            </div>
          )}

          <button
            onClick={save}
            disabled={busy}
            className="w-full rounded-lg bg-slate-900 py-3 text-base font-medium text-white
                       transition disabled:cursor-not-allowed disabled:bg-slate-300
                       hover:bg-slate-800 focus:outline-none focus:ring-2 focus:ring-slate-500"
          >
            {busy ? "Saving…" : "Save brand profile"}
          </button>
        </div>
      )}
    </main>
  );
}
