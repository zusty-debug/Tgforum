"""Standalone, client-side searchable index — no backend, no CDN, no
external assets (works offline; ~90k filenames live in data.json).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .country import flag
from .planner import human_size, topic_icon


def describe(g) -> str:
    bits = []
    if g.is_multipart:
        bits.append(f"Multi-part dataset, {len(g.members)} files, {human_size(g.total_size)}.")
    else:
        bits.append("Single file.")
    if g.country and g.country.confidence >= 0.75:
        bits.append(f"Country signal: {g.country.name} ({g.country.confidence:.2f}).")
    if g.missing_parts:
        bits.append(f"Missing parts: {', '.join(map(str, g.missing_parts))}.")
    if g.partial_start and g.part_numbers:
        bits.append(f"Parts {g.part_numbers[0]}–{g.part_numbers[-1]} present; earlier parts not in source.")
    if g.status == "REVIEW":
        bits.append(f"Classification confidence: medium ({g.confidence:.2f}). Needs review.")
    elif g.status == "QUARANTINED":
        bits.append("Held in quarantine — not for redistribution.")
    return " ".join(bits)


def build_records(groups, plans, archives, db) -> list[dict]:
    records = []
    for g in groups:
        gid = getattr(g, "_db_id", None)
        p = plans.get(gid) if gid is not None else None
        per_archive = {}
        for a in archives:
            url = None
            if p and p.topic_title:
                row = db.find_destination(a["id"], p.topic_title)
                if row and row["topic_id"]:
                    url = f"https://t.me/c/{abs(a['chat_id'])}/{row['topic_id']}"
            per_archive[a["id"]] = {"url": url}
        filenames = [m.filename for m in g.members]
        haystack = " ".join(
            [g.name, g.category or "", g.country.name if g.country else "",
             describe(g), g.status, *filenames]
        ).lower()
        records.append({
            "name": g.name,
            "flag": flag(g.country.code) if g.country else "🌐",
            "country": g.country.name if g.country else None,
            "country_conf": g.country.confidence if g.country else None,
            "category": g.category or "Miscellaneous",
            "status": g.status,
            "is_multipart": g.is_multipart,
            "part_count": len(g.part_numbers),
            "file_count": len(filenames),
            "total_size": g.total_size,
            "size_human": human_size(g.total_size),
            "description": describe(g),
            "confidence": g.confidence,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "archives": per_archive,
            "filenames": filenames,
            "haystack": haystack,
        })
    records.sort(key=lambda r: r["name"].lower())
    return records


def generate(out_dir: str | Path, groups, plans, archives, db,
             single_file_html: bool = False):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "archives": [{"id": a["id"], "name": a["name"], "chat_id": a["chat_id"]}
                     for a in archives],
        "topics": build_records(groups, plans, archives, db),
    }
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    (out / "data.json").write_text(payload, encoding="utf-8")
    html = INDEX_HTML
    if single_file_html:
        safe = payload.replace("</", "<\\/")
        html = html.replace("let DATA = /*__DATA__*/null;", f"let DATA = /*__DATA__*/{safe};")
    (out / "index.html").write_text(html, encoding="utf-8")
    return out / "index.html", out / "data.json"


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Organized Telegram Archive</title>
<style>
  :root { --bg:#0f1115; --panel:#171a21; --line:#262b36; --text:#e6e9ef;
          --mut:#8b93a7; --acc:#4f8cff; }
  * { box-sizing:border-box; }
  body { margin:0; font:14px/1.45 system-ui,'Segoe UI',Roboto,sans-serif;
         background:var(--bg); color:var(--text); }
  header { padding:16px 22px; border-bottom:1px solid var(--line); background:var(--panel);
           position:sticky; top:0; z-index:5; }
  h1 { margin:0 0 10px; font-size:18px; }
  .bar { display:flex; gap:8px; flex-wrap:wrap; }
  input,select { background:#0c0e12; color:var(--text); border:1px solid var(--line);
                 border-radius:8px; padding:8px 10px; font-size:13px; }
  input#q { flex:1 1 260px; }
  #meta { color:var(--mut); font-size:12px; margin-top:8px; }
  .row { display:flex; gap:14px; padding:12px 22px; border-bottom:1px solid var(--line);
         align-items:flex-start; }
  .flag { font-size:20px; line-height:1.3; }
  .body { flex:1; min-width:0; }
  .name { font-weight:600; font-size:15px; word-break:break-word; }
  .sub { color:var(--mut); font-size:12px; margin-top:3px; display:flex; gap:8px;
         flex-wrap:wrap; align-items:center; }
  .tag { background:#202634; border:1px solid var(--line); padding:1px 8px;
         border-radius:10px; font-size:11px; color:var(--mut); white-space:nowrap; }
  .tag.REVIEW { color:#f5b942; border-color:#5c4a1e; }
  .tag.QUARANTINED { color:#ff6b6b; border-color:#5c2323; }
  .tag.IGNORED { color:#8b93a7; }
  a.btn { color:var(--acc); text-decoration:none; border:1px solid #274066;
          padding:4px 10px; border-radius:8px; font-size:12px; }
  a.btn:hover { background:#1a2333; }
  details { margin-top:6px; }
  summary { cursor:pointer; color:var(--mut); font-size:12px; }
  ul.files { margin:6px 0 0; padding-left:18px; color:var(--mut); font-size:12px; }
  .empty { padding:40px; text-align:center; color:var(--mut); }
</style>
</head>
<body>
<header>
  <h1>📦 ORGANIZED TELEGRAM ARCHIVE</h1>
  <div class="bar">
    <input id="q" type="search" placeholder="Search files, databases, books, topics…" autocomplete="off">
    <select id="f-country"><option value="">Country: all</option></select>
    <select id="f-category"><option value="">Category: all</option></select>
    <select id="f-status">
      <option value="">Status: all</option><option>OK</option><option>REVIEW</option>
      <option>QUARANTINED</option><option>IGNORED</option>
    </select>
    <select id="f-type">
      <option value="">Type: all</option><option value="multi">Multi-part</option>
      <option value="single">Single file</option><option value="ulp">ULPs</option>
    </select>
    <select id="f-archive"><option value="">Archive: all</option></select>
    <select id="sort">
      <option value="name">Sort: name</option><option value="country">Sort: country</option>
      <option value="files">Sort: file count</option><option value="size">Sort: size</option>
      <option value="updated">Sort: last updated</option>
    </select>
  </div>
  <div id="meta">Loading…</div>
</header>
<div id="list"></div>
<script>
let DATA = /*__DATA__*/null;
const $ = s => document.querySelector(s);
async function boot(){
  if (DATA) { render(); return; }
  try { DATA = await (await fetch("data.json")).json(); }
  catch(e) { $("#meta").textContent =
    "Could not load data.json — serve this folder (nginx/caddy) or use single-file mode."; return; }
  render();
}
function render(){
  const t = DATA.topics;
  const countries = [...new Set(t.map(x => x.country).filter(Boolean))].sort();
  const cats = [...new Set(t.map(x => x.category).filter(Boolean))].sort();
  $("#f-country").innerHTML = '<option value="">Country: all</option>' +
    countries.map(c => `<option>${esc(c)}</option>`).join("");
  $("#f-category").innerHTML = '<option value="">Category: all</option>' +
    cats.map(c => `<option>${esc(c)}</option>`).join("");
  $("#f-archive").innerHTML = '<option value="">Archive: all</option>' +
    DATA.archives.map(a => `<option value="${a.id}">${esc(a.name)}</option>`).join("");
  $("#meta").textContent = `${t.length} topics · ${t.reduce((s,x)=>s+x.file_count,0).toLocaleString()} files · generated ${DATA.generated_at}`;
  document.addEventListener("input", update);
  document.addEventListener("change", update);
  update();
}
function update(){
  const q = $("#q").value.trim().toLowerCase();
  const fc = $("#f-country").value, fk = $("#f-category").value,
        fs = $("#f-status").value, ft = $("#f-type").value, fa = $("#f-archive").value;
  const sort = $("#sort").value;
  const rows = DATA.topics.filter(t =>
    (!fc || t.country === fc) &&
    (!fk || t.category === fk) &&
    (!fs || t.status === fs) &&
    (!ft || (ft === "ulp" ? t.category === "ULP"
           : ft === "multi" ? t.is_multipart : !t.is_multipart)) &&
    (!fa || (t.archives[fa] && t.archives[fa].url)) &&
    (!q || t.haystack.includes(q)));
  rows.sort((a, b) => {
    if (sort === "size") return b.total_size - a.total_size;
    if (sort === "files") return b.file_count - a.file_count;
    if (sort === "country") return (a.country || "~").localeCompare(b.country || "~");
    if (sort === "updated") return (b.updated_at || "").localeCompare(a.updated_at || "");
    return a.name.localeCompare(b.name);
  });
  const box = $("#list");
  if (!rows.length) { box.innerHTML = '<div class="empty">Nothing matches.</div>'; return; }
  box.innerHTML = rows.map(t => {
    const links = DATA.archives
      .filter(a => t.archives[a.id] && t.archives[a.id].url)
      .map(a => `<a class="btn" href="${t.archives[a.id].url}" target="_blank" rel="noopener">Open in ${esc(a.name)}</a>`)
      .join("");
    return `<div class="row">
      <div class="flag">${t.flag}</div>
      <div class="body">
        <div class="name">${esc(t.name)}</div>
        <div class="sub">
          <span class="tag">${esc(t.country || "Unknown country")}</span>
          <span class="tag">${esc(t.category)}</span>
          <span class="tag ${t.status}">${t.status}</span>
          ${t.is_multipart ? `<span class="tag">${t.part_count} parts</span>` : ""}
          <span class="tag">${t.file_count} files</span>
          <span class="tag">${esc(t.size_human)}</span>
          <span class="tag">conf ${Number(t.confidence || 0).toFixed(2)}</span>
        </div>
        ${t.description ? `<div class="sub">${esc(t.description)}</div>` : ""}
        <div class="sub">${links || '<span class="tag">no topic yet</span>'}</div>
        ${t.filenames && t.filenames.length ? `<details><summary>Original filenames (${t.filenames.length})</summary><ul class="files">${t.filenames.slice(0, 500).map(f => `<li>${esc(f)}</li>`).join("")}${t.filenames.length > 500 ? `<li>…and ${t.filenames.length - 500} more</li>` : ""}</ul></details>` : ""}
      </div>
    </div>`;
  }).join("");
}
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g,
    c => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[c]));
}
boot();
</script>
</body>
</html>
"""
