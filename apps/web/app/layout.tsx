import type { Metadata, Viewport } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "VMlab — Display Analysis",
  description: "AI-assisted retail display review",
};

// Mobile-first is a PRD requirement, not a preference: the user is standing in
// front of a display with a phone, not at a desk.
export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  maximumScale: 1,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en-GB">
      <body>
        <div className="mx-auto min-h-screen w-full max-w-2xl px-4 pb-16">{children}</div>
      </body>
    </html>
  );
}
