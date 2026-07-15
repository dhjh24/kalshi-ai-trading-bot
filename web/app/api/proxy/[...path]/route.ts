import { NextRequest, NextResponse } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

function resolveUpstreamBase(): string {
  return (
    process.env.DASHBOARD_API_INTERNAL_URL ||
    process.env.DASHBOARD_API_URL ||
    process.env.NEXT_PUBLIC_DASHBOARD_API_URL ||
    "http://127.0.0.1:4000"
  ).replace(/\/$/, "");
}

async function proxyRequest(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> }
): Promise<NextResponse> {
  const { path } = await context.params;
  if (!path?.length) {
    return NextResponse.json({ ok: false, error: "missing_proxy_path" }, { status: 400 });
  }

  const upstream = `${resolveUpstreamBase()}/${path.join("/")}${request.nextUrl.search}`;
  const headers = new Headers();
  const contentType = request.headers.get("content-type");
  if (contentType) {
    headers.set("content-type", contentType);
  }

  const token = (process.env.DASHBOARD_API_TOKEN || "").trim();
  if (token) {
    headers.set("x-dashboard-token", token);
  }

  const init: RequestInit = {
    method: request.method,
    headers,
    cache: "no-store"
  };

  if (request.method !== "GET" && request.method !== "HEAD") {
    init.body = await request.arrayBuffer();
  }

  try {
    const upstreamResponse = await fetch(upstream, init);
    const responseHeaders = new Headers();
    const upstreamContentType = upstreamResponse.headers.get("content-type");
    if (upstreamContentType) {
      responseHeaders.set("content-type", upstreamContentType);
    }

    return new NextResponse(upstreamResponse.body, {
      status: upstreamResponse.status,
      headers: responseHeaders
    });
  } catch (error) {
    return NextResponse.json(
      {
        ok: false,
        error: "proxy_upstream_unreachable",
        message: error instanceof Error ? error.message : "Upstream request failed"
      },
      { status: 502 }
    );
  }
}

export const POST = proxyRequest;
export const PUT = proxyRequest;
export const PATCH = proxyRequest;
export const DELETE = proxyRequest;
