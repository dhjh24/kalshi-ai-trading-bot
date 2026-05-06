export default function Loading() {
  return (
    <div className="space-y-6 animate-pulse">
      <div className="h-4 w-40 rounded-full bg-slate-200" />
      <div className="grid gap-4 lg:grid-cols-2">
        <div className="h-44 rounded-[28px] bg-slate-200/85" />
        <div className="h-44 rounded-[28px] bg-slate-200/85" />
      </div>
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <div className="h-24 rounded-[24px] bg-slate-200/85" />
        <div className="h-24 rounded-[24px] bg-slate-200/85" />
        <div className="h-24 rounded-[24px] bg-slate-200/85" />
        <div className="h-24 rounded-[24px] bg-slate-200/85" />
      </div>
      <div className="h-80 rounded-[28px] bg-slate-200/85" />
      <div className="h-80 rounded-[28px] bg-slate-200/85" />
    </div>
  );
}
