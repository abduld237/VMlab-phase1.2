import type { Config } from "tailwindcss";

export default {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // One colour per specialist perspective. The PRD (7.1) requires the
        // three sections be visually distinct without implying a hierarchy, so
        // these are equal in weight and saturation rather than ranked.
        creative: "#7c3aed",
        psychology: "#0891b2",
        commercial: "#c2410c",
      },
    },
  },
  plugins: [],
} satisfies Config;
