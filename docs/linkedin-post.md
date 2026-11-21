# LinkedIn post

Every morning, same question in front of the almirah: "kya pehnu?"

So I built an AI stylist that actually knows what's in mine, and I'm open-sourcing it today.

Kya Pehnu (क्या पहनूँ):

👕 Photograph your clothes. A vision model cuts out and tags every piece and builds your digital almirah.
🌦️ Outfits planned around the weather and the occasion, drawn on a mannequin from your own wardrobe.
💬 A chat stylist you can just ask: "casual dinner with friends tonight". It only suggests what you own, and never tells you to go shop.

Bring your own key. No hosted service, no subscription, no account.

→ git clone, then docker compose up
→ paste your Claude, OpenAI, Gemini or OpenRouter key (or run Ollama locally)
→ done

Your photos stay on your machine and go only to the AI provider you pick. No telemetry.

Repo (MIT): https://github.com/suraj-singh-sde/kya-pehnu

#opensource #genai #docker #fastapi #react #selfhosted

P.S. I built this with AI-Craftman, my open-source pipeline that takes a requirement through design, tests, build, QA and deploy with Claude Code agents. Take a look: https://github.com/suraj-singh-sde/ai-craftman

---

Attach: docs/demo/kya-pehnu.mp4 (sign in, almirah, ask the stylist, outfits on the mannequin).
Or images: desktop-07-almirah-open (hero), desktop-08-stylist-outfits, desktop-06-review; phone: 07-almirah-open, 08-stylist-outfits, 04-byok.
