# What if the shopping list were a conversation with a browser?

Buying groceries online is not especially difficult. Open a page, sign in, type
“Canary Islands bananas”, compare a few options and add one of them to the cart.
What is difficult is turning that sequence of clicks into something useful when
the website changes, authentication needs a person and I do not want a language
model to invent the product it has just bought.

This project is my small experiment with that idea: a Python TUI that lets me
talk to a Strands agent while a remote Amazon Bedrock AgentCore browser displays
the BM Supermercados online store. A person performs the login in a Live View.
Then they type requests in the terminal and the agent uses the real browser to
search for products and, only after an explicit confirmation, click “ADD”.

This is not a scraping bot. It is not an automated checkout system either. It is
a conversational interface in front of a browser tool, with a human boundary
at the point where an action starts to have consequences.

## The idea

The first version had a custom tool built with synchronous Playwright. The agent
said it had found the search box, but the page did not move. The problem was not
BM's selector: the tool was running in an agent worker thread while the `Page`
object belonged to another asynchronous loop.

The solution was to stop wrapping Playwright and use the official
`AgentCoreBrowser` tool from `strands-agents-tools`. The tool owns its
asynchronous context, exposes generic actions (`navigate`, `get_html`, `type`,
`press_key`, `click`, …) and keeps the session the agent must use.

The final flow is deliberately small:

1. Create a remote AgentCore Browser session.
2. Open BM's online store.
3. Serve a local Live View so a person can take control.
4. Enter username, password and MFA manually.
5. Return control to automation.
6. Reconnect Playwright on a new page in the same authenticated context.
7. Chat with Claude; the model inspects the DOM, searches and reads results.
8. Add something to the cart only after an explicit confirmation.

The important decision is not that the model knows how to type “bananas”. The
important decision is that the model never receives the credentials and that a
shopping action is not confused with a text response.

## The experiment's architecture

```text
┌────────────────────┐       ┌───────────────────────────┐
│ TUI / Rich          │──────▶│ Strands Agent + Claude     │
│ request in English  │       │ decides which action to use│
└────────────────────┘       └─────────────┬─────────────┘
                                           │ browser tool
                                           ▼
                              ┌───────────────────────────┐
                              │ AgentCoreBrowser           │
                              │ Playwright over CDP        │
                              └─────────────┬─────────────┘
                                            │ remote session
                         ┌──────────────────┴──────────────────┐
                         ▼                                     ▼
               ┌──────────────────┐                  ┌────────────────┐
               │ BM Supermercados │                  │ Live View DCV  │
               │ real store       │                  │ localhost      │
               └──────────────────┘                  └────────────────┘
```

There are two different channels to the same browser:

- Playwright/CDP lets the agent inspect and operate the page.
- DCV lets the person see the screen and enter sensitive data.

`browser_viewer.py` does not try to fake a browser view. It downloads Amazon's
official DCV web client, verifies its SHA-256 checksum and serves it only on
`127.0.0.1`. The signed Live View URL is generated when the page loads and is
not persisted in the project.

## The non-obvious part: login breaks the page

During testing I found a particularly misleading failure. After login, the URL
was still `https://www.online.bmsupermercados.es/es/`, but selectors could not
even find `body`. It looked as if the agent was operating on the store; in fact,
it was reusing an automation target invalidated by the human takeover.

The reconnection does two things:

```python
cdp_url, cdp_headers = browser_client.generate_ws_headers()
browser = await playwright.chromium.connect_over_cdp(
    endpoint_url=cdp_url,
    headers=cdp_headers,
)

context = browser.contexts[0]
page = await context.new_page()
await page.goto(SHOP_URL, wait_until="domcontentloaded")
await page.locator("body").wait_for(state="attached")
```

The old page can keep showing a correct URL, so checking only `page.url` is not
enough. The new page belongs to the same remote context and therefore keeps the
session cookies, while giving Playwright a fresh target. This is why
`reconnect_session()` exists in `main.py`.

## The agent does not know BM's HTML in advance

The search box may change its `input`, placeholder or Angular component. Instead
of hard-coding one selector, the prompt tells the agent to inspect the current
page and locate a visible input using its real attributes.

Creating the agent is almost all of the integration code:

```python
agent = Agent(
    model=BedrockModel(model_id=MODEL_ID, region_name=REGION),
    tools=[browser_tool.browser],
    system_prompt=SYSTEM_PROMPT,
)
```

The tool performs the deterministic interaction. The model decides the order:
inspect, locate, type, press Enter, wait for results and summarize the visible
options. If an action returns `status=error`, the prompt forbids it from saying
that the search completed successfully.

The add-to-cart rule is just as important:

> The agent may show options after searching, but it may click “ADD” only when
> the latest user message confirms the exact product.

For example:

```text
You:     search for Canary Islands bananas
Agent:   1. Canary Islands IGP bananas … €2.99/kg
         2. Organic bananas … €3.49/kg

You:     add option 1
Agent:   [uses browser.click on the confirmed product]
```

The model is not the catalogue. The catalogue remains in the store, and the
source of truth is what the tool reads from the browser.

## The human session

When the program starts, it opens a browser window with Live View and calls
`take_control()`. The terminal waits:

```text
1. Sign in exclusively inside Live View.
2. Return to this terminal.
3. Press ENTER to give control back to the agent.
```

After Enter, the program calls `release_control()`, reconnects and only then
creates the agent. The model therefore does not participate in login, does not
need to see a password and cannot ask for one as part of the conversation.

## Running it

You need Python 3.13, Poetry, an AWS account with access to Bedrock and
AgentCore Browser, and a local AWS profile. Poetry owns dependency resolution and
`poetry.lock` is kept in the repository.

```bash
poetry install
AWS_PROFILE=sandbox AWS_REGION=eu-central-1 \
poetry run python main.py
```

Optional configuration:

| Variable | Default |
| --- | --- |
| `AWS_REGION` | `eu-west-1` |
| `TARGET_URL` | `https://www.bmsupermercados.es/` |
| `SHOP_URL` | `https://www.online.bmsupermercados.es/es/` |
| `BEDROCK_MODEL_ID` | `eu.anthropic.claude-sonnet-4-6` |
| `LIVE_VIEW_PORT` | `8005` |

`TARGET_URL` is displayed as a startup reference; shopping navigation uses
`SHOP_URL`, the direct entry point to the online store.

## What is deliberately out of scope

This project does not try to solve the whole online grocery experience:

- It does not automate login or store credentials.
- The agent does not use cookies, network interception or CDP commands.
- It does not confirm checkout, payment, address or delivery slot.
- It cannot guarantee that selectors will survive a BM frontend change.
- It never turns a model response into a purchase without human confirmation.
- It is not a catalogue API or a reusable scraper for other stores.

Cost is part of the design too. Each request may involve a Claude call and a
remote browser session. That may be a reasonable personal experiment for a
small shop; a multi-user version would need a serious look at cost,
concurrency, observability, usage limits and the store's terms of service.

## What I learned

A browser is not just another tool when it contains an authenticated session. It
has state, a lifecycle and a boundary between human control and agent control.
The visible URL is not enough to know whether the target is alive, and a
convincing model response does not prove that the interface changed.

The interesting part of this PoC is that separation:

- Claude interprets the request and chooses the next step.
- AgentCore owns the remote browser.
- Playwright verifies that an operable page exists.
- Rich makes the result visible in the terminal.
- The person keeps control of login and consequential actions.

I did not try to build a grocery platform. I tried to build a small interface for
a task I already know how to do manually, and observe where the agent helps and
where I need a boundary.