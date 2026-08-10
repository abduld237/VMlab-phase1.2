"use client";

/**
 * Screen 1: sign in. Magic link rather than a password, so no credential is
 * ever typed into or stored by this application.
 */

import { useState } from "react";
import { getSupabase } from "@/lib/api";

export default function LoginPage() {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function signIn(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    const { error } = await getSupabase().auth.signInWithOtp({
      email,
      options: { emailRedirectTo: typeof window !== "undefined" ? window.location.origin : undefined },
    });
    if (error) setError(error.message);
    else setSent(true);
    setBusy(false);
  }

  return (
    <main className="flex min-h-screen flex-col justify-center py-12">
      <h1 className="text-2xl font-semibold text-slate-900">VMlab</h1>
      <p className="mt-1 text-sm text-slate-600">Sign in to your retailer workspace.</p>

      {sent ? (
        <div className="mt-8 rounded-lg border border-slate-200 bg-white p-4 text-sm text-slate-700">
          Check <span className="font-medium">{email}</span> for a sign-in link.
        </div>
      ) : (
        <form onSubmit={signIn} className="mt-8 space-y-4">
          <label className="block">
            <span className="text-sm font-medium text-slate-700">Work email</span>
            <input
              type="email"
              required
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              autoComplete="email"
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-base
                         focus:border-slate-500 focus:outline-none focus:ring-1 focus:ring-slate-500"
            />
          </label>

          {error && (
            <p className="rounded-lg border border-red-300 bg-red-50 p-3 text-sm text-red-800">
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={busy}
            className="w-full rounded-lg bg-slate-900 py-3 font-medium text-white
                       disabled:bg-slate-300 hover:bg-slate-800"
          >
            {busy ? "Sending…" : "Email me a sign-in link"}
          </button>
        </form>
      )}
    </main>
  );
}
