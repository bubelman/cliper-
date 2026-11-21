# Clipper

Macht jeden Tag automatisch Clips aus den Twitch-Kanälen in `kanaele.txt`
(nur die ersten 2 Stunden eines Streams, 10 Clips à 30 s), lädt sie auf
Litterbox hoch (72 h gültig) und schreibt die Links in `clips/tiktok_links.json`.
Claude plant sie dann über Metricool auf TikTok und YouTube ein.

Von Hand starten: Reiter **Actions** → **Clips machen** → **Run workflow**.
