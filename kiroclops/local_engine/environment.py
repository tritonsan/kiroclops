"""Process-local runtime locations; never modify the system PATH or user config."""
import os
from pathlib import Path


def configure_environment():
    browser=Path(__file__).resolve().parents[2]/'.tools/browser-local'
    if browser.is_dir() and not os.environ.get('PLAYWRIGHT_BROWSERS_PATH'):
        os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(browser)
    return {'browser_store':os.environ.get('PLAYWRIGHT_BROWSERS_PATH','playwright_user_cache')}
