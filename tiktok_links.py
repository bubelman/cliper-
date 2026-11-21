"""
Lädt alle fertigen Clips aus dem Ordner "clips" auf Litterbox hoch
(kostenlos, ohne Konto, Dateien werden nach 72 Stunden automatisch gelöscht)
und speichert die Links in clips/tiktok_links.json.
Claude liest diese Datei und plant die Clips über Metricool auf TikTok ein.
"""
import json, time, uuid, urllib.request
from pathlib import Path

ORDNER = Path(__file__).parent / "clips"
LINKS = ORDNER / "tiktok_links.json"
API = "https://litterbox.catbox.moe/resources/internals/api.php"
HALTBAR = 70 * 3600  # Links nach 70 Stunden neu hochladen


def lade_hoch(datei: Path) -> str:
    grenze = uuid.uuid4().hex
    kopf = (
        f"--{grenze}\r\nContent-Disposition: form-data; name=\"reqtype\"\r\n\r\nfileupload\r\n"
        f"--{grenze}\r\nContent-Disposition: form-data; name=\"time\"\r\n\r\n72h\r\n"
        f"--{grenze}\r\nContent-Disposition: form-data; name=\"fileToUpload\"; "
        f"filename=\"{datei.name}\"\r\nContent-Type: video/mp4\r\n\r\n"
    ).encode()
    ende = f"\r\n--{grenze}--\r\n".encode()
    daten = kopf + datei.read_bytes() + ende
    anfrage = urllib.request.Request(API, data=daten, method="POST", headers={
        "Content-Type": f"multipart/form-data; boundary={grenze}",
        "User-Agent": "clipper",
    })
    with urllib.request.urlopen(anfrage, timeout=600) as antwort:
        text = antwort.read().decode().strip()
    if not text.startswith("http"):
        raise RuntimeError(text[:200])
    return text


def main():
    links = {}
    if LINKS.exists():
        try:
            links = json.loads(LINKS.read_text(encoding="utf-8"))
        except Exception:
            links = {}

    clips = sorted(ORDNER.glob("*.mp4"))
    if not clips:
        print("Keine Clips im Ordner 'clips' gefunden.")
        return

    jetzt = time.time()
    for clip in clips:
        alt = links.get(clip.name)
        if alt and jetzt - alt.get("zeit", 0) < HALTBAR and alt.get("groesse") == clip.stat().st_size:
            print(f"OK (schon da)  {clip.name}")
            continue
        print(f"Lade hoch ...  {clip.name}")
        try:
            url = lade_hoch(clip)
        except Exception as fehler:
            print(f"   FEHLER: {fehler}")
            continue
        alt = links.pop(clip.name, None)
        if alt and alt.get("url"):
            links[f"{clip.stem}_{int(alt.get('zeit', 0))}.mp4"] = alt   # alten Link behalten
        links[clip.name] = {"url": url, "zeit": jetzt, "groesse": clip.stat().st_size}
        LINKS.write_text(json.dumps(links, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"   fertig: {url}")

    # Alte Einträge entfernen (Litterbox löscht nach 72 Stunden)
    links = {k: v for k, v in links.items()
             if (ORDNER / k).exists() or time.time() - v.get("zeit", 0) < 72 * 3600}
    LINKS.write_text(json.dumps(links, indent=1, ensure_ascii=False), encoding="utf-8")
    print("\nFertig. Sag Claude jetzt: \"Clips sind hochgeladen\".")


if __name__ == "__main__":
    main()
