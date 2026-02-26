from __future__ import annotations


def timeline_dashboard_html(api_base: str = "") -> str:
    base = api_base.rstrip("/")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Open Timeline Engine Dashboard</title>
  <style>
    :root {{
      --bg: #f7f8f2;
      --panel: #ffffff;
      --ink: #0f172a;
      --muted: #475569;
      --line: #d0d7de;
      --accent: #0b7a75;
      --warn: #9a3412;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at 10% 10%, #ddeedd 0, transparent 30%),
        radial-gradient(circle at 90% 5%, #e5f0ff 0, transparent 32%),
        var(--bg);
      min-height: 100vh;
    }}
    .wrap {{ max-width: 980px; margin: 0 auto; padding: 24px; }}
    h1 {{ font-family: "IBM Plex Serif", Georgia, serif; margin: 0 0 8px; }}
    .subtitle {{ color: var(--muted); margin: 0 0 20px; }}
    .grid {{ display: grid; gap: 14px; grid-template-columns: 1fr; }}
    @media (min-width: 860px) {{ .grid {{ grid-template-columns: 1.2fr .8fr; }} }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 16px;
      box-shadow: 0 8px 24px rgba(15, 23, 42, 0.06);
    }}
    label {{ font-weight: 700; font-size: 13px; display: block; margin-bottom: 6px; }}
    input, textarea, button, select {{
      width: 100%;
      border-radius: 10px;
      border: 1px solid var(--line);
      font: inherit;
      padding: 10px 12px;
    }}
    textarea {{ min-height: 70px; resize: vertical; }}
    .row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }}
    button {{
      background: var(--accent);
      color: #fff;
      font-weight: 700;
      cursor: pointer;
      border: 0;
    }}
    .muted {{ color: var(--muted); font-size: 13px; }}
    .warn {{ color: var(--warn); font-size: 13px; }}
    ul {{ margin: 10px 0 0; padding-left: 20px; }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Open Timeline Engine</h1>
    <p class="subtitle">Quick timeline explorer for events, patterns, and context bundles.</p>
    <div class="grid">
      <section class="card">
        <div class="row">
          <div>
            <label for="token">API token</label>
            <input id="token" placeholder="local-dev-token" />
          </div>
          <div>
            <label for="consumer">Consumer</label>
            <input id="consumer" value="dashboard-user" />
          </div>
        </div>
        <div class="row" style="margin-top:10px">
          <div>
            <label for="role">Role</label>
            <select id="role">
              <option value="user">user</option>
              <option value="executor">executor</option>
              <option value="advisor">advisor</option>
            </select>
          </div>
          <div>
            <label for="domain">Domain filter</label>
            <input id="domain" placeholder="coding" />
          </div>
        </div>
        <label for="query" style="margin-top:10px">Search query</label>
        <input id="query" value="recent work" />
        <button id="loadBtn" style="margin-top:10px">Load Timeline Context</button>
        <p id="status" class="muted"></p>
        <div id="errors" class="warn"></div>
      </section>

      <section class="card">
        <h3 style="margin-top:0">Bundle Summary</h3>
        <p id="summary" class="muted">No data yet.</p>
        <h4>Do</h4>
        <ul id="doList"></ul>
        <h4>Don't</h4>
        <ul id="dontList"></ul>
      </section>
    </div>

    <section class="card" style="margin-top:14px">
      <h3 style="margin-top:0">Recent Events</h3>
      <ul id="eventList"></ul>
    </section>

    <section class="card" style="margin-top:14px">
      <h3 style="margin-top:0">Top Patterns</h3>
      <ul id="patternList"></ul>
    </section>
  </div>

  <script>
    const API_BASE = "{base}";
    const statusEl = document.getElementById("status");
    const errEl = document.getElementById("errors");
    const summaryEl = document.getElementById("summary");
    const doList = document.getElementById("doList");
    const dontList = document.getElementById("dontList");
    const eventList = document.getElementById("eventList");
    const patternList = document.getElementById("patternList");
    const tokenInput = document.getElementById("token");
    const consumerInput = document.getElementById("consumer");
    const roleInput = document.getElementById("role");
    const queryInput = document.getElementById("query");
    const domainInput = document.getElementById("domain");

    function headers() {{
      return {{
        "Content-Type": "application/json",
        "Authorization": "Bearer " + (tokenInput.value || "local-dev-token"),
        "X-TCE-Consumer": consumerInput.value || "dashboard-user",
        "X-TCE-Role": roleInput.value || "user"
      }};
    }}

    function setList(el, rows, mapFn) {{
      el.innerHTML = "";
      for (const row of rows) {{
        const li = document.createElement("li");
        li.innerHTML = mapFn(row);
        el.appendChild(li);
      }}
      if (!rows.length) {{
        const li = document.createElement("li");
        li.textContent = "No results";
        el.appendChild(li);
      }}
    }}

    async function loadData() {{
      errEl.textContent = "";
      statusEl.textContent = "Loading...";
      const query = queryInput.value || "recent work";
      const domain = domainInput.value || undefined;
      try {{
        const searchRes = await fetch(API_BASE + "/v1/search", {{
          method: "POST",
          headers: headers(),
          body: JSON.stringify({{ query, filters: domain ? {{ domain }} : {{}}, k: 12 }})
        }});
        if (!searchRes.ok) throw new Error("Search failed (" + searchRes.status + ")");
        const searchJson = await searchRes.json();

        const bundleRes = await fetch(API_BASE + "/v1/context_bundle", {{
          method: "POST",
          headers: headers(),
          body: JSON.stringify({{
            task: query,
            app_context: domain ? {{ domain }} : {{}},
            constraints: {{ k: 10 }}
          }})
        }});
        if (!bundleRes.ok) throw new Error("Context bundle failed (" + bundleRes.status + ")");
        const bundle = await bundleRes.json();

        const patternsRes = await fetch(API_BASE + "/v1/patterns?min_confidence=0.5" + (domain ? "&domain=" + encodeURIComponent(domain) : ""), {{
          headers: headers()
        }});
        if (!patternsRes.ok) throw new Error("Pattern read failed (" + patternsRes.status + ")");
        const patterns = await patternsRes.json();

        summaryEl.textContent = bundle.summary || "No summary";
        setList(doList, bundle.do_dont?.do || [], (x) => x);
        setList(dontList, bundle.do_dont?.dont || [], (x) => x);
        setList(eventList, searchJson.result?.hits || [], (hit) => `<span class="mono">${{hit.ts}}</span> - <strong>${{hit.title}}</strong> (${{hit.domain}}/${{hit.task_type}})`);
        setList(patternList, patterns || [], (p) => `<strong>${{p.pattern_type}}</strong>: ${{p.statement}} <span class="mono">(conf=${{Number(p.confidence).toFixed(2)}})</span>`);
        statusEl.textContent = "Loaded.";
      }} catch (err) {{
        errEl.textContent = String(err);
        statusEl.textContent = "Failed.";
      }}
    }}

    document.getElementById("loadBtn").addEventListener("click", loadData);
  </script>
</body>
</html>"""
