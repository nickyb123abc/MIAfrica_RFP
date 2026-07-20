#!/usr/bin/env python3
"""
RFP / grant-call collector — v3 (config-driven, self-healing).

Configuration lives in sources.json (the runbook-as-data): Tier-1 sources,
keywords, GIZ country loop, gated modules. Changing the sweep = editing JSON.

Guarantees:
  * every source isolated (one failure never stops the run)
  * a source failing twice consecutively -> data/broken_sources.md -> GitHub issue
  * a page that changed but parsed zero items -> PARSER ALERT item, never silence
  * monthly page rotations (africanngos, Impact Funding) via RSS auto-discovery

Outputs:
  data/seen.json            dedupe state
  data/health.json          consecutive-failure counts per source
  data/fingerprints.json    page-change hashes
  data/opportunities.csv    cumulative raw log
  data/opportunities.json   v3 schema for the app (items primed with needs_scoring)
  data/new_items.md         written only when new items found  -> "finds" issue
  data/broken_sources.md    written only when a source breaks  -> "broken" issue
  REPORT.md                 open-items digest
"""

import csv, hashlib, json, os, re, sys
from datetime import date, datetime
from pathlib import Path

import requests
import feedparser
from bs4 import BeautifulSoup

UA = {"User-Agent": "rfp-screener/3.0 (+personal opportunity monitor)"}
TIMEOUT = 30
TODAY = date.today()

DATA = Path("data"); DATA.mkdir(exist_ok=True)
SEEN_F, HEALTH_F, FP_F = DATA/"seen.json", DATA/"health.json", DATA/"fingerprints.json"
CSV_F, JSON_F = DATA/"opportunities.csv", DATA/"opportunities.json"
NEW_F, BROKEN_F = DATA/"new_items.md", DATA/"broken_sources.md"
REPORT_F = Path("REPORT.md")

CFG = json.loads(Path("sources.json").read_text())
AUTO = CFG.get("auto_modules", {})
KEYWORDS = [k.lower() for ks in CFG.get("keywords", {}).values() for k in ks]

AFRICAN_COUNTRIES = ["Algeria","Angola","Benin","Botswana","Burkina Faso","Burundi","Cabo Verde",
 "Cape Verde","Cameroon","Central African Republic","Chad","Comoros","Congo","DR Congo",
 "Democratic Republic of the Congo","Cote d'Ivoire","Côte d'Ivoire","Ivory Coast","Djibouti",
 "Egypt","Equatorial Guinea","Eritrea","Eswatini","Ethiopia","Gabon","Gambia","Ghana","Guinea",
 "Guinea-Bissau","Kenya","Lesotho","Liberia","Libya","Madagascar","Malawi","Mali","Mauritania",
 "Mauritius","Morocco","Mozambique","Namibia","Niger","Nigeria","Rwanda","Senegal","Seychelles",
 "Sierra Leone","Somalia","South Africa","South Sudan","Sudan","Tanzania","Togo","Tunisia",
 "Uganda","Zambia","Zimbabwe","Africa","African","Sahel","Sub-Saharan","sub-Saharan"]
COUNTRIES_LC = [c.lower() for c in AFRICAN_COUNTRIES]

# ------------------------------------------------------------------ helpers
def get(url):
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text

def load_json(p, default):
    try:
        return json.loads(p.read_text()) if p.exists() else default
    except Exception:
        return default

def uid(it):
    return hashlib.sha1((it.get("link") or it.get("title","")).encode("utf-8","ignore")).hexdigest()

def hits(text, vocab):
    t = text.lower()
    return [v for v in vocab if v in t]

def parse_deadline(raw):
    if not raw: return None
    raw = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", str(raw).strip().rstrip("."))
    for f in ("%d %B %Y","%d %b %Y","%d-%b-%Y","%B %d, %Y","%d/%m/%Y","%Y-%m-%d"):
        try: return datetime.strptime(raw, f).date()
        except ValueError: pass
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+),?\s+(\d{4})", raw)
    if m:
        try: return datetime.strptime(" ".join(m.groups()), "%d %B %Y").date()
        except ValueError: pass
    return None

def fingerprint_guard(source_id, html, items, url):
    """If page changed but zero items parsed -> alert item instead of silence."""
    fps = load_json(FP_F, {})
    h = hashlib.sha256(html.encode("utf-8","ignore")).hexdigest()
    changed = fps.get(source_id) != h
    fps[source_id] = h
    FP_F.write_text(json.dumps(fps, indent=1))
    if changed and not items:
        return [{"title": f"PARSER ALERT: {source_id} changed but 0 items parsed — review manually",
                 "issuer": "screener", "link": url, "deadline_raw": "", "countries": "",
                 "source": source_id}]
    return items

def rss_newest(feed_url, title_contains):
    feed = feedparser.parse(feed_url)
    for e in feed.entries:
        if title_contains.lower() in (e.get("title") or "").lower():
            return e.link
    return None

def block_parser(html, url, source_name, container="article"):
    """Shared state machine for africanngos / Impact-Funding style roundups:
    Title line, then labelled fields (Issued by/Funder, Value/Amount, Deadline), then URL."""
    soup = BeautifulSoup(html, "html.parser")
    body = soup.find(container) or soup
    lines = [ln.strip() for ln in body.get_text("\n").split("\n") if ln.strip()]
    items, cur, prev = [], {}, ""
    for ln in lines:
        low = ln.lower()
        if low.startswith(("issued by","funder:")):
            cur = {"title": prev, "issuer": ln.split(":",1)[-1].strip()}
        elif low.startswith(("value","amount","funding:")) and cur:
            cur["value"] = ln.split(":",1)[-1].strip()
        elif low.startswith("deadline") and cur:
            cur["deadline_raw"] = ln.split(":",1)[-1].strip()
        elif ln.startswith("http") and cur:
            cur["link"] = ln.split()[0]
            blob = " ".join(str(v) for v in cur.values())
            cur["countries"] = ", ".join(hits(blob, COUNTRIES_LC)[:4])
            cur["source"] = source_name
            items.append(cur); cur = {}
        prev = ln
    return items

# ------------------------------------------------------------------ Tier-1 sources
def src_africanngos():
    url = rss_newest("https://africanngos.org/feed/", "funding opportunities") \
          or CFG["tier1"][0]["url"]
    html = get(url)
    return fingerprint_guard("africanngos", html,
                             block_parser(html, url, "africanngos.org"), url)

def src_impactfunding():
    url = rss_newest("https://impactfunding.substack.com/feed", "funding roundup")
    if not url:
        raise RuntimeError("Impact Funding: no roundup post found in feed")
    html = get(url)
    items = block_parser(html, url, "Impact Funding", container="div")
    return fingerprint_guard("impactfunding", html, items, url)

def src_terraviva():
    url = "https://www.terravivagrants.org/category/cross-cutting/"
    html = get(url)
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for h in soup.find_all(["h2","h3"]):
        a = h.find("a", href=True)
        if not a: continue
        title = a.get_text(" ", strip=True)
        if len(title) < 12: continue
        ctx = h.find_next("p")
        ctx_txt = ctx.get_text(" ", strip=True) if ctx else ""
        m = re.search(r"(?:deadline|closing)[^.]{0,60}?(\d{1,2}\s+\w+\s+\d{4}|\d{1,2}-\w{3}-\d{4})",
                      ctx_txt, re.I)
        blob = title + " " + ctx_txt
        if not (hits(blob, KEYWORDS) or hits(blob, COUNTRIES_LC)
                or re.search(r"grant|fund|prize|call|award", title, re.I)):
            continue
        items.append({"title": title[:220], "issuer": "", "link": a["href"],
                      "deadline_raw": m.group(1) if m else "",
                      "countries": ", ".join(hits(blob, COUNTRIES_LC)[:4]),
                      "source": "Terra Viva Grants"})
    return fingerprint_guard("terraviva", html, items, url)

def src_coefficient():
    url = "https://coefficientgiving.org/apply-for-funding/"
    html = get(url)
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for a in soup.find_all("a", href=True):
        href, text = a["href"], a.get_text(" ", strip=True)
        if "/funds/" in href and len(text) > 12 and re.search(r"rfp|request|review|call", text, re.I):
            items.append({"title": f"Coefficient: {text[:200]}", "issuer": "Coefficient Giving",
                          "link": requests.compat.urljoin(url, href), "deadline_raw": "",
                          "countries": "", "source": "coefficientgiving.org"})
    # watch for the Effective Giving section reappearing
    fps = load_json(FP_F, {})
    has_eg = "effective giving" in html.lower()
    if has_eg and not fps.get("coefficient_eg", False):
        items.append({"title": "ALERT: 'Effective Giving' section is live on Coefficient's funding page",
                      "issuer": "Coefficient Giving", "link": url, "deadline_raw": "",
                      "countries": "", "source": "coefficientgiving.org"})
    fps["coefficient_eg"] = has_eg
    FP_F.write_text(json.dumps(fps, indent=1))
    return fingerprint_guard("coefficient", html, items, url)

def src_fundsforngos():
    for feed_url in ("https://www2.fundsforngos.org/feed/", "https://www.fundsforngos.org/feed/"):
        try:
            feed = feedparser.parse(feed_url)
            if not feed.entries: continue
            items = []
            for e in feed.entries:
                blob = f"{e.get('title','')} {e.get('summary','')}"
                c, k = hits(blob, COUNTRIES_LC), hits(blob, KEYWORDS)
                if not (c and k): continue
                m = re.search(r"Deadline:\s*([0-9]{1,2}-\w{3}-[0-9]{4}|[^<\n]{4,30})", e.get("summary",""))
                items.append({"title": e.title, "issuer": "", "link": e.link,
                              "deadline_raw": (m.group(1).strip() if m else ""),
                              "countries": ", ".join(c[:4]), "source": "fundsforNGOs"})
            return items
        except Exception as ex:
            print(f"[ffn] {feed_url}: {ex}", file=sys.stderr)
    raise RuntimeError("fundsforNGOs: no feed reachable")

# ------------------------------------------------------------------ auto modules
def src_undp_board():
    url = "https://procurement-notices.undp.org/"
    html = get(url)
    items = []
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        if len(text) < 15: continue
        c, k = hits(text, COUNTRIES_LC), hits(text, KEYWORDS)
        if c or k:
            items.append({"title": text[:220], "issuer": "UNDP",
                          "link": requests.compat.urljoin(url, a["href"]),
                          "deadline_raw": "", "countries": ", ".join(c[:4]),
                          "source": "UNDP notices"})
    return items

def src_worldbank_api():
    data = requests.get("https://search.worldbank.org/api/procnotices?format=json&rows=200&os=0",
                        headers=UA, timeout=TIMEOUT).json()
    recs = data.get("procnotices") if isinstance(data, dict) else None
    if recs is None and isinstance(data, dict):
        recs = next((v for v in data.values() if isinstance(v, list)), [])
    items = []
    for r in recs or []:
        if not isinstance(r, dict): continue
        blob = " ".join(str(v) for v in r.values() if isinstance(v, str))
        c, k = hits(blob, COUNTRIES_LC), hits(blob, KEYWORDS)
        if not (c and k): continue
        title = r.get("bid_description") or r.get("project_name") or blob[:160]
        dl = next((str(v) for f,v in r.items() if "deadline" in f.lower() and v), "")
        link = next((str(v) for f,v in r.items() if isinstance(v,str) and v.startswith("http")), "")
        items.append({"title": str(title)[:220], "issuer": "World Bank project",
                      "link": link or "https://projects.worldbank.org/en/projects-operations/procurement",
                      "deadline_raw": dl, "countries": ", ".join(c[:4]),
                      "source": "WB procnotices API"})
    return items

def src_eu_ft_api():
    q = {"bool": {"must": [{"terms": {"status": ["31094501","31094502"]}}]}}
    r = requests.post("https://api.tech.ec.europa.eu/search-api/prod/rest/search",
                      params={"apiKey":"SEDIA","text":"Africa","pageSize":"50","pageNumber":"1"},
                      files={"query": (None, json.dumps(q))}, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    items = []
    for res in r.json().get("results", []):
        md = res.get("metadata") or {}
        blob = f"{res.get('title','')} {res.get('summary','')} {json.dumps(md)[:4000]}"
        c = hits(blob, COUNTRIES_LC)
        if not c: continue
        dl = md.get("deadlineDate")
        if isinstance(dl, list): dl = dl[0] if dl else ""
        items.append({"title": str(res.get("title") or "EU call")[:220],
                      "issuer": "European Commission", "link": str(res.get("url") or ""),
                      "deadline_raw": str(dl or "")[:30], "countries": ", ".join(c[:4]),
                      "source": "EU F&T (SEDIA)"})
    return items

def src_aerc():
    url = AUTO.get("aerc_url", "https://www.aercafrica.org/opportunities/")
    html = get(url)
    items = []
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        if len(text) > 15 and re.search(r"rfp|proposal|expression of interest|eoi|call|consultan", text, re.I):
            items.append({"title": text[:220], "issuer": "AERC",
                          "link": requests.compat.urljoin(url, a["href"]),
                          "deadline_raw": "", "countries": "Africa", "source": "AERC"})
    return fingerprint_guard("aerc", html, items, url)

def src_giz_loop():
    """Missing country tab = no active tenders (by design). Never counts as failure."""
    items = []
    pattern = AUTO.get("giz_url_pattern", "https://www.giz.de/en/regions/africa/{slug}/tenders")
    for slug in AUTO.get("giz_country_slugs", []):
        url = pattern.format(slug=slug)
        try:
            html = get(url)
        except Exception:
            continue  # tab removed -> nothing active
        for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
            text = a.get_text(" ", strip=True)
            if len(text) > 20 and hits(text, KEYWORDS):
                items.append({"title": f"GIZ {slug.replace('-',' ').title()}: {text[:180]}",
                              "issuer": "GIZ", "link": requests.compat.urljoin(url, a["href"]),
                              "deadline_raw": "", "countries": slug.replace("-"," ").title(),
                              "source": "GIZ tenders"})
    return items

def src_ungm_api():
    cid, sec = os.environ.get("UNGM_CLIENT_ID"), os.environ.get("UNGM_CLIENT_SECRET")
    if not (cid and sec):
        print("[ungm] secrets not set — skipping (add UNGM_CLIENT_ID / UNGM_CLIENT_SECRET to enable)")
        return []
    tok = requests.post("https://www.ungm.org/API/token", headers=UA, timeout=TIMEOUT,
                        data={"grant_type":"client_credentials","client_id":cid,"client_secret":sec})
    tok.raise_for_status()
    access = tok.json().get("access_token")
    r = requests.get("https://www.ungm.org/API/Notices", headers={**UA, "Authorization": f"Bearer {access}"},
                     timeout=TIMEOUT)
    r.raise_for_status()
    items = []
    for n in (r.json() if isinstance(r.json(), list) else r.json().get("value", []))[:300]:
        blob = json.dumps(n)[:3000]
        c = hits(blob, COUNTRIES_LC)
        if not c: continue
        nid = n.get("id") or n.get("noticeId") or ""
        items.append({"title": str(n.get("title") or n.get("Title") or "UNGM notice")[:220],
                      "issuer": str(n.get("agency") or "UN system"),
                      "link": f"https://www.ungm.org/Public/Notice/{nid}",
                      "deadline_raw": str(n.get("deadline") or ""), "countries": ", ".join(c[:4]),
                      "source": "UNGM API"})
    return items

SOURCES = {
    "africanngos": src_africanngos, "impactfunding": src_impactfunding,
    "terraviva": src_terraviva, "coefficient": src_coefficient,
    "fundsforngos": src_fundsforngos, "undp_board": src_undp_board,
    "worldbank_api": src_worldbank_api, "eu_ft_api": src_eu_ft_api,
    "aerc": src_aerc, "giz_loop": src_giz_loop, "ungm_api": src_ungm_api,
}

def enabled_sources():
    ids = [t["id"] for t in CFG.get("tier1", []) if t["id"] in SOURCES]
    ids += [m for m in AUTO.get("always_on", []) if m in SOURCES]
    ids += list(AUTO.get("gated_on_secrets", {}).keys() & SOURCES.keys()) \
           if isinstance(AUTO.get("gated_on_secrets"), dict) else []
    seen = set(); out = []
    for i in ids:
        if i not in seen:
            seen.add(i); out.append(i)
    return out

# ------------------------------------------------------------------ main
def main():
    for f in (NEW_F, BROKEN_F):
        if f.exists(): f.unlink()

    seen = load_json(SEEN_F, {})
    health = load_json(HEALTH_F, {})
    new, broken = [], []

    for sid in enabled_sources():
        try:
            for it in SOURCES[sid]():
                i = uid(it)
                if i in seen: continue
                seen[i] = str(TODAY)
                it["found"] = str(TODAY)
                new.append(it)
            health[sid] = 0
        except Exception as ex:
            health[sid] = health.get(sid, 0) + 1
            print(f"[{sid}] FAILED ({health[sid]} consecutive): {ex}", file=sys.stderr)
            if health[sid] >= 2:
                broken.append(f"- **{sid}** — {health[sid]} consecutive failures. Last error: `{str(ex)[:200]}`")

    SEEN_F.write_text(json.dumps(seen, indent=1))
    HEALTH_F.write_text(json.dumps(health, indent=1))
    if broken:
        BROKEN_F.write_text("## Source health alert — " + str(TODAY) + "\n\n" + "\n".join(broken)
                            + "\n\nFix or paste this into Claude for a patch.")

    if new:
        exists = CSV_F.exists()
        with CSV_F.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["found","source","title","issuer","countries","deadline_raw","value","link"])
            if not exists: w.writeheader()
            for it in new: w.writerow({k: it.get(k,"") for k in w.fieldnames})

        lines = [f"## {len(new)} new opportunity item(s) — {TODAY}\n"]
        for it in new:
            lines.append(f"- **{it['title']}** ({it.get('issuer','')}; deadline {it.get('deadline_raw') or '—'}) "
                         f"[{it['source']}]({it['link']})")
        NEW_F.write_text("\n".join(lines))

    # v3 opportunities.json: append primed items, preserve meta + scored history
    blob = load_json(JSON_F, {"updated": "", "meta": {}, "items": []})
    known = {(it.get("link") or it.get("title")) for it in blob["items"]}
    for it in new:
        key = it.get("link") or it.get("title")
        if key in known: continue
        d = parse_deadline(it.get("deadline_raw",""))
        blob["items"].append({"ref":"","title":it.get("title",""),"issuer":it.get("issuer",""),
            "funder":"","country":it.get("countries",""),"language":"",
            "source":it.get("source",""),"link":it.get("link",""),
            "found":it.get("found",str(TODAY)),"deadline":d.isoformat() if d else "",
            "value":it.get("value",""),"status":"Open","pipeline":"",
            "eligible":"?","dims":None,"score":None,"rationales":None,
            "needs_scoring":True,
            "notes":("" if d else it.get("deadline_raw",""))})
    blob["updated"] = str(TODAY)
    JSON_F.write_text(json.dumps(blob, indent=1))

    # REPORT.md digest (open / undated)
    rows = blob["items"]
    open_rows = []
    for r in rows:
        d = parse_deadline(r.get("deadline",""))
        if d is None or d >= TODAY:
            if str(r.get("status","")).lower() != "closed":
                r["_d"] = d or date(2100,1,1); open_rows.append(r)
    open_rows.sort(key=lambda r: r["_d"])
    out = [f"# Opportunity screener — open items\n_Updated {TODAY} · {len(open_rows)} open of {len(rows)} logged_\n",
           "| Deadline | Fit | Title | Source |", "|---|---|---|---|"]
    for r in open_rows:
        out.append(f"| {r.get('deadline') or 'rolling'} | {r.get('score') if r.get('score') is not None else '—'} "
                   f"| [{r['title'][:100]}]({r['link']}) | {r['source']} |")
    REPORT_F.write_text("\n".join(out))
    print(f"done: {len(new)} new · {len(rows)} logged · {len(broken)} broken source(s)")

if __name__ == "__main__":
    main()
