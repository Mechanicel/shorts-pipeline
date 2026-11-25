"""Screenshots aller Seiten (1440×900, 390×844) nach docs/ui/ und automatische Prüfungen:
- „Heute“ ≤ 900 px hoch, wenn „Braucht dich“ leer ist (Box wird dafür durch ihren Leerzustand ersetzt)
- keine horizontale Scrollleiste bei 390 px
- keine Konsolenfehler
Läuft im offiziellen Playwright-Container (deploy/ui_check.sh), kein Browser auf dem Host nötig.
"""
import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8090"
PASSWORD = "ui-check-nur-lokal"
OUT = "/work/docs/ui"

PAGES = [("heute", "/"), ("freigabe", "/freigabe"), ("videos", "/videos"), ("videos-oeffentlich", "/videos?tab=oeffentlich"),
         ("videos-geloescht", "/videos?tab=geloescht"), ("video-detail", None), ("statistik", "/statistik"),
         ("recherche", "/recherche"), ("recherche-detail", None), ("jobs", "/jobs"), ("job-detail", None),
         ("einstellungen", "/einstellungen")]

problems, report = [], []
with sync_playwright() as p:
    browser = p.chromium.launch()
    for vw, vh, tag in ((1440, 900, "desktop"), (390, 844, "mobil")):
        ctx = browser.new_context(viewport={"width": vw, "height": vh}, device_scale_factor=1)
        page = ctx.new_page()
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(BASE + "/login")
        page.fill("input[name=password]", PASSWORD)
        page.click("button")
        page.wait_for_url(BASE + "/")
        # Detail-Adressen aus den Listen übernehmen
        page.goto(BASE + "/videos?tab=oeffentlich")
        video = page.locator("a.rowlink").first.get_attribute("href")
        page.goto(BASE + "/recherche")
        research = page.locator("a.rowlink").first.get_attribute("href")
        page.goto(BASE + "/jobs")
        job = page.locator("a.rowlink").first.get_attribute("href")
        details = {"video-detail": video, "recherche-detail": research, "job-detail": job}
        for name, path in PAGES:
            path = path or details[name]
            errors.clear()
            page.goto(BASE + path, wait_until="networkidle")
            page.evaluate("document.fonts.ready")
            file = f"{OUT}/{name}-{tag}.png"
            page.screenshot(path=file, full_page=True)
            width = page.evaluate("document.documentElement.scrollWidth")
            height = page.evaluate("document.documentElement.scrollHeight")
            line = f"{tag:7} {name:18} {path:28} {height:5} px hoch, {width} px breit"
            if tag == "mobil" and width > vw:
                problems.append(f"{name} ({tag}): horizontale Scrollleiste ({width} px > {vw} px)")
            if errors:
                problems.append(f"{name} ({tag}): Konsolenfehler {errors}")
            if name == "heute" and tag == "desktop":
                h_empty = page.evaluate("""() => {
                    const h2 = [...document.querySelectorAll('h2')].find(e => e.textContent.trim() === 'Braucht dich');
                    const box = h2.nextElementSibling;
                    const p = document.createElement('p'); p.className = 'muted'; p.textContent = 'Alles erledigt ✓';
                    box.replaceWith(p);
                    return document.documentElement.scrollHeight; }""")
                page.screenshot(path=f"{OUT}/heute-leer-{tag}.png", full_page=True)
                line += f" · mit leerer Box „Braucht dich“: {h_empty} px"
                if h_empty > 900:
                    problems.append(f"Heute: {h_empty} px hoch bei leerer Box (Grenze 900 px)")
            report.append(line)
        ctx.close()
    browser.close()

print("\n".join(report))
print("\nPROBLEME:\n" + "\n".join(problems) if problems else "\nAlle Prüfungen bestanden")
sys.exit(1 if problems else 0)
