"use client";

/**
 * Screen 1: sign in. Magic link rather than a password, so no credential is
 * ever typed into or stored by this application.
 *
 * A password path exists behind NEXT_PUBLIC_ALLOW_PASSWORD_LOGIN, off unless
 * explicitly set. It is there because magic links need working email: Supabase's
 * built-in sender allows only a handful of messages an hour and will not deliver
 * to a made-up domain at all, so with links alone nobody can sign in locally or
 * demonstrate the product. Configure real SMTP before a pilot and leave this
 * flag unset in production.
 */

import { useState } from "react";
import { useRouter } from "next/navigation";
import { getSupabase } from "@/lib/api";

const PASSWORD_LOGIN = process.env.NEXT_PUBLIC_ALLOW_PASSWORD_LOGIN === "true";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function signIn(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);

    if (PASSWORD_LOGIN && password) {
      const { error } = await getSupabase().auth.signInWithPassword({ email, password });
      if (error) setError(error.message);
      else router.replace("/");
      setBusy(false);
      return;
    }

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

          {PASSWORD_LOGIN && (
            <label className="block">
              <span className="text-sm font-medium text-slate-700">
                Password{" "}
                <span className="font-normal text-slate-500">
                  (leave blank to be sent a link)
                </span>
              </span>
              <input
                type="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                autoComplete="current-password"
                className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-base
                           focus:border-slate-500 focus:outline-none focus:ring-1 focus:ring-slate-500"
              />
            </label>
          )}

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
            {busy
              ? "Signing in…"
              : PASSWORD_LOGIN && password
                ? "Sign in"
                : "Email me a sign-in link"}
          </button>
        </form>
      )}
    </main>
  );
}
