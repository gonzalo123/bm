"""Small local viewer for the AgentCore Browser Live View stream.

Adapted from AWS's ``BrowserViewerServer`` sample. AgentCore returns a signed
DCV streaming endpoint, not a web page, so a DCV web client must consume it.
"""

import html
import hashlib
import io
import json
import mimetypes
import threading
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from bedrock_agentcore.tools.browser_client import BrowserClient


DCV_SDK_URL = (
    "https://d1uj6qtbmh3dt5.cloudfront.net/webclientsdk/"
    "nice-dcv-web-client-sdk-1.9.100-952.zip"
)
DCV_ARCHIVE_PREFIX = "nice-dcv-web-client-sdk/dcvjs-umd/"
DCV_SDK_SHA256 = "b2df16bad8fc17838284bbc9b11d99fe671f35fce1826a84fcac2b7ef3894967"


class BrowserViewerServer:
    """Serve a localhost page containing Amazon's DCV web client."""

    def __init__(self, client: BrowserClient, port: int = 8005):
        self.client = client
        self.port = port
        self.cache_dir = Path(__file__).parent / ".dcv"
        self.dcv_dir = self.cache_dir / "dcvjs"
        self.server: ThreadingHTTPServer | None = None
        self.server_thread: threading.Thread | None = None

    def _ensure_dcv_sdk(self) -> None:
        if (self.dcv_dir / "dcv.js").exists():
            return

        print("Downloading the Amazon DCV Web Client SDK (first run only)...")
        request = Request(DCV_SDK_URL, headers={"User-Agent": "bm-agentcore-shopping-poc"})
        with urlopen(request, timeout=60) as response:  # noqa: S310 - fixed AWS URL
            archive_bytes = response.read()
            if hashlib.sha256(archive_bytes).hexdigest() != DCV_SDK_SHA256:
                raise RuntimeError("The downloaded DCV SDK checksum is not the expected one")
            archive = zipfile.ZipFile(io.BytesIO(archive_bytes))
            for member in archive.infolist():
                if member.is_dir() or not member.filename.startswith(DCV_ARCHIVE_PREFIX):
                    continue
                relative_name = member.filename.removeprefix(DCV_ARCHIVE_PREFIX)
                target = (self.dcv_dir / relative_name).resolve()
                if not target.is_relative_to(self.dcv_dir.resolve()):
                    raise RuntimeError("Invalid path in the DCV SDK archive")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(member))

        if not (self.dcv_dir / "dcv.js").exists():
            raise RuntimeError("The downloaded DCV SDK does not contain dcv.js")

    def _viewer_html(self) -> bytes:
        signed_stream_url = self.client.generate_live_view_url(expires=300)
        session_id = html.escape(self.client.session_id or "unknown")
        javascript_url = json.dumps(signed_stream_url)
        return f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BM AgentCore Live View</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: system-ui, sans-serif; background: #171717; color: white; }}
    header {{ height: 48px; display: flex; align-items: center; gap: 16px; padding: 0 16px; }}
    #status {{ color: #ffd800; }}
    #display-wrapper {{ height: calc(100vh - 48px); overflow: auto; display: grid; place-items: start center; }}
    #dcv-display {{ width: 1456px; height: 900px; background: black; }}
    button {{ margin-left: auto; padding: 6px 12px; cursor: pointer; }}
  </style>
</head>
<body>
  <header>
    <strong>AgentCore Live View</strong>
    <span>Sesión {session_id}</span>
    <span id="status">Conectando…</span>
    <button onclick="location.reload()">Reconectar</button>
  </header>
  <div id="display-wrapper"><div id="dcv-display"></div></div>
  <script src="/dcvjs/dcv.js"></script>
  <script>
    const signedStreamUrl = {javascript_url};
    const statusElement = document.getElementById("status");
    const setStatus = message => statusElement.textContent = message;
    const authParameters = () => new URL(signedStreamUrl).searchParams;

    if (typeof dcv === "undefined") {{
      setStatus("No se pudo cargar el SDK de DCV");
    }} else {{
      if (dcv.setWorkerPath) dcv.setWorkerPath("/dcvjs/dcv/");
      dcv.authenticate(signedStreamUrl, {{
        promptCredentials: () => setStatus("La URL firmada no fue aceptada"),
        error: (_auth, error) => setStatus("Error de autenticación DCV: " + error),
        success: (_auth, result) => {{
          if (!result || !result[0]) {{
            setStatus("AgentCore no devolvió una sesión DCV");
            return;
          }}
          const {{ sessionId, authToken }} = result[0];
          dcv.connect({{
            url: signedStreamUrl,
            sessionId,
            authToken,
            divId: "dcv-display",
            baseUrl: "/dcvjs",
            callbacks: {{
              firstFrame: () => setStatus("Conectado — control manual activo"),
              error: error => setStatus("Error de conexión DCV: " + error),
              httpExtraSearchParams: authParameters
            }}
          }}).then(connection => {{ window.dcvConnection = connection; }})
             .catch(error => setStatus("No se pudo conectar: " + error));
        }},
        httpExtraSearchParams: authParameters
      }});
    }}
  </script>
</body>
</html>""".encode()

    @staticmethod
    def _send(
        handler: BaseHTTPRequestHandler,
        body: bytes,
        content_type: str,
        status: int = 200,
    ) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(body)

    def _handler_class(self):
        viewer = self

        class ViewerHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                path = urlparse(self.path).path
                if path == "/":
                    viewer._send(self, viewer._viewer_html(), "text/html; charset=utf-8")
                    return
                if path == "/health":
                    viewer._send(self, b"ok", "text/plain; charset=utf-8")
                    return
                if path.startswith("/dcvjs/"):
                    relative_name = unquote(path.removeprefix("/dcvjs/"))
                    target = (viewer.dcv_dir / relative_name).resolve()
                    if target.is_relative_to(viewer.dcv_dir.resolve()) and target.is_file():
                        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                        viewer._send(self, target.read_bytes(), content_type)
                        return
                viewer._send(self, b"Not found", "text/plain; charset=utf-8", status=404)

            def log_message(self, _format: str, *_args) -> None:
                return

        return ViewerHandler

    def start(self, open_browser: bool = True) -> str:
        self._ensure_dcv_sdk()
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), self._handler_class())
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        viewer_url = f"http://127.0.0.1:{self.port}"
        if open_browser:
            webbrowser.open(viewer_url)
        return viewer_url

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
