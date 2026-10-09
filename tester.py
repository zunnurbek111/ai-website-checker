#!/usr/bin/env python3
"""AI Website Tester.
Drives a real Chromium browser through a website, looks for problems (broken links, dead buttons,
forms that accept bad input, filters that return wrong results ...), saves screenshots as evidence,
then asks Claude to explain each problem and suggest a fix. Output: report.html + results.json."""
import argparse, base64, datetime, html, json, os, re, sys
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
NOT_FOUND = re.compile(r"not found|404|doesn.t exist|no such page|something went wrong", re.I)
SKIP_BTN = re.compile(r"reset|clear|cancel|close|remove|delete", re.I)
SUBMIT = "button[type=submit], input[type=submit], button:not([type])"
FIELDS = "input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=submit]):not([type=button]), textarea"
FIELD_INFO = """e=>{const l=e.labels&&e.labels[0];return {tag:e.tagName.toLowerCase(),type:(e.type||'text').toLowerCase(),
mode:e.inputMode||'',label:((l?l.innerText:(e.getAttribute('aria-label')||e.placeholder||e.name||e.id))||'').trim(),value:e.value||''}}"""
OUTCOME = """f=>{const vis=e=>e.offsetParent!==null&&(e.innerText||'').trim().length>0;
return {ok:[...f.querySelectorAll('.ok,.success,[role=status],[data-testid*=success]')].filter(vis).map(e=>e.innerText.trim()),
er:[...f.querySelectorAll('.err,.error,.invalid-feedback,[role=alert]')].filter(vis).map(e=>e.innerText.trim())}}"""
HINTS = {
 "link": "Check the href: it must match a route the app really handles.",
 "form": "Validate this field before showing success, and show an error message next to it.",
 "button": "Make sure the click handler is attached, runs, and updates the state and the UI.",
 "filter": "Check the comparison operator and the value used in the filter function.",
 "search": "Make sure the search box re-filters the list on every input event.",
 "console": "Open DevTools > Console on this page and fix the reported error.",
 "a11y": "Connect each field to a <label for=...> or add an aria-label.",
 "layout": "Use relative widths and let wide content wrap or scroll inside its own container.",
}

class Tester:
    def __init__(self, url, out, headless=True):
        self.url, self.out, self.headless = url, Path(out), headless
        self.findings, self.keys, self.notes, self.console, self.n, self.source = [], set(), [], [], 0, ""
        self.stats = dict(pages=0, forms=0, form_cases=0, buttons=0, filter_cases=0)
        self.out.mkdir(parents=True, exist_ok=True)

    # ---------- helpers ----------
    def route(self, page):
        u = urlparse(page.url)
        path = u.fragment.split("?")[0] if u.fragment else ("/" if u.scheme == "file" else u.path)
        return re.sub(r"\d+", "N", path or "/")

    def fresh(self, page, url):
        page.goto("about:blank"); r = page.goto(url); page.wait_for_timeout(300); return r

    def text(self, page):
        return page.evaluate("(document.querySelector('main')||document.body).innerText")

    def results_text(self, page):
        """Page text without forms and dropdowns, so filter options are not mistaken for results."""
        return page.evaluate("""()=>{const c=(document.querySelector('main')||document.body).cloneNode(true);
c.querySelectorAll('form,select,nav,header').forEach(e=>e.remove());return c.innerText}""")

    def add(self, page, cat, sev, subject, title, evidence):
        key = f"{cat}|{self.route(page)}|{subject}"
        if key in self.keys: return
        self.keys.add(key); self.n += 1
        shot = self.out / f"{self.n:02d}_{cat}.png"
        page.screenshot(path=str(shot))
        self.findings.append(dict(id=len(self.findings) + 1, key=key, category=cat, severity=sev, page=self.route(page),
                                  title=title, evidence=evidence, hint=HINTS[cat], screenshot=shot.name))
        print(f"  [{sev.upper():6}] {title}  ({self.route(page)})")

    # ---------- main flow ----------
    def run(self):
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
            page.on("pageerror", lambda e: self.console.append(f"Uncaught error: {e}"))
            page.on("console", lambda m: m.type == "error" and "Failed to load resource" not in m.text
                    and self.console.append(f"Console error: {m.text}"))
            page.on("requestfailed", lambda r: not re.search(r"googleapis|gstatic", r.url)
                    and self.console.append(f"Request failed: {r.url}"))
            resp = self.fresh(page, self.url)
            if resp and resp.status >= 400:
                self.add(page, "link", "high", "start", "The website's start page does not load", f"HTTP status {resp.status}")
            self.source = page.content()
            self.crawl(page)
            for msg in dict.fromkeys(self.console):
                self.add(page, "console", "medium", msg[:60], "Error in the browser console", msg)
            browser.close()

    def crawl(self, page):
        queue, tested, per, names = [page.url], set(), {}, {}
        site = urlparse(self.url)
        while queue and len(tested) < 40:
            u = queue.pop(0); raw = u.split("?")[0]; pat = re.sub(r"\d+", "N", raw)
            if u in tested or per.get(pat, 0) >= (2 if pat != raw else 1): continue
            tested.add(u); per[pat] = per.get(pat, 0) + 1
            print(f"Testing page: {u.split('#')[-1] if '#' in u else u}")
            resp = self.fresh(page, u); txt = self.text(page); name = names.get(u, "")
            if (resp and resp.status >= 400) or NOT_FOUND.search(txt[:300]) or len(txt.strip()) < 30:
                self.add(page, "link", "high", name or u, f'Link "{name}" leads to a broken page' if name else "Page is broken or empty",
                         f'Opened {u}. The page shows: "{txt.strip()[:100]}"')
                continue
            self.stats["pages"] += 1
            self.check_forms(page, u); self.check_buttons(page, u); self.check_filters(page, u)
            self.check_search(page, u); self.check_quality(page, u)
            self.fresh(page, u)
            for l in page.eval_on_selector_all("a[href]", "els=>els.map(a=>({href:a.href,text:(a.innerText||a.getAttribute('aria-label')||'').trim()}))"):
                pu = urlparse(l["href"])
                if pu.scheme == site.scheme and pu.netloc == site.netloc and l["href"] not in tested:
                    names.setdefault(l["href"], l["text"]); queue.append(l["href"])

    # ---------- forms ----------
    def good_value(self, i):
        L = i["label"].lower()
        if i["type"] == "email" or "email" in L: return "test@example.com"
        if i["type"] == "tel" or "phone" in L: return "+998901234567"
        if i["tag"] == "textarea": return "This is a valid test message for the agent."
        if i["value"].strip(): return i["value"]
        if i["type"] == "number" or i["mode"] in ("numeric", "decimal"): return "100"
        return "Test User"

    def bad_values(self, i):
        L, out = i["label"].lower(), []
        num = i["type"] == "number" or i["mode"] in ("numeric", "decimal")
        if "optional" not in L: out.append("")
        if i["type"] == "email" or "email" in L: out.append("abc")
        elif i["type"] == "tel" or "phone" in L: out.append("12")
        elif i["tag"] == "textarea": out.append("x")
        elif num: out += ["-5", "abc"] + (["150"] if "%" in L else [])
        return out

    def submit(self, page, u, idx, values):
        self.fresh(page, u)
        form = page.locator("form").nth(idx); fields = form.locator(FIELDS)
        for k, v in enumerate(values):
            fields.nth(k).evaluate("(e,v)=>{e.value=v;e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}))}", v)
        before = page.url
        form.locator(SUBMIT).first.click(); page.wait_for_timeout(350)
        if page.url != before: return {"ok": [], "er": [], "nav": True}
        return form.evaluate(OUTCOME)

    def check_forms(self, page, u):
        self.fresh(page, u)
        for i in range(page.locator("form").count()):
            form = page.locator("form").nth(i)
            if not form.locator(SUBMIT).count(): continue
            fid = form.get_attribute("id") or f"form{i + 1}"
            fields = form.locator(FIELDS)
            infos = [fields.nth(k).evaluate(FIELD_INFO) for k in range(fields.count())]
            if not infos: continue
            self.stats["forms"] += 1
            good = [self.good_value(x) for x in infos]
            base = self.submit(page, u, i, good)
            if not (base["ok"] and not base["er"]):
                self.notes.append(f'Form "{fid}" on {self.route(page)}: the generic valid test data did not produce a success message, so field-level tests were skipped (the tester cannot guess site-specific data).')
                continue
            for k, info in enumerate(infos):
                for bad in self.bad_values(info):
                    vals = list(good); vals[k] = bad
                    res = self.submit(page, u, i, vals); self.stats["form_cases"] += 1
                    if res["ok"] and not res["er"]:
                        self.add(page, "form", "high", f"{fid}:{info['label']}", f'Form accepts invalid input in "{info["label"]}"',
                                 f'Typed "{bad}" in "{info["label"]}" (all other fields valid) and submitted. The form showed "{res["ok"][0][:80]}" and no error message.')

    # ---------- buttons ----------
    def check_buttons(self, page, u):
        self.fresh(page, u); seen = set()
        sel = "button:not([type=submit]):not([disabled])"
        btns = page.eval_on_selector_all(sel, "els=>els.map((e,i)=>({i,name:(e.getAttribute('aria-label')||e.innerText||'').trim(),vis:!!e.offsetParent}))")
        for b in btns:
            if not b["vis"] or not b["name"] or b["name"] in seen or SKIP_BTN.search(b["name"]): continue
            seen.add(b["name"]); self.fresh(page, u)
            state = lambda: page.evaluate("location.href+document.body.innerHTML")
            before = state(); page.locator(sel).nth(b["i"]).click(); page.wait_for_timeout(300)
            self.stats["buttons"] += 1
            if state() == before:
                self.add(page, "button", "high", b["name"], f'Button "{b["name"]}" has no visible effect',
                         f'Clicked "{b["name"]}". The URL and the page content were identical before and after the click.')

    # ---------- filters and search ----------
    def violation(self, label, opt, txt):
        n = int(re.sub(r"\D", "", opt) or 0)
        prices = [int(p.replace(",", "")) for p in re.findall(r"\$(\d[\d,]{3,})", txt)]
        if "price" in label and n:
            if "max" in label and (v := [p for p in prices if p > n]): return f"Results include prices above the limit: {', '.join(f'${x:,}' for x in v[:4])}."
            if "min" in label and (v := [p for p in prices if p < n]): return f"Results include prices below the minimum: {', '.join(f'${x:,}' for x in v[:4])}."
        if "bed" in label and n:
            beds = [0 if b == "Studio" else int(b.split()[0]) for b in re.findall(r"Studio|\d+ bed", txt)]
            if any(b < n for b in beds): return f"Results include homes with fewer than {n} bedrooms."
        if "type" in label and opt not in ("Any", ""):
            other = {t for t in re.findall(r"(House|Townhouse|Condo|Apartment) ·", txt) if t != opt}
            if other: return f"Results include other types: {', '.join(sorted(other))}."
        return ""

    def check_filters(self, page, u):
        self.fresh(page, u)
        sels = page.eval_on_selector_all("select", """els=>els.map((e,i)=>{const f=e.closest('form');
const sub=f&&f.querySelector('button[type=submit],input[type=submit],button:not([type])');const l=e.labels&&e.labels[0];
return {i,sub:!!sub,vis:!!e.offsetParent,label:((l?l.innerText:(e.getAttribute('aria-label')||e.id))||'').trim(),opts:[...e.options].map(o=>o.text.trim())}})""")
        for s in sels:
            if s["sub"] or not s["vis"] or len(s["opts"]) < 2: continue
            for k in range(1, min(len(s["opts"]), 5)):
                self.fresh(page, u)
                page.locator("select").nth(s["i"]).select_option(index=k); page.wait_for_timeout(300)
                self.stats["filter_cases"] += 1
                bad = self.violation(s["label"].lower(), s["opts"][k], self.results_text(page))
                if bad:
                    self.add(page, "filter", "high", s["label"], f'Filter "{s["label"]}" returns results that do not match it',
                             f'Set "{s["label"]}" to "{s["opts"][k]}". {bad}')

    def check_search(self, page, u):
        self.fresh(page, u); box = page.locator("input[type=search]")
        if not box.count(): return
        cards = ".card, [data-testid=listing-card], article"
        before = page.locator(cards).count()
        box.first.fill("zzzxqjv"); page.wait_for_timeout(400); self.stats["filter_cases"] += 1
        if before and page.locator(cards).count() >= before:
            self.add(page, "search", "high", "search", "Search box does not filter the results",
                     f'Typed a nonsense word ("zzzxqjv"). {before} items were shown before and the same number after.')

    # ---------- quality ----------
    def check_quality(self, page, u):
        self.fresh(page, u)
        miss = page.evaluate("""()=>[...document.querySelectorAll('input:not([type=hidden]),select,textarea')].filter(e=>e.offsetParent
&&!(e.labels&&e.labels.length)&&!e.getAttribute('aria-label')&&!e.getAttribute('aria-labelledby')).map(e=>e.id||e.name||e.tagName)""")
        if miss: self.add(page, "a11y", "low", "labels", "Form fields without a label", f"These fields have no label: {', '.join(miss[:5])}.")
        page.set_viewport_size({"width": 375, "height": 800}); page.wait_for_timeout(200)
        over = page.evaluate("document.documentElement.scrollWidth-window.innerWidth")
        if over > 2: self.add(page, "layout", "medium", "overflow", "Page scrolls sideways on a phone-sized screen", f"At 375 px wide the page is {over}px wider than the screen.")
        page.set_viewport_size({"width": 1280, "height": 900})

# ---------- AI explanation ----------
def explain(findings, source):
    if not findings or not os.getenv("ANTHROPIC_API_KEY"): return {}
    try:
        import anthropic
        payload = [{k: f[k] for k in ("id", "category", "severity", "page", "title", "evidence")} for f in findings]
        prompt = ("You are a senior QA engineer. An automated browser tester found the problems below on a website (with evidence), "
                  "and here is the page source. For EACH problem return an object with: id, explanation (1-2 plain sentences: what is wrong and why a user cares), "
                  "root_cause (point to the relevant code), fix (a short concrete code change). Reply with ONLY a JSON array.\n\n"
                  f"PROBLEMS:\n{json.dumps(payload, indent=1)}\n\nPAGE SOURCE:\n{source[:40000]}")
        msg = anthropic.Anthropic().messages.create(model=MODEL, max_tokens=4000, messages=[{"role": "user", "content": prompt}])
        arr = json.loads(re.search(r"\[.*\]", msg.content[0].text, re.S).group(0))
        return {int(x["id"]): x for x in arr}
    except Exception as e:
        print(f"AI explanation skipped: {e}"); return {}

# ---------- report ----------
def build_report(t, expl, cmp_info):
    esc = lambda x: html.escape(str(x))
    cards = []
    for f in sorted(t.findings, key=lambda f: ("high", "medium", "low").index(f["severity"])):
        e = expl.get(f["id"])
        ai = (f'<p><b>AI explanation:</b> {esc(e.get("explanation",""))}</p><p><b>Likely cause:</b> {esc(e.get("root_cause",""))}</p><p><b>Suggested fix:</b></p><pre>{esc(e.get("fix",""))}</pre>'
              if e else f'<p><b>Suggested fix (rule-based):</b> {esc(f["hint"])}</p>')
        img = base64.b64encode((t.out / f["screenshot"]).read_bytes()).decode()
        cards.append(f'<section class="card {f["severity"]}"><h3>#{f["id"]} {esc(f["title"])}</h3><p class="m">{f["severity"].upper()} · {esc(f["category"])} · page {esc(f["page"])}</p>'
                     f'<p><b>Evidence:</b> {esc(f["evidence"])}</p>{ai}<img alt="Screenshot of problem {f["id"]}" src="data:image/png;base64,{img}"></section>')
    s = t.stats
    cmpx = ""
    if cmp_info:
        li = lambda xs: "".join(f"<li>{esc(x)}</li>" for x in xs) or "<li>none</li>"
        cmpx = (f'<div class="cmp"><h2>Comparison with the previous run</h2><p><b>Fixed ({len(cmp_info["fixed"])}):</b></p><ul>{li(cmp_info["fixed"])}</ul>'
                f'<p><b>Still failing ({len(cmp_info["same"])}):</b></p><ul>{li(cmp_info["same"])}</ul><p><b>New ({len(cmp_info["new"])}):</b></p><ul>{li(cmp_info["new"])}</ul></div>')
    notes = "".join(f"<li>{esc(n)}</li>" for n in dict.fromkeys(t.notes))
    return f"""<!doctype html><meta charset=utf-8><title>AI Website Tester report</title><style>
body{{font:16px/1.5 system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem;color:#14232b}}
.card{{border:1px solid #ccd;border-left:6px solid #888;border-radius:8px;padding:1rem;margin:1rem 0}}
.high{{border-left-color:#b3261e}}.medium{{border-left-color:#e8a317}}.low{{border-left-color:#1f4e5f}}
.m{{color:#567}}img{{max-width:100%;border:1px solid #ccd;border-radius:6px}}pre{{background:#f1f4f6;padding:.7rem;overflow:auto;border-radius:6px;white-space:pre-wrap}}
.cmp{{background:#eef6ee;border-radius:8px;padding:.5rem 1rem}}</style>
<h1>AI Website Tester report</h1><p>Site: <b>{esc(t.url)}</b><br>Run: {datetime.datetime.now():%Y-%m-%d %H:%M}</p>
<p><b>{len(t.findings)} problem(s) found.</b> Tested {s['pages']} pages, {s['forms']} forms ({s['form_cases']} bad-input cases), {s['buttons']} buttons, {s['filter_cases']} filter/search cases.</p>
{cmpx}{''.join(cards) or '<p>No problems found.</p>'}<h2>Coverage notes</h2><ul>{notes or '<li>none</li>'}</ul>"""

def main():
    ap = argparse.ArgumentParser(description="AI Website Tester")
    ap.add_argument("url", help="http(s):// URL or path to a local .html file")
    ap.add_argument("--out", default="reports/run"); ap.add_argument("--compare", help="results.json of an earlier run")
    ap.add_argument("--headed", action="store_true", help="show the browser window"); ap.add_argument("--no-ai", action="store_true")
    a = ap.parse_args()
    url = a.url if re.match(r"(https?|file)://", a.url) else Path(a.url).resolve().as_uri()
    t = Tester(url, a.out, headless=not a.headed)
    print(f"Testing {url}\n"); t.run()
    expl = {} if a.no_ai else explain(t.findings, t.source)
    if not a.no_ai and not os.getenv("ANTHROPIC_API_KEY"): print("No ANTHROPIC_API_KEY set: using rule-based hints instead of AI explanations.")
    cmp_info = None
    if a.compare:
        prev = {f["key"]: f["title"] for f in json.load(open(a.compare))["findings"]}; cur = {f["key"]: f["title"] for f in t.findings}
        cmp_info = dict(fixed=[v for k, v in prev.items() if k not in cur], same=[v for k, v in prev.items() if k in cur], new=[v for k, v in cur.items() if k not in prev])
        print(f"\nCompared with earlier run: {len(cmp_info['fixed'])} fixed, {len(cmp_info['same'])} still failing, {len(cmp_info['new'])} new")
    for f in t.findings: f["ai"] = expl.get(f["id"])
    (t.out / "results.json").write_text(json.dumps(dict(url=url, stats=t.stats, notes=t.notes, findings=t.findings), indent=1, default=str))
    (t.out / "report.html").write_text(build_report(t, expl, cmp_info), encoding="utf-8")
    print(f"\n{len(t.findings)} problem(s). Report: {t.out / 'report.html'}")

if __name__ == "__main__": main()
