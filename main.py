"""BM shopping chat using Strands and an AgentCore Browser session."""

import asyncio
import os
import time
from typing import Any

from bedrock_agentcore.tools.browser_client import BrowserClient
from playwright.async_api import Error as PlaywrightError
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from strands import Agent
from strands.models import BedrockModel
from strands_tools.browser import AgentCoreBrowser

from browser_viewer import BrowserViewerServer


REGION = os.getenv("AWS_REGION", "eu-west-1")
TARGET_URL = os.getenv("TARGET_URL", "https://www.bmsupermercados.es/")
SHOP_URL = os.getenv("SHOP_URL", "https://www.online.bmsupermercados.es/es/")
MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "eu.anthropic.claude-sonnet-4-6")
SESSION_NAME = "bm-shopping-session"
console = Console()


class LiveViewAgentCoreBrowser(AgentCoreBrowser):
    """Official Strands browser tool attached to one visible AgentCore session."""

    def __init__(
        self,
        *args: Any,
        viewport: dict[str, int] | None = None,
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        self.viewport = viewport or {"width": 1456, "height": 900}
        self.browser_client: BrowserClient | None = None

    async def create_browser_session(self):
        if not self._playwright:
            raise RuntimeError("Playwright is not initialized")

        client = BrowserClient(region=self.region)
        client.start(
            identifier=self.identifier,
            session_timeout_seconds=self.session_timeout,
            viewport=self.viewport,
        )
        self.browser_client = client
        self._client_dict[SESSION_NAME] = client
        cdp_url, cdp_headers = client.generate_ws_headers()
        return await self._playwright.chromium.connect_over_cdp(
            endpoint_url=cdp_url,
            headers=cdp_headers,
        )

    async def _setup_session_from_browser(self, browser):
        if not browser.contexts:
            raise RuntimeError("AgentCore Browser did not provide a context")
        context = browser.contexts[0]
        open_pages = [page for page in context.pages if not page.is_closed()]
        page = open_pages[-1] if open_pages else await context.new_page()
        return browser, context, page

    def open_shop(self) -> tuple[str, str]:
        """Navigate within the tool's own event loop and return safe page details."""
        return self._execute_async(self._open_shop())

    async def _open_shop(self) -> tuple[str, str]:
        page = self.get_session_page(SESSION_NAME)
        if page is None:
            raise RuntimeError("The BM browser session was not initialized")
        try:
            await page.goto(SHOP_URL, wait_until="domcontentloaded", timeout=60_000)
        except PlaywrightError as exc:
            if "ERR_ABORTED" not in str(exc) and not page.url.startswith("http"):
                raise
        return page.url, await page.title()

    def reconnect_session(self) -> tuple[str, str]:
        """Attach a fresh automation connection after the human takeover."""
        last_error: Exception | None = None
        for _attempt in range(10):
            try:
                return self._execute_async(self._reconnect_session())
            except (IndexError, PlaywrightError, RuntimeError) as exc:
                last_error = exc
                time.sleep(1)
        raise RuntimeError("AgentCore did not reopen the automation stream") from last_error

    async def _reconnect_session(self) -> tuple[str, str]:
        if not self._playwright or not self.browser_client:
            raise RuntimeError("The AgentCore browser client is not available")
        session = self._sessions.get(SESSION_NAME)
        if session is None:
            raise RuntimeError("The local browser session is not available")

        cdp_url, cdp_headers = self.browser_client.generate_ws_headers()
        browser = await self._playwright.chromium.connect_over_cdp(
            endpoint_url=cdp_url,
            headers=cdp_headers,
        )
        if not browser.contexts:
            raise RuntimeError("The authenticated browser context is not available")
        context = browser.contexts[0]

        # AgentCore invalidates the old page's automation target during human
        # takeover. Its URL may still look valid while selectors consistently
        # fail. A new page in the same context keeps the authenticated cookies
        # and gives Playwright a fresh, usable target.
        page = await context.new_page()
        await page.goto(SHOP_URL, wait_until="domcontentloaded", timeout=60_000)
        await page.locator("body").wait_for(state="attached", timeout=30_000)

        session.browser = browser
        session.context = context
        session.page = page
        session.tabs = {"main": page}
        session.active_tab_id = "main"
        return page.url, await page.title()


def initialize_browser(browser_tool: LiveViewAgentCoreBrowser) -> None:
    result = browser_tool.browser(
        browser_input={
            "action": {
                "type": "init_session",
                "description": "Authenticated BM shopping session",
                "session_name": SESSION_NAME,
            }
        }
    )
    if result.get("status") != "success":
        raise RuntimeError(f"Could not initialize AgentCore Browser: {result}")


def create_agent(browser_tool: LiveViewAgentCoreBrowser) -> Agent:
    model = BedrockModel(model_id=MODEL_ID, region_name=REGION)
    return Agent(
        model=model,
        tools=[browser_tool.browser],
        system_prompt=f"""
Eres un asistente de compra de BM Supermercados. Dispones de una sesión de
navegador ya inicializada y autenticada llamada {SESSION_NAME!r}.

Para cada petición de producto debes usar la tool browser y operar la web real:
1. Inspecciona primero la página actual con get_text, get_html o evaluate.
2. Localiza un input visible de búsqueda por sus atributos reales. No asumas que
   el placeholder ni el type son siempre iguales.
3. Escribe un término de catálogo corto, pulsa Enter y espera a que cambien los
   resultados.
4. Lee los resultados visibles y responde con hasta 8 opciones, incluyendo
   nombre, formato y precio cuando aparezcan.

No digas que has buscado si las acciones browser no han terminado con éxito.
No uses get_cookies, set_cookies, network_intercept ni execute_cdp. No solicites
ni leas usuario, contraseña o MFA. Solo pulsa un botón AÑADIR cuando el último
mensaje del usuario confirme explícitamente el producto exacto. Nunca abras ni
confirmes el checkout. No cierres la sesión del navegador. Responde en español.
""",
    )


async def shopping_chat(agent: Agent) -> None:
    console.print(
        Panel(
            "Pide un producto, por ejemplo: “busca plátano de Canarias”. "
            "Después puedes decir: “añade la opción 2”. Usa /salir para terminar.",
            title="Compra BM",
        )
    )
    while True:
        request = (
            await asyncio.to_thread(Prompt.ask, "[bold cyan]Tú[/bold cyan]")
        ).strip()
        if request.lower() in {"/salir", "salir", "exit", "quit"}:
            return
        if not request:
            continue
        try:
            response = await agent.invoke_async(request)
            console.print(Panel(str(response), title="Agente", border_style="yellow"))
        except Exception as exc:
            console.print(f"[red]Error del agente ({type(exc).__name__}): {exc}[/red]")


async def main() -> None:
    console.print(f"Región: [bold]{REGION}[/bold]")
    console.print(f"Modelo: [bold]{MODEL_ID}[/bold]")
    console.print(f"Opening: [link={TARGET_URL}]{TARGET_URL}[/link]")

    browser_tool = LiveViewAgentCoreBrowser(
        region=REGION,
        session_timeout=3_600,
        viewport={"width": 1456, "height": 900},
    )
    viewer: BrowserViewerServer | None = None

    try:
        initialize_browser(browser_tool)
        current_url, title = browser_tool.open_shop()
        client = browser_tool.browser_client
        if client is None:
            raise RuntimeError("AgentCore Browser did not expose its client")

        viewer = BrowserViewerServer(client, port=int(os.getenv("LIVE_VIEW_PORT", "8005")))
        live_view_url = viewer.start(open_browser=True)
        console.print(f"[green]Browser session started.[/green]\nLive View: {live_view_url}")
        console.print(f"Current URL: {current_url}\nPage title: {title}")

        # Human takeover: automation is disabled while credentials are entered.
        client.take_control()
        console.print(
            "\n1. Inicia sesión exclusivamente dentro de Live View.\n"
            "2. Vuelve a esta terminal.\n"
            "3. Pulsa ENTER para devolver el control al agente."
        )
        await asyncio.to_thread(input, "\nPulsa ENTER después de autenticarte...")
        client.release_control()

        current_url, title = browser_tool.reconnect_session()
        console.print(f"\nCurrent URL: {current_url}\nPage title: {title}")
        await shopping_chat(create_agent(browser_tool))
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelado.[/yellow]")
    finally:
        if viewer:
            viewer.stop()
        browser_tool._cleanup()
        console.print("[dim]Sesión remota cerrada.[/dim]")


if __name__ == "__main__":
    asyncio.run(main())
