"""
html_server_service.py
----------------------
Démarre un mini-serveur HTTP temporaire qui sert le HTML fourni par l'utilisateur
sur un port libre, pour que Selenium puisse l'ouvrir via une vraie URL.

Usage :
    async with serve_html(html_code) as url:
        # url = "http://localhost:9347"
        # Selenium peut faire driver.get(url)
"""
import asyncio
import socket
import threading
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer


def _find_free_port() -> int:
    """Trouve un port TCP libre sur localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


def _make_handler(html_bytes: bytes):
    """Fabrique un handler HTTP qui sert toujours le même HTML."""
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html_bytes)))
            self.end_headers()
            self.wfile.write(html_bytes)

        def log_message(self, format, *args):
            pass  # Silencieux

    return _Handler


@asynccontextmanager
async def serve_html(html_code: str):
    """
    Context manager asynchrone.
    Lance un serveur HTTP dans un thread dédié, yield l'URL,
    puis arrête proprement le serveur à la sortie du bloc.

    Exemple :
        async with serve_html(html_code) as url:
            # url = "http://localhost:54321"
            script = await generate_selenium_script(scenario, analysis, url)
    """
    html_bytes = html_code.encode("utf-8")
    port = _find_free_port()
    handler = _make_handler(html_bytes)

    server = HTTPServer(("localhost", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    url = f"http://localhost:{port}"
    try:
        yield url
    finally:
        server.shutdown()
        thread.join(timeout=3)
