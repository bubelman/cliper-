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
ANZAHL = 20          # so viele Clips macht der Clipper pro Video
BEHALTEN = 10        # die besten 10 werden hochgeladen; Claude wählt daraus die 5 für TikTok
UNTERTITEL = False   # Untertitel automatisch einbrennen (True = an)
CLIP_SEKUNDEN = 60   # Länge des Grund-Clips (Varianten werden daraus geschnitten)
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


YUNET = HIER / "modelle" / "face_detection_yunet_2023mar.onnx"
_YUNET = {}


def finde_gesichter(cv2, modell, bild):
    """Gesichter als (x, y, w, h) in Pixeln. YuNet (genauer, auch mit Headset/seitlich),
    sonst die alte Haar-Erkennung."""
    if YUNET.exists():
        try:
            if "d" not in _YUNET:
                _YUNET["d"] = cv2.FaceDetectorYN.create(str(YUNET), "", (320, 320), 0.6, 0.3, 50)
            d = _YUNET["d"]
            h, w = bild.shape[:2]
            d.setInputSize((w, h))
            _, treffer = d.detect(bild)
            if treffer is None:
                return []
            return [(int(t[0]), int(t[1]), int(t[2]), int(t[3])) for t in treffer if t[2] >= 20]
        except Exception as fehler:
            log(f"  YuNet nicht nutzbar ({fehler}) - nehme Haar")
    if modell is None:
        return []
    grau = cv2.cvtColor(bild, cv2.COLOR_BGR2GRAY)
    return list(modell.detectMultiScale(grau, 1.1, 5, minSize=(28, 28)))


def analysiere_bild(wc, video, proben=14):
    """Sucht Gesichter: feste Facecam-Position und/oder wo das Hauptgesicht ist."""
    cv2, modell = wc.hole_opencv()
    if cv2 is None or (modell is None and not YUNET.exists()):
        log("  Gesichtserkennung fehlt (OpenCV/Modell)")
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
        K = klein.shape
        treffer = finde_gesichter(cv2, modell, klein)
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


def finde_cam_rahmen(wc, video, face, proben=10):
    """Sucht den Rahmen der Facecam-Box: Kanten, die in allen Bildern an derselben
    Stelle bleiben (das Spiel bewegt sich, der Rand der Kamera-Box nicht).
    face = (Mitte x, Mitte y, Breite, Höhe) als Anteil. Ergebnis (x, y, b, h) oder None."""
    cv2, _ = wc.hole_opencv()
    if cv2 is None:
        return None
    import numpy as np
    cap = cv2.VideoCapture(str(video))
    gesamt = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    karten = []
    for i in range(proben):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(gesamt * (i + 0.5) / proben))
        ok, bild = cap.read()
        if not ok:
            continue
        hb, bb = bild.shape[:2]
        klein = cv2.resize(bild, (960, int(hb * 960 / bb)))
        grau = cv2.cvtColor(klein, cv2.COLOR_BGR2GRAY)
        karten.append((cv2.Canny(grau, 40, 120) > 0).astype(np.float32))
    cap.release()
    if len(karten) < 4:
        return None
    # Kante zählt nur, wenn sie in den meisten Bildern da ist
    fest = (np.mean(karten, axis=0) > 0.5).astype(np.float32)
    # 1 Pixel Toleranz, falls die Kante leicht flackert
    fest = cv2.dilate(fest, np.ones((3, 3), np.uint8))
    H, B = fest.shape
    mx, my, fw, fh = face[0] * B, face[1] * H, face[2] * B, face[3] * H

    def suche(achse, von, bis, schritt, quer_von, quer_bis):
        """Erste Linie (Spalte bzw. Zeile), die über die Quer-Strecke fast durchgehend ist."""
        quer_von, quer_bis = max(0, int(quer_von)), min(H if achse == "x" else B, int(quer_bis))
        if quer_bis - quer_von < 10:
            return None
        pos = int(von)
        while (pos < bis if schritt > 0 else pos > bis):
            if 0 <= pos < (B if achse == "x" else H):
                linie = fest[quer_von:quer_bis, pos] if achse == "x" else fest[pos, quer_von:quer_bis]
                if linie.mean() > 0.7:
                    return pos
            pos += schritt
        return None

    links = suche("x", mx - fw * 0.3, mx - fw * 4, -1, my - fh * 0.45, my + fh * 0.45)
    rechts = suche("x", mx + fw * 0.3, mx + fw * 4, 1, my - fh * 0.45, my + fh * 0.45)
    oben = suche("y", my - fh * 0.3, my - fh * 4, -1, mx - fw * 0.45, mx + fw * 0.45)
    unten = suche("y", my + fh * 0.3, my + fh * 4, 1, mx - fw * 0.45, mx + fw * 0.45)
    # Box am Bildrand: dann ist der Bildrand die Grenze
    if links is None and mx - fw * 4 <= 0:
        links = 0
    if rechts is None and mx + fw * 4 >= B:
        rechts = B
    if oben is None and my - fh * 4 <= 0:
        oben = 0
    if unten is None and my + fh * 4 >= H:
        unten = H
    if None in (links, rechts, oben, unten):
        return None
    b, h = (rechts - links) / B, (unten - oben) / H
    # Plausibel? Box muss das Gesicht deutlich enthalten und darf nicht riesig sein
    if not (face[2] * 1.3 < b < 0.6 and face[3] * 1.3 < h < 0.7):
        return None
    rand = 2 / B     # Rahmenlinie selbst weglassen
    return (links / B + rand, oben / H + rand, b - 2 * rand, h - 2 * rand)


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
            rahmen = finde_cam_rahmen(wc, master, bild["facecam"])
            if rahmen:
                x, y, b, h = rahmen        # genau die Kamera-Box, nichts vom Spiel drumherum
                art = "Kamera-Box"
            else:
                # Box nicht gefunden: enger Bereich ums Gesicht (Kopf und Schultern)
                h_px = min(qh, mh * qh * 2.4)
                b_px = min(qb, h_px * 1080 / 672)
                b, h = b_px / qb, h_px / qh
                x = min(max(0.0, mx - b / 2), 1 - b)
                y = min(max(0.0, my - h / 2 + h * 0.08), 1 - h)
                art = "Gesicht"
            # Höhe des Facecam-Streifens passend zur Box wählen, damit kaum etwas abgeschnitten wird
            seiten = (b * qb) / max(1.0, h * qh)
            anteil = min(0.42, max(0.28, (1080 / seiten) / 1920))
            ziel_seiten = 1080 / (1920 * anteil)
            if seiten < ziel_seiten:
                # Box zu hoch: oben/unten etwas weglassen, Gesicht bleibt in der Mitte
                neu_h = (b * qb / ziel_seiten) / qh
                y = min(max(y, my - neu_h / 2), y + h - neu_h)
                h = neu_h
            # Spiel: mittlere 76 % der Breite, volle Höhe, ganz zeigen (Rest unscharf gefüllt).
            # Liegt die Facecam am Rand, wird sie dabei mit abgeschnitten.
            einst["facecam"] = {
                "anteil": anteil,
                "face": {"x": x, "y": y, "b": b, "h": h},
                "haupt": {"x": 0.12, "y": 0, "b": 0.76, "h": 1},
                "einpassen": True,
            }
            log(f"  Clip {nr}: Facecam erkannt ({art}) -> Facecam oben, Spiel unten")
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
            # Spiel ohne Kamera: mittlere 64 % ganz zeigen statt nur einen schmalen Streifen
            einst["ganz"] = {"x": 0.18, "y": 0, "b": 0.64, "h": 1}
            log(f"  Clip {nr}: kein Gesicht -> Spiel ganz, Rest unscharf")
        try:
            wc.rendere_clip(nr, einst)
        except Exception as fehler:
            log(f"  Clip {nr}: Bearbeitung fehlgeschlagen ({fehler}) - Original bleibt")


# Test für mehr Watchtime (Levin, 22.11.2026): jeder Clip bekommt eine Variante
# aus Länge und Vorlauf (Sekunden vor dem Höhepunkt). Geschnitten wird aus dem
# fertigen 30-s-Clip, dessen Höhepunkt bei VORLAUF_SEKUNDEN liegt.
VARIANTEN = [(15, 0), (20, 0), (30, VORLAUF_SEKUNDEN), (60, VORLAUF_SEKUNDEN), (15, 1), (20, 1)]


def stille_stellen(datei):
    """Pausen im Ton (Start, Ende) - dort kann man sauber schneiden."""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(datei), "-af",
                        "silencedetect=noise=-32dB:d=0.25", "-f", "null", "-"],
                       capture_output=True, text=True)
    pausen, anfang = [], None
    for zeile in r.stderr.splitlines():
        if "silence_start:" in zeile:
            try:
                anfang = float(zeile.split("silence_start:")[1].split()[0])
            except ValueError:
                anfang = None
        elif "silence_end:" in zeile and anfang is not None:
            try:
                pausen.append((anfang, float(zeile.split("silence_end:")[1].split()[0])))
            except ValueError:
                pass
            anfang = None
    return pausen


def schnittpunkte(c, datei, laenge, vor, gesamt):
    """Start und Ende so legen, dass nie mitten im Wort oder Satz geschnitten wird.

    Start: Satzanfang (oder Sprechpause) kurz vor "Höhepunkt minus Vorlauf".
    Ende: Satzende (oder Pause) möglichst nah an der Wunschlänge.
    """
    woerter = [w for w in (c.get("woerter") or []) if len(w) == 3]
    pausen = stille_stellen(datei)
    hoch = float(c.get("hoehepunkt", VORLAUF_SEKUNDEN) or VORLAUF_SEKUNDEN)
    wunsch = max(0.0, hoch - vor)

    anfaenge, enden = [], []
    for i, (s, e, w) in enumerate(woerter):
        vorher = woerter[i - 1] if i else None
        if vorher is None or s - vorher[1] > 0.35 or vorher[2][-1:] in ".!?":
            anfaenge.append(s)
        danach = woerter[i + 1] if i + 1 < len(woerter) else None
        if danach is None or danach[0] - e > 0.35 or w[-1:] in ".!?":
            enden.append(e)
    anfaenge += [b for a, b in pausen]          # Ende einer Pause = hier geht es los
    enden += [a for a, b in pausen]             # Anfang einer Pause = hier ist Schluss

    # Start: spätester Satzanfang knapp vor dem Wunschpunkt
    passend = [t for t in anfaenge if wunsch - 2.5 <= t <= wunsch + 0.3]
    if passend:
        start = max(passend)
    else:
        laufend = [s for s, e, w in woerter if s <= wunsch < e]
        start = laufend[0] if laufend else wunsch
    start = max(0.0, start - 0.15)

    # Ende: Satzende nah an der Wunschlänge, nie über das Clipende hinaus
    ziel = min(start + laenge, gesamt)
    passend = [t for t in enden if ziel - 3.0 <= t <= ziel + 2.0 and t - start >= 6 and t <= gesamt]
    if passend:
        ende = min(passend, key=lambda t: abs(t - ziel)) + 0.25
    else:
        laufend = [e for s, e, w in woerter if s < ziel < e]
        ende = (laufend[0] + 0.25) if laufend else ziel
    ende = min(ende, gesamt)
    return round(start, 2), round(ende, 2)


def teste_varianten(wc):
    for i, c in enumerate(wc.STATE["clips"]):
        laenge, vor = VARIANTEN[i % len(VARIANTEN)]
        quelle = wc.AUSGABEORDNER / c["datei"]
        if not quelle.is_file():
            continue
        try:
            gesamt = float(wc.dauer_von(quelle)) or CLIP_SEKUNDEN
        except Exception:
            gesamt = CLIP_SEKUNDEN
        start, ende = schnittpunkte(c, quelle, laenge, vor, gesamt)
        dauer = max(3.0, ende - start)
        ziel = quelle.with_name(f"{quelle.stem}_{laenge}s{vor}.mp4")
        blende = f"afade=t=in:st=0:d=0.08,afade=t=out:st={max(0, dauer - 0.3):.2f}:d=0.3"
        befehl = ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.2f}", "-i", str(quelle),
                  "-t", f"{dauer:.2f}", "-af", blende, "-c:v", "libx264", "-preset", "veryfast",
                  "-crf", "20", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(ziel)]
        if subprocess.run(befehl).returncode == 0 and ziel.exists() and ziel.stat().st_size > 50000:
            quelle.unlink()
            c["datei"] = ziel.name
            c["dauer"] = round(dauer, 1)
            c["variante"] = f"{laenge}s, {vor}s vor Höhepunkt"
            c["schnitt"] = f"{start:.1f}-{ende:.1f}"
            log(f"  {ziel.name}: Variante {c['variante']}, Schnitt {start:.1f}-{ende:.1f} s")
        else:
            c["variante"] = f"{CLIP_SEKUNDEN}s, {VORLAUF_SEKUNDEN}s vor Höhepunkt"
            if ziel.exists():
                ziel.unlink()


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
    wc.IGNORIERE_START = 600        # Startbildschirm/Intro der Streams (erste 10 Min.) überspringen
    wc.UNTERTITEL_SPRACHE = ""      # Sprache selbst erkennen (sonst wird Englisch als Deutsch abgetippt)
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

    # Nur die besten Clips behalten (Bewertung des Clippers: Ton, Chat, Gesagtes)
    def wert(c):
        # kaum Gesprochenes (Musik, Startbildschirm, stilles Spielen) = schwacher Clip
        woerter = len((c.get("text") or "").split())
        return c.get("punkte", 0) * (0.5 if woerter < 15 else 1.0)
    clips = sorted(wc.STATE["clips"], key=wert, reverse=True)
    for c in clips[BEHALTEN:]:
        datei = wc.AUSGABEORDNER / c.get("datei", "")
        if datei.is_file():
            datei.unlink()
    wc.STATE["clips"] = clips[:BEHALTEN]

    # Eindeutige Namen: Datum_Kanal_Nummer.mp4 (sonst gibt es jeden Tag wieder clip_01 ...)
    vorne = time.strftime("%Y-%m-%d") + "_" + "".join(z for z in kanal.lstrip("@") if z.isalnum())
    for c in wc.STATE["clips"]:
        alt = wc.AUSGABEORDNER / c.get("datei", "")
        neu = wc.AUSGABEORDNER / f"{vorne}_{int(c.get('nummer', 0)):02d}.mp4"
        if alt.is_file():
            alt.rename(neu)
            c["datei"] = neu.name
    wc.STATE["kanal"] = kanal.lstrip("@")
    teste_varianten(wc)
    log("Beste Clips: " + ", ".join(f"{c.get('datei')} ({c.get('punkte', 0)} P.)"
                                      for c in wc.STATE["clips"]))

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
