"use client";

/**
 * Screens 2 and 3: capture or upload, add optional context, submit.
 *
 * `capture="environment"` on the file input is what makes a phone open the rear
 * camera directly rather than the photo library, which is the difference
 * between a merchandiser photographing the display in front of them and hunting
 * through their gallery afterwards.
 */

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ApiError, createAnalysis, getSupabase, uploadImage } from "@/lib/api";

export default function CapturePage() {
  const router = useRouter();
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [displayType, setDisplayType] = useState("");
  const [campaignObjective, setCampaignObjective] = useState("");
  const [heroProduct, setHeroProduct] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    getSupabase().auth.getSession().then(({ data }) => {
      if (!data.session) router.replace("/login");
    });
  }, [router]);

  // Object URLs leak if they are not revoked; on a phone, repeatedly retaking a
  // photo would otherwise pin every previous frame in memory.
  useEffect(() => {
    if (!file) {
      setPreview(null);
      return;
    }
    const url = URL.createObjectURL(file);
    setPreview(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);

  async function submit() {
    if (!file) return;
    setBusy(true);
    setError(null);
    setWarnings([]);

    try {
      const result = await uploadImage(file, {
        displayType: displayType || undefined,
        campaignObjective: campaignObjective || undefined,
        heroProduct: heroProduct || undefined,
      });
      if (result.quality_flags.length > 0) setWarnings(result.quality_flags);

      // Starting the analysis returns as soon as the row exists; the graph runs
      // on the server and the analysis page polls it. Routing on the analysis
      // id rather than the upload id is what makes that page addressable -- the
      // user can close the tab and come back to the same run.
      const analysis = await createAnalysis(result.upload_id);
      router.push(`/analysis/${analysis.id}`);
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Something went wrong. Please try again.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="py-8">
      <header className="mb-6 flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold text-slate-900">Review a display</h1>
          <p className="mt-1 text-sm text-slate-600">
            Photograph the display straight on, at eye level, filling most of the frame.
          </p>
        </div>
        <div className="flex shrink-0 gap-4">
          <Link href="/brand" className="text-sm font-medium underline">
            Brand
          </Link>
          <Link href="/history" className="text-sm font-medium underline">
            Past reviews
          </Link>
        </div>
      </header>

      <input
        ref={inputRef}
        type="file"
        accept="image/jpeg,image/png,image/heic"
        capture="environment"
        className="sr-only"
        onChange={(event) => setFile(event.target.files?.[0] ?? null)}
      />

      {preview ? (
        <div className="space-y-3">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={preview}
            alt="The display you photographed"
            className="w-full rounded-lg border border-slate-200 object-cover"
          />
          <button
            onClick={() => inputRef.current?.click()}
            className="text-sm font-medium text-slate-700 underline"
          >
            Retake or choose another
          </button>
        </div>
      ) : (
        <button
          onClick={() => inputRef.current?.click()}
          className="flex h-56 w-full flex-col items-center justify-center gap-2 rounded-lg
                     border-2 border-dashed border-slate-300 bg-white text-slate-600
                     transition hover:border-slate-400 focus:outline-none focus:ring-2
                     focus:ring-slate-400"
        >
          <span className="text-3xl" aria-hidden>
            📷
          </span>
          <span className="font-medium">Take a photo or upload one</span>
          <span className="text-xs text-slate-500">JPEG, PNG or HEIC</span>
        </button>
      )}

      <section className="mt-6 space-y-4">
        <h2 className="text-sm font-semibold text-slate-700">
          Context <span className="font-normal text-slate-500">(optional)</span>
        </h2>

        {[
          { label: "Display type", value: displayType, set: setDisplayType, hint: "Window, end-cap, table…" },
          { label: "Campaign or objective", value: campaignObjective, set: setCampaignObjective, hint: "Summer sale, new arrivals…" },
          { label: "Hero product", value: heroProduct, set: setHeroProduct, hint: "The product this should sell" },
        ].map((field) => (
          <label key={field.label} className="block">
            <span className="text-sm font-medium text-slate-700">{field.label}</span>
            <input
              type="text"
              value={field.value}
              placeholder={field.hint}
              onChange={(event) => field.set(event.target.value)}
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-base
                         focus:border-slate-500 focus:outline-none focus:ring-1 focus:ring-slate-500"
            />
          </label>
        ))}
      </section>

      {warnings.length > 0 && (
        <div className="mt-4 rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">
          <p className="font-medium">This photo may limit the analysis</p>
          <ul className="mt-1 list-inside list-disc">
            {warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </div>
      )}

      {error && (
        <div className="mt-4 rounded-lg border border-red-300 bg-red-50 p-3 text-sm text-red-800">
          {error}
        </div>
      )}

      <button
        onClick={submit}
        disabled={!file || busy}
        className="mt-6 w-full rounded-lg bg-slate-900 py-3 text-base font-medium text-white
                   transition disabled:cursor-not-allowed disabled:bg-slate-300
                   hover:bg-slate-800 focus:outline-none focus:ring-2 focus:ring-slate-500"
      >
        {busy ? "Starting analysis…" : "Start analysis"}
      </button>
    </main>
  );
}
