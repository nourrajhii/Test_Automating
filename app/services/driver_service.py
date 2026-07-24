from webdriver_manager.chrome import ChromeDriverManager

_cached_path: str | None = None

def get_driver_path() -> str:
    global _cached_path
    if _cached_path is None:
        _cached_path = ChromeDriverManager().install()
    return _cached_path