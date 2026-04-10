import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}"
  ],
  theme: {
    extend: {
      colors: {
        ink: "#111827",
        mist: "#f5f8fc",
        signal: "#0f766e",
        ember: "#b45309",
        rose: "#be123c",
        steel: "#1f2937"
      },
      boxShadow: {
        panel: "0 18px 40px rgba(15, 23, 42, 0.08)"
      },
      backgroundImage: {
        halo:
          "radial-gradient(circle at top left, rgba(20, 184, 166, 0.18), transparent 38%), radial-gradient(circle at top right, rgba(245, 158, 11, 0.14), transparent 32%), linear-gradient(180deg, #f8fbff 0%, #eef6ff 100%)"
      }
    }
  },
  plugins: []
};

export default config;
