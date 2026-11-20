# ADR-0012: The chat model sees allow-listed views, with a session-event rule (amends ADR-0007)

Status: Accepted (2026-10-06). Amends ADR-0007 for chat.

## Context
ADR-0007 covered one event sent to the outfit pick. Chat reaches the whole wardrobe and history through tools.

## Decision
- Tools return domain objects. `chat/privacy.py` builds what the model sees field by field.
- Items: name, category, colours, pattern, fabric, formality, warmth, seasons, status and your tags. Events: type, date and time, indoors or outdoors, dress code. Title, place and notes only for events created in that same chat. A place equal to your home city is sent as "your home city". Wear dates are sent. Names in "Who will be there" (or how many), budget, prices, item notes and photos are never sent.
- The nested outfit pick started from chat gets the same rule. The Outfits page keeps ADR-0007 unchanged.
- The nested pick's call-log rows from chat hold the chat event's title, place and notes. They carry a `session=<id>` marker and are deleted with the chat.

## Alternatives
Stripping fields from REST payloads (a deny-list): a new field would leak without anyone noticing.

## Consequences
A new field reaches the model only when someone adds it to a view. Marker tests guard this.
