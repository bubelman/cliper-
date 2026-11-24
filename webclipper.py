"""
webclipper.py - Clipper mit Weboberfläche

Starten:
    py webclipper.py

Der Browser öffnet sich von selbst. Dieses Fenster offen lassen.
Beenden: Strg+C in der Eingabeaufforderung.

Braucht: ffmpeg, yt-dlp, numpy
"""

import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import wave
import webbrowser
from pathlib import Path
from urllib.parse import unquote

import numpy as np

# Damit Umlaute auch in der Eingabeaufforderung sauber ankommen
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ----------------------------------------------------------------------
# EINSTELLUNGEN
# ----------------------------------------------------------------------

PORT            = 8000
CLIP_LAENGE     = 40     # Sekunden pro Clip
MAX_LAENGE      = 7200   # nur die ersten X Sekunden eines Streams nutzen (0 = alles)
VORLAUF         = 12     # Sekunden vor dem lauten Moment
MIN_ABSTAND     = 90     # Mindestabstand zwischen zwei Clips
ANTEIL_PEGEL    = 0.35   # 1.0 = nur Lautstärke, 0.0 = nur plötzlicher Anstieg
AUTO_AUSSCHNITT = True   # Bildausschnitt folgt Gesicht bzw. Bewegung
SPRACHE_AUSWERTEN = True # Kandidaten abtippen und nach dem Gesagten bewerten
ANTEIL_SPRACHE  = 0.5    # Gewicht des Gesagten gegenüber dem Ton
CHAT_AUSWERTEN  = True   # Twitch und YouTube: Chat-Aktivität mitbewerten
ANTEIL_CHAT     = 0.5    # Gewicht des Chats, wenn er verfügbar ist
CHAT_KANDIDATEN = True   # Stellen mit viel Chat auch dann prüfen, wenn es dort nicht laut ist
CHAT_VERZOEGERUNG = 6    # so viele Sekunden reagiert der Chat später als das Geschehen
KANDIDATEN_FAKTOR = 4    # so viele Kandidaten je gewünschtem Clip
KI_BEWERTEN     = True   # Claude bewertet die Kandidaten (braucht ANTHROPIC_API_KEY)
KI_MODELL       = "claude-sonnet-5-5"
ANTEIL_KI       = 0.5    # Gewicht der Claude-Bewertung
IGNORIERE_START = 30     # Intro überspringen
IGNORIERE_ENDE  = 30     # Outro überspringen

UNTERTITEL_MODELL  = "base"   # small/medium = genauer, aber langsamer
UNTERTITEL_SPRACHE = "de"     # leer lassen für automatische Erkennung
UNTERTITEL_SCHRIFT = "Arial"  # Schriftart der Untertitel
UNTERTITEL_FARBE   = "00E0FF" # Hervorhebung, Reihenfolge Blau-Grün-Rot (hier Gelb)
UNTERTITEL_BETONT  = True     # gesprochenes Wort farbig hervorheben

GLEICHZEITIG    = 16     # Stücke, die parallel geladen werden (mehr = schneller)
PARALLEL_CLIPS  = 3      # wie viele Clips gleichzeitig bearbeitet werden
HOEHE           = 1080   # max. Videohöhe. 720 ist deutlich schneller als 1080
CLIP_BREITE     = 1080   # Ausgabegröße im Hochformat (1080x1920)

# Zugriffswege, die der Reihe nach probiert werden, wenn eine Seite blockt
STRATEGIEN = [
    ("normal",             []),
    ("TV-App",             ["--extractor-args", "youtube:player_client=tv"]),
    ("Handy-Seite",        ["--extractor-args", "youtube:player_client=mweb"]),
    ("iOS-App",            ["--extractor-args", "youtube:player_client=ios"]),
    ("Cookies aus Edge",   ["--cookies-from-browser", "edge"]),
    ("Cookies aus Chrome", ["--cookies-from-browser", "chrome"]),
]

ZUGANG_DATEI  = Path("google_zugang.json")     # aus der Google Cloud Console
TOKEN_DATEI   = Path("arbeit/youtube_token.json")
YT_RECHTE     = ["https://www.googleapis.com/auth/youtube.upload"]

ARBEITSORDNER = Path("arbeit")
AUSGABEORDNER = Path("clips")
MERKZETTEL    = ARBEITSORDNER / "letzte_quelle.txt"

# ----------------------------------------------------------------------

STATE = {
    "phase": "bereit",
    "log": [],
    "kurve": [],
    "peaks": [],
    "dauer": 0,
    "clips": [],
    "clip_laenge": CLIP_LAENGE,
    "vorlauf": VORLAUF,
    "lauf": 0,
    "titel": "",
    "uploads": {},
    "schnitte": {},
}
SPERRE   = threading.Lock()
LAUF     = {"nummer": 0}          # zaehlt hoch bei jedem Start und jedem Stopp
PROZESSE = {}                     # laufende Programme je Arbeitsfaden
ARBEITER = {"thread": None}
LOKAL    = threading.local()
QUELLE   = {"url": "", "video": None, "hochformat": True}


class Abbruch(Exception):
    pass


def bin_ich_aktuell():
    return getattr(LOKAL, "lauf", None) == LAUF["nummer"]


def notiz(text):
    if getattr(LOKAL, "lauf", None) is not None and not bin_ich_aktuell():
        return
    with SPERRE:
        STATE["log"].append(text)
        del STATE["log"][:-200]
    print(text)


def phase(name):
    if getattr(LOKAL, "lauf", None) is not None and not bin_ich_aktuell():
        return
    with SPERRE:
        STATE["phase"] = name


def pruefe_stopp():
    if getattr(LOKAL, "lauf", None) is not None and not bin_ich_aktuell():
        raise Abbruch()


def merke_prozess(proc):
    PROZESSE[threading.get_ident()] = proc


def vergiss_prozess(proc):
    if PROZESSE.get(threading.get_ident()) is proc:
        PROZESSE.pop(threading.get_ident(), None)


def beende_laufenden_prozess():
    for proc in list(PROZESSE.values()):
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass


def zeit(sekunden):
    s = int(sekunden)
    return f"{s // 60:02d}:{s % 60:02d}"


# ----------------------------------------------------------------------
# Programme aufrufen
# ----------------------------------------------------------------------

def lauf_prozess(befehl):
    """Startet ein Programm, schreibt die Ausgabe in den Log, reagiert auf Stopp."""
    pruefe_stopp()
    proc = subprocess.Popen(
        befehl,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    merke_prozess(proc)

    puffer, letzte = "", ""
    try:
        while True:
            if not bin_ich_aktuell():
                proc.terminate()
                raise Abbruch()
            zeichen = proc.stdout.read(1)
            if not zeichen:
                break
            if zeichen in ("\r", "\n"):
                zeile, puffer = puffer.strip(), ""
                if not zeile:
                    continue
                if "[download]" in zeile and "%" in zeile:
                    treffer = re.search(r"(\d+\.?\d*)%", zeile)
                    if treffer:
                        prozent = treffer.group(1).split(".")[0]
                        if prozent != letzte:
                            letzte = prozent
                            notiz(f"Download {prozent} %")
                    continue
                notiz(zeile)
            else:
                puffer += zeichen
        proc.wait()
        return proc.returncode
    finally:
        vergiss_prozess(proc)


def yt_dlp_befehl(url, ziel, extra, format_wahl):
    return [
        sys.executable, "-m", "yt_dlp",
        "-f", format_wahl,
        "--newline",
        "--no-warnings",
        "--ignore-config",
        "--concurrent-fragments", str(GLEICHZEITIG),
        "-N", str(GLEICHZEITIG),
        "-o", str(ziel),
    ] + extra + [url]


def versuche_download(url, ziel, format_wahl, zusatz=None, was="Download"):
    for name, extra in STRATEGIEN:
        pruefe_stopp()
        notiz(f"{was}: {name}")
        befehl = yt_dlp_befehl(url, ziel, extra + (zusatz or []), format_wahl)
        if lauf_prozess(befehl) == 0 and ziel.exists() and ziel.stat().st_size > 0:
            return True
        if ziel.exists():
            ziel.unlink()
    return False


def lade_tonspur(url, ziel):
    phase("laden")
    notiz("Lade nur die Tonspur – das spart Zeit und Datenvolumen.")
    zusatz = ["--download-sections", f"*0-{MAX_LAENGE}"] if MAX_LAENGE else None
    if MAX_LAENGE:
        notiz(f"Nur die ersten {MAX_LAENGE // 60} Minuten werden geladen.")
    if not versuche_download(url, ziel, "bestaudio/best", zusatz, was="Tonspur"):
        raise RuntimeError(
            "Kein Zugriff auf das Video. Möglich: Link falsch, Video privat "
            "oder die Seite blockt gerade. Später nochmal versuchen."
        )
    notiz(f"Tonspur geladen ({ziel.stat().st_size / 1048576:.0f} MB).")


def hole_infos(url):
    """Holt Titel und echte Länge des Videos."""
    for name, extra in STRATEGIEN[:3]:
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "yt_dlp", "--skip-download", "--no-warnings",
                 "--ignore-config", "--print", "%(title)s|%(duration)s"] + extra + [url],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=90)
            zeilen = [z for z in (proc.stdout or "").strip().splitlines() if "|" in z]
            if proc.returncode == 0 and zeilen:
                titel, _, dauer = zeilen[-1].rpartition("|")
                try:
                    sekunden = int(float(dauer))
                except ValueError:
                    sekunden = 0
                return titel.strip()[:80], sekunden
        except Exception:
            pass
    return "", 0


def dauer_von(datei):
    """Liest die Spieldauer einer Videodatei in Sekunden."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(datei)],
            capture_output=True, text=True, timeout=60)
        return float((proc.stdout or "0").strip() or 0)
    except Exception:
        return 0.0


def brauchbar(datei, soll_dauer, mindest_anteil=0.5):
    """Prüft, ob eine Datei wirklich Inhalt hat."""
    if not datei.exists() or datei.stat().st_size < 50_000:
        return False
    return dauer_von(datei) >= soll_dauer * mindest_anteil


def lade_abschnitt(url, start, dauer, ziel):
    """Lädt ein Stück des Videos. Prüft danach, ob wirklich etwas drin ist."""
    varianten = [
        ("genau", ["--force-keyframes-at-cuts"]),
        ("grob",  []),
    ]
    for wie, extra in varianten:
        pruefe_stopp()
        zusatz = [
            "--download-sections", f"*{int(start)}-{int(start + dauer)}",
            "--merge-output-format", "mp4",
        ] + extra
        if versuche_download(
                url, ziel,
                f"bestvideo[height<={HOEHE}]+bestaudio/best[height<={HOEHE}]/best",
                zusatz, was=f"  Ausschnitt ({wie})"):
            if brauchbar(ziel, dauer):
                return True
            notiz("  Ausschnitt kam leer zurück – nächster Versuch.")
            if ziel.exists():
                ziel.unlink()
    return False


def lade_video(url, ziel):
    phase("laden")
    notiz("Lade das komplette Video – der Ausschnitt-Download geht hier nicht.")
    if not versuche_download(
            url, ziel,
            f"bestvideo[height<={HOEHE}][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            ["--merge-output-format", "mp4"]):
        raise RuntimeError("Kein Zugriff auf das Video.")
    notiz("Download fertig.")


# ----------------------------------------------------------------------
# Tonanalyse
# ----------------------------------------------------------------------

def lauf_ffmpeg(befehl):
    """ffmpeg starten, dabei auf Abbruch reagieren."""
    pruefe_stopp()
    proc = subprocess.Popen(befehl, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    merke_prozess(proc)
    try:
        while proc.poll() is None:
            if not bin_ich_aktuell():
                proc.terminate()
                raise Abbruch()
            try:
                proc.wait(timeout=0.3)
            except subprocess.TimeoutExpired:
                pass
        if proc.returncode != 0:
            raise RuntimeError("ffmpeg konnte die Datei nicht verarbeiten.")
    finally:
        vergiss_prozess(proc)


def hat_tonspur(datei):
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(datei)],
            capture_output=True, text=True, timeout=60)
        return "audio" in (proc.stdout or "")
    except Exception:
        return True                      # im Zweifel weitermachen


def hole_audio(quelle, ziel_wav):
    if not hat_tonspur(quelle):
        raise RuntimeError("Dieses Video hat keine Tonspur – ohne Ton lassen sich "
                           "die lauten Stellen nicht finden.")
    lauf_ffmpeg([
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(quelle), "-vn", "-ac", "1", "-ar", "8000", str(ziel_wav),
    ])


def lautstaerke_pro_sekunde(wav_datei):
    with wave.open(str(wav_datei), "rb") as w:
        rate = w.getframerate()
        daten = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)

    daten = daten.astype(np.float32) / 32768.0
    sekunden = len(daten) // rate
    if sekunden < 1:
        raise RuntimeError("Keine Tonspur gefunden.")
    daten = daten[: sekunden * rate].reshape(sekunden, rate)

    werte = np.sqrt(np.mean(daten ** 2, axis=1))
    return np.convolve(werte, np.ones(5) / 5, mode="same")


# Wörter, die auf einen guten Moment hindeuten (Reaktionen, Pointen, Haken)
REIZWOERTER = {
    "stark": ("krass", "wtf", "unglaublich", "unfassbar", "was zur", "oh mein gott",
              "omg", "digga", "alter", "brudi", "nein", "niemals", "wallah",
              "geisteskrank", "insane", "no way", "bruder", "hilfe", "panik",
              "gewonnen", "verloren", "clutch", "rekord", "endlich", "geschafft",
              "gescheitert", "peinlich", "schock", "boah", "wow", "lol", "haha"),
    "mittel": ("richtig", "wirklich", "niemals", "immer", "beste", "schlimmste",
               "größte", "erste", "letzte", "problem", "fehler", "geheimnis",
               "achtung", "warte", "guck", "schau", "hör zu", "pass auf"),
}


CHAT_HYPE = ("kekw", "lul", "lulw", "omegalul", "pog", "pogchamp", "poggers", "pogu",
             "wtf", "omg", "no way", "noway", "lmao", "lol", "xd", "xdd", "haha",
             "icant", "pepelaugh", "monkas", "holy", "insane", "krass", "digga",
             "alter", "bruder", "😂", "🤣", "💀", "😱", "🔥", "!!!", "???")
CHAT_CLIP = ("clip", "clipped", "klip", "geclippt")


def chat_gewicht(text, nutzer, sekunde, zuletzt):
    """Wie viel eine Chatnachricht zählt.

    Hype-Wörter und "clip it" zählen mehr, Spam desselben Nutzers weniger.
    """
    if nutzer:
        vorher = zuletzt.get(nutzer)
        zuletzt[nutzer] = sekunde
        if vorher is not None and sekunde - vorher < 4:
            return 0.0
    gewicht = 1.0
    klein = (text or "").lower()
    if klein:
        woerter = set(re.findall(r"[\wäöüß]+", klein))
        if any(c in klein for c in CHAT_CLIP):
            gewicht += 2.5
        if any((h in woerter) if h.isalnum() else (h in klein) for h in CHAT_HYPE):
            gewicht += 1.5
        buchstaben = [b for b in text if b.isalpha()]
        if len(buchstaben) >= 4 and sum(b.isupper() for b in buchstaben) / len(buchstaben) > 0.7:
            gewicht += 0.5
    return gewicht


def twitch_video_id(url):
    """Zieht die VOD-Nummer aus einer Twitch-Adresse."""
    treffer = re.search(r"twitch\.tv/videos/(\d+)", url or "")
    return treffer.group(1) if treffer else None


def hole_twitch_chat(video_id, hoechstens=500000):
    """Lädt den Chatverlauf eines Twitch-VODs.

    Liefert die Sekunden, zu denen Nachrichten geschrieben wurden.
    """
    import urllib.request

    kopf = {
        "Client-ID": "kimne78kx3ncx6brgo4mv6wki5h1ko",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
    }
    zeiten, cursor, zuletzt = [], None, {}

    while len(zeiten) < hoechstens:
        pruefe_stopp()
        variablen = {"videoID": str(video_id)}
        if cursor:
            variablen["cursor"] = cursor
        else:
            variablen["contentOffsetSeconds"] = 0

        anfrage = [{
            "operationName": "VideoCommentsByOffsetOrCursor",
            "variables": variablen,
            "extensions": {"persistedQuery": {
                "version": 1,
                "sha256Hash": "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a",
            }},
        }]
        try:
            req = urllib.request.Request(
                "https://gql.twitch.tv/gql",
                data=json.dumps(anfrage).encode("utf-8"), headers=kopf)
            with urllib.request.urlopen(req, timeout=25) as antwort:
                daten = json.loads(antwort.read().decode("utf-8"))
        except Exception as fehler:
            if not zeiten:
                notiz(f"Chat nicht abrufbar: {str(fehler)[:60]}")
            break

        try:
            erster = daten[0] if isinstance(daten, list) and daten else daten
            video = ((erster or {}).get("data") or {}).get("video") or {}
            seite = video.get("comments") or {}
            kanten = seite.get("edges") or []

            if not kanten:
                if not zeiten:
                    notiz("Twitch hat keinen Chatverlauf geliefert.")
                break

            for kante in kanten:
                knoten = (kante or {}).get("node") or {}
                versatz = knoten.get("contentOffsetSeconds")
                if versatz is not None:
                    nachricht = knoten.get("message") or {}
                    text = "".join((f or {}).get("text") or ""
                                   for f in (nachricht.get("fragments") or []))
                    nutzer = ((knoten.get("commenter") or {}).get("id")) or ""
                    if MAX_LAENGE and int(versatz) > MAX_LAENGE:
                        return zeiten
                    w = chat_gewicht(text, nutzer, int(versatz), zuletzt)
                    if w > 0:
                        zeiten.append((int(versatz), w))
            if zeiten and len(zeiten) // 20000 != (len(zeiten) - len(kanten)) // 20000:
                notiz(f"Chat: {len(zeiten)} Nachrichten geladen (bis {zeit(zeiten[-1][0])}) ...")

            if not (seite.get("pageInfo") or {}).get("hasNextPage"):
                break
            cursor = (kanten[-1] or {}).get("cursor")
            if not cursor:
                break
        except Exception as fehler:
            if not zeiten:
                notiz(f"Chatverlauf nicht lesbar: {str(fehler)[:60]}")
            break

    return zeiten


def hole_youtube_chat(url):
    """Lädt die Chat-Wiederholung eines YouTube-Livestreams.

    Liefert die Sekunden, zu denen Nachrichten geschrieben wurden.
    """
    ziel = ARBEITSORDNER / "ytchat"
    for alt in ARBEITSORDNER.glob("ytchat*"):
        try:
            alt.unlink()
        except Exception:
            pass
    for name, extra in STRATEGIEN[:3]:
        pruefe_stopp()
        try:
            subprocess.run(
                [sys.executable, "-m", "yt_dlp", "--skip-download", "--no-warnings",
                 "--ignore-config", "--write-subs", "--sub-langs", "live_chat",
                 "-o", str(ziel)] + extra + [url],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=1800)
        except Exception:
            continue
        dateien = list(ARBEITSORDNER.glob("ytchat*.json"))
        if dateien:
            break
    else:
        return []

    zeiten, zuletzt = [], {}
    with open(dateien[0], encoding="utf-8", errors="replace") as f:
        for zeile in f:
            try:
                eintrag = json.loads(zeile)
            except Exception:
                continue
            aktion = eintrag.get("replayChatItemAction") or {}
            ms = aktion.get("videoOffsetTimeMsec")
            if ms is None:
                continue
            try:
                sekunde = int(int(ms) / 1000)
            except ValueError:
                continue
            text, nutzer = "", ""
            try:
                for a in aktion.get("actions") or []:
                    r = ((a.get("addChatItemAction") or {}).get("item") or {}).get(
                        "liveChatTextMessageRenderer") or {}
                    if r:
                        nutzer = r.get("authorExternalChannelId") or ""
                        for run in (r.get("message") or {}).get("runs") or []:
                            if "text" in run:
                                text += run["text"]
                            elif "emoji" in run:
                                text += " " + " ".join(
                                    (run["emoji"].get("shortcuts") or [""])[:1]) + " "
            except Exception:
                pass
            w = chat_gewicht(text, nutzer, sekunde, zuletzt)
            if w > 0:
                zeiten.append((sekunde, w))
    return zeiten


def chat_hoehepunkte(chat, anzahl, laenge, abstand):
    """Findet die Stellen, an denen der Chat am stärksten anspringt."""
    kopie = chat.copy()
    vorn = min(IGNORIERE_START, max(0, int(laenge * 0.08)))
    hinten = min(IGNORIERE_ENDE, max(0, int(laenge * 0.08)))
    if vorn:
        kopie[:vorn] = -1
    if hinten:
        kopie[-hinten:] = -1
    treffer = []
    while len(treffer) < anzahl:
        beste = int(np.argmax(kopie))
        if kopie[beste] <= 0.15:
            break
        # Der Chat reagiert verzögert - der Moment selbst liegt etwas davor
        moment = max(0, min(laenge - CLIP_LAENGE - 1, beste - CHAT_VERZOEGERUNG))
        treffer.append(moment)
        kopie[max(0, beste - abstand):min(len(kopie), beste + abstand)] = -1
    return treffer


def chat_kurve(zeiten, laenge):
    """Macht aus den Chatzeiten eine Kurve: Wo wird plötzlich viel geschrieben?"""
    if not zeiten or laenge < 10:
        return None

    zaehler = np.zeros(laenge, dtype=np.float32)
    for eintrag in zeiten:
        t, w = eintrag if isinstance(eintrag, tuple) else (eintrag, 1.0)
        t = int(t)
        if 0 <= t < laenge:
            zaehler[t] += w

    # über 5 Sekunden glätten
    zaehler = np.convolve(zaehler, np.ones(5) / 5, mode="same")

    # Grundrauschen der letzten 90 Sekunden abziehen - Zuschauerzahl schwankt
    fenster = min(90, max(10, laenge // 6))
    kern = np.ones(fenster) / fenster
    verlaengert = np.concatenate([np.full(fenster, zaehler[:fenster].mean()), zaehler])
    grund = np.convolve(verlaengert, kern, mode="full")[fenster - 1: fenster - 1 + laenge]

    anstieg = np.clip(zaehler - grund, 0, None)
    if anstieg.max() > 0:
        anstieg = anstieg / anstieg.max()
    return anstieg


def sprach_note(woerter):
    """Bewertet, wie interessant das Gesagte klingt - ohne Sprachmodell.

    Gewertet werden Reizwörter, Fragen und wie dicht gesprochen wird.
    """
    if not woerter:
        return 0.0

    text = " ".join(w[2] for w in woerter).lower()
    dauer = max(1.0, woerter[-1][1] - woerter[0][0])

    punkte = 0.0
    for begriff in REIZWOERTER["stark"]:
        if begriff in text:
            punkte += 1.0
    for begriff in REIZWOERTER["mittel"]:
        if begriff in text:
            punkte += 0.4

    # Fragen ziehen Aufmerksamkeit
    punkte += text.count("?") * 0.6

    # Sprechdichte: viele Wörter je Sekunde heißt meist Aufregung
    dichte = len(woerter) / dauer
    punkte += min(2.0, max(0.0, (dichte - 1.8) * 1.6))

    # Gar nichts gesagt ist ein schlechtes Zeichen
    if len(woerter) < 4:
        punkte *= 0.3

    return min(1.0, punkte / 5.0)


def satzanfang(woerter, ziel_zeit, rueckwaerts=14.0):
    """Sucht den Anfang des Satzes, der zu dieser Stelle gehört."""
    if not woerter:
        return None
    kandidat = None
    for i, (start, ende, wort) in enumerate(woerter):
        if start > ziel_zeit:
            break
        if start < ziel_zeit - rueckwaerts:
            continue
        # Nach einem Satzzeichen oder einer Sprechpause beginnt etwas Neues
        vorher = woerter[i - 1] if i > 0 else None
        pause = (start - vorher[1]) if vorher else 99
        if vorher is None or pause > 0.6 or vorher[2].rstrip()[-1:] in ".!?":
            kandidat = start
    return kandidat


def bewerte(werte):
    """Bewertet jede Sekunde: Wie laut ist es, und wie stark steigt es an?

    Reine Lautstärke findet auch Dauerlärm wie Hintergrundmusik. Interessant
    sind die Stellen, an denen es plötzlich lauter wird als kurz zuvor.
    """
    laenge = len(werte)
    hoechster = float(werte.max()) or 1.0
    pegel = werte / hoechster

    # Grundpegel der letzten 30 Sekunden (gleitender Mittelwert)
    fenster = min(30, max(3, laenge // 4))
    kern = np.ones(fenster) / fenster
    vorlauf = np.concatenate([np.full(fenster, pegel[0]), pegel])
    grund = np.convolve(vorlauf, kern, mode="full")[fenster - 1: fenster - 1 + laenge]

    anstieg = np.clip(pegel - grund, 0, None)
    if anstieg.max() > 0:
        anstieg = anstieg / anstieg.max()

    return ANTEIL_PEGEL * pegel + (1 - ANTEIL_PEGEL) * anstieg


def finde_hoehepunkte(werte, anzahl):
    laenge = len(werte)
    kopie = bewerte(werte)

    # Bei kurzen Videos darf die Intro-/Outro-Sperre nicht alles schlucken
    vorn = min(IGNORIERE_START, max(0, int(laenge * 0.08)))
    hinten = min(IGNORIERE_ENDE, max(0, int(laenge * 0.08)))
    if vorn:
        kopie[:vorn] = -1
    if hinten:
        kopie[-hinten:] = -1
    kopie[max(0, laenge - CLIP_LAENGE):] = -1

    # Auch der Mindestabstand muss zur Videolänge passen
    abstand = MIN_ABSTAND
    if anzahl > 1:
        platz = max(1, laenge - vorn - hinten - CLIP_LAENGE)
        abstand = int(max(CLIP_LAENGE * 0.6, min(MIN_ABSTAND, platz / anzahl)))

    treffer = []
    while len(treffer) < anzahl:
        beste = int(np.argmax(kopie))
        if kopie[beste] <= 0:
            break
        treffer.append(beste)
        kopie[max(0, beste - abstand):min(laenge, beste + abstand)] = -1
    return sorted(treffer)


def hoere_hinein(ton, kandidaten, fenster=45):
    """Tippt nur die Kandidaten-Abschnitte ab statt des ganzen Videos.

    Alle Fenster werden zu einer Datei zusammengesetzt, damit Whisper nur
    einmal starten muss. Liefert je Kandidat die erkannten Wörter.
    """
    if not kandidaten:
        return {}

    ARBEITSORDNER.mkdir(exist_ok=True)
    teile, versatz, lage = [], 0.0, {}
    liste = ARBEITSORDNER / "fenster.txt"
    zeilen = []

    for i, moment in enumerate(kandidaten):
        von = max(0.0, moment - fenster / 2)
        stueck = ARBEITSORDNER / f"fenster_{i:02d}.wav"
        try:
            lauf_ffmpeg(["ffmpeg", "-y", "-loglevel", "error",
                         "-ss", f"{von:.2f}", "-i", str(ton), "-t", f"{fenster}",
                         "-ac", "1", "-ar", "16000", str(stueck)])
        except Abbruch:
            raise
        except Exception:
            continue
        echte = dauer_von(stueck)
        if echte < 2:
            continue
        lage[moment] = (versatz, versatz + echte, von)
        versatz += echte
        teile.append(stueck)
        zeilen.append(f"file '{stueck.name}'")

    if not teile:
        return {}

    liste.write_text("\n".join(zeilen), encoding="utf-8")
    zusammen = ARBEITSORDNER / "kandidaten.wav"
    try:
        lauf_ffmpeg(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat",
                     "-safe", "0", "-i", str(liste), "-ac", "1", "-ar", "16000",
                     str(zusammen)])
    except Abbruch:
        raise
    except Exception:
        return {}

    ergebnis = whisper_woerter(zusammen)
    for t in teile + [liste, zusammen]:
        if t.exists():
            try:
                t.unlink()
            except OSError:
                pass
    if not ergebnis:
        return {}

    # Wörter den Kandidaten zuordnen und auf die Videozeit zurückrechnen
    zuordnung = {}
    for moment, (ab, bis, quelle_von) in lage.items():
        drin = [(s - ab + quelle_von, e - ab + quelle_von, wort)
                for s, e, wort in ergebnis if ab <= s < bis]
        zuordnung[moment] = drin
    return zuordnung


def whisper_woerter(wav):
    """Lässt Whisper eine Tondatei abtippen und liefert die Wörter mit Zeiten."""
    versuche = [[sys.executable, "-m", "whisper"]]
    exe = Path(sys.executable).parent / "Scripts" / "whisper.exe"
    if exe.exists():
        versuche.append([str(exe)])

    for anfang in versuche:
        befehl = anfang + [
            str(wav), "--model", UNTERTITEL_MODELL, "--output_format", "json",
            "--output_dir", str(ARBEITSORDNER), "--fp16", "False",
            "--word_timestamps", "True", "--task", "transcribe",
        ]
        if UNTERTITEL_SPRACHE:
            befehl += ["--language", UNTERTITEL_SPRACHE]
        try:
            if lauf_prozess(befehl) != 0:
                continue
            datei = ARBEITSORDNER / f"{wav.stem}.json"
            if not datei.exists():
                continue
            woerter = lies_wortzeiten(datei)
            datei.unlink()
            if woerter:
                return woerter
        except Abbruch:
            raise
        except Exception:
            pass
    return []


def ki_noten(saetze, chat=None):
    """Lässt Claude bewerten, welche Ausschnitte als Short funktionieren.

    Ein einziger Aufruf für alle Kandidaten. Ohne API-Schlüssel oder bei
    Fehlern kommt ein leeres Ergebnis zurück und es geht ohne weiter.
    """
    if not KI_BEWERTEN or not os.environ.get("ANTHROPIC_API_KEY"):
        return {}, {}
    try:
        import anthropic
    except Exception:
        return {}, {}

    liste, nummern, anfaenge = [], {}, {}
    for i, (p, woerter) in enumerate(sorted(saetze.items())):
        woerter = woerter or []
        if len(" ".join(w[2] for w in woerter).strip()) < 15:
            continue
        ab = woerter[0][0]
        # Text mit Zeitmarken alle paar Sekunden, damit Claude den Einstieg wählen kann
        stuecke, marke = [], -99.0
        for s_, _e, wort in woerter:
            if s_ - marke >= 4:
                stuecke.append(f"[{s_ - ab:.0f}s]")
                marke = s_
            stuecke.append(wort.strip())
        text = " ".join(stuecke)
        chat_info = ""
        if chat is not None and int(p) < len(chat):
            wert = float(chat[max(0, int(p) - 2):min(len(chat), int(p) + 12)].max())
            stufe = "sehr hoch" if wert > 0.6 else "hoch" if wert > 0.3 else "mittel" if wert > 0.1 else "niedrig"
            chat_info = f" (Chat-Reaktion: {stufe})"
        nummern[str(i)] = p
        anfaenge[str(i)] = ab
        liste.append(f"[{i}]{chat_info} {text[:1500]}")
    if len(liste) < 2:
        return {}, {}

    auftrag = (
        "Du suchst die besten Momente aus einem Stream für YouTube Shorts und TikTok. "
        "Unten stehen abgetippte Ausschnitte (je ca. 45 Sekunden). Bewerte jeden mit 0 bis 10: "
        "Wie gut funktioniert er als eigenständiger Clip? Hoch bewerten: klare Pointe, lustiger "
        "oder überraschender Moment, starke Emotion, Streit, Fail, Rekord, verständlich ohne "
        "Vorwissen. Niedrig: Smalltalk, Werbung, Begrüßung, Spielmenü, nichts passiert. "
        "Eine starke Chat-Reaktion ist ein gutes Zeichen.\n"
        f"Wähle für jeden Ausschnitt auch den besten Einstieg (Sekunde laut Zeitmarken): "
        f"Der Clip ist {CLIP_LAENGE} Sekunden lang und soll sofort packen, also mit einem Satz "
        "beginnen, der neugierig macht, und die Pointe mit drin haben.\n\n"
        + "\n\n".join(liste)
        + '\n\nAntworte NUR mit JSON in der Form {"0": {"note": 7, "start": 5}, "1": {"note": 3, "start": 0}, ...}.'
    )
    try:
        antwort = anthropic.Anthropic().messages.create(
            model=KI_MODELL, max_tokens=600,
            messages=[{"role": "user", "content": auftrag}])
        text = "".join(getattr(b, "text", "") for b in antwort.content)
        roh = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
    except Exception as fehler:
        notiz(f"Claude-Bewertung übersprungen: {str(fehler)[:60]}")
        return {}, {}

    noten, starts = {}, {}
    for k, v in roh.items():
        k = str(k)
        if k not in nummern:
            continue
        p = nummern[k]
        try:
            if isinstance(v, dict):
                noten[p] = max(0.0, min(1.0, float(v.get("note", 0)) / 10))
                if v.get("start") is not None:
                    start = anfaenge[k] + float(v["start"])
                    # Der laute Moment muss im Clip bleiben
                    starts[p] = max(p - CLIP_LAENGE + 5, min(p + 5, start))
            else:
                noten[p] = max(0.0, min(1.0, float(v) / 10))
        except (TypeError, ValueError):
            pass
    return noten, starts


def waehle_stellen(werte, anzahl, ton, chat=None):
    """Mehrstufig: grob nach Ton, dann nach dem Gesagten und dem Chat."""
    grob = finde_hoehepunkte(werte, anzahl * KANDIDATEN_FAKTOR)

    # Stellen, an denen der Chat ausrastet, auch prüfen, wenn es dort nicht laut ist
    if chat is not None and CHAT_KANDIDATEN:
        neu = 0
        for p in chat_hoehepunkte(chat, anzahl * 2, len(werte), MIN_ABSTAND // 2):
            if p < len(werte) and all(abs(p - g) >= 20 for g in grob):
                grob.append(p)
                neu += 1
        if neu:
            notiz(f"{neu} zusätzliche Stellen wegen viel Chat-Aktivität.")
        grob = sorted(grob)

    if not grob:
        return [], {}

    noten = bewerte(werte)
    akustik = {p: float(noten[p]) for p in grob}
    hoechste = max(akustik.values()) or 1.0

    saetze = {}
    if SPRACHE_AUSWERTEN and len(grob) > anzahl:
        notiz(f"Höre in {len(grob)} Kandidaten hinein ...")
        saetze = hoere_hinein(ton, grob)
        if not saetze:
            notiz("Abtippen nicht möglich – wähle nur nach dem Ton aus.")

    ki, ki_start = {}, {}
    if saetze:
        ki, ki_start = ki_noten(saetze, chat)
        if ki:
            notiz(f"Claude hat {len(ki)} Kandidaten bewertet.")

    gesamt = {}
    for p in grob:
        ak = akustik[p] / hoechste
        teile = [(1.0, ak)]
        if saetze.get(p):
            teile.append((ANTEIL_SPRACHE / (1 - ANTEIL_SPRACHE), sprach_note(saetze[p])))
        if chat is not None and p < len(chat):
            # den besten Wert im Umfeld nehmen - der Chat reagiert verzögert
            von, bis = max(0, p - 2), min(len(chat), p + 12)
            teile.append((ANTEIL_CHAT / (1 - ANTEIL_CHAT), float(chat[von:bis].max())))
        if p in ki:
            teile.append((ANTEIL_KI / (1 - ANTEIL_KI), ki[p]))
        summe = sum(g for g, _ in teile)
        gesamt[p] = sum(g * wert for g, wert in teile) / summe

    # Beste auswählen, dabei Mindestabstand einhalten
    gewaehlt = []
    for p in sorted(gesamt, key=lambda x: -gesamt[x]):
        if all(abs(p - g) >= MIN_ABSTAND for g in gewaehlt):
            gewaehlt.append(p)
        if len(gewaehlt) >= anzahl:
            break

    return sorted(gewaehlt), {"noten": gesamt, "saetze": saetze, "ki_start": ki_start}


def schneide_clip(quelle, start, ziel, hochformat, dauer=None, folgen=False):
    laenge = dauer if dauer is not None else CLIP_LAENGE
    befehl = ["ffmpeg", "-y", "-loglevel", "error",
              "-ss", str(start), "-i", str(quelle), "-t", str(laenge)]
    if hochformat:
        hoehe = CLIP_BREITE * 16 // 9
        vorher = (f"scale={CLIP_BREITE}:{hoehe}:force_original_aspect_ratio=increase"
                  f":flags=bicubic")

        ausdruck = None
        if folgen and AUTO_AUSSCHNITT:
            qb, qh = masse_von(quelle)
            # Breite des Bildes, nachdem es auf Rahmenhöhe gebracht wurde
            skaliert = qb * (hoehe / qh) if qh else CLIP_BREITE
            spielraum = int(skaliert - CLIP_BREITE)
            if spielraum > 8:
                punkte = finde_motiv(quelle)
                # Nur den Abschnitt behalten und auf die Clipzeit umrechnen,
                # denn im fertigen Clip beginnt die Zeit wieder bei null.
                punkte = [(t - start, x, y, art) for t, x, y, art in punkte
                          if start - 1 <= t <= start + laenge + 1]
                if punkte:
                    anteil = sum(1 for p in punkte if p[3] == "gesicht") / len(punkte)
                    pfad = glaette_pfad(punkte, 1)
                    # Ausschnittmitte auf das Motiv legen und auf 0..1 normieren
                    versetzt = []
                    for t, x in pfad:
                        links = x * skaliert - CLIP_BREITE / 2
                        versetzt.append((t, max(0.0, min(1.0, links / spielraum))))
                    ausdruck = crop_ausdruck(versetzt, spielraum)
                    if ausdruck:
                        notiz(f"  Bildausschnitt folgt dem Motiv "
                              f"({'Gesicht' if anteil > 0.4 else 'Bewegung'}).")

        if ausdruck:
            befehl += ["-vf", f"{vorher},crop={CLIP_BREITE}:{hoehe}:'{ausdruck}':0"]
        else:
            befehl += ["-vf", f"{vorher},crop={CLIP_BREITE}:{hoehe}"]
    befehl += ["-threads", "0"] + video_args() + [
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart",
        str(ziel),
    ]
    lauf_ffmpeg(befehl)


# ----------------------------------------------------------------------
# Ablauf
# ----------------------------------------------------------------------

def arbeite(url, anzahl, hochformat, neu_laden, meine_nummer):
    LOKAL.lauf = meine_nummer
    try:
        ARBEITSORDNER.mkdir(exist_ok=True)
        AUSGABEORDNER.mkdir(exist_ok=True)
        for alt in AUSGABEORDNER.glob("*.mp4"):
            alt.unlink()
        for alt_master in ARBEITSORDNER.glob("master_*.mp4"):
            alt_master.unlink()

        video   = ARBEITSORDNER / "video.mp4"
        tonspur = ARBEITSORDNER / "tonspur.m4a"
        wav     = ARBEITSORDNER / "ton.wav"

        eigene = Path(url.strip().strip('"'))
        ist_datei = (eigene.is_file() and eigene.suffix.lower() in
                     (".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".ts"))

        QUELLE.update(url=url, video=None, hochformat=hochformat)

        if ist_datei:
            notiz(f"Nutze Datei vom Rechner: {eigene.name}")
            video = eigene
            quelle_ton = video
            QUELLE["video"] = video
            echte_dauer = int(dauer_von(video))
            with SPERRE:
                STATE["titel"] = eigene.stem[:80]
        else:
            merk = MERKZETTEL.read_text(encoding="utf-8").strip() if MERKZETTEL.exists() else ""

            # Reste vom vorherigen Video wegräumen, sonst werden Clips
            # aus dem falschen Film geschnitten.
            if video.exists() and (merk != url or neu_laden):
                notiz("Alte Videodatei vom letzten Durchlauf wird verworfen.")
                video.unlink()

            if tonspur.exists() and merk == url and not neu_laden:
                notiz("Tonspur liegt schon vor – Download übersprungen.")
            else:
                if tonspur.exists():
                    tonspur.unlink()
                lade_tonspur(url, tonspur)
                MERKZETTEL.write_text(url, encoding="utf-8")
            quelle_ton = tonspur
            titel, echte_dauer = hole_infos(url)
            if MAX_LAENGE and echte_dauer > MAX_LAENGE:
                echte_dauer = MAX_LAENGE
            if titel:
                notiz(f"Titel: {titel}")
            with SPERRE:
                STATE["titel"] = titel

        phase("analysieren")
        notiz("Suche die lautesten Momente ...")
        hole_audio(quelle_ton, wav)
        werte = lautstaerke_pro_sekunde(wav)
        gemessen = len(werte)
        notiz(f"Länge: {zeit(gemessen)}")

        # Die Tonspur kann verrutschte Zeitstempel haben. Gegen die echte
        # Länge abgleichen, sonst liegen die Stellen hinter dem Videoende.
        faktor = 1.0
        if echte_dauer > 60 and abs(gemessen - echte_dauer) / echte_dauer > 0.05:
            faktor = echte_dauer / gemessen
            notiz(f"Tonspur weicht ab (echte Länge {zeit(echte_dauer)}) – "
                  f"Zeiten werden um Faktor {faktor:.2f} korrigiert.")

        chat_werte = None
        if CHAT_AUSWERTEN and not ist_datei:
            vid = twitch_video_id(url)
            ist_youtube = "youtube.com" in url or "youtu.be" in url
            if vid or ist_youtube:
                notiz("Hole den Chatverlauf ...")
                try:
                    zeiten = hole_twitch_chat(vid) if vid else hole_youtube_chat(url)
                    if zeiten and faktor != 1.0:
                        zeiten = [(t / faktor, w) for t, w in zeiten]
                    if zeiten:
                        chat_werte = chat_kurve(zeiten, gemessen)
                        notiz(f"{len(zeiten)} Chatnachrichten ausgewertet.")
                    else:
                        notiz("Kein Chat verfügbar – weiter ohne dieses Signal.")
                except Abbruch:
                    raise
                except Exception as fehler:
                    notiz(f"Chat übersprungen: {str(fehler)[:60]}")
                    chat_werte = None

        hoehepunkte, zusatz = waehle_stellen(werte, anzahl, wav, chat_werte)
        if not hoehepunkte:
            raise RuntimeError("Keine passenden Stellen gefunden. Video zu kurz?")

        alle_noten = zusatz.get("noten", {})
        saetze = zusatz.get("saetze", {})
        bester = max([alle_noten.get(p, 0) for p in hoehepunkte] + [0.0001])
        punkte_je_stelle = {p: int(round(min(100, alle_noten.get(p, 0) / bester * 100)))
                            for p in hoehepunkte}

        # Clipbeginn auf den Satzanfang legen, damit nicht mitten im Wort begonnen wird
        satzstarts = {}
        ki_start = zusatz.get("ki_start", {})
        for p in hoehepunkte:
            w_liste = saetze.get(p) or []
            if p in ki_start:
                # Claudes Einstieg, aber sauber auf einen Satz-/Wortanfang gelegt
                genau = satzanfang(w_liste, ki_start[p] + 0.5, rueckwaerts=3.0)
                satzstarts[p] = genau if genau is not None else ki_start[p]
                continue
            # nur so weit zurück, wie es der eingestellte Vorlauf zulässt
            treffer = satzanfang(w_liste, p, rueckwaerts=min(VORLAUF + 4, 14))
            if treffer is not None:
                satzstarts[p] = treffer

        ziel_punkte = 600
        if len(werte) > ziel_punkte:
            block = len(werte) // ziel_punkte
            gekuerzt = werte[: block * ziel_punkte].reshape(ziel_punkte, block).max(axis=1)
        else:
            gekuerzt = werte
        hoechster = float(gekuerzt.max()) or 1.0

        pruefe_stopp()
        if faktor != 1.0:
            grenze = echte_dauer - CLIP_LAENGE
            hoehepunkte = [p for p in (int(round(h * faktor)) for h in hoehepunkte)
                           if 0 <= p <= grenze]
            if not hoehepunkte:
                raise RuntimeError("Nach der Korrektur blieb keine Stelle übrig.")

        anzeige_dauer = echte_dauer if faktor != 1.0 else gemessen

        with SPERRE:
            STATE["kurve"] = [round(float(v) / hoechster, 3) for v in gekuerzt]
            STATE["dauer"] = int(anzeige_dauer)
            STATE["peaks"] = [int(p) for p in hoehepunkte]

        phase("schneiden")
        notiz(f"{len(hoehepunkte)} Stellen gefunden. Hole die Ausschnitte ...")

        vollbild_sperre = threading.Lock()
        meine_nummer_lokal = meine_nummer

        def mach_clip(nummer, moment):
            LOKAL.lauf = meine_nummer_lokal
            pruefe_stopp()
            if moment in satzstarts:
                # ein bis zwei Sekunden Luft vor dem ersten Wort
                start = max(0, int(satzstarts[moment] - 1.2))
            else:
                start = max(0, moment - VORLAUF)
            name = f"clip_{nummer:02d}_{zeit(start).replace(':', 'm')}s.mp4"
            ziel = AUSGABEORDNER / name
            master = master_pfad(nummer)
            if master.exists():
                master.unlink()

            if ist_datei or video.exists():
                lege_master_an(video, start, CLIP_LAENGE, master)
            else:
                notiz(f"Clip {nummer}: Ausschnitt ab {zeit(start)}")
                # direkt als Master laden - spart einen kompletten Durchlauf
                if not lade_abschnitt(url, start, CLIP_LAENGE, master):
                    with vollbild_sperre:
                        if not video.exists():
                            notiz("  Ausschnitt geht nicht – lade das komplette Video.")
                            lade_video(url, video)
                            QUELLE["video"] = video
                            phase("schneiden")
                    lege_master_an(video, start, CLIP_LAENGE, master)

            if hochformat:
                schneide_clip(master, 0, ziel, True, dauer=CLIP_LAENGE, folgen=True)
            else:
                shutil.copy(master, ziel)

            if not brauchbar(ziel, CLIP_LAENGE, 0.3):
                if ziel.exists():
                    ziel.unlink()
                notiz(f"  Clip {nummer} blieb leer und wird übersprungen.")
                return None

            return {"nummer": nummer, "datei": name, "start": start,
                    "start_text": zeit(start), "dauer": CLIP_LAENGE,
                    "punkte": punkte_je_stelle.get(moment, 0),
                    "text": " ".join(w[2].strip() for w in (saetze.get(moment) or []))[:500],
                    # fuer saubere Schnitte: Woerter und Hoehepunkt relativ zum Clipbeginn
                    "hoehepunkt": round(float(moment - start), 2),
                    "woerter": [[round(s - start, 2), round(e - start, 2), w.strip()]
                                for s, e, w in (saetze.get(moment) or [])
                                if 0 <= s - start <= CLIP_LAENGE]}

        clips = []
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=max(1, PARALLEL_CLIPS)) as pool:
            auftraege = {pool.submit(mach_clip, n, m): n
                         for n, m in enumerate(hoehepunkte, start=1)}
            for auftrag in auftraege:
                try:
                    ergebnis = auftrag.result()
                except Abbruch:
                    raise
                except Exception as fehler:
                    notiz(f"  Ein Clip ist fehlgeschlagen: {str(fehler)[:80]}")
                    continue
                if ergebnis:
                    clips.append(ergebnis)
                    clips.sort(key=lambda c: c["nummer"])
                    pruefe_stopp()
                    with SPERRE:
                        STATE["clips"] = list(clips)

        if not clips:
            raise RuntimeError("Es konnte kein einziger Clip erzeugt werden.")
        if len(clips) < len(hoehepunkte):
            notiz(f"Hinweis: {len(hoehepunkte) - len(clips)} Stelle(n) ließen sich "
                  "nicht schneiden und wurden weggelassen.")

        notiz("Fertig.")
        phase("fertig")
        stand_sichern()

    except Abbruch:
        notiz("Abgebrochen.")
        phase("bereit")
    except Exception as fehler:
        notiz(f"Abgebrochen: {fehler}")
        phase("fehler")


def setze_masse(laenge, vorlauf):
    """Übernimmt Cliplänge und Vorlauf aus der Oberfläche."""
    global CLIP_LAENGE, VORLAUF, MIN_ABSTAND
    CLIP_LAENGE = laenge
    VORLAUF = min(vorlauf, max(0, laenge - 3))
    MIN_ABSTAND = max(laenge + 10, 45)


def starte_arbeit(url, anzahl, hochformat, neu_laden):
    """Bricht einen laufenden Vorgang ab und startet sofort neu."""
    LAUF["nummer"] += 1
    meine = LAUF["nummer"]

    beende_laufenden_prozess()

    with SPERRE:
        STATE.update(phase="laden", log=[], kurve=[], peaks=[], dauer=0, clips=[],
                     clip_laenge=CLIP_LAENGE, vorlauf=VORLAUF, lauf=meine,
                     titel="", uploads={}, schnitte={})

    alt = ARBEITER["thread"]
    if alt and alt.is_alive():
        alt.join(timeout=15)

    t = threading.Thread(target=arbeite,
                         args=(url, anzahl, hochformat, neu_laden, meine),
                         daemon=True)
    ARBEITER["thread"] = t
    t.start()


def stoppe_arbeit():
    LAUF["nummer"] += 1
    beende_laufenden_prozess()
    with SPERRE:
        STATE["phase"] = "bereit"
        STATE["log"].append("Gestoppt.")


# ----------------------------------------------------------------------
# Clip nachträglich ändern
# ----------------------------------------------------------------------

NACHSCHNITT = threading.Lock()


def schneide_neu(nummer, start, dauer):
    """Schneidet einen einzelnen Clip mit neuer Startzeit und Länge."""
    LOKAL.lauf = LAUF["nummer"]

    with SPERRE:
        eintrag = next((c for c in STATE["clips"] if c["nummer"] == nummer), None)
        hochformat = QUELLE.get("hochformat", True)
    if not eintrag:
        with SPERRE:
            STATE["schnitte"][str(nummer)] = {
                "text": "Diesen Clip gibt es nicht mehr.", "fertig": True}
        return

    alt_datei = AUSGABEORDNER / eintrag["datei"]
    neu_name = f"clip_{nummer:02d}_{zeit(start).replace(':', 'm')}s_v{int(time.time()) % 10000}.mp4"
    ziel = AUSGABEORDNER / neu_name

    def stand(text, fertig=False):
        with SPERRE:
            STATE["schnitte"][str(nummer)] = {"text": text, "fertig": fertig}

    with NACHSCHNITT:
        try:
            stand("Schneide neu ...")
            video = QUELLE.get("video")

            if video and Path(video).exists():
                schneide_clip(video, start, ziel, hochformat, dauer=dauer)
            else:
                roh = ARBEITSORDNER / f"nachschnitt_{nummer}.mp4"
                if roh.exists():
                    roh.unlink()
                stand("Hole den Ausschnitt ...")
                if not lade_abschnitt(QUELLE.get("url", ""), start, dauer, roh):
                    stand("Ausschnitt konnte nicht geladen werden.", True)
                    return
                schneide_clip(roh, 0, ziel, hochformat, dauer=dauer)
                if roh.exists():
                    roh.unlink()

            if not brauchbar(ziel, dauer, 0.3):
                if ziel.exists():
                    ziel.unlink()
                stand("Ergebnis war leer – andere Zeit versuchen.", True)
                return

            if alt_datei.exists() and alt_datei != ziel:
                alt_datei.unlink()

            with SPERRE:
                for c in STATE["clips"]:
                    if c["nummer"] == nummer:
                        c["datei"] = neu_name
                        c["start"] = start
                        c["start_text"] = zeit(start)
                        c["dauer"] = dauer
                STATE["uploads"].pop(eintrag["datei"], None)
            stand("", True)
            notiz(f"Clip {nummer} neu geschnitten: ab {zeit(start)}, {dauer} s")

        except BaseException as fehler:
            stand(f"Fehlgeschlagen: {str(fehler)[:80]}", True)


# ----------------------------------------------------------------------
# Bildbearbeitung
# ----------------------------------------------------------------------

SCHRIFTEN = [
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\seguisb.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def finde_schrift():
    for pfad in SCHRIFTEN:
        if Path(pfad).exists():
            return pfad
    return None


def fuer_drawtext(text):
    """Sonderzeichen für den ffmpeg-Textfilter entschärfen."""
    for zeichen, ersatz in (("\\", "\\\\"), (":", "\\:"), ("'", "\u2019"),
                            ("%", "\\%"), ("[", "\\["), ("]", "\\]"),
                            (",", "\\,"), (";", "\\;")):
        text = text.replace(zeichen, ersatz)
    return text


ENCODER = {"geprueft": False, "v": None, "name": "CPU"}


def waehle_encoder():
    """Sucht einen Grafikkarten-Encoder - der ist um ein Vielfaches schneller."""
    if ENCODER["geprueft"]:
        return ENCODER["v"]
    ENCODER["geprueft"] = True

    try:
        liste = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                               capture_output=True, text=True, timeout=30).stdout
    except Exception:
        liste = ""

    kandidaten = [
        ("h264_nvenc", ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "23"], "NVIDIA"),
        ("h264_qsv",   ["-c:v", "h264_qsv", "-global_quality", "23"], "Intel"),
        ("h264_amf",   ["-c:v", "h264_amf", "-quality", "speed", "-rc", "cqp",
                        "-qp_i", "23", "-qp_p", "23"], "AMD"),
    ]
    for name, args, marke in kandidaten:
        if name not in liste:
            continue
        try:
            probe = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                 "-i", "testsrc=size=640x360:rate=15:duration=1"] + args +
                ["-f", "null", "-"],
                capture_output=True, text=True, timeout=60)
            if probe.returncode == 0:
                ENCODER["v"] = args
                ENCODER["name"] = marke
                return args
        except Exception:
            pass

    ENCODER["v"] = None
    return None


def video_args():
    """Die Encoder-Einstellungen für alle Videoausgaben."""
    gpu = waehle_encoder()
    if gpu:
        return list(gpu)
    return ["-c:v", "libx264", "-preset", "superfast", "-crf", "22"]


def masse_von(datei):
    """Breite und Höhe einer Videodatei."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(datei)],
            capture_output=True, text=True, timeout=60)
        b, h = (proc.stdout or "").strip().split("x")[:2]
        return int(b), int(h)
    except Exception:
        notiz("Warnung: Videomaße nicht lesbar – rechne mit 1920x1080.")
        return 1920, 1080


OPENCV = {"geladen": False, "cv2": None, "gesichter": None}
OPENCV_SPERRE = threading.Lock()


def hole_opencv():
    """Lädt OpenCV genau einmal - mehrere Arbeitsfäden dürfen sich nicht stören."""
    with OPENCV_SPERRE:
        if OPENCV["geladen"]:
            return OPENCV["cv2"], OPENCV["gesichter"]
        OPENCV["geladen"] = True
        try:
            import cv2
        except ImportError:
            notiz("Für den automatischen Bildausschnitt fehlt OpenCV. Installieren mit: "
                  "py -m pip install opencv-python")
            return None, None
        try:
            pfad = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            modell = cv2.CascadeClassifier(pfad)
            if modell.empty():
                modell = None
        except Exception:
            modell = None                 # dann nur Bewegungserkennung
        OPENCV["cv2"], OPENCV["gesichter"] = cv2, modell
        return cv2, modell


def finde_motiv(datei, takt=0.5):
    """Verfolgt, wo im Bild etwas passiert - Gesicht bevorzugt, sonst Bewegung.

    Liefert eine Liste (Sekunde, x, y) mit Werten zwischen 0 und 1.
    """
    cv2, gesichter = hole_opencv()
    if cv2 is None:
        return []

    try:
        cap = cv2.VideoCapture(str(datei))
        if not cap.isOpened():
            return []

        breite = cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1
        hoehe = cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 1
        laenge = dauer_von(datei)

        punkte, vorheriges = [], None
        zeitpunkt = 0.0
        while zeitpunkt < laenge:
            pruefe_stopp()
            cap.set(cv2.CAP_PROP_POS_MSEC, zeitpunkt * 1000)
            ok, bild = cap.read()
            if not ok:
                break

            klein = cv2.resize(bild, (320, int(320 * hoehe / breite)))
            grau = cv2.cvtColor(klein, cv2.COLOR_BGR2GRAY)

            fund = gesichter.detectMultiScale(grau, 1.15, 5, minSize=(24, 24)) \
                   if gesichter is not None else []
            if len(fund):
                x, y, b, h = max(fund, key=lambda f: f[2] * f[3])
                punkte.append((zeitpunkt, (x + b / 2) / klein.shape[1],
                               (y + h / 2) / klein.shape[0], "gesicht"))
            elif vorheriges is not None:
                # kein Gesicht: dorthin schauen, wo sich am meisten bewegt
                diff = cv2.absdiff(grau, vorheriges)
                _, maske = cv2.threshold(diff, 18, 255, cv2.THRESH_BINARY)
                m = cv2.moments(maske)
                if m["m00"] > 400:
                    punkte.append((zeitpunkt, m["m10"] / m["m00"] / klein.shape[1],
                                   m["m01"] / m["m00"] / klein.shape[0], "bewegung"))
            vorheriges = grau
            zeitpunkt += takt

        cap.release()
        return punkte
    except Abbruch:
        raise
    except Exception as fehler:
        notiz(f"Bildverfolgung nicht möglich: {str(fehler)[:60]}")
        return []


def glaette_pfad(punkte, achse, fenster=5):
    """Glättet die Bewegung, damit das Bild nicht zappelt."""
    if not punkte:
        return []
    werte = [p[achse] for p in punkte]
    zeiten = [p[0] for p in punkte]
    geglaettet = []
    for i in range(len(werte)):
        von = max(0, i - fenster // 2)
        bis = min(len(werte), i + fenster // 2 + 1)
        geglaettet.append(sum(werte[von:bis]) / (bis - von))
    return list(zip(zeiten, geglaettet))


def crop_ausdruck(pfad, spielraum, stuetzen=24):
    """Baut aus dem Pfad einen ffmpeg-Ausdruck, der den Ausschnitt mitführt."""
    if not pfad or spielraum <= 1:
        return None

    # auf wenige Stützstellen eindampfen, sonst wird der Ausdruck riesig
    schritt = max(1, len(pfad) // stuetzen)
    knoten = pfad[::schritt]
    if knoten[-1][0] != pfad[-1][0]:
        knoten.append(pfad[-1])
    if len(knoten) < 2:
        return f"{int(round(knoten[0][1] * spielraum))}"

    # von hinten nach vorn verschachteln: lineare Überblendung je Abschnitt
    ausdruck = f"{int(round(knoten[-1][1] * spielraum))}"
    for i in range(len(knoten) - 2, -1, -1):
        t0, w0 = knoten[i]
        t1, w1 = knoten[i + 1]
        x0 = max(0, min(spielraum, int(round(w0 * spielraum))))
        x1 = max(0, min(spielraum, int(round(w1 * spielraum))))
        spanne = max(0.001, t1 - t0)
        ausdruck = (f"if(lt(t,{t1:.2f}),"
                    f"{x0}+({x1}-{x0})*(t-{t0:.2f})/{spanne:.3f},{ausdruck})")
    return ausdruck


def bild_bereich(rechteck, ziel_b, ziel_h):
    """Schneidet einen Bereich aus und füllt damit eine Zielfläche."""
    x = max(0.0, min(0.999, float(rechteck.get("x", 0))))
    y = max(0.0, min(0.999, float(rechteck.get("y", 0))))
    b = max(0.02, min(1.0 - x, float(rechteck.get("b", 1))))
    h = max(0.02, min(1.0 - y, float(rechteck.get("h", 1))))
    return (f"crop=iw*{b:.5f}:ih*{h:.5f}:iw*{x:.5f}:ih*{y:.5f},"
            f"scale={ziel_b}:{ziel_h}:force_original_aspect_ratio=increase,"
            f"crop={ziel_b}:{ziel_h}")


def untertitel_filter(srt, balken_unten):
    """Untertitel einbrennen - möglichst mittig im schwarzen Balken unten."""
    zeilenhoehe = 67                      # gemessen bei Fontsize 16
    if balken_unten >= zeilenhoehe + 40:
        abstand = (balken_unten - zeilenhoehe) / 2
    else:
        abstand = 150                     # kein Balken: klassisch über dem Bildrand
    rand = max(4, int(round((abstand - 18) / 6.657)))
    stil = ("FontName=Arial,Fontsize=16,Bold=1,PrimaryColour=&H00FFFFFF,"
            "OutlineColour=&H00000000,BorderStyle=1,Outline=1.5,Shadow=0.4,"
            f"Alignment=2,MarginV={rand}")
    return f"subtitles={srt_fuer_ffmpeg(srt)}:force_style='{stil}'"


def baue_filter(einst, srt=None, quelle=None):
    """Setzt die Bildfilter aus den Einstellungen der Bearbeitungsseite zusammen."""
    hell  = max(-0.5, min(0.5, float(einst.get("helligkeit", 0.0))))
    kontr = max(0.5, min(2.0, float(einst.get("kontrast", 1.0))))
    satt  = max(0.0, min(3.0, float(einst.get("saettigung", 1.0))))
    text  = str(einst.get("text", "")).strip()[:80]

    W = CLIP_BREITE
    H = CLIP_BREITE * 16 // 9

    def gerade(x):
        return max(2, int(round(x / 2)) * 2)

    face = einst.get("facecam") or None
    balken_unten = 0

    if face:
        anteil = max(0.15, min(0.5, float(face.get("anteil", 0.3))))
        h_oben = gerade(H * anteil)
        h_unten = H - h_oben
        kette = (
            "split[q1][q2];"
            f"[q1]{bild_bereich(face.get('face', {}), W, h_oben)}[oben];"
            f"[q2]{bild_bereich(face.get('haupt', {}), W, h_unten)}[unten];"
            "[oben][unten]vstack=inputs=2"
        )
    else:
        zoom = max(0.25, min(3.0, float(einst.get("zoom", 1.0))))
        vx   = max(-1.0, min(1.0, float(einst.get("versatz_x", 0.0))))
        vy   = max(-1.0, min(1.0, float(einst.get("versatz_y", 0.0))))

        qb, qh = masse_von(quelle) if quelle else (1920, 1080)
        s = max(W * zoom / qb, H * zoom / qh)
        bw, bh = gerade(qb * s), gerade(qh * s)

        # waagerecht: zu breit -> beschneiden, zu schmal -> schwarz auffüllen
        if bw >= W:
            cw, cx, px = W, int(round((bw - W) / 2 * (1 - vx))), 0
        else:
            cw, cx, px = bw, 0, int(round((W - bw) / 2 * (1 + vx)))
        if bh >= H:
            ch, cy, py = H, int(round((bh - H) / 2 * (1 - vy))), 0
        else:
            ch, cy, py = bh, 0, int(round((H - bh) / 2 * (1 + vy)))

        balken_unten = H - (py + ch)

        kette = f"scale={bw}:{bh}"
        if cw != bw or ch != bh:
            kette += f",crop={cw}:{ch}:{cx}:{cy}"
        if cw != W or ch != H:
            kette += f",pad={W}:{H}:{px}:{py}:color=black"

    teile = [kette]

    if abs(hell) > 0.001 or abs(kontr - 1) > 0.001 or abs(satt - 1) > 0.001:
        teile.append(f"eq=brightness={hell:.3f}:contrast={kontr:.3f}:"
                     f"saturation={satt:.3f}")

    if srt:
        if isinstance(srt, tuple):
            ziel = ARBEITSORDNER / f"ut_{srt[2]:02d}.ass"
            gebaut = baue_ass(srt[1], balken_unten, ziel)
            if gebaut:
                teile.append(f"subtitles={srt_fuer_ffmpeg(gebaut)}")
        else:
            teile.append(untertitel_filter(srt, balken_unten))

    if text:
        schrift = finde_schrift()
        if schrift:
            oben = float(einst.get("text_oben", 0.08))
            groesse = int(W * 0.067)
            teile.append(
                "drawtext=fontfile='{}':text='{}':fontcolor=white:"
                "fontsize={}:borderw={}:bordercolor=black@0.85:"
                "x=(w-text_w)/2:y=h*{:.3f}".format(
                    schrift.replace("\\", "/").replace(":", "\\:"),
                    fuer_drawtext(text), groesse, max(3, groesse // 12), oben))

    return ",".join(teile)


def erzeuge_untertitel(quelle, von, dauer, nummer):
    """Lässt Whisper den Ton abtippen und liefert eine SRT-Datei."""
    ton = ARBEITSORDNER / f"ut_{nummer:02d}.wav"
    lauf_ffmpeg([
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{von:.2f}", "-i", str(quelle), "-t", f"{dauer:.2f}",
        "-vn", "-ac", "1", "-ar", "16000", str(ton),
    ])

    versuche = [[sys.executable, "-m", "whisper"]]
    exe = Path(sys.executable).parent / "Scripts" / "whisper.exe"
    if exe.exists():
        versuche.append([str(exe)])

    form = "json" if UNTERTITEL_BETONT else "srt"
    for anfang in versuche:
        befehl = anfang + [
            str(ton), "--model", UNTERTITEL_MODELL, "--output_format", form,
            "--output_dir", str(ARBEITSORDNER), "--fp16", "False",
            "--task", "transcribe",
        ]
        if UNTERTITEL_BETONT:
            befehl += ["--word_timestamps", "True"]
        if UNTERTITEL_SPRACHE:
            befehl += ["--language", UNTERTITEL_SPRACHE]
        try:
            if lauf_prozess(befehl) != 0:
                continue
            ergebnis = ARBEITSORDNER / f"{ton.stem}.{form}"
            if not (ergebnis.exists() and ergebnis.stat().st_size > 10):
                continue
            if form == "srt":
                return ("srt", ergebnis)
            woerter = lies_wortzeiten(ergebnis)
            if woerter:
                return ("woerter", woerter)
        except Abbruch:
            raise
        except Exception:
            pass
    return None


def lies_wortzeiten(json_datei):
    """Holt die einzelnen Wörter samt Zeiten aus dem Whisper-Ergebnis."""
    try:
        daten = json.loads(json_datei.read_text(encoding="utf-8"))
    except Exception:
        return []
    woerter = []
    for abschnitt in daten.get("segments", []):
        for w in abschnitt.get("words", []) or []:
            text = str(w.get("word", "")).strip()
            if not text:
                continue
            woerter.append((float(w.get("start", 0)), float(w.get("end", 0)), text))
    return woerter


def ass_zeit(sekunden):
    sekunden = max(0.0, sekunden)
    st = int(sekunden // 3600)
    mi = int((sekunden % 3600) // 60)
    se = sekunden % 60
    return f"{st}:{mi:02d}:{se:05.2f}"


def ass_text(text):
    return text.replace("{", "(").replace("}", ")").replace("\n", " ").strip()


def baue_ass(woerter, balken_unten, ziel):
    """Baut Untertitel, bei denen das betonte Wort farbig hervorgehoben wird.

    woerter: Liste aus (start, ende, wort)
    """
    if not woerter:
        return None

    hoehe = CLIP_BREITE * 16 // 9
    schriftgroesse = int(CLIP_BREITE * 0.062)
    if balken_unten >= schriftgroesse * 1.9:
        rand = int((balken_unten - schriftgroesse) / 2)
    else:
        rand = int(hoehe * 0.09)

    # in Gruppen von höchstens 4 Wörtern zusammenfassen
    gruppen, aktuell = [], []
    for w in woerter:
        aktuell.append(w)
        zu_lang = sum(len(x[2]) for x in aktuell) > 24
        if len(aktuell) >= 4 or zu_lang:
            gruppen.append(aktuell); aktuell = []
    if aktuell:
        gruppen.append(aktuell)

    zeilen = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {CLIP_BREITE}",
        f"PlayResY: {hoehe}", "WrapStyle: 2", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, "
        "Bold, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Haupt,{UNTERTITEL_SCHRIFT},{schriftgroesse},&H00FFFFFF,&H00000000,"
        f"&H00000000,1,1,{max(3, schriftgroesse // 14)},2,2,60,60,{rand},1", "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    for gruppe in gruppen:
        von, bis = gruppe[0][0], gruppe[-1][1]
        if bis <= von:
            bis = von + 0.4
        # jedes Wort einmal hervorheben, solange es gesprochen wird
        for i, (ws, we, wort) in enumerate(gruppe):
            teile = []
            for j, (_, _, anderes) in enumerate(gruppe):
                if j == i:
                    teile.append("{\\c&H" + UNTERTITEL_FARBE + "&}" +
                                 ass_text(anderes) + "{\\c&HFFFFFF&}")
                else:
                    teile.append(ass_text(anderes))
            ab = ws if i > 0 else von
            zu = we if i < len(gruppe) - 1 else bis
            if zu <= ab:
                continue
            zeilen.append(f"Dialogue: 0,{ass_zeit(ab)},{ass_zeit(zu)},Haupt,,0,0,0,,"
                          + " ".join(teile))

    ziel.write_text("\n".join(zeilen), encoding="utf-8")
    return ziel


def srt_fuer_ffmpeg(pfad):
    """Pfad so schreiben, dass der Untertitelfilter ihn frisst."""
    text = str(pfad).replace("\\", "/")
    return text.replace(":", "\\:").replace("'", "\\'")


STAND_DATEI = ARBEITSORDNER / "stand.json"


def stand_sichern():
    """Merkt sich die fertigen Clips, damit sie einen Neustart überleben."""
    try:
        with SPERRE:
            daten = {"clips": list(STATE["clips"]), "titel": STATE.get("titel", ""),
                     "kanal": STATE.get("kanal", ""),
                     "kurve": STATE.get("kurve", []), "peaks": STATE.get("peaks", []),
                     "dauer": STATE.get("dauer", 0),
                     "quelle": {"url": QUELLE.get("url", ""),
                                "hochformat": QUELLE.get("hochformat", True)}}
        STAND_DATEI.write_text(json.dumps(daten), encoding="utf-8")
    except Exception:
        pass


def stand_laden():
    """Holt die Clips vom letzten Mal zurück, sofern die Dateien noch da sind."""
    if not STAND_DATEI.exists():
        return
    try:
        daten = json.loads(STAND_DATEI.read_text(encoding="utf-8"))
    except Exception:
        return

    clips = [c for c in daten.get("clips", [])
             if (AUSGABEORDNER / c.get("datei", "")).exists()]
    if not clips:
        return

    with SPERRE:
        STATE["clips"] = clips
        STATE["titel"] = daten.get("titel", "")
        STATE["kurve"] = daten.get("kurve", [])
        STATE["peaks"] = daten.get("peaks", [])
        STATE["dauer"] = daten.get("dauer", 0)
        STATE["phase"] = "fertig"
    QUELLE.update(daten.get("quelle", {}))
    print(f"{len(clips)} Clips vom letzten Mal wiederhergestellt.")


def master_pfad(nummer):
    """Das ungeschnittene Originalstück eines Clips."""
    return ARBEITSORDNER / f"master_{nummer:02d}.mp4"


def lege_master_an(quelle, start, dauer, ziel, darf_kopieren=False):
    """Schneidet das Stück heraus, ohne am Bild etwas zu ändern."""
    if darf_kopieren:
        # Reines Umpacken - um ein Vielfaches schneller als neu berechnen
        try:
            lauf_ffmpeg([
                "ffmpeg", "-y", "-loglevel", "error",
                "-ss", str(start), "-i", str(quelle), "-t", str(dauer),
                "-c", "copy", "-movflags", "+faststart", str(ziel),
            ])
            if brauchbar(ziel, dauer, 0.6):
                return
        except Abbruch:
            raise
        except Exception:
            pass
        if ziel.exists():
            ziel.unlink()

    lauf_ffmpeg([
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", str(start), "-i", str(quelle), "-t", str(dauer),
        "-threads", "0",
    ] + video_args() + [
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
        str(ziel),
    ])


def rendere_clip(nummer, einst):
    """Wendet die Bearbeitung auf einen Clip an."""
    LOKAL.lauf = LAUF["nummer"]

    with SPERRE:
        eintrag = next((c for c in STATE["clips"] if c["nummer"] == nummer), None)
    if not eintrag:
        with SPERRE:
            STATE["schnitte"][str(nummer)] = {
                "text": "Diesen Clip gibt es nicht mehr – bitte neu erzeugen.",
                "fertig": True}
        return

    quelle = master_pfad(nummer)
    if not quelle.exists():
        quelle = AUSGABEORDNER / eintrag["datei"]     # Notfall: der Clip selbst
    if not quelle.exists():
        with SPERRE:
            STATE["schnitte"][str(nummer)] = {"text": "Clip nicht gefunden", "fertig": True}
        return

    gesamt = dauer_von(quelle)
    von = max(0.0, float(einst.get("von", 0)))
    bis = float(einst.get("bis", gesamt))
    if bis <= von + 1:
        bis = min(gesamt, von + 3)
    dauer = round(min(bis, gesamt) - von, 2)

    neu_name = f"clip_{nummer:02d}_bearb_{int(time.time()) % 100000}.mp4"
    ziel = AUSGABEORDNER / neu_name

    def stand(text, fertig=False):
        with SPERRE:
            STATE["schnitte"][str(nummer)] = {"text": text, "fertig": fertig}

    with NACHSCHNITT:
        try:
            srt = None
            if einst.get("untertitel"):
                stand("Untertitel werden erzeugt – das dauert etwas ...")
                ergebnis = erzeuge_untertitel(quelle, von, dauer, nummer)
                if ergebnis is None:
                    notiz("Untertitel konnten nicht erzeugt werden – "
                          "Clip wird ohne gerendert.")
                elif ergebnis[0] == "srt":
                    srt = ergebnis[1]
                else:
                    srt = ("woerter", ergebnis[1], nummer)

            stand("Wird gerendert ...")
            befehl = ["ffmpeg", "-y", "-loglevel", "error",
                      "-ss", f"{von:.2f}", "-i", str(quelle), "-t", f"{dauer:.2f}",
                      "-vf", baue_filter(einst, srt, quelle),
                      "-threads", "0"] + video_args() + [
                      "-c:a", "aac", "-b:a", "160k",
                      "-movflags", "+faststart", str(ziel)]
            lauf_ffmpeg(befehl)

            if not brauchbar(ziel, dauer, 0.3):
                if ziel.exists():
                    ziel.unlink()
                stand("Rendern ergab keine brauchbare Datei.", True)
                return

            alt_datei = AUSGABEORDNER / eintrag["datei"]
            if alt_datei.exists() and alt_datei != ziel:
                alt_datei.unlink()

            with SPERRE:
                for c in STATE["clips"]:
                    if c["nummer"] == nummer:
                        c["datei"] = neu_name
                        c["dauer"] = int(round(dauer))
                STATE["uploads"].pop(eintrag["datei"], None)
            stand("", True)
            stand_sichern()
            notiz(f"Clip {nummer} bearbeitet ({dauer:.0f} s).")

        except BaseException as fehler:
            stand(f"Fehlgeschlagen: {str(fehler)[:80]}", True)
        finally:
            for rest in (ARBEITSORDNER / f"ut_{nummer:02d}.wav",
                         ARBEITSORDNER / f"ut_{nummer:02d}.srt",
                         ARBEITSORDNER / f"ut_{nummer:02d}.json",
                         ARBEITSORDNER / f"ut_{nummer:02d}.ass"):
                if rest.exists():
                    try:
                        rest.unlink()
                    except OSError:
                        pass


# ----------------------------------------------------------------------
# YouTube-Upload
# ----------------------------------------------------------------------

def mache_vorschaubild(clip, ziel):
    """Zieht ein Standbild aus dem Clip und legt es auf 1280x720 an."""
    stelle = min(VORLAUF, max(1, CLIP_LAENGE // 3))
    befehl = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", str(stelle), "-i", str(clip), "-frames:v", "1",
        "-vf", ("split[a][b];"
                "[a]scale=1280:720:force_original_aspect_ratio=increase,"
                "crop=1280:720,boxblur=28:2[bg];"
                "[b]scale=-1:720[fg];"
                "[bg][fg]overlay=(W-w)/2:0"),
        "-q:v", "2", str(ziel),
    ]
    try:
        subprocess.run(befehl, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=120)
        return ziel.exists() and ziel.stat().st_size > 1000
    except Exception:
        return False



UPLOAD_SPERRE = threading.Lock()


def upload_stand(datei, zustand, text, video_id=None):
    with SPERRE:
        STATE["uploads"][datei] = {"zustand": zustand, "text": text, "id": video_id,
                                   "laeuft_schon": zustand == "laeuft"}


def youtube_dienst():
    """Meldet sich bei YouTube an. Beim ersten Mal öffnet sich der Browser."""
    try:
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError:
        raise RuntimeError(
            "Bibliotheken fehlen. In der Eingabeaufforderung ausführen: "
            "py -m pip install google-auth-oauthlib google-api-python-client"
        )

    if not ZUGANG_DATEI.exists():
        raise RuntimeError(
            f"{ZUGANG_DATEI} fehlt. Die JSON-Datei aus der Google Cloud Console "
            "muss so heißen und neben webclipper.py liegen."
        )

    zugang = None
    if TOKEN_DATEI.exists():
        try:
            zugang = Credentials.from_authorized_user_file(str(TOKEN_DATEI), YT_RECHTE)
        except Exception:
            zugang = None

    if zugang and zugang.expired and zugang.refresh_token:
        try:
            zugang.refresh(Request())
        except Exception:
            zugang = None

    if not zugang or not zugang.valid:
        notiz("Bitte im Browser den Zugriff auf dein YouTube-Konto bestätigen ...")
        flow = InstalledAppFlow.from_client_secrets_file(str(ZUGANG_DATEI), YT_RECHTE)
        zugang = flow.run_local_server(port=8765, prompt="consent")
        TOKEN_DATEI.write_text(zugang.to_json(), encoding="utf-8")
        notiz("Zugriff erteilt.")

    return build("youtube", "v3", credentials=zugang)


def lade_hoch(datei):
    """Lädt einen fertigen Clip zu YouTube hoch."""
    pfad = AUSGABEORDNER / datei
    if not pfad.exists():
        upload_stand(datei, "fehler", "Datei nicht gefunden")
        return

    # Doppelklick soll nicht zweimal hochladen
    with SPERRE:
        schon = STATE["uploads"].get(datei, {})
        if schon.get("zustand") == "fertig":
            return
        if schon.get("laeuft_schon"):
            return
        STATE["uploads"][datei] = {"zustand": "laeuft", "text": "Wird vorbereitet ...",
                                   "id": None, "laeuft_schon": True}

    with UPLOAD_SPERRE:
        try:
            try:
                from googleapiclient.http import MediaFileUpload
            except ImportError:
                raise RuntimeError(
                    "Bibliotheken fehlen. In der Eingabeaufforderung ausführen: "
                    "py -m pip install google-auth-oauthlib google-api-python-client"
                )

            upload_stand(datei, "laeuft", "Anmelden ...")
            dienst = youtube_dienst()

            with SPERRE:
                grund = STATE.get("titel") or "Clip"
                nummer = next((c["nummer"] for c in STATE["clips"]
                               if c["datei"] == datei), 1)
                stelle = next((c["start_text"] for c in STATE["clips"]
                               if c["datei"] == datei), "")

            titel = f"{grund} #{nummer}"[:95]
            beschreibung = f"Ausschnitt ab {stelle} aus: {grund}"

            upload_stand(datei, "laeuft", "Lädt hoch ...")
            medien = MediaFileUpload(str(pfad), chunksize=1024 * 1024 * 4,
                                     resumable=True, mimetype="video/mp4")
            anfrage = dienst.videos().insert(
                part="snippet,status",
                body={
                    "snippet": {"title": titel, "description": beschreibung,
                                "categoryId": "20"},
                    "status": {"privacyStatus": "private",
                               "selfDeclaredMadeForKids": False},
                },
                media_body=medien,
            )

            antwort = None
            while antwort is None:
                fortschritt, antwort = anfrage.next_chunk()
                if fortschritt:
                    upload_stand(datei, "laeuft",
                                 f"Lädt hoch {int(fortschritt.progress() * 100)} %")

            video_id = antwort.get("id", "")
            upload_stand(datei, "laeuft", "Setze Vorschaubild ...")

            bild = ARBEITSORDNER / f"vorschau_{pfad.stem}.jpg"
            if video_id and mache_vorschaubild(pfad, bild):
                try:
                    dienst.thumbnails().set(
                        videoId=video_id,
                        media_body=MediaFileUpload(str(bild), mimetype="image/jpeg"),
                    ).execute()
                    notiz("Vorschaubild gesetzt.")
                except Exception:
                    notiz("Vorschaubild abgelehnt – dafür muss der YouTube-Kanal "
                          "bestätigt sein (Telefonnummer). Das Video ist trotzdem oben.")
                finally:
                    if bild.exists():
                        bild.unlink()

            upload_stand(datei, "fertig", "Hochgeladen (privat)", video_id)
            notiz(f"{datei} hochgeladen: https://youtu.be/{video_id}")

        except BaseException as fehler:
            kurz = str(fehler) or fehler.__class__.__name__
            if len(kurz) > 160:
                kurz = kurz[:160] + " ..."
            upload_stand(datei, "fehler", kurz)
            notiz(f"Upload fehlgeschlagen: {kurz}")


# ----------------------------------------------------------------------
# Weboberfläche
# ----------------------------------------------------------------------

SEITE = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Clipper</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@500;600;700&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#0C0D14; --flaeche:#161822; --flaeche-hoch:#1E2130; --rand:#282C3C;
  --text:#E9EBF4; --gedaempft:#868DA6;
  --akzent:#9B6BFF; --akzent-hell:#B892FF; --signal:#FF4D6D; --gruen:#3DD68C;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font-family:'Inter',system-ui,sans-serif;font-size:15px;line-height:1.55;
  -webkit-font-smoothing:antialiased}
body::before{content:'';position:fixed;inset:0;pointer-events:none;z-index:0;
  background:radial-gradient(60rem 32rem at 12% -8%, rgba(155,107,255,.16), transparent 65%),
             radial-gradient(48rem 28rem at 92% 4%, rgba(255,77,109,.09), transparent 60%)}
.blatt{position:relative;z-index:1;max-width:1140px;margin:0 auto;padding:36px 22px 90px}

header{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:30px}
h1{font-family:'Outfit',sans-serif;font-weight:700;font-size:27px;letter-spacing:-.03em;margin:0}
h1 span{color:var(--akzent)}
.untertitel{color:var(--gedaempft);font-size:13.5px}
.lampe{margin-left:auto;display:flex;align-items:center;gap:9px;background:var(--flaeche);
  border:1px solid var(--rand);border-radius:999px;padding:7px 15px 7px 13px;
  font-size:12.5px;font-weight:500;color:var(--gedaempft)}
.punkt{width:8px;height:8px;border-radius:50%;background:var(--gedaempft);flex:none}
.punkt.aktiv{background:var(--akzent);box-shadow:0 0 0 0 rgba(155,107,255,.55);animation:puls 1.6s infinite}
.punkt.fertig{background:var(--gruen)}
.punkt.fehler{background:var(--signal)}
@keyframes puls{70%{box-shadow:0 0 0 9px rgba(155,107,255,0)}100%{box-shadow:0 0 0 0 rgba(155,107,255,0)}}

.pult{background:var(--flaeche);border:1px solid var(--rand);border-radius:16px;padding:20px}
.reihe{display:flex;gap:12px;align-items:stretch;flex-wrap:wrap}
.feld{flex:1;min-width:240px}
label.klein{display:block;font-size:12px;font-weight:500;color:var(--gedaempft);margin-bottom:7px}
input[type=text],input[type=number]{width:100%;padding:13px 15px;border:1px solid var(--rand);
  border-radius:11px;background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;
  font-size:14.5px;transition:border-color .15s,box-shadow .15s}
input[type=text]::placeholder{color:#5B6178}
input[type=text]:focus,input[type=number]:focus{outline:none;border-color:var(--akzent);
  box-shadow:0 0 0 3px rgba(155,107,255,.16)}
input:focus-visible,button:focus-visible,a:focus-visible{outline:2px solid var(--akzent-hell);outline-offset:2px}

button{align-self:flex-end;padding:13px 26px;border:none;border-radius:11px;background:var(--akzent);
  color:#fff;font-family:'Outfit',sans-serif;font-weight:600;font-size:15px;cursor:pointer;
  box-shadow:0 6px 20px -6px rgba(155,107,255,.7);transition:transform .12s,background .15s}
button:hover{background:var(--akzent-hell)}
button:active{transform:translateY(1px)}
button.stopp{background:transparent;color:var(--signal);border:1px solid var(--signal);box-shadow:none}
button.stopp:hover{background:rgba(255,77,109,.12)}

.optionen{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}
.chip{display:inline-flex;align-items:center;gap:8px;cursor:pointer;user-select:none;
  background:var(--flaeche-hoch);border:1px solid var(--rand);border-radius:999px;
  padding:8px 15px;font-size:13.5px;color:var(--gedaempft);transition:.15s}
.chip:hover{border-color:#3A4055;color:var(--text)}
.chip input{accent-color:var(--akzent);width:15px;height:15px;margin:0;cursor:pointer}
.chip:has(input:checked){color:var(--text);border-color:var(--akzent);background:rgba(155,107,255,.12)}
.zahlchip{cursor:default}
.zahlchip input[type=number]{width:52px;padding:2px 6px;border-radius:7px;
  border:1px solid var(--rand);background:var(--bg);color:var(--text);
  font-family:'JetBrains Mono',monospace;font-size:12.5px;text-align:center}

.spur{margin-top:22px;background:var(--flaeche);border:1px solid var(--rand);
  border-radius:16px;padding:18px 18px 12px;display:none}
.spur.sichtbar{display:block}
.spur h2{font-family:'Outfit',sans-serif;font-size:14px;font-weight:600;margin:0 0 14px;color:var(--gedaempft)}
svg{width:100%;height:200px;display:block}
.achse{display:flex;justify-content:space-between;font-family:'JetBrains Mono',monospace;
  font-size:11.5px;color:var(--gedaempft);margin-top:6px}

.log{margin-top:22px;background:var(--flaeche);border:1px solid var(--rand);border-radius:16px;
  padding:16px 18px;color:var(--gedaempft);font-family:'JetBrains Mono',monospace;
  font-size:12.5px;line-height:1.75;max-height:170px;overflow-y:auto;white-space:pre-wrap}
.log:empty{display:none}
.log::-webkit-scrollbar{width:8px}
.log::-webkit-scrollbar-thumb{background:var(--rand);border-radius:99px}

.raster{margin-top:26px;display:grid;gap:18px;grid-template-columns:repeat(auto-fill,minmax(240px,1fr))}
.karte{background:var(--flaeche);border:1px solid var(--rand);border-radius:16px;overflow:hidden;
  transition:border-color .15s,transform .15s}
.karte:hover{border-color:#3D4356;transform:translateY(-2px)}
.karte video{width:100%;display:block;background:#000;aspect-ratio:9/16;object-fit:contain}
.fuss{display:flex;align-items:center;gap:8px;padding:11px 12px;flex-wrap:wrap}
.nr{font-family:'Outfit',sans-serif;font-weight:700;font-size:13px;color:var(--akzent);
  background:rgba(155,107,255,.13);border-radius:7px;padding:2px 8px}
.stempel{font-family:'JetBrains Mono',monospace;font-size:12px;color:var(--gedaempft)}
.note{font-family:'Outfit',sans-serif;font-weight:700;font-size:12px;border-radius:7px;
  padding:2px 7px;cursor:help}
.note.top{color:#0C0D14;background:var(--gruen)}
.note.gut{color:var(--gruen);background:rgba(61,214,140,.15)}
.note.mittel{color:var(--gedaempft);background:var(--flaeche-hoch)}
.fuss a{color:var(--gedaempft);text-decoration:none;font-size:13px;font-weight:500}
.fuss a:hover{color:var(--akzent-hell)}
.fuss .rechts{margin-left:auto;display:flex;align-items:center;gap:9px;flex-wrap:wrap}
.hoch{padding:6px 12px;border-radius:8px;border:1px solid var(--rand);
  background:var(--flaeche-hoch);color:var(--text);font-family:'Inter',sans-serif;
  font-weight:500;font-size:12.5px;cursor:pointer;box-shadow:none;align-self:auto;
  white-space:nowrap}
.hoch:hover:not(:disabled){background:rgba(155,107,255,.16);border-color:var(--akzent);color:#fff}
.hoch:disabled{opacity:.55;cursor:default}
.uplauf{padding:9px 13px;border-top:1px solid var(--rand);font-size:12px;
  font-family:'JetBrains Mono',monospace;color:var(--gedaempft)}
.uplauf.fertig{color:var(--gruen)}
.uplauf.fehler{color:var(--signal)}
.uplauf a{color:var(--akzent-hell);text-decoration:none}

.aendern{color:var(--gedaempft);text-decoration:none;font-size:13px;font-weight:500}
.aendern:hover{color:var(--akzent-hell)}
.schnittinfo{display:none;font-size:12px;font-family:'JetBrains Mono',monospace;
  color:var(--gedaempft);padding:9px 13px;border-top:1px solid var(--rand)}

.leer{margin-top:30px;text-align:center;color:var(--gedaempft);font-size:14px;
  border:1px dashed var(--rand);border-radius:16px;padding:38px 20px}

@media (max-width:600px){h1{font-size:23px}button{width:100%;align-self:stretch}.feld{min-width:100%}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style>
</head>
<body>
<div class="blatt">

<header>
  <h1>Clip<span>per</span></h1>
  <span class="untertitel">findet die lautesten Momente</span>
  <span class="lampe"><span class="punkt" id="punkt"></span><span id="phase">bereit</span></span>
</header>

<div class="pult">
  <div class="reihe">
    <div class="feld">
      <label class="klein" for="url">Video-Link</label>
      <input type="text" id="url" placeholder="Link einfügen – oder Pfad zu einer Datei auf dem Rechner" autocomplete="off" spellcheck="false">
    </div>
    <div style="width:110px">
      <label class="klein" for="anzahl">Clips</label>
      <input type="number" id="anzahl" value="5" min="1" max="20">
    </div>
    <button id="start">Clips finden</button>
    <button id="stopp" class="stopp" style="display:none">Stopp</button>
  </div>
  <div class="optionen">
    <label class="chip"><input type="checkbox" id="hochformat" checked> Hochformat 9:16</label>
    <label class="chip"><input type="checkbox" id="neu"> Neu herunterladen</label>
    <span class="chip zahlchip">Länge
      <input type="number" id="laenge" value="40" min="5" max="180" step="5"> s</span>
    <span class="chip zahlchip">Vorlauf
      <input type="number" id="vorlauf" value="12" min="0" max="60" step="1"> s</span>
  </div>
</div>

<div class="spur" id="spur">
  <h2>Lautstärke über die Zeit</h2>
  <svg id="graf" viewBox="0 0 1000 200" preserveAspectRatio="none" aria-label="Lautstärkeverlauf mit den gefundenen Stellen"></svg>
  <div class="achse"><span>00:00</span><span id="ende">--:--</span></div>
</div>

<div class="log" id="log"></div>

<div class="leer" id="leer">Noch nichts geschnitten.<br>Link einfügen und starten.</div>
<div class="raster" id="raster"></div>

</div>

<script>
const $ = id => document.getElementById(id);
const UMBRUCH = String.fromCharCode(10);
let ticker = null, letzterLauf = -1;

$('start').onclick = async () => {
  const url = $('url').value.trim();
  if (!url) { $('url').focus(); return; }
  $('leer').style.display = 'none';
  $('graf').innerHTML = '';
  $('spur').classList.remove('sichtbar');
  $('raster').innerHTML = '';
  $('log').textContent = '';
  $('start').disabled = true;
  await fetch('/start', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({url: url, anzahl: parseInt($('anzahl').value) || 5,
      hochformat: $('hochformat').checked, neu: $('neu').checked,
      laenge: parseInt($('laenge').value) || 40,
      vorlauf: parseInt($('vorlauf').value) || 0})});
  $('start').disabled = false;
  if (ticker) clearTimeout(ticker);
  tick();
};

$('stopp').onclick = async () => {
  $('stopp').disabled = true;
  await fetch('/stopp', {method:'POST'});
  $('stopp').disabled = false;
};

$('url').addEventListener('keydown', e => { if (e.key === 'Enter') $('start').click(); });

function zeit(s){ s = Math.floor(s);
  return String(Math.floor(s/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0'); }

function zeichne(kurve, peaks, dauer, clipLaenge, vorlauf){
  if (!kurve.length || !dauer) return;
  const B = 1000, mitte = 96, maxH = 74;
  const breite = B / kurve.length;
  let out = '';
  peaks.forEach(p => {
    const von = Math.max(0, p - vorlauf) / dauer * B;
    const bis = Math.min(dauer, Math.max(0, p - vorlauf) + clipLaenge) / dauer * B;
    out += '<rect x="'+von.toFixed(1)+'" y="8" width="'+Math.max(2,bis-von).toFixed(1)+
           '" height="176" fill="#9B6BFF" opacity="0.13" rx="4"/>';
  });
  kurve.forEach((v,i) => {
    const h = Math.max(1.2, v * maxH), x = i * breite, w = Math.max(0.7, breite - 0.5);
    out += '<rect x="'+x.toFixed(2)+'" y="'+(mitte-h).toFixed(2)+'" width="'+w.toFixed(2)+
           '" height="'+(h*2).toFixed(2)+'" fill="#5E6480" rx="'+Math.min(w/2,1.2).toFixed(2)+'"/>';
  });
  peaks.forEach((p,i) => {
    const x = (p / dauer) * B;
    out += '<line x1="'+x.toFixed(1)+'" y1="12" x2="'+x.toFixed(1)+
           '" y2="180" stroke="#FF4D6D" stroke-width="1.6" opacity="0.85"/>';
    out += '<circle cx="'+x.toFixed(1)+'" cy="186" r="9" fill="#FF4D6D"/>';
    out += '<text x="'+x.toFixed(1)+'" y="190" fill="#0C0D14" font-family="Outfit,sans-serif"'+
           ' font-size="11" font-weight="700" text-anchor="middle">'+(i+1)+'</text>';
  });
  $('graf').innerHTML = out;
  $('spur').classList.add('sichtbar');
  $('ende').textContent = zeit(dauer);
}

function karten(clips, uploads, schnitte){
  const jetzt = clips.map(c => c.datei).join('|');
  if (jetzt !== karten.stand){
    karten.stand = jetzt;
    $('raster').innerHTML = clips.map(c =>
      '<div class="karte" data-datei="'+c.datei+'">'+
      '<video src="/clips/'+encodeURIComponent(c.datei)+'" controls preload="metadata"></video>'+
      '<div class="fuss">'+
      '<span class="nr">'+String(c.nummer).padStart(2,'0')+'</span>'+
      (c.punkte ? '<span class="note '+
        (c.punkte>=80?'top':c.punkte>=55?'gut':'mittel')+'" title="Wie stark '+
        'die Stelle heraussticht">'+c.punkte+'</span>' : '')+
      '<span class="stempel">ab '+c.start_text+'</span>'+
      '<span class="rechts">'+
      '<a class="aendern" href="/bearbeiten?clip='+c.nummer+'">bearbeiten</a>'+
      '<a href="/clips/'+encodeURIComponent(c.datei)+'" download>speichern</a>'+
      '<button class="hoch">YouTube</button>'+
      '</span></div>'+
      '<div class="schnittinfo"></div>'+
      '<div class="uplauf" style="display:none"></div></div>').join('');

    document.querySelectorAll('.karte').forEach(k => {
      k.querySelector('.hoch').onclick = async () => {
        const datei = k.dataset.datei;
        k.querySelector('.hoch').disabled = true;
        const z = k.querySelector('.uplauf');
        z.style.display = 'block';
        z.className = 'uplauf';
        z.textContent = 'Wird vorbereitet ...';
        await fetch('/upload', {method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({datei: datei})});
        if (!upTicker) upTick();
      };
    });
  }

  Object.entries(schnitte || {}).forEach(([nummer, s]) => {
    const k = [...document.querySelectorAll('.karte')].find(
      x => x.querySelector('.nr').textContent === String(nummer).padStart(2,'0'));
    if (!k) return;
    const info = k.querySelector('.schnittinfo');
    info.textContent = s.text || '';
    info.style.display = s.text ? 'block' : 'none';
  });

  Object.keys(uploads || {}).forEach(datei => {
    const k = document.querySelector('.karte[data-datei="'+datei+'"]');
    if (!k) return;
    const u = uploads[datei], z = k.querySelector('.uplauf');
    z.style.display = 'block';
    z.className = 'uplauf' + (u.zustand === 'fertig' ? ' fertig'
                            : u.zustand === 'fehler' ? ' fehler' : '');
    z.innerHTML = u.zustand === 'fertig' && u.id
      ? 'Hochgeladen (privat) &middot; <a href="https://youtu.be/'+u.id+
        '" target="_blank" rel="noopener">ansehen</a>'
      : u.text;
    k.querySelector('.hoch').disabled = (u.zustand !== 'fehler');
  });
}

let upTicker = null;
async function upTick(){
  const s = await (await fetch('/status')).json();
  if (s.clips.length){
    $('leer').style.display = 'none';
    karten(s.clips, s.uploads, s.schnitte);
  }
  const laeuft = Object.values(s.uploads || {}).some(u => u.zustand === 'laeuft')
              || Object.values(s.schnitte || {}).some(x => !x.fertig);
  upTicker = laeuft ? setTimeout(upTick, 1200) : null;
}

const TEXTE = {bereit:'bereit', laden:'lädt', analysieren:'analysiert Ton',
               schneiden:'schneidet', fertig:'fertig', fehler:'Fehler'};

async function tick(){
  const s = await (await fetch('/status')).json();

  if (s.lauf !== letzterLauf){
    letzterLauf = s.lauf;
    $('graf').innerHTML = '';
    $('spur').classList.remove('sichtbar');
    $('raster').innerHTML = '';
  }

  $('phase').textContent = TEXTE[s.phase] || s.phase;
  const p = $('punkt');
  p.className = 'punkt' + (s.phase === 'fertig' ? ' fertig'
                        : s.phase === 'fehler' ? ' fehler'
                        : s.phase === 'bereit' ? '' : ' aktiv');

  const laeuft = ['laden','analysieren','schneiden'].includes(s.phase);
  $('stopp').style.display = laeuft ? 'block' : 'none';
  $('start').textContent = laeuft ? 'Neu starten' : 'Clips finden';

  const log = $('log');
  const unten = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  log.textContent = s.log.join(UMBRUCH);
  if (unten) log.scrollTop = log.scrollHeight;

  if (s.kurve.length) zeichne(s.kurve, s.peaks, s.dauer, s.clip_laenge, s.vorlauf);
  if (s.clips.length){
    $('leer').style.display = 'none';
    karten(s.clips, s.uploads, s.schnitte);
  }

  if (!laeuft) return;
  ticker = setTimeout(tick, 700);
}
tick();
</script>
</body>
</html>"""


SEITE_EDITOR = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Clip bearbeiten</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@500;600;700&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#0C0D14; --flaeche:#161822; --flaeche-hoch:#1E2130; --rand:#282C3C;
  --text:#E9EBF4; --gedaempft:#868DA6;
  --akzent:#9B6BFF; --akzent-hell:#B892FF; --signal:#FF4D6D; --gruen:#3DD68C;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font-family:'Inter',system-ui,sans-serif;font-size:15px;line-height:1.55;
  -webkit-font-smoothing:antialiased}
body::before{content:'';position:fixed;inset:0;pointer-events:none;z-index:0;
  background:radial-gradient(60rem 32rem at 12% -8%, rgba(155,107,255,.16), transparent 65%),
             radial-gradient(48rem 28rem at 92% 4%, rgba(255,77,109,.09), transparent 60%)}
.blatt{position:relative;z-index:1;max-width:1240px;margin:0 auto;padding:26px 22px 70px}

header{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:20px}
h1{font-family:'Outfit',sans-serif;font-weight:700;font-size:22px;letter-spacing:-.02em;margin:0}
.zurueck{color:var(--gedaempft);text-decoration:none;font-size:14px}
.zurueck:hover{color:var(--akzent-hell)}
.merker{margin-left:auto;font-family:'JetBrains Mono',monospace;font-size:12.5px;
  color:var(--gedaempft)}

.bau{display:grid;grid-template-columns:1fr 330px;gap:20px;align-items:start}

.buehne{background:var(--flaeche);border:1px solid var(--rand);border-radius:18px;padding:14px}
.tisch{position:relative;width:100%;background:#000;border-radius:12px;overflow:hidden;
  user-select:none;touch-action:none}
.tisch video{display:block;width:100%;height:auto}
.schablone{position:absolute;inset:0;pointer-events:none}
.dunkel{position:absolute;background:rgba(6,7,12,.68)}
.kasten{position:absolute;border:2px solid var(--akzent);border-radius:3px;
  box-shadow:0 0 0 1px rgba(0,0,0,.55),0 0 26px rgba(155,107,255,.4);
  cursor:grab;pointer-events:auto}
.kasten.zieht{cursor:grabbing}
.kasten.face{border-color:var(--signal);
  box-shadow:0 0 0 1px rgba(0,0,0,.55),0 0 26px rgba(255,77,109,.4)}
.kasten.face .griff{background:var(--signal)}
.kasten.face::after{content:'Facecam'}
.kasten::after{content:'Spiel';position:absolute;top:5px;left:6px;
  font-family:'JetBrains Mono',monospace;font-size:10.5px;color:#fff;
  background:rgba(12,13,20,.72);padding:1px 5px;border-radius:4px}
.griff{position:absolute;width:15px;height:15px;background:var(--akzent);
  border:2px solid #fff;border-radius:3px}
.griff.lo{left:-8px;top:-8px;cursor:nwse-resize}
.griff.ro{right:-8px;top:-8px;cursor:nesw-resize}
.griff.lu{left:-8px;bottom:-8px;cursor:nesw-resize}
.griff.ru{right:-8px;bottom:-8px;cursor:nwse-resize}
.textmarke{position:absolute;left:0;right:0;text-align:center;pointer-events:none;
  font-family:'Outfit',sans-serif;font-weight:700;color:#fff;
  text-shadow:0 0 6px rgba(0,0,0,.95),0 2px 10px rgba(0,0,0,.85);
  padding:0 5%;word-break:break-word;line-height:1.15}

.abspieler{display:flex;align-items:center;gap:11px;margin-top:12px}
.leiste{flex:1;height:6px;background:var(--flaeche-hoch);border-radius:99px;
  position:relative;cursor:pointer}
.gefuellt{position:absolute;top:0;bottom:0;left:0;background:var(--akzent);border-radius:99px}
.uhr{font-family:'JetBrains Mono',monospace;font-size:12px;color:var(--gedaempft);
  min-width:78px;text-align:right}
.tipp{font-size:12.5px;color:var(--gedaempft);flex:1}
.unten{display:flex;gap:16px;align-items:flex-start;margin-top:12px}
.ergebnis{flex:none;text-align:center}
.etikett{font-size:11.5px;color:var(--gedaempft);margin-bottom:6px}
.mini{position:relative;width:132px;aspect-ratio:9/16;background:#000;border-radius:8px;
  overflow:hidden;border:1px solid var(--rand)}
.mini .teil{position:absolute;left:0;right:0;overflow:hidden}
.mini video{position:absolute;max-width:none}
.mini .teil{background:#000}

.pult{background:var(--flaeche);border:1px solid var(--rand);border-radius:18px;padding:18px}
.gruppe{margin-bottom:20px}
h2{font-family:'Outfit',sans-serif;font-size:12.5px;font-weight:600;margin:0 0 11px;
  color:var(--gedaempft);text-transform:uppercase;letter-spacing:.06em}
.regler{display:flex;align-items:center;gap:11px;margin-bottom:10px}
.regler label{font-size:13.5px;width:88px;flex:none}
input[type=range]{flex:1;accent-color:var(--akzent);height:4px;min-width:0}
.zahl{font-family:'JetBrains Mono',monospace;font-size:12px;color:var(--gedaempft);
  width:52px;text-align:right;flex:none}
input[type=text]{width:100%;padding:11px 13px;border:1px solid var(--rand);border-radius:10px;
  background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;font-size:14px}
input[type=text]:focus{outline:none;border-color:var(--akzent);
  box-shadow:0 0 0 3px rgba(155,107,255,.16)}
input:focus-visible,button:focus-visible,a:focus-visible{outline:2px solid var(--akzent-hell);
  outline-offset:2px}

button{padding:10px 18px;border:none;border-radius:10px;background:var(--akzent);color:#fff;
  font-family:'Outfit',sans-serif;font-weight:600;font-size:14.5px;cursor:pointer;
  transition:background .15s,transform .12s}
button:hover:not(:disabled){background:var(--akzent-hell)}
button:active:not(:disabled){transform:translateY(1px)}
button:disabled{background:var(--flaeche-hoch);color:var(--gedaempft);cursor:not-allowed}
button.leise{background:var(--flaeche-hoch);color:var(--text);border:1px solid var(--rand);
  font-family:'Inter',sans-serif;font-weight:500;font-size:13px;padding:8px 13px}
button.leise:hover:not(:disabled){background:rgba(155,107,255,.16);border-color:var(--akzent)}
.rund{width:38px;height:38px;padding:0;border-radius:50%;font-size:14px;flex:none;
  background:var(--flaeche-hoch);color:var(--text);border:1px solid var(--rand)}

.wahl{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.wahl button{flex:1}
.knopfreihe{display:flex;gap:10px;margin-top:4px}
.knopfreihe button{flex:1;font-size:14.5px;padding:11px 16px}
.rueckmeldung{margin-top:11px;font-family:'JetBrains Mono',monospace;font-size:12.5px;
  color:var(--gedaempft);min-height:18px}
.rueckmeldung.gut{color:var(--gruen)}
.rueckmeldung.schlecht{color:var(--signal)}
.spanne{display:flex;align-items:center;gap:8px;font-family:'JetBrains Mono',monospace;
  font-size:12.5px;color:var(--gedaempft);margin-top:4px}
.spanne .mitte{flex:1;text-align:center}
.schalter{display:flex;align-items:center;gap:9px;font-size:13.5px;cursor:pointer;
  user-select:none;padding:4px 0}
.schalter input{accent-color:var(--akzent);width:16px;height:16px;margin:0;cursor:pointer}
.hinweis{font-size:12.5px;color:var(--gedaempft);margin-top:5px}

@media (max-width:900px){ .bau{grid-template-columns:1fr} }
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style>
</head>
<body>
<div class="blatt">

<header>
  <a class="zurueck" href="/">&larr; zurück</a>
  <h1>Clip <span id="nr">–</span> bearbeiten</h1>
  <span class="merker" id="merker"></span>
</header>

<div class="bau">

  <div class="buehne">
    <div class="tisch" id="tisch">
      <video id="film" preload="auto" playsinline></video>
      <div class="schablone">
        <div class="dunkel" id="dOben"></div>
        <div class="dunkel" id="dUnten"></div>
        <div class="dunkel" id="dLinks"></div>
        <div class="dunkel" id="dRechts"></div>
        <div class="kasten" id="kasten">
          <div class="griff lo" data-ecke="lo"></div>
          <div class="griff ro" data-ecke="ro"></div>
          <div class="griff lu" data-ecke="lu"></div>
          <div class="griff ru" data-ecke="ru"></div>
          <div class="textmarke" id="textmarke"></div>
        </div>
        <div class="kasten face" id="kastenFace" style="display:none">
          <div class="griff lo" data-ecke="lo"></div>
          <div class="griff ro" data-ecke="ro"></div>
          <div class="griff lu" data-ecke="lu"></div>
          <div class="griff ru" data-ecke="ru"></div>
        </div>
      </div>
    </div>

    <div class="abspieler">
      <button class="rund" id="spiel">▶</button>
      <div class="leiste" id="leiste"><div class="gefuellt" id="gefuellt"></div></div>
      <span class="uhr" id="uhr">0:00 / 0:00</span>
    </div>
    <div class="unten">
      <div class="tipp">Rahmen verschieben: hineinziehen. Größe ändern: an den Ecken ziehen.
        Was im Rahmen liegt, kommt ins Short.</div>
      <div class="ergebnis">
        <div class="etikett">So wird das Short</div>
        <div class="mini" id="mini">
          <div class="teil" id="mOben"><video id="vOben" muted playsinline></video></div>
          <div class="teil" id="mUnten"><video id="vUnten" muted playsinline></video></div>
          <div class="textmarke" id="mText"></div>
        </div>
      </div>
    </div>
  </div>

  <div class="pult">

    <div class="gruppe">
      <h2>Rahmen</h2>
      <div class="wahl">
        <button class="leise" id="ganzeBreite">Ganze Breite</button>
        <button class="leise" id="fuellen">Bildhöhe</button>
      </div>
      <div class="regler">
        <label for="groesse">Größe</label>
        <input type="range" id="groesse" min="15" max="140" step="1" value="100">
        <span class="zahl" id="groesseZahl">100 %</span>
      </div>
      <label class="schalter"><input type="checkbox" id="facecam"> Facecam oben einblenden</label>
      <div id="faceTeil" style="display:none">
        <div class="regler" style="margin-top:9px">
          <label for="anteil">Höhe oben</label>
          <input type="range" id="anteil" min="18" max="45" step="1" value="30">
          <span class="zahl" id="anteilZahl">30 %</span>
        </div>
        <div class="hinweis">Roten Rahmen auf die Facecam ziehen, blauen auf das Spiel.</div>
      </div>
    </div>

    <div class="gruppe">
      <h2>Anfang und Ende</h2>
      <div class="regler">
        <label for="von">Anfang</label>
        <input type="range" id="von" min="0" max="100" step="0.5" value="0">
        <span class="zahl" id="vonZahl">0,0 s</span>
      </div>
      <div class="regler">
        <label for="bis">Ende</label>
        <input type="range" id="bis" min="0" max="100" step="0.5" value="100">
        <span class="zahl" id="bisZahl">– s</span>
      </div>
      <div class="spanne">
        <button class="leise" id="hierAnfang">Anfang = jetzt</button>
        <span class="mitte" id="laenge">–</span>
        <button class="leise" id="hierEnde">Ende = jetzt</button>
      </div>
    </div>

    <div class="gruppe">
      <h2>Bild</h2>
      <div class="regler">
        <label for="hell">Helligkeit</label>
        <input type="range" id="hell" min="-0.3" max="0.3" step="0.01" value="0">
        <span class="zahl" id="hellZahl">0</span>
      </div>
      <div class="regler">
        <label for="kontr">Kontrast</label>
        <input type="range" id="kontr" min="0.6" max="1.8" step="0.02" value="1">
        <span class="zahl" id="kontrZahl">1,00</span>
      </div>
      <div class="regler">
        <label for="satt">Farbe</label>
        <input type="range" id="satt" min="0" max="2.5" step="0.05" value="1">
        <span class="zahl" id="sattZahl">1,00</span>
      </div>
    </div>

    <div class="gruppe">
      <h2>Text im Bild</h2>
      <input type="text" id="text" placeholder="Leer lassen für keinen Text" maxlength="80">
      <div class="regler" style="margin-top:11px">
        <label for="toben">Position</label>
        <input type="range" id="toben" min="0.02" max="0.85" step="0.01" value="0.08">
        <span class="zahl" id="tobenZahl">oben</span>
      </div>
    </div>

    <div class="gruppe">
      <h2>Untertitel</h2>
      <label class="schalter"><input type="checkbox" id="untertitel">
        Automatisch aus dem Ton erzeugen</label>
      <div class="hinweis">Braucht beim Speichern etwa eine halbe bis zwei Minuten.</div>
    </div>

    <div class="knopfreihe">
      <button class="leise" id="zuruecksetzen">Zurücksetzen</button>
      <button id="speichern">Speichern</button>
    </div>
    <div class="rueckmeldung" id="rueckmeldung"></div>

  </div>
</div>
</div>

<script>
const $ = id => document.getElementById(id);
const P = new URLSearchParams(location.search);
const NUMMER = parseInt(P.get('clip') || '1');

const ZIEL_B = 1080, ZIEL_H = 1920;     // Ausgabegröße
const film = $('film'), tisch = $('tisch'), kasten = $('kasten');

let gesamt = 0;
// Rahmen in Anteilen der Bildbreite/-höhe (kann über den Rand hinausgehen)
let R = {x: 0, y: 0, b: 0.35};
let F = {x: 0.72, y: 0.02, b: 0.24};      // Facecam-Rahmen
const kastenFace = $('kastenFace');
function faceAn(){ return $('facecam').checked; }
function anteil(){ return parseInt($('anteil').value) / 100; }

const S = {hell:$('hell'), kontr:$('kontr'), satt:$('satt'), toben:$('toben'),
           von:$('von'), bis:$('bis'), groesse:$('groesse')};

function zeit(s){
  s = Math.max(0, s);
  return Math.floor(s/60) + ':' + String(Math.floor(s%60)).padStart(2,'0');
}
function komma(x,n){ return x.toFixed(n).replace('.', ','); }
function masse(){
  return {b: film.clientWidth || 1, h: film.clientHeight || 1,
          iw: film.videoWidth || 16, ih: film.videoHeight || 9};
}

/* Rahmenhöhe folgt aus 9:16 und dem Seitenverhältnis des Bildes */
function hoeheFuer(breite, teil){
  // teil = Anteil der Ausgabehöhe, den dieser Rahmen füllt
  const m = masse();
  return breite * (m.iw / m.ih) * (16 / 9) * teil;
}
function rahmenHoehe(){ return hoeheFuer(R.b, faceAn() ? 1 - anteil() : 1); }
function faceHoehe(){ return hoeheFuer(F.b, anteil()); }

function maxBreite(teil){
  // größte Rahmenbreite, bei der die Höhe noch ins Bild passt
  const m = masse();
  return (m.ih / m.iw) * (9 / 16) / Math.max(0.01, teil);
}

function begrenzen(){
  // Im Facecam-Betrieb bleibt der Rahmen im Bild, damit keine Ränder entstehen
  const grenze = faceAn() ? Math.min(1.0, maxBreite(1 - anteil())) : 2.2;
  R.b = Math.min(grenze, Math.max(0.12, R.b));
  const h = rahmenHoehe();
  R.x = R.b >= 1 ? (1 - R.b) / 2 : Math.min(1 - R.b, Math.max(0, R.x));
  R.y = h    >= 1 ? (1 - h)    / 2 : Math.min(1 - h,    Math.max(0, R.y));

  F.b = Math.min(Math.min(1.0, maxBreite(anteil())), Math.max(0.06, F.b));
  const fh = faceHoehe();
  F.x = Math.min(1 - F.b, Math.max(0, F.x));
  F.y = fh >= 1 ? 0 : Math.min(1 - fh, Math.max(0, F.y));
}

function malen(){
  begrenzen();
  const m = masse(), h = rahmenHoehe();
  const px = {x: R.x * m.b, y: R.y * m.h, b: R.b * m.b, h: h * m.h};

  kasten.style.left = px.x + 'px';
  kasten.style.top = px.y + 'px';
  kasten.style.width = px.b + 'px';
  kasten.style.height = px.h + 'px';

  $('dOben').style.cssText   = `left:0;top:0;width:100%;height:${Math.max(0,px.y)}px`;
  $('dUnten').style.cssText  = `left:0;top:${px.y+px.h}px;width:100%;bottom:0`;
  $('dLinks').style.cssText  = `left:0;top:${Math.max(0,px.y)}px;width:${Math.max(0,px.x)}px;`
                             + `height:${px.h}px`;
  $('dRechts').style.cssText = `left:${px.x+px.b}px;top:${Math.max(0,px.y)}px;right:0;`
                             + `height:${px.h}px`;

  kastenFace.style.display = faceAn() ? 'block' : 'none';
  if (faceAn()){
    const fh = faceHoehe();
    kastenFace.style.left   = (F.x * m.b) + 'px';
    kastenFace.style.top    = (F.y * m.h) + 'px';
    kastenFace.style.width  = (F.b * m.b) + 'px';
    kastenFace.style.height = (fh  * m.h) + 'px';
  }
  $('faceTeil').style.display = faceAn() ? 'block' : 'none';
  $('anteilZahl').textContent = Math.round(anteil() * 100) + ' %';

  film.style.filter = 'brightness(' + (1 + parseFloat(S.hell.value)) + ') ' +
                      'contrast(' + S.kontr.value + ') saturate(' + S.satt.value + ')';

  const tm = $('textmarke');
  tm.textContent = $('text').value.trim();
  tm.style.top = (parseFloat(S.toben.value) * 100) + '%';
  tm.style.fontSize = (px.b * 0.067) + 'px';

  let v = parseFloat(S.von.value), b = parseFloat(S.bis.value);
  if (b < v + 1){ b = Math.min(gesamt, v + 1); S.bis.value = b; }

  S.groesse.value = Math.round(R.b * 100);
  $('groesseZahl').textContent = Math.round(R.b * 100) + ' %';
  $('hellZahl').textContent  = komma(parseFloat(S.hell.value),2);
  $('kontrZahl').textContent = komma(parseFloat(S.kontr.value),2);
  $('sattZahl').textContent  = komma(parseFloat(S.satt.value),2);
  $('tobenZahl').textContent = parseFloat(S.toben.value) < 0.4 ? 'oben'
                             : parseFloat(S.toben.value) < 0.65 ? 'mitte' : 'unten';
  $('vonZahl').textContent = komma(v,1) + ' s';
  $('bisZahl').textContent = komma(b,1) + ' s';
  $('laenge').textContent  = 'Länge ' + komma(b - v,1) + ' s';

  miniMalen();
}

/* Ergebnis-Vorschau: einen Bildbereich formatfüllend in eine Fläche legen */
function setzeAusschnitt(vid, box, rect, schwarzRand){
  const m = masse();
  const W = box.clientWidth, H = box.clientHeight;
  if (!W || !H || !m.iw) return;
  // Bereich auf das Bild begrenzen - genau wie beim Rendern
  const bx = Math.max(0, Math.min(1, rect.x)), by = Math.max(0, Math.min(1, rect.y));
  const rw = Math.min(rect.b, 1) * m.iw, rh = Math.min(rect.h, 1) * m.ih;
  rect = {x: bx, y: by, b: rw / m.iw, h: rh / m.ih};
  const s = schwarzRand ? Math.min(W / rw, H / rh) : Math.max(W / rw, H / rh);
  vid.style.width  = (m.iw * s) + 'px';
  vid.style.height = (m.ih * s) + 'px';
  vid.style.left   = (-(rect.x * m.iw * s) - (rw * s - W) / 2) + 'px';
  vid.style.top    = (-(rect.y * m.ih * s) - (rh * s - H) / 2) + 'px';
}

function miniMalen(){
  const mini = $('mini'), oben = $('mOben'), unten = $('mUnten');
  const H = mini.clientHeight;
  if (!H) return;

  if (faceAn()){
    const hO = Math.round(H * anteil());
    oben.style.display = 'block';
    oben.style.top = '0px'; oben.style.height = hO + 'px';
    unten.style.top = hO + 'px'; unten.style.height = (H - hO) + 'px';
    setzeAusschnitt($('vOben'), oben, {x: F.x, y: F.y, b: F.b, h: faceHoehe()});
    setzeAusschnitt($('vUnten'), unten, {x: R.x, y: R.y, b: R.b, h: rahmenHoehe()});
  } else {
    oben.style.display = 'none';
    unten.style.top = '0px'; unten.style.height = H + 'px';
    setzeAusschnitt($('vUnten'), unten,
                    {x: R.x, y: R.y, b: R.b, h: rahmenHoehe()}, true);
  }

  const tm = $('mText');
  tm.textContent = $('text').value.trim();
  tm.style.top = (parseFloat(S.toben.value) * 100) + '%';
  tm.style.fontSize = (mini.clientWidth * 0.067) + 'px';

  const f = film.style.filter;
  ['vOben','vUnten'].forEach(id => { $(id).style.filter = f; });
}

/* Rahmen ziehen */
let zug = null;
function punkt(e){
  const k = tisch.getBoundingClientRect();
  return {x: (e.clientX - k.left) / (film.clientWidth || 1),
          y: (e.clientY - k.top)  / (film.clientHeight || 1)};
}
function zugStart(kiste, ziel){
  kiste.addEventListener('pointerdown', e => {
    const ecke = e.target.dataset.ecke;
    zug = {ecke: ecke || null, start: punkt(e), r: Object.assign({}, ziel), ziel: ziel};
    kiste.classList.add('zieht');
    kiste.setPointerCapture(e.pointerId);
    e.preventDefault();
    e.stopPropagation();
  });
}
zugStart(kasten, R);
zugStart(kastenFace, F);
window.addEventListener('pointermove', e => {
  if (!zug) return;
  const Z = zug.ziel;
  const p = punkt(e), dx = p.x - zug.start.x, dy = p.y - zug.start.y;
  const istFace = (Z === F);
  const hoeheVon = b => hoeheFuer(b, istFace ? anteil() : (faceAn() ? 1 - anteil() : 1));

  if (!zug.ecke){
    Z.x = zug.r.x + dx;
    Z.y = zug.r.y + dy;
  } else {
    const links = zug.ecke === 'lo' || zug.ecke === 'lu';
    const alt = zug.r.b;
    Z.b = Math.min(istFace ? 1.0 : 2.2, Math.max(istFace ? 0.06 : 0.12,
                   links ? alt - dx : alt + dx));
    if (links) Z.x = zug.r.x + (alt - Z.b);
    if (zug.ecke === 'lo' || zug.ecke === 'ro')
      Z.y = zug.r.y + (hoeheVon(alt) - hoeheVon(Z.b));
  }
  malen();
});
window.addEventListener('pointerup', () => {
  zug = null;
  kasten.classList.remove('zieht');
  kastenFace.classList.remove('zieht');
});

S.groesse.oninput = () => {
  const mitteX = R.x + R.b/2, h1 = rahmenHoehe();
  const grenze = faceAn() ? Math.min(1.0, maxBreite(1 - anteil())) : 2.2;
  R.b = Math.min(grenze, parseInt(S.groesse.value) / 100);
  R.x = mitteX - R.b/2;
  R.y = R.y + (h1 - rahmenHoehe())/2;
  malen();
};

$('ganzeBreite').onclick = () => { R.b = 1; R.x = 0; malen(); };
$('fuellen').onclick = () => {
  const m = masse();
  R.b = (m.ih / m.iw) * (9 / 16);      // Rahmenhöhe = Bildhöhe
  R.x = (1 - R.b) / 2; R.y = 0;
  malen();
};

/* Abspieler */
film.addEventListener('loadedmetadata', () => {
  gesamt = film.duration || 0;
  S.von.max = gesamt; S.bis.max = gesamt;
  S.von.value = 0; S.bis.value = gesamt;
  $('fuellen').click();
  setTimeout(malen, 60);
});
const nebenVideos = () => ['vOben','vUnten'].map(id => $(id));
function gleichlauf(){
  nebenVideos().forEach(v => {
    if (Math.abs(v.currentTime - film.currentTime) > 0.3)
      try { v.currentTime = film.currentTime; } catch (e) {}
  });
}
film.addEventListener('play',  () => {
  gleichlauf(); nebenVideos().forEach(v => v.play().catch(()=>{}));
});
film.addEventListener('pause', () => nebenVideos().forEach(v => v.pause()));
film.addEventListener('seeked', gleichlauf);

film.addEventListener('timeupdate', () => {
  gleichlauf();
  const b = parseFloat(S.bis.value), v = parseFloat(S.von.value);
  if (film.currentTime > b){ film.currentTime = v; film.pause(); $('spiel').textContent = '▶'; }
  $('gefuellt').style.width = (gesamt ? film.currentTime/gesamt*100 : 0) + '%';
  $('uhr').textContent = zeit(film.currentTime) + ' / ' + zeit(gesamt);
});
$('spiel').onclick = () => {
  if (film.paused){
    if (film.currentTime < parseFloat(S.von.value) ||
        film.currentTime > parseFloat(S.bis.value))
      film.currentTime = parseFloat(S.von.value);
    film.play(); $('spiel').textContent = '❚❚';
  } else { film.pause(); $('spiel').textContent = '▶'; }
};
$('leiste').onclick = e => {
  const k = e.currentTarget.getBoundingClientRect();
  film.currentTime = (e.clientX - k.left) / k.width * gesamt;
};
$('hierAnfang').onclick = () => { S.von.value = film.currentTime.toFixed(1); malen(); };
$('hierEnde').onclick   = () => { S.bis.value = film.currentTime.toFixed(1); malen(); };

[S.hell, S.kontr, S.satt, S.toben, S.von, S.bis].forEach(r => r.oninput = malen);
function spielRahmenSetzen(){
  R.b = Math.min(1, maxBreite(1 - anteil()));
  R.x = (1 - R.b) / 2;
  R.y = (1 - rahmenHoehe()) / 2;
}
$('facecam').onchange = () => { if (faceAn()) spielRahmenSetzen(); malen(); };
$('anteil').oninput = () => { if (faceAn()) spielRahmenSetzen(); malen(); };
$('text').oninput = malen;
window.addEventListener('resize', malen);

$('zuruecksetzen').onclick = () => {
  S.hell.value = 0; S.kontr.value = 1; S.satt.value = 1;
  S.toben.value = 0.08; $('text').value = '';
  S.von.value = 0; S.bis.value = gesamt;
  $('facecam').checked = false; $('untertitel').checked = false;
  $('fuellen').click();
};

/* Rahmen in Zoom und Versatz umrechnen - genau die Werte, mit denen gerendert wird */
function rechne(){
  const m = masse();
  const deckung = Math.max(ZIEL_B / m.iw, ZIEL_H / m.ih);
  const breiteQuelle = R.b * m.iw;                       // Rahmenbreite in Bildpunkten
  const z = ZIEL_B / (deckung * breiteQuelle);
  const bw = m.iw * deckung * z, bh = m.ih * deckung * z;

  const links = -(R.x * m.iw) * deckung * z;
  const oben  = -(R.y * m.ih) * deckung * z;
  const nx = Math.abs(ZIEL_B - bw) / 2, ny = Math.abs(ZIEL_H - bh) / 2;
  return {
    zoom: z,
    versatz_x: nx > 0.5 ? (links - (ZIEL_B - bw) / 2) / nx : 0,
    versatz_y: ny > 0.5 ? (oben  - (ZIEL_H - bh) / 2) / ny : 0
  };
}

$('speichern').onclick = async () => {
  $('speichern').disabled = true;
  const m = $('rueckmeldung');
  m.className = 'rueckmeldung';
  m.textContent = 'Wird gerendert ...';
  const r = rechne();
  const daten = faceAn() ? {
    anteil: anteil(),
    face:  {x: F.x, y: F.y, b: F.b, h: faceHoehe()},
    haupt: {x: R.x, y: R.y, b: R.b, h: rahmenHoehe()}
  } : null;
  await fetch('/rendern', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      nummer: NUMMER,
      von: parseFloat(S.von.value), bis: parseFloat(S.bis.value),
      zoom: r.zoom, versatz_x: r.versatz_x, versatz_y: r.versatz_y,
      facecam: daten, untertitel: $('untertitel').checked,
      helligkeit: parseFloat(S.hell.value), kontrast: parseFloat(S.kontr.value),
      saettigung: parseFloat(S.satt.value),
      text: $('text').value, text_oben: parseFloat(S.toben.value)
    })});
  warten();
};

async function warten(){
  const s = await (await fetch('/status')).json();
  const z = (s.schnitte || {})[String(NUMMER)];
  const m = $('rueckmeldung');
  if (!z || !z.fertig){
    if (z && z.text) m.textContent = z.text;
    setTimeout(warten, 900);
    return;
  }
  $('speichern').disabled = false;
  if (z.text){ m.textContent = z.text; m.className = 'rueckmeldung schlecht'; return; }
  m.textContent = 'Gespeichert. Auf der Übersicht ansehen.';
  m.className = 'rueckmeldung gut';
}

async function laden(){
  const s = await (await fetch('/status')).json();
  const clip = (s.clips || []).find(c => c.nummer === NUMMER);
  if (!clip){
    $('rueckmeldung').textContent = 'Diesen Clip gibt es nicht mehr.';
    $('rueckmeldung').className = 'rueckmeldung schlecht';
    return;
  }
  $('nr').textContent = String(clip.nummer).padStart(2,'0');
  $('merker').textContent = 'ab ' + clip.start_text;
  film.onerror = () => {
    if (film.src.indexOf('/master/') === -1) return;
    film.src = '/clips/' + encodeURIComponent(clip.datei);
  };
  film.src = '/master/' + clip.nummer + '.mp4';
  nebenVideos().forEach(v => { v.src = film.src; });
}

laden();
</script>
</body>
</html>"""


class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, *args):
        pass

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except OSError:
            self.close_connection = True

    def _sende_datei(self, pfad, typ="video/mp4"):
        """Liefert eine Datei aus - auch teilweise, damit Vorspulen funktioniert."""
        groesse = pfad.stat().st_size
        bereich = self.headers.get("Range", "")
        von, bis = 0, groesse - 1
        teil = False

        treffer = re.match(r"bytes=(\d*)-(\d*)", bereich or "")
        if treffer and (treffer.group(1) or treffer.group(2)):
            if treffer.group(1):
                von = int(treffer.group(1))
                if treffer.group(2):
                    bis = int(treffer.group(2))
            else:                                   # z. B. "bytes=-500"
                von = max(0, groesse - int(treffer.group(2)))
            von = max(0, min(von, groesse - 1))
            bis = max(von, min(bis, groesse - 1))
            teil = True

        laenge = bis - von + 1
        try:
            self.send_response(206 if teil else 200)
            self.send_header("Content-Type", typ)
            self.send_header("Content-Length", str(laenge))
            self.send_header("Accept-Ranges", "bytes")
            if teil:
                self.send_header("Content-Range", f"bytes {von}-{bis}/{groesse}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

            with open(pfad, "rb") as f:
                f.seek(von)
                rest = laenge
                while rest > 0:
                    brocken = f.read(min(262144, rest))
                    if not brocken:
                        break
                    self.wfile.write(brocken)
                    rest -= len(brocken)
        except OSError:
            pass                # Browser hat die Übertragung abgebrochen - normal

    def _sende(self, inhalt, typ="text/html; charset=utf-8", code=200):
        daten = inhalt.encode("utf-8") if isinstance(inhalt, str) else inhalt
        try:
            self.send_response(code)
            self.send_header("Content-Type", typ)
            self.send_header("Content-Length", str(len(daten)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(daten)
        except OSError:
            pass                # Browser hat abgebrochen

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._sende(SEITE)
        elif self.path.startswith("/bearbeiten"):
            self._sende(SEITE_EDITOR)

        elif self.path == "/favicon.ico":
            self._sende(b"", "image/x-icon", 204)
        elif self.path == "/status":
            with SPERRE:
                self._sende(json.dumps(STATE), "application/json")
        elif self.path.startswith("/master/"):
            try:
                nummer = int(os.path.basename(unquote(self.path[len("/master/"):]))
                             .split(".")[0].split("?")[0])
            except ValueError:
                self._sende("nicht gefunden", "text/plain; charset=utf-8", 404)
                return
            datei = master_pfad(nummer)
            if datei.exists():
                self._sende_datei(datei)
            else:
                self._sende("nicht gefunden", "text/plain; charset=utf-8", 404)

        elif self.path.startswith("/clips/"):
            name = os.path.basename(unquote(self.path[len("/clips/"):]))
            datei = AUSGABEORDNER / name
            if datei.exists() and datei.suffix == ".mp4":
                self._sende_datei(datei)
            else:
                self._sende("nicht gefunden", "text/plain; charset=utf-8", 404)
        else:
            self._sende("nicht gefunden", "text/plain; charset=utf-8", 404)

    def do_POST(self):
        if self.path == "/upload":
            try:
                laenge = int(self.headers.get("Content-Length", 0))
                daten = json.loads(self.rfile.read(laenge) or "{}")
                datei = os.path.basename(str(daten.get("datei", "")))
            except Exception:
                self._sende(json.dumps({"ok": False}), "application/json", 400)
                return
            if datei:
                threading.Thread(target=lade_hoch, args=(datei,), daemon=True).start()
            self._sende(json.dumps({"ok": bool(datei)}), "application/json")
            return

        if self.path == "/rendern":
            try:
                laenge = int(self.headers.get("Content-Length", 0))
                daten = json.loads(self.rfile.read(laenge) or "{}")
                nummer = int(daten.get("nummer", 0))
            except Exception:
                self._sende(json.dumps({"ok": False}), "application/json", 400)
                return
            with SPERRE:
                STATE["schnitte"][str(nummer)] = {"text": "In der Warteschlange ...",
                                                  "fertig": False}
            threading.Thread(target=rendere_clip, args=(nummer, daten),
                             daemon=True).start()
            self._sende(json.dumps({"ok": True}), "application/json")
            return

        if self.path == "/nachschneiden":
            try:
                laenge = int(self.headers.get("Content-Length", 0))
                daten = json.loads(self.rfile.read(laenge) or "{}")
                nummer = int(daten.get("nummer", 0))
                start  = max(0, int(daten.get("start", 0)))
                dauer  = max(3, min(180, int(daten.get("dauer", CLIP_LAENGE))))
            except Exception:
                self._sende(json.dumps({"ok": False}), "application/json")
                return
            threading.Thread(target=schneide_neu, args=(nummer, start, dauer),
                             daemon=True).start()
            self._sende(json.dumps({"ok": True}), "application/json")
            return

        if self.path == "/stopp":
            stoppe_arbeit()
            self._sende(json.dumps({"ok": True}), "application/json")
            return

        if self.path != "/start":
            self._sende("nicht gefunden", "text/plain; charset=utf-8", 404)
            return

        try:
            laenge = int(self.headers.get("Content-Length", 0))
            daten = json.loads(self.rfile.read(laenge) or "{}")
            url = str(daten.get("url", "")).strip()
            try:
                anzahl = max(1, min(20, int(float(daten.get("anzahl", 5)))))
            except (TypeError, ValueError):
                anzahl = 5
            try:
                laenge = max(5, min(180, int(float(daten.get("laenge", CLIP_LAENGE)))))
            except (TypeError, ValueError):
                laenge = CLIP_LAENGE
            try:
                vorlauf = max(0, min(60, int(float(daten.get("vorlauf", VORLAUF)))))
            except (TypeError, ValueError):
                vorlauf = VORLAUF
        except Exception:
            self._sende(json.dumps({"ok": False}), "application/json", 400)
            return

        self._sende(json.dumps({"ok": True}), "application/json")

        if not url:
            with SPERRE:
                STATE["phase"] = "fehler"
                STATE["log"] = ["Kein Link und keine Datei angegeben."]
            return

        setze_masse(laenge, vorlauf)

        threading.Thread(target=starte_arbeit, args=(
            url, anzahl,
            bool(daten.get("hochformat", True)),
            bool(daten.get("neu", False)),
        ), daemon=True).start()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    for werkzeug in ("ffmpeg", "ffprobe"):
        if shutil.which(werkzeug) is None:
            print(f"FEHLER: {werkzeug} nicht gefunden. Eingabeaufforderung neu öffnen")
            print("oder installieren mit:  winget install Gyan.FFmpeg")
            sys.exit(1)

    ARBEITSORDNER.mkdir(exist_ok=True)
    AUSGABEORDNER.mkdir(exist_ok=True)

    stand_laden()
    waehle_encoder()
    if ENCODER["v"]:
        print(f"Videoberechnung über die Grafikkarte ({ENCODER['name']}) – schnell.")
    else:
        print("Videoberechnung über den Prozessor (keine passende Grafikkarte gefunden).")

    adresse = f"http://localhost:{PORT}"
    print(f"Clipper läuft auf {adresse}")
    print("Dieses Fenster offen lassen. Beenden mit Strg+C.\n")

    server = None
    for versuch in range(PORT, PORT + 10):
        try:
            server = Server(("127.0.0.1", versuch), Handler)
            if versuch != PORT:
                print(f"Port {PORT} war belegt – laufe stattdessen auf "
                      f"http://localhost:{versuch}")
                adresse = f"http://localhost:{versuch}"
            break
        except OSError:
            continue

    if server is None:
        print(f"FEHLER: Die Ports {PORT} bis {PORT + 9} sind alle belegt.")
        print("Läuft der Clipper vielleicht schon in einem anderen Fenster?")
        sys.exit(1)

    threading.Timer(1.0, lambda: webbrowser.open(adresse)).start()

    with server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nBeendet.")


if __name__ == "__main__":
    main()