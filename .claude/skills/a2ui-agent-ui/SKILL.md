---
name: a2ui-agent-ui
description: >
  Complete implementation guide for A2UI v0.9 in ADK agents. Covers the
  correct component schema (NOT the docs — the real format), SSE transport
  via tagged text, builder patterns, React renderer, CSS targeting with
  data-id, and the broken ADK toolset workaround. Based on the working
  ucp-commerce-agent implementation validated against the live schema.
metadata:
  author: Karthikeyan TS
  version: 1.0.0
  requires:
    packages:
      - a2ui-agent-sdk>=0.2.4
    install: "uv add a2ui-agent-sdk"
---

# A2UI — Agent-to-UI Protocol v0.9

A2UI is a JSON schema for describing structured UI component trees that an agent
sends to a client (browser/mobile). Instead of the agent returning plain text, it
returns a component tree that the frontend renders as cards, buttons, lists, etc.

---

## Critical: Real v0.9 Format vs. Official Docs

**The official docs show this — DO NOT USE:**
```json
{"type": "Card", "props": {"title": "Item"}}
```

**The actual working format is:**
```json
{"id": "card-1", "component": "Card", "child": "inner-1"}
```

Key differences discovered by validating against the live schema:
- Field is `"component"` not `"type"`
- **No `"props"` wrapper** — properties are direct fields on the object
- `Column`/`Row` use `"children": ["id1", "id2"]` (array of IDs)
- `Card`/`Button` use `"child": "single-id"` (single ID string)
- Root component ID must be `"root"` and must appear **first** in the array
- `Button` requires `"action": {"event": {"name": "action-name-string"}}`

---

## Component Reference

### Column
```json
{"id": "my-col", "component": "Column", "children": ["child-1", "child-2"]}
```
Renders as `display: flex; flex-direction: column`.

### Row
```json
{"id": "my-row", "component": "Row", "children": ["child-1", "child-2"]}
```
Renders as `display: flex; flex-direction: row; flex-wrap: wrap`.
Use for horizontal chip layouts (options, quantities, tags, variants).

### Card
```json
{"id": "my-card", "component": "Card", "child": "card-inner"}
```
A contained surface with background + border + padding. Single child only.

### Text
```json
{"id": "my-text", "component": "Text", "text": "Hello world"}
```
Add `white-space: pre-line` in CSS to support `\n` line breaks in `text`.

### Divider
```json
{"id": "my-divider", "component": "Divider"}
```
Horizontal separator line.

### Button
```json
{
  "id": "my-btn",
  "component": "Button",
  "child": "btn-label",
  "action": {"event": {"name": "my_action|param1|param2"}}
}
```
Button has one child (the label, usually a Text). The `action.event.name` string
is returned to the agent when clicked — use `|`-delimited encoding for parameters.

---

## A2UI Message Format

Every A2UI payload is a **list of two messages** (per surface):

```python
CATALOG_ID = "https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json"

[
    # Message 1: declare the surface
    {
        "version": "v0.9",
        "createSurface": {
            "surfaceId": "my-surface-id",    # unique per screen/card
            "catalogId": CATALOG_ID,         # required — omitting causes silent failure
        }
    },
    # Message 2: populate the surface with components
    {
        "version": "v0.9",
        "updateComponents": {
            "surfaceId": "my-surface-id",    # must match createSurface
            "components": [
                # root must be first
                {"id": "root", "component": "Column", "children": ["heading", "card"]},
                {"id": "heading", "component": "Text", "text": "My Card"},
                {"id": "card", "component": "Card", "child": "card-inner"},
                {"id": "card-inner", "component": "Column", "children": ["row1", "total"]},
                {"id": "row1",  "component": "Text", "text": "Item: Product Name"},
                {"id": "total", "component": "Text", "text": "Total: $24.00"},
            ]
        }
    }
]
```

---

## Transport: SSE Tag Approach (Recommended)

The ADK toolset helpers (`SendA2uiToClientToolset`, `A2uiEventConverter`) are
**broken** as of a2a-sdk v1.1.0 which dropped `DataPart`/`TextPart`.

The working approach: embed the A2UI JSON in the agent's text output inside
`<a2ui-json>...</a2ui-json>` tags. The frontend parser detects the tag and
routes it to the renderer instead of displaying it as text.

**Backend (Python — works inside any ADK node):**

```python
import json
from google.genai import types as genai_types
from google.adk.events.event import Event

CATALOG_ID = "https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json"

def _a2ui_content(messages: list[dict]) -> genai_types.Content:
    # IMPORTANT: json.dumps WITHOUT indent= — keep it on one line.
    # The frontend SSE parser splits on newlines; multi-line JSON breaks parsing.
    payload = f"<a2ui-json>{json.dumps(messages)}</a2ui-json>"
    return genai_types.Content(
        role="model",
        parts=[genai_types.Part(text=payload)]
    )

# Usage inside a node:
yield Event(content=_a2ui_content([
    {"version": "v0.9", "createSurface": {"surfaceId": "my-card", "catalogId": CATALOG_ID}},
    {"version": "v0.9", "updateComponents": {"surfaceId": "my-card", "components": [
        {"id": "root", "component": "Text", "text": "Hello from agent!"},
    ]}},
]))
```

**Frontend SSE parser (TypeScript):**

```typescript
// In the SSE event loop:
if (p.text) {
    const match = (p.text as string).match(/<a2ui-json>([\s\S]*?)<\/a2ui-json>/)
    if (match) {
        try {
            yield { a2ui: JSON.parse(match[1]) as A2uiSurface[] }
        } catch { /* ignore malformed */ }
    } else {
        yield { text: p.text }   // plain text falls through normally
    }
}
```

---

## TypeScript Types

```typescript
export interface A2uiComponent {
    id: string
    component: 'Column' | 'Row' | 'Card' | 'Text' | 'Divider' | 'Button'
    children?: string[]          // Column, Row
    child?: string               // Card, Button
    text?: string                // Text
    action?: { event: { name: string } }  // Button
}

export interface A2uiSurface {
    version: string
    createSurface?:    { surfaceId: string; catalogId: string }
    updateComponents?: { surfaceId: string; components: A2uiComponent[] }
}

// Extend your Message type:
export interface Message {
    id: string
    role: 'user' | 'agent' | 'system'
    text: string
    a2ui?: A2uiSurface[]
}
```

---

## React Renderer

```tsx
// src/components/A2uiRenderer.tsx
import type { A2uiSurface, A2uiComponent } from '../api'

interface Props {
    surfaces: A2uiSurface[]
    onAction?: (actionName: string) => void
    disabled?: boolean
}

export default function A2uiRenderer({ surfaces, onAction, disabled }: Props) {
    // Collect all components across all updateComponents messages
    const compMap = new Map<string, A2uiComponent>()
    for (const surface of surfaces) {
        if (surface.updateComponents) {
            for (const comp of surface.updateComponents.components) {
                compMap.set(comp.id, comp)
            }
        }
    }

    const render = (id: string): React.ReactNode => {
        const comp = compMap.get(id)
        if (!comp) return null
        switch (comp.component) {
            case 'Column':
                return <div key={id} className="a2ui-column" data-id={id}>
                    {(comp.children ?? []).map(cid => render(cid))}
                </div>
            case 'Row':
                return <div key={id} className="a2ui-row" data-id={id}>
                    {(comp.children ?? []).map(cid => render(cid))}
                </div>
            case 'Card':
                return <div key={id} className="a2ui-card" data-id={id}>
                    {comp.child ? render(comp.child) : null}
                </div>
            case 'Text':
                return <p key={id} className="a2ui-text" data-id={id}>{comp.text ?? ''}</p>
            case 'Divider':
                return <hr key={id} className="a2ui-divider" data-id={id} />
            case 'Button': {
                const actionName = comp.action?.event?.name ?? ''
                return <button key={id} className="a2ui-button" data-id={id}
                    disabled={disabled}
                    onClick={() => !disabled && onAction?.(actionName)}>
                    {comp.child ? render(comp.child) : null}
                </button>
            }
            default: return null
        }
    }

    return <div className="a2ui-surface">{render('root')}</div>
}
```

**Key points:**
- Always render from `id='root'` — the root component
- Pass `data-id={id}` to all elements — enables CSS targeting (see below)
- Pass `disabled={busy}` from parent to prevent double-clicks while agent processes

---

## Integrating into a Chat Page

```tsx
// In your chat component:
const handleA2uiAction = (actionName: string) => {
    if (!hitl || busy) return         // guard against double-clicks
    const h = hitl
    setHitl(null)
    runStream([{
        functionResponse: {
            id: h.interruptId,
            name: 'adk_request_input',
            response: { result: actionName },   // the button action name goes here
        },
    }])
}

// In the message render loop:
{messages.map(m => (
    <div key={m.id} className={`msg msg-${m.role}${m.a2ui ? ' msg-a2ui' : ''}`}>
        {m.a2ui
            ? <A2uiRenderer surfaces={m.a2ui} onAction={handleA2uiAction} disabled={busy} />
            : m.text}
    </div>
))}
```

**HITL unlock rule:** Set `busy = false` when the HITL interrupt arrives (so buttons
become clickable), except for the payment/PIN modal interrupt which should keep
`busy = true` until the PIN modal handles it.

```typescript
if (ev.hitl) {
    setHitl(ev.hitl)
    if (ev.hitl.interruptId !== 'payment_auth') setBusy(false)
}
```

---

## CSS Targeting with `data-id`

Since `data-id` is set on every element, you can target specific components by
their semantic ID — regardless of where they appear in the tree. Use attribute
selectors for patterns:

```css
/* Exact ID match — specific named elements */
[data-id="heading"]     { font-size: 1.2rem; font-weight: 800; }
[data-id="total-row"]   { color: var(--accent); font-weight: 700; }
[data-id="order-id-row"]{ color: var(--gold);   font-weight: 700; }

/* Prefix match — all item card elements */
[data-id^="item-card-"]  { border-left: 4px solid var(--accent); }
[data-id^="item-title-"] { font-size: 1.1rem; font-weight: 800; }
[data-id^="item-btn-"]   { background: transparent; border: 1.5px solid var(--accent); }

/* Option chips (variants, sizes, tiers) — pill shape, auto width */
[data-id^="option-btn-"] {
    width: auto;
    border-radius: 24px;
    background: transparent;
    border: 1.5px solid #4A4A70;
}

/* Quantity picker — circular buttons */
[data-id^="qty-btn-"] {
    width: 52px;
    height: 52px;
    border-radius: 50%;
    padding: 0;
}

/* Status text — green for verified/valid, accent for price, gold for reference IDs */
[data-id="sig-row"],
[data-id="mandate-row"]  { color: #2ECC71; font-weight: 700; }
[data-id="total-row"],
[data-id="charged-row"]  { color: var(--accent); font-weight: 800; }
[data-id="order-id-row"] { color: #F5A623; font-weight: 700; }
```

**Rule:** Assign stable, semantic IDs in your builders (e.g. `"heading"`, `"total-row"`,
`"item-card-{id}"`) — never random IDs. The CSS depends on them.

---

## Builder Pattern (Python)

Keep builders as plain functions returning `list[dict]`. Group related components
under a Column. Use Row for horizontal layouts (chips, tags, pickers).

```python
CATALOG_ID = "https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json"

def _build_my_card(title: str, items: list[str], total_cents: int) -> list[dict]:
    surface_id = f"mycard-{hash(title) % 10000}"
    row_ids = [f"item-{i}" for i in range(len(items))]

    components = [
        # root always first
        {"id": "root", "component": "Column",
         "children": ["heading", "divider-0", "card"]},

        {"id": "heading",   "component": "Text", "text": title},
        {"id": "divider-0", "component": "Divider"},

        {"id": "card", "component": "Card", "child": "card-inner"},
        {"id": "card-inner", "component": "Column",
         "children": [*row_ids, "divider-1", "total-row"]},

        *[{"id": f"item-{i}", "component": "Text", "text": item}
          for i, item in enumerate(items)],

        {"id": "divider-1", "component": "Divider"},
        {"id": "total-row", "component": "Text",
         "text": f"Total: ${total_cents / 100:.2f}"},
    ]

    return [
        {"version": "v0.9",
         "createSurface": {"surfaceId": surface_id, "catalogId": CATALOG_ID}},
        {"version": "v0.9",
         "updateComponents": {"surfaceId": surface_id, "components": components}},
    ]
```

---

## Encoding Button Actions for Multi-Step Flows

Use `|`-delimited strings in `action.event.name` to carry data forward:

```python
# Encode
action = f"item_selected|{item_id}|{item_title}|{variant}"

# Decode in the resuming node
action_str = str(ctx.resume_inputs.get("item_selected", "")).strip()
parts = action_str.split("|")
# parts[0] = "item_selected"
# parts[1] = item_id
# parts[-1] = last positional field
# middle = "|".join(parts[2:-1])  ← handles titles with pipes
```

**Naming convention for interrupt IDs:** use the same name as the action prefix
(e.g. action `"item_selected|..."` → interrupt ID `"item_selected"`). Keeps the
node's first-invocation check obvious:

```python
if "item_selected" not in ctx.resume_inputs:
    # first invocation — emit A2UI + RequestInput
    yield Event(content=_a2ui_content(...))
    yield RequestInput(interrupt_id="item_selected", message="...")
    return
# resumed — parse ctx.resume_inputs["item_selected"]
```

---

## Gotchas

| # | Symptom | Cause | Fix |
|---|---------|-------|-----|
| 1 | `ImportError: cannot import name 'DataPart'` | a2a-sdk ≥ 1.1.0 removed DataPart/TextPart; ADK toolset helpers broken | Use the `<a2ui-json>` SSE tag transport instead |
| 2 | Buttons render but clicking does nothing | `hitl` is null when button is clicked | Ensure HITL `setBusy(false)` fires for A2UI interrupts so buttons are enabled |
| 3 | Components not rendering | `root` is not first in the `components` array | Always put `{"id": "root", ...}` as index 0 |
| 4 | JSON parse error in frontend | `json.dumps(messages, indent=2)` — multi-line breaks SSE framing | Use `json.dumps(messages)` — no `indent=` argument |
| 5 | Missing `catalogId` error | `createSurface` without `catalogId` field | Always include `"catalogId": CATALOG_ID` |
| 6 | `children` not working on Card | Card uses `child` (singular string), not `children` | Card and Button take `"child": "single-id"`, not `"children": [...]` |
| 7 | Button text wrong color | `.a2ui-button .a2ui-text` inherits wrong color | Add `.a2ui-button .a2ui-text { color: inherit; }` to CSS |
| 8 | Stale A2UI card still clickable | Previous surfaces remain in message list | Pass `disabled={busy}` to `A2uiRenderer` — only the latest card's buttons should fire |
