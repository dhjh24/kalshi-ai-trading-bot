import clsx from "clsx";
import Link from "next/link";
import { ReactNode } from "react";

const NAV_LINKS = [
  {
    href: "/",
    label: "Overview"
  },
  {
    href: "/live-trade",
    label: "Live Trade"
  },
  {
    href: "/quick-flip",
    label: "Quick Flip"
  },
  {
    href: "/markets",
    label: "Markets"
  },
  {
    href: "/portfolio",
    label: "Portfolio"
  },
  {
    href: "/safety",
    label: "Safety"
  },
  {
    href: "/analysis",
    label: "Analysis"
  }
];

export function AppFrame({ children }: { children: ReactNode }) {
  return (
    <div className="min-h-screen bg-halo text-ink">
      <header className="border-b border-white/60 bg-white/80 backdrop-blur">
        <div className="mx-auto flex max-w-7xl flex-col gap-4 px-6 py-5 lg:flex-row lg:items-center lg:justify-between">
          <div>
            <p className="text-xs uppercase tracking-[0.35em] text-signal">
              Kalshi AI Trading Bot
            </p>
            <div className="mt-1 flex flex-wrap items-center gap-3">
              <h1 className="font-serif text-2xl font-semibold text-steel">
                Node Dashboard
              </h1>
              <HeaderModeBadge />
            </div>
          </div>
          <nav className="grid w-full grid-cols-2 gap-2 text-sm font-medium text-slate-600 sm:flex sm:w-auto sm:flex-wrap">
            {NAV_LINKS.map((route) => (
              <NavLink key={route.href} href={route.href}>
                {route.label}
              </NavLink>
            ))}
          </nav>
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-6 py-8">{children}</main>
    </div>
  );
}

function parseEnvBoolean(value: string | undefined): boolean | null {
  if (!value) {
    return null;
  }

  const normalized = value.trim().toLowerCase();
  if (["1", "true", "yes", "on", "enabled"].includes(normalized)) {
    return true;
  }
  if (["0", "false", "no", "off", "disabled"].includes(normalized)) {
    return false;
  }
  return null;
}

function HeaderModeBadge() {
  const live =
    parseEnvBoolean(process.env.LIVE_TRADING_ENABLED) ??
    parseEnvBoolean(process.env.NEXT_PUBLIC_LIVE_TRADING_ENABLED);
  const shadow =
    parseEnvBoolean(process.env.SHADOW_MODE_ENABLED) ??
    parseEnvBoolean(process.env.NEXT_PUBLIC_SHADOW_MODE_ENABLED);
  const paper =
    parseEnvBoolean(process.env.PAPER_TRADING_MODE) ??
    parseEnvBoolean(process.env.NEXT_PUBLIC_PAPER_TRADING_MODE) ??
    (live === true || shadow === true ? false : true);
  const label = live ? "Live default" : shadow ? "Shadow default" : paper ? "Paper mode" : "Mode unset";
  const className = live
    ? "border-rose-200 bg-rose-50 text-rose-700"
    : shadow
      ? "border-amber-200 bg-amber-50 text-amber-700"
      : paper
        ? "border-emerald-200 bg-emerald-50 text-emerald-700"
        : "border-slate-200 bg-slate-50 text-slate-600";

  return (
    <span className={clsx("rounded-full border px-3 py-1 text-xs font-semibold", className)}>
      {label}
    </span>
  );
}

function NavLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <Link
      href={href}
      className="rounded-full border border-slate-200 bg-white px-4 py-2 text-center transition hover:border-signal hover:text-signal"
    >
      {children}
    </Link>
  );
}

export function Panel({
  title,
  eyebrow,
  children,
  className
}: {
  title?: string;
  eyebrow?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section
      className={clsx(
        "rounded-[28px] border border-white/70 bg-white/85 p-6 shadow-panel backdrop-blur",
        className
      )}
    >
      {(eyebrow || title) && (
        <div className="mb-4">
          {eyebrow ? (
            <p className="text-xs uppercase tracking-[0.35em] text-slate-400">{eyebrow}</p>
          ) : null}
          {title ? (
            <h2 className="mt-2 break-words text-xl font-semibold text-steel">{title}</h2>
          ) : null}
        </div>
      )}
      {children}
    </section>
  );
}

export function StatCard({
  label,
  value,
  tone = "default",
  helpText
}: {
  label: string;
  value: string;
  tone?: "default" | "positive" | "warning" | "negative";
  helpText?: string;
}) {
  const toneClasses = {
    default: "text-steel",
    positive: "text-signal",
    warning: "text-ember",
    negative: "text-rose"
  }[tone];

  return (
    <div className="rounded-[24px] border border-slate-100 bg-slate-50/90 p-5">
      <p className="text-xs uppercase tracking-[0.28em] text-slate-500">{label}</p>
      <p className={clsx("mt-3 text-3xl font-semibold", toneClasses)}>{value}</p>
      {helpText ? <p className="mt-2 text-sm text-slate-500">{helpText}</p> : null}
    </div>
  );
}

export function Badge({
  children,
  tone = "neutral"
}: {
  children: ReactNode;
  tone?: "neutral" | "positive" | "warning" | "negative";
}) {
  const toneClasses = {
    neutral: "bg-slate-100 text-slate-700",
    positive: "bg-emerald-100 text-emerald-700",
    warning: "bg-amber-100 text-amber-700",
    negative: "bg-rose-100 text-rose-700"
  }[tone];

  return (
    <span className={clsx("rounded-full px-3 py-1 text-xs font-semibold", toneClasses)}>
      {children}
    </span>
  );
}

export function LlmTokenBadge({ children = "LLM" }: { children?: string }) {
  return (
    <span className="inline-flex items-center gap-1 rounded-full border border-amber-200 bg-amber-50 px-3 py-1 text-xs font-semibold text-amber-700">
      <span className="h-1.5 w-1.5 rounded-full bg-amber-500" />
      {children}
    </span>
  );
}

export function EmptyState({
  title,
  body
}: {
  title: string;
  body: string;
}) {
  return (
    <div className="rounded-[24px] border border-dashed border-slate-200 bg-slate-50/80 px-6 py-10 text-center">
      <h3 className="text-lg font-semibold text-steel">{title}</h3>
      <p className="mt-2 text-sm text-slate-500">{body}</p>
    </div>
  );
}
