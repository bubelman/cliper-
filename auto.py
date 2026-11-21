"""
Automatik: Holt das neueste Video (VOD) eines Kanals aus kanaele.txt,
lässt den Clipper daraus Clips schneiden und lädt sie für TikTok hoch.
Läuft einmal am Tag über die Windows-Aufgabenplanung.
"""
import json, os, subprocess, sys, time
from pathlib import Path

HIER = Path(__file__).resolve().parent
os.chdir(HIER)
sys.path.insert(0, str(HIER))

KANAELE = HIER / "kanaele.txt"
ERLEDIGT = HIER / "arbeit" / "erledigte_videos.json"
ANZAHL = 10          # Clips pro Video
UNTERTITEL = False   # Untertitel automatisch einbrennen (True = an)
CLIP_SEKUNDEN = 30   # Länge pro Clip
VORLAUF_SEKUNDEN = 4 # so viele Sekunden vor dem Höhepunkt starten
MAX_MINUTEN = 120    # nur die ersten X Minuten eines Streams nutzen
SPERRE = HIER / "arbeit" / "automatik_laeuft.txt"
LOG = HIER / "arbeit" / "automatik_log.txt"


def log(text):
    zeile = f"{time.strftime('%Y-%m-%d %H:%M')}  {text}"
    print(zeile)
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(zeile + "\n")


def kanaele():
    if not KANAELE.exists():
        return []
    liste = []
    for zeile in KANAELE.read_text(encoding="utf-8").splitlines():
        zeile = zeile.strip()
        if zeile and not zeile.startswith("#"):
            liste.append(zeile)
    return liste


def videos_seite(kanal):
    if kanal.startswith("http"):
        return kanal
    if kanal.startswith("@"):                       # YouTube-Kanal
        return f"https://www.youtube.com/{kanal}/streams"
    return f"https://www.twitch.tv/{kanal}/videos?filter=archives&sort=time"


def neueste_videos(kanal, wie_viele=3):
    befehl = [sys.executable, "-m", "yt_dlp", "--flat-playlist", "-J",
              "--playlist-end", str(wie_viele), videos_seite(kanal)]
    ergebnis = subprocess.run(befehl, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=180)
    if ergebnis.returncode != 0:
        raise RuntimeError(ergebnis.stderr.strip()[-300:])
    daten = json.loads(ergebnis.stdout)
    urls = []
    for e in daten.get("entries") or []:
        # laufende oder angekündigte Streams haben noch kein fertiges Video
        if e.get("live_status") in ("is_live", "is_upcoming", "post_live"):
            continue
        url = e.get("url") or e.get("webpage_url")
        if url and not url.startswith("http"):
            url = f"https://www.youtube.com/watch?v={url}"
        if url:
            urls.append(url)
    return urls


def analysiere_bild(wc, video, proben=14):
    """Sucht Gesichter: feste Facecam-Position und/oder wo das Hauptgesicht ist."""
    cv2, modell = wc.hole_opencv()
    if cv2 is None or modell is None:
        return {"facecam": None, "gesicht_x": None}
    cap = cv2.VideoCapture(str(video))
    gesamt = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    gross, zellen = [], {}
    gelesen = 0
    for i in range(proben):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(gesamt * (i + 0.5) / proben))
        ok, bild = cap.read()
        if not ok:
            continue
        gelesen += 1
        hb, bb = bild.shape[:2]
        f = min(1.0, 1920 / bb)
        klein = cv2.resize(bild, (int(bb * f), int(hb * f)))
        grau = cv2.cvtColor(klein, cv2.COLOR_BGR2GRAY)
        K = klein.shape
        treffer = modell.detectMultiScale(grau, 1.1, 5, minSize=(28, 28))
        boxen = [((x + w / 2) / K[1], (y + h / 2) / K[0], w / K[1], h / K[0])
                 for x, y, w, h in treffer]
        if boxen:
            gross.append(max(boxen, key=lambda t: t[2]))
        gesehen = set()
        for bx in boxen:
            if bx[2] >= 0.11:
                continue
            zelle = (round(bx[0] / 0.08), round(bx[1] / 0.08))
            if zelle in gesehen:
                continue
            gesehen.add(zelle)
            zellen.setdefault(zelle, []).append(bx)
    cap.release()
    import statistics as st
    ergebnis = {"facecam": None, "gesicht_x": None}
    if not gelesen:
        return ergebnis
    # Facecam = kleines Gesicht, das in den meisten Bildern an derselben Stelle ist
    if zellen:
        beste = max(zellen.values(), key=len)
        if len(beste) >= gelesen * 0.5:
            ergebnis["facecam"] = tuple(st.median(b[k] for b in beste) for k in range(4))
    if len(gross) >= gelesen * 0.3:
        ergebnis["gesicht_x"] = st.median(g[0] for g in gross)
    return ergebnis


def bearbeite_clips(wc, irl=False):
    """Facecam oben + Spiel unten, sonst Ausschnitt aufs Gesicht. Dazu Untertitel."""
    wc.UNTERTITEL_SPRACHE = ""          # Sprache automatisch erkennen
    for clip in list(wc.STATE.get("clips", [])):
        nr = clip["nummer"]
        master = wc.master_pfad(nr)
        if not master.exists():
            continue
        bild = analysiere_bild(wc, master)
        einst = {"untertitel": UNTERTITEL}
        qb, qh = wc.masse_von(master)
        if bild["facecam"] and not irl:
            mx, my, mw, mh = bild["facecam"]
            # Bereich um das Gesicht, im Seitenverhältnis des oberen Streifens (1080x576)
            h_px = min(qh, mh * qh * 3.2)
            b_px = min(qb, h_px * 1080 / 576)
            b, h = b_px / qb, h_px / qh
            x = min(max(0.0, mx - b / 2), 1 - b)
            y = min(max(0.0, my - h / 2), 1 - h)
            breite_spiel = (1344 / 1080) ** -1 * qh / qb   # Spiel: Mitte, volle Höhe
            breite_spiel = min(1.0, breite_spiel)
            einst["facecam"] = {
                "anteil": 0.3,
                "face": {"x": x, "y": y, "b": b, "h": h},
                "haupt": {"x": (1 - breite_spiel) / 2, "y": 0, "b": breite_spiel, "h": 1},
            }
            log(f"  Clip {nr}: Facecam erkannt -> Facecam oben, Spiel unten")
        elif bild["gesicht_x"] is not None:
            wert = bild["gesicht_x"]
            # fester Ausschnitt mittig aufs Gesicht
            H = 1920
            bw = qb * H / qh
            spiel = (bw - 1080) / 2
            if spiel > 1:
                cx = min(max(0.0, wert * bw - 540), bw - 1080)
                einst["versatz_x"] = max(-1.0, min(1.0, 1 - cx / spiel))
            log(f"  Clip {nr}: Kamera-Stream -> Ausschnitt aufs Gesicht")
        else:
            log(f"  Clip {nr}: kein Gesicht -> Bildmitte")
        try:
            wc.rendere_clip(nr, einst)
        except Exception as fehler:
            log(f"  Clip {nr}: Bearbeitung fehlgeschlagen ({fehler}) - Original bleibt")


def main():
    liste = kanaele()
    if not liste:
        log("kanaele.txt ist leer – bitte mindestens einen Kanal eintragen.")
        return

    erledigt = json.loads(ERLEDIGT.read_text(encoding="utf-8")) if ERLEDIGT.exists() else []

    # jeden Tag mit einem anderen Kanal anfangen, damit alle drankommen
    tag = int(time.strftime("%j"))
    liste = liste[tag % len(liste):] + liste[:tag % len(liste)]

    import webclipper as wc
    wc.CLIP_LAENGE = CLIP_SEKUNDEN
    wc.VORLAUF = VORLAUF_SEKUNDEN
    wc.MAX_LAENGE = MAX_MINUTEN * 60
    wc.ARBEITSORDNER.mkdir(exist_ok=True)
    wc.AUSGABEORDNER.mkdir(exist_ok=True)
    wc.waehle_encoder()

    # Kandidaten der Reihe nach probieren: klappt ein Video nicht, nimm das nächste
    versuche = 0
    ziel, irl = None, False
    for zeile in liste:
        teile = zeile.split()
        kanal = teile[0]
        try:
            urls = neueste_videos(kanal)
        except Exception as fehler:
            log(f"{kanal}: Videoliste nicht abrufbar ({fehler})")
            continue
        for url in urls:
            if url in erledigt:
                continue
            versuche += 1
            irl = len(teile) > 1 and teile[1].lower() == "irl"
            log(f"Neues Video: {url}")
            wc.LAUF["nummer"] += 1
            wc.arbeite(url, ANZAHL, True, False, wc.LAUF["nummer"])
            if wc.STATE.get("phase") == "fertig" and wc.STATE.get("clips"):
                ziel = url
                break
            log(f"Clipper ist nicht fertig geworden (Phase: {wc.STATE.get('phase')}).")
            for z in wc.STATE.get("log", [])[-2:]:
                log("   " + z)
            if versuche >= 4:
                break
        if ziel or versuche >= 4:
            break

    if not ziel:
        log("Kein Video hat geklappt – heute keine neuen Clips.")
        return

    log("Bearbeite Clips (Facecam, Untertitel) ...")
    bearbeite_clips(wc, irl)

    wc.stand_sichern()          # Titel und Clips für Claude in arbeit/stand.json
    erledigt.append(ziel)
    ERLEDIGT.write_text(json.dumps(erledigt[-200:], indent=1), encoding="utf-8")
    log(f"{len(wc.STATE['clips'])} Clips fertig. Lade für TikTok hoch ...")

    import tiktok_links
    tiktok_links.main()
    log("Fertig. Claude plant die Clips um 19:54 Uhr ein.")


if __name__ == "__main__":
    # nicht doppelt laufen (z. B. Autostart + "Neue Clips jetzt")
    if SPERRE.exists() and time.time() - SPERRE.stat().st_mtime < 3 * 3600:
        print("Die Automatik läuft schon.")
        sys.exit(0)
    SPERRE.parent.mkdir(exist_ok=True)
    SPERRE.write_text(time.strftime("%Y-%m-%d %H:%M"), encoding="utf-8")
    try:
        main()
    finally:
        SPERRE.unlink(missing_ok=True)
