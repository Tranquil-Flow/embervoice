"""Exercise the actual drag/drop handler without starting inference."""

from e2e_support import URL as TEST_URL, mock_ready
import base64
from pathlib import Path
from playwright.sync_api import sync_playwright

root = Path(__file__).resolve().parent.parent
blob = base64.b64encode((root / "tests/fixtures/synthetic.epub").read_bytes()).decode()
with sync_playwright() as p:
    browser = p.chromium.launch(
        executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", headless=True
    )
    page = browser.new_page()
    mock_ready(page)
    page.goto(TEST_URL, wait_until="networkidle")
    page.locator("#drop-zone").evaluate(
        """(node, encoded) => {
      const bytes=Uint8Array.from(atob(encoded), c=>c.charCodeAt(0));
      const file=new File([bytes], 'dragged.epub',{type:'application/epub+zip'});
      const transfer=new DataTransfer(); transfer.items.add(file);
      node.dispatchEvent(new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer:transfer}));
    }""",
        blob,
    )
    page.locator("#book-title").get_by_text("A Free World").wait_for(timeout=15000)
    assert page.locator("#full").is_enabled()
    assert page.locator("#chapter-select,#passage-select,#correction-panel").count() == 0
    assert page.locator("#preview").is_enabled()
    print("DRAG_DROP", page.locator("#book-title").inner_text(), "full_generation_ready", True)
    page.evaluate("localStorage.setItem('studio.job', '00000000000000000000000000000000')")
    page.reload(wait_until="networkidle")
    page.get_by_text("Unknown job", exact=False).wait_for(timeout=10000)
    assert page.locator("#full").is_enabled()
    print("STALE_JOB_RECOVERY", page.locator("#full").is_enabled())
    browser.close()
