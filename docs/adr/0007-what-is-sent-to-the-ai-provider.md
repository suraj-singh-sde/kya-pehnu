# ADR-0007: What is and is not sent to the AI provider and to Open-Meteo

Status: Accepted (2026-10-06). Amended by ADR-0012 (for chat).

## Context
Outfit picking sends text to the configured AI provider. Event titles and notes are free text.

## Decision
- Sent to the AI provider (text only): event type, title, date and hours, place, indoors or outdoors, dress code, notes, the weather summary and notes, a subset of the profile (style, colours, fit, body notes, skin tone), and the tags of the candidates.
- Sent to Open-Meteo: the place name and the dates.
- Never sent: names in "Who will be there" (or how many), wear dates, photos, home city, budget, other wardrobe items. Because the prompt holds no names, the stored call log holds none.
- Title and notes are still sent, because they carry context the rules cannot use ("mehndi", "beach"). The form says so under both fields. Text from outside is collapsed to one short line (40 characters per item field, 1000 for notes) so it cannot add prompt lines.

## Consequences
A name typed into notes reaches the provider. This is stated in the form and the README, as for photos in Phase 1.
