import { createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/api/news")({
  server: {
    handlers: {
      GET: async ({ request }) => {
        const url    = new URL(request.url);
        const symbol = url.searchParams.get("symbol");

        try {
          // Yahoo Finance — no API key needed
          const query = symbol
            ? symbol
            : "stock market today";

          const res = await fetch(
            `https://query1.finance.yahoo.com/v1/finance/search?q=${encodeURIComponent(query)}&newsCount=15&enableFuzzyQuery=false`,
            { headers: { "User-Agent": "Mozilla/5.0" } }
          );

          if (!res.ok) {
            return Response.json({ error: `Yahoo ${res.status}` }, { status: 502 });
          }

          const data  = await res.json();
          const items = (data.news ?? []).map((item: Record<string, unknown>) => ({
            headline: item.title,
            source:   item.publisher,
            url:      item.link,
            datetime: item.providerPublishTime,
            summary:  item.title,
          }));

          return new Response(JSON.stringify(items), {
            status: 200,
            headers: {
              "Content-Type":  "application/json",
              "Cache-Control": "public, max-age=60",
            },
          });
        } catch (e) {
          return Response.json({ error: (e as Error).message }, { status: 500 });
        }
      },
    },
  },
});
