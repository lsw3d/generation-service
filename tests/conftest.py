import time

import httpx
import pytest

from tests.helpers import ALPHA_URL, API_URL, BETA_URL, DEFAULT_PROVIDER_SETTINGS


@pytest.fixture(scope="session", autouse=True)
def wait_for_api():
    deadline = time.time() + 120
    last_error = None
    while time.time() < deadline:
        try:
            response = httpx.get(f"{API_URL}/health", timeout=2)
            if response.status_code == 200:
                return
        except httpx.HTTPError as exc:
            last_error = exc
        time.sleep(1)
    raise RuntimeError(f"API is not reachable at {API_URL}: {last_error}")


@pytest.fixture(autouse=True)
def reset_providers():
    for url in (ALPHA_URL, BETA_URL):
        response = httpx.put(f"{url}/admin/settings", json=DEFAULT_PROVIDER_SETTINGS, timeout=5)
        response.raise_for_status()
    yield
