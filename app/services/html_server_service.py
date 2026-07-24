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
<<<<<<< HEAD
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
=======
from http.server import BaseHTTPRequestHandler, HTTPServer
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd


def _find_free_port() -> int:
    """Trouve un port TCP libre sur localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


def _make_handler(html_bytes: bytes):
    """Fabrique un handler HTTP qui sert toujours le même HTML."""
    class _Handler(BaseHTTPRequestHandler):
<<<<<<< HEAD
        # Force "Connection: close" après CHAQUE réponse. Par défaut, Chrome
        # envoie "Connection: keep-alive" et le serveur reste bloqué à
        # attendre une 2e requête sur le même socket -> quand Chrome ferme
        # la connexion (navigation suivante, fin du test), le serveur se
        # prend un ConnectionResetError en pleine attente. Fermer nous-mêmes
        # la connexion après la réponse évite cette attente inutile.
        close_connection = True

=======
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html_bytes)))
<<<<<<< HEAD
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(html_bytes)
            self.close_connection = True
=======
            self.end_headers()
            self.wfile.write(html_bytes)
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd

        def log_message(self, format, *args):
            pass  # Silencieux

    return _Handler


<<<<<<< HEAD
class _QuietThreadingHTTPServer(ThreadingHTTPServer):
    """
    ThreadingHTTPServer qui n'imprime pas de traceback pour les erreurs
    réseau bénignes (connexion fermée par le client — Chrome qui coupe une
    socket déjà servie). Les autres erreurs restent affichées normalement.
    """
    def handle_error(self, request, client_address):
        import sys
        exc_type = sys.exc_info()[0]
        if exc_type in (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            return  # bruit inoffensif, on l'ignore silencieusement
        super().handle_error(request, client_address)


=======
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd
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

<<<<<<< HEAD
    server = _QuietThreadingHTTPServer(("localhost", port), handler)
    server.daemon_threads = True
=======
    server = HTTPServer(("localhost", port), handler)
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    url = f"http://localhost:{port}"
    try:
        yield url
    finally:
        server.shutdown()
<<<<<<< HEAD
        thread.join(timeout=3)
=======
        thread.join(timeout=3)
>>>>>>> 9187a6f133368f59938ee0cf3b3cb68806004bcd
