"""Run against a running dashboard. This browser check never sends commands."""

# /// script
# requires-python = ">=3.12"
# dependencies = ["playwright>=1.48"]
# ///
import argparse
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright


def check(page: Page, url: str, screenshots: Path | None) -> None:
    failures = []
    page.on("pageerror", lambda err: failures.append(str(err)))
    page.route("**/api/operations", lambda route: route.abort())
    page.goto(url)
    native = page.locator("#trace-tn4")
    native.locator("tr[data-seq]").first.wait_for(timeout=45_000)
    for view in ("mixed", "trace"):
        page.locator(f'button[data-view="{view}"]').click()
        width = page.evaluate("""() => {
            const pane=document.querySelector('.trace-pane').getBoundingClientRect();
            return [pane.left,pane.right,document.documentElement.clientWidth,document.documentElement.scrollWidth];
        }""")
        assert (
            abs(width[0]) <= 1
            and abs(width[1] - width[2]) <= 1
            and width[2] == width[3]
        ), width
    expect(page.get_by_role("tab", name="tN4", exact=True)).to_have_attribute(
        "aria-selected", "true"
    )
    row = native.locator("tr:has(.bit-group)").first
    row.wait_for()
    for col in ("header", "payload", "record", "framing"):
        positions = native.locator(
            f'tbody tr[data-seq] > td[data-col="{col}"]'
        ).evaluate_all(
            "els=>new Set(els.map(el=>Math.round(el.getBoundingClientRect().left))).size"
        )
        assert positions == 1, (col, positions)
    source = row.evaluate("""r => {
        const octets=[...r.querySelectorAll('.wire-byte')].sort((a,b)=>parseInt(a.dataset.offset,16)-parseInt(b.dataset.offset,16));
        r.click();
        return octets.map(n=>n.querySelector('code').textContent).join('');
    }""")
    modal = page.locator("#detail")
    expect(modal.locator(".frame-diagram")).to_have_count(1)
    assert modal.locator(".frame-diagram > .boundary").count() == 2
    assert not modal.locator("details").count()
    offsets, wire = modal.locator(".wire-byte").evaluate_all("""bytes => [
        bytes.map(b=>parseInt(b.dataset.offset,16)),bytes.map(b=>b.querySelector('code').textContent).join('')
    ]""")
    assert offsets == list(range(len(offsets))) and wire == source
    nested = modal.locator('.wire-group[data-part="header"]:has(.bit-group)').first
    selected = nested.locator(".bit-group").first
    selected.click()
    assert (
        selected.evaluate("b=>b.closest('.wire-field').getAttribute('aria-pressed')")
        == "true"
    )
    assert "tN4" in modal.locator(".wire-selection").inner_text()
    expect(modal.locator(".wire-field[aria-pressed=true]")).to_have_count(1)
    modal.locator(".wire-field[aria-pressed=true]").press("End")
    expect(modal.locator(".wire-selection")).to_contain_text("EOF")
    if screenshots:
        page.screenshot(path=screenshots / f"frame-{page.viewport_size['width']}.png")
    assert modal.evaluate("m=>m.scrollWidth <= m.clientWidth+1")
    view = page.evaluate("""async () => {
        const {wireDiagram}=await import(location.origin+'/static/wire.js');
        const w=wireDiagram({ wire_hex:'ca03062fca2f2f2f353735',escapes:[3,5,7],layout:[
           {kind:'boundary',end:1,label:'SOF'},{kind:'length',end:2,label:'Length: 3 B'},
           {kind:'type',end:3,label:'Type: 6'}, {kind:'group',label:'Payload',children:[
             {kind:'field',end:9,label:'Reserved values'}]},
           {kind:'checksum',end:10,label:'Checksum'},{kind:'boundary',end:11,label:'EOF'}]},true);
        w.querySelector('.escape code').click();
        return [[...w.querySelectorAll('.wire-byte code')].map(n=>n.textContent).join('').toLowerCase(),
            w.querySelectorAll('.escape').length,w.querySelector('[aria-pressed=true]').dataset.label,
            w.querySelector('.wire-selection').textContent];
    }""")
    assert view == [
        "ca03062fca2f2f2f353735",
        3,
        "Reserved values",
        "W+03–08 › TPCK › Payload › Reserved values · Decoded",
    ]
    modal.get_by_role("button", name="Close details").click()
    scroll = native.locator(".trace-scroll")
    scroll.locator("tbody tr").nth(30).wait_for()
    scroll.evaluate("(s)=>s.scrollTop=180")
    native.locator(".follow:enabled").wait_for()
    anchor = """s=>[s.scrollTop, s.querySelector('tbody tr').dataset.seq,
        s.querySelector('tbody tr').getBoundingClientRect().top-s.getBoundingClientRect().top]"""
    before = scroll.evaluate(anchor)
    seq = page.locator("#sequence").inner_text()
    expect(page.locator("#sequence")).not_to_have_text(seq, timeout=45_000)
    assert before == scroll.evaluate(anchor), "incoming messages moved held history"
    page.get_by_role("tab", name="tRPC").click()
    trpc = page.locator("#trace-trpc")
    trpc.locator("tr[data-seq]").first.wait_for(timeout=45_000)
    assert not trpc.locator("tbody tr[data-seq]:not([data-protocol=trpc])").count()
    expect(trpc.locator("th[data-col=service]")).to_be_visible()
    page.get_by_role("tab", name="tN4", exact=True).click()
    assert before == scroll.evaluate(anchor), "switching tabs moved held history"
    native.locator(".follow").click()
    assert scroll.evaluate("s=>s.scrollTop") == 0
    if screenshots:
        page.screenshot(path=screenshots / f"trace-{page.viewport_size['width']}.png")
    link = native.locator(".endpoint-link").first
    link.evaluate("b=>b.click()")
    assert page.locator("#endpoint-filter").input_value()
    page.locator("#endpoint-filter").select_option("")
    page.locator('[data-page="diagnostics"]').click()
    assert page.locator(".counter-group").count() == 6
    page.locator('[data-page="monitor"]').click()
    page.locator('button[data-view="devices"]').click()
    card = page.locator('.device-record[data-device^="tha:"]:has(.thermo)').first
    card.wait_for(timeout=120_000)
    assert not page.locator("#device-cards .parameters").count()
    page.locator('[data-device="gateway"] .instrument').click()
    modal.get_by_role("heading", name="Gateway configuration").wait_for()
    modal.get_by_role("button", name="Close details").click()
    assert not card.get_by_role("heading", name="Raw setting values").count()
    card.locator(".instrument").click()
    modal.get_by_role("heading", name="Device metadata").wait_for()
    assert modal.get_by_role("heading", name="Reported configuration").count()
    table = modal.locator(".parameters").first
    aligned = table.locator("th[scope=row] + td").evaluate_all(
        "cells => new Set(cells.map(c=>Math.round(c.getBoundingClientRect().left))).size"
    )
    assert aligned == 1
    modal.get_by_role("button", name="Close details").click()
    fixture = page.evaluate("""async () => {
        const {parameters}=await import(location.origin+'/static/parameters.js');
        const table=parameters({adjustments:{tables:[{target:'heat',slots:[40,35,42,36,30]}]}});
        return [...table.querySelectorAll('th[scope=row]')].map(n=>[n.textContent,n.nextElementSibling.textContent]);
    }""")
    assert ["Wake", "68.0 °F / 20.0 °C (raw 40)"] in fixture
    assert any(name == "Away" for name, _ in fixture)
    if screenshots:
        page.screenshot(path=screenshots / f"devices-{page.viewport_size['width']}.png")
    page.locator('[data-page="operations"]').click()
    page.locator('[data-operation="catalog"]').click()
    forms = page.locator("#operations")
    for name in forms.locator('[name="command"] option').evaluate_all(
        "opts=>opts.map(o=>o.value)"
    ):
        forms.locator('[name="command"]').select_option(name)
        assert not forms.locator(
            "input:not([type=checkbox]), textarea, [contenteditable=true]"
        ).count(), name
    forms.locator('[name="command"]').select_option("read-status")
    forms.locator('[data-param="status_service"]').select_option("humidity")
    assert forms.locator('[data-param="selector"]').is_visible()
    forms.locator('[name="command"]').select_option("write-block")
    assert forms.get_by_role("button", name="Add field", exact=True).is_visible()
    forms.locator('[data-operation="raw"]').click()
    raw = forms.locator("#raw")
    raw.get_by_role("button", name="Add byte").click()
    raw.get_by_label("Payload byte", exact=True).select_option("CA")
    raw.get_by_role("button", name="Send once").click()
    expect(page.locator("#confirm")).to_be_visible()
    expect(page.locator("#review")).to_contain_text("40000001CA")
    page.locator("#confirm [data-close]").first.click()
    forms.locator('[data-operation="trpc"]').click()
    forms.locator("#trpc [name=method]").select_option("CURRENT_TEMPERATURE")
    forms.locator("#trpc button[type=submit], #trpc button:not([type])").last.click()
    expect(page.locator("#confirm")).to_be_visible()
    page.locator("#confirm [data-close]").first.click()
    assert not failures, failures


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "url", help="URL of a dashboard with live traffic and inventoried devices"
    )
    parser.add_argument(
        "--screenshots", type=Path, help="existing private output directory"
    )
    args = parser.parse_args()
    with sync_playwright() as driver:
        browser = driver.chromium.launch()
        for width, height in ((1440, 900), (390, 844)):
            page = browser.new_page(viewport={"width": width, "height": height})
            check(page, args.url, args.screenshots)
            page.close()
            print(
                f"{width}x{height}: full-width trace, stable history, frame inspector and inventory passed"
            )
        browser.close()


if __name__ == "__main__":
    main()
